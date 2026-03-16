"""
Push Notification System
────────────────────────

Architecture
  1. Device token registration  — clients register their FCM/Expo token with
     the backend so the server can push to them even when offline.
  2. In-app notification store  — every notification is persisted in MongoDB
     so it can be fetched, read, and deleted via REST.
  3. SocketIO real-time push    — if the recipient is connected, a `notification`
     event is emitted to their personal room immediately.
  4. FCM silent/data push       — if firebase-admin is configured via the
     FIREBASE_CREDENTIALS_PATH env var, a FCM message is sent to every
     registered device token for the recipient.

MongoDB collections
  push_token    — one doc per (userId, deviceId) pair
  notification  — one doc per in-app notification

Endpoints (all under /api/notifications)
  POST   /device-token                   register or refresh a push token
  DELETE /device-token                   unregister token on logout
  GET    /                               paginated notification list
  GET    /unread-count                   badge count
  PUT    /<notification_id>/read         mark one notification read
  PUT    /read-all                       mark every notification read
  DELETE /<notification_id>              delete one notification

Internal helper (imported by chat.py and socket_events.py)
  trigger_message_notification(sender_doc, recipient_oids, conv_id, preview, app)
"""

import logging
import os
from datetime import datetime, timezone

import requests as _http_requests
from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, jsonify, request

from .auth import decode_token

log = logging.getLogger(__name__)

notifications_bp = Blueprint("notifications", __name__, url_prefix="/api/notifications")


# ── FCM initialisation (optional — degrades gracefully if not configured) ─────

_fcm_ready = False


def _init_fcm():
    """
    Initialise Firebase Admin SDK once.
    Set FIREBASE_CREDENTIALS_PATH to the path of your service-account JSON.
    """
    global _fcm_ready
    if _fcm_ready:
        return True
    creds_path = os.environ.get("FIREBASE_CREDENTIALS_PATH", "")
    if not creds_path:
        log.warning("FCM disabled: FIREBASE_CREDENTIALS_PATH env var not set")
        return False
    if not os.path.isfile(creds_path):
        log.warning("FCM disabled: credentials file not found at %s", creds_path)
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials as fb_creds
        if not firebase_admin._apps:
            cred = fb_creds.Certificate(creds_path)
            firebase_admin.initialize_app(cred)
        _fcm_ready = True
        log.info("Firebase Admin SDK initialised from %s", creds_path)
        return True
    except Exception as exc:
        log.error("FCM init failed: %s", exc)
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _parse_oid(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _fmt_notification(n):
    return {
        "id": str(n["_id"]),
        "type": n.get("type"),
        "title": n.get("title"),
        "body": n.get("body"),
        "data": n.get("data") or {},
        "read": n.get("read", False),
        "createdAt": n["createdAt"].isoformat(),
    }


def _send_fcm(tokens: list[str], title: str, body: str, data: dict, badge: int = 1):
    """
    Send a push notification to a list of device tokens.

    Automatically splits tokens into two groups:
      • Expo tokens  (ExponentPushToken[...])  → Expo Push API
      • FCM tokens   (everything else)          → Firebase Admin SDK

    Android 8+ requires a notification channel; we use "default".
    Silently logs — never raises.
    """
    if not tokens:
        return

    expo_tokens = [t for t in tokens if t.startswith("ExponentPushToken")]
    fcm_tokens  = [t for t in tokens if not t.startswith("ExponentPushToken")]

    str_data = {k: str(v) for k, v in (data or {}).items()}

    # ── Expo push notifications ───────────────────────────────────────────────
    if expo_tokens:
        try:
            messages = [
                {
                    "to":    token,
                    "title": title,
                    "body":  body,
                    "data":  str_data,
                    "sound": "default",
                    "badge": badge,
                    "priority": "high",
                    "channelId": "default",
                }
                for token in expo_tokens
            ]
            resp = _http_requests.post(
                "https://exp.host/--/api/v2/push/send",
                json=messages,
                headers={"Accept": "application/json", "Accept-Encoding": "gzip, deflate",
                         "Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                log.warning("Expo push returned %s: %s", resp.status_code, resp.text[:200])
            else:
                results = resp.json().get("data", [])
                failed  = [r for r in results if r.get("status") == "error"]
                if failed:
                    log.warning("Expo push errors: %s", failed)
        except Exception as exc:
            log.error("Expo push failed: %s", exc)

    # ── FCM push notifications ────────────────────────────────────────────────
    if not fcm_tokens:
        return
    if not _init_fcm():
        return
    try:
        from firebase_admin import messaging
        message = messaging.MulticastMessage(
            tokens=fcm_tokens,
            notification=messaging.Notification(title=title, body=body),
            data=str_data,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="default",
                    sound="default",
                ),
            ),
            apns=messaging.APNSConfig(
                headers={"apns-priority": "10"},
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound="default",
                        badge=badge,
                        content_available=True,
                    )
                ),
            ),
        )
        response = messaging.send_each_for_multicast(message)
        log.info("FCM: %d sent, %d failed out of %d tokens",
                 response.success_count, response.failure_count, len(fcm_tokens))

        # Collect tokens that are no longer valid and remove them
        bad_tokens = []
        for i, r in enumerate(response.responses):
            if not r.success:
                log.warning("FCM token[%d] error: %s", i, r.exception)
                if hasattr(r.exception, "code") and r.exception.code in (
                    "registration-token-not-registered",
                    "invalid-registration-token",
                ):
                    bad_tokens.append(fcm_tokens[i])
        if bad_tokens:
            from flask import current_app as _app
            _app.db.push_token.delete_many({"token": {"$in": bad_tokens}})
            log.info("Removed %d stale FCM tokens", len(bad_tokens))
    except Exception as exc:
        log.error("FCM send failed: %s", exc)


# ── Public trigger (called by chat & socket_events) ──────────────────────────

def trigger_message_notification(sender_doc, recipient_oids, conv_id_str, preview, app):
    """
    Create an in-app notification, push a SocketIO event, and send FCM for
    each recipient.

    Parameters
    ----------
    sender_doc      : MongoDB user document of the sender
    recipient_oids  : list of ObjectId – the other participants
    conv_id_str     : str  – conversation._id as a string
    preview         : str  – first 100 chars of the message text
    app             : Flask application object (needed outside request context)
    """
    if not recipient_oids:
        return

    sender_name = (
        f"{sender_doc.get('firstName', '')} {sender_doc.get('lastName', '')}".strip()
        or sender_doc.get("email", "Someone")
    )
    title = f"New message from {sender_name}"
    body  = preview[:100] if preview else "You have a new message"

    db = app.db

    try:
        from .socket import socketio
        _socketio = socketio
    except Exception:
        _socketio = None

    for recipient_oid in recipient_oids:
        # 1. Persist in-app notification
        notif_doc = {
            "userId": recipient_oid,
            "type": "new_message",
            "title": title,
            "body": body,
            "data": {
                "conversationId": conv_id_str,
                "senderId": str(sender_doc["_id"]),
                "senderEmail": sender_doc.get("email", ""),
                "senderName": sender_name,
            },
            "read": False,
            "createdAt": _now(),
        }
        result = db.notification.insert_one(notif_doc)
        notif_doc["_id"] = result.inserted_id

        # 2. Emit SocketIO event to the recipient's personal room (if online)
        if _socketio:
            try:
                _socketio.emit(
                    "notification",
                    _fmt_notification(notif_doc),
                    to=str(recipient_oid),
                )
            except Exception:
                pass

        # 3. FCM push to all registered device tokens for this recipient
        token_docs = list(db.push_token.find({"userId": recipient_oid}, {"token": 1}))
        tokens = [t["token"] for t in token_docs if t.get("token")]
        if tokens:
            badge = db.notification.count_documents({"userId": recipient_oid, "read": False})
            _send_fcm(
                tokens,
                title,
                body,
                {
                    "conversationId": conv_id_str,
                    "senderId": str(sender_doc["_id"]),
                    "type": "new_message",
                },
                badge=badge,
            )


# ── Document notification trigger ─────────────────────────────────────────────

# Notification type → (title_template, body_template)
# Available placeholders: {signer_name}, {owner_name}, {doc_title}
_DOC_NOTIF_TEMPLATES = {
    "document_signing_request": (
        "Signature requested: {doc_title}",
        "{owner_name} has asked you to sign a document.",
    ),
    "document_signed": (
        "Document signed: {doc_title}",
        "{signer_name} has signed the document.",
    ),
    "document_completed": (
        "Document completed: {doc_title}",
        "All parties have signed \"{doc_title}\".",
    ),
    "document_declined": (
        "Signature declined: {doc_title}",
        "{signer_name} declined to sign the document.",
    ),
    "document_voided": (
        "Document voided: {doc_title}",
        "\"{doc_title}\" has been cancelled by {owner_name}.",
    ),
    "document_distributed": (
        "Document shared: {doc_title}",
        "{owner_name} has shared a document with you.",
    ),
}


def trigger_document_notification(
    notif_type: str,
    recipient_oids: list,
    doc_id_str: str,
    doc_title: str,
    actor_doc: dict,
    app,
    extra_data: dict | None = None,
):
    """
    Persist an in-app notification, emit a SocketIO event, and send FCM
    for each recipient for document-lifecycle events.

    Parameters
    ----------
    notif_type      : one of the keys in _DOC_NOTIF_TEMPLATES
    recipient_oids  : list of ObjectId — users who should receive the notification
    doc_id_str      : str — document._id as string
    doc_title       : str — human-readable document title
    actor_doc       : user document of the person who triggered the event
    app             : Flask application object
    extra_data      : optional dict merged into notification.data
    """
    if not recipient_oids:
        return

    title_tpl, body_tpl = _DOC_NOTIF_TEMPLATES.get(
        notif_type,
        ("{doc_title}", "You have a document notification."),
    )

    actor_name = (
        f"{actor_doc.get('firstName', '')} {actor_doc.get('lastName', '')}".strip()
        or actor_doc.get("email", "Someone")
    )
    fmt = {"doc_title": doc_title, "owner_name": actor_name, "signer_name": actor_name}
    title = title_tpl.format(**fmt)
    body  = body_tpl.format(**fmt)

    db = app.db

    try:
        from .socket import socketio as _socketio
    except Exception:
        _socketio = None

    for recipient_oid in recipient_oids:
        notif_doc = {
            "userId": recipient_oid,
            "type": notif_type,
            "title": title,
            "body": body,
            "data": {
                "documentId": doc_id_str,
                "actorId": str(actor_doc["_id"]),
                "actorEmail": actor_doc.get("email", ""),
                "actorName": actor_name,
                **(extra_data or {}),
            },
            "read": False,
            "createdAt": _now(),
        }
        result = db.notification.insert_one(notif_doc)
        notif_doc["_id"] = result.inserted_id

        if _socketio:
            try:
                _socketio.emit(
                    "notification",
                    _fmt_notification(notif_doc),
                    to=str(recipient_oid),
                )
            except Exception:
                pass

        token_docs = list(db.push_token.find({"userId": recipient_oid}, {"token": 1}))
        tokens = [t["token"] for t in token_docs if t.get("token")]
        if tokens:
            badge = db.notification.count_documents({"userId": recipient_oid, "read": False})
            _send_fcm(
                tokens,
                title,
                body,
                {
                    "documentId": doc_id_str,
                    "type": notif_type,
                },
                badge=badge,
            )


# ── Device token endpoints ────────────────────────────────────────────────────

@notifications_bp.route("/device-token", methods=["POST"])
def register_device_token():
    """
    Register or refresh a push notification token for the authenticated user.

    JSON body:
      - token       (string, required)  FCM registration token or Expo push token
      - platform    (string, required)  "android" | "ios" | "web"
      - deviceId    (string, optional)  client-generated unique device identifier;
                                        used to replace the old token for the same
                                        device without duplicating records.

    Response: 200 (upserted)
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    token    = (data.get("token") or "").strip()
    platform = (data.get("platform") or "").strip().lower()
    device_id = (data.get("deviceId") or "").strip()

    if not token:
        return jsonify({"success": False, "message": "token is required"}), 400
    if platform not in ("android", "ios", "web"):
        return jsonify({"success": False, "message": "platform must be android, ios, or web"}), 400

    user_oid = _parse_oid(user_id_str)
    db = current_app.db
    now = _now()

    # Build the filter: match by userId + deviceId if provided, else by token
    filt = {"userId": user_oid, "token": token}
    if device_id:
        filt = {"userId": user_oid, "deviceId": device_id}

    db.push_token.update_one(
        filt,
        {"$set": {"token": token, "platform": platform, "updatedAt": now},
         "$setOnInsert": {"userId": user_oid, "deviceId": device_id, "createdAt": now}},
        upsert=True,
    )
    return jsonify({"success": True, "message": "Device token registered"}), 200


@notifications_bp.route("/device-token", methods=["DELETE"])
def unregister_device_token():
    """
    Remove a push token so no further notifications are sent to this device.
    Call this on logout or when the user disables push notifications.

    JSON body (at least one required):
      - token     (string)  the FCM/Expo token to remove
      - deviceId  (string)  remove all tokens for this device
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    token     = (data.get("token") or "").strip()
    device_id = (data.get("deviceId") or "").strip()

    if not token and not device_id:
        return jsonify({"success": False, "message": "token or deviceId is required"}), 400

    user_oid = _parse_oid(user_id_str)
    db = current_app.db

    filt = {"userId": user_oid}
    if token:
        filt["token"] = token
    if device_id:
        filt["deviceId"] = device_id

    result = db.push_token.delete_many(filt)
    return jsonify({"success": True, "deleted": result.deleted_count}), 200


# ── In-app notification endpoints ────────────────────────────────────────────

@notifications_bp.route("/", methods=["GET"])
def list_notifications():
    """
    Return paginated in-app notifications for the authenticated user,
    sorted newest-first.

    Query params:
      - page    (int, default 1)
      - limit   (int, default 20, max 100)
      - unread  (bool string "true") — filter to unread only
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    db = current_app.db

    try:
        page  = max(1, int(request.args.get("page", 1)))
        limit = min(100, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        return jsonify({"success": False, "message": "page and limit must be integers"}), 400

    filt = {"userId": user_oid}
    if request.args.get("unread", "").lower() == "true":
        filt["read"] = False

    skip  = (page - 1) * limit
    docs  = list(
        db.notification.find(filt)
        .sort("createdAt", -1)
        .skip(skip)
        .limit(limit)
    )
    total = db.notification.count_documents(filt)

    return jsonify({
        "success": True,
        "data": [_fmt_notification(n) for n in docs],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit,
        },
    }), 200


@notifications_bp.route("/unread-count", methods=["GET"])
def unread_count():
    """
    Return the number of unread notifications for the authenticated user.
    Use this to drive the badge counter in the UI.

    Response: { "success": true, "data": { "count": <int> } }
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    count = current_app.db.notification.count_documents(
        {"userId": user_oid, "read": False}
    )
    return jsonify({"success": True, "data": {"count": count}}), 200


@notifications_bp.route("/<notification_id>/read", methods=["PUT"])
def mark_one_read(notification_id):
    """
    Mark a single notification as read.

    Response: 200
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    notif_oid = _parse_oid(notification_id)
    if not notif_oid:
        return jsonify({"success": False, "message": "Invalid notification ID"}), 400

    user_oid = _parse_oid(user_id_str)
    db = current_app.db

    result = db.notification.update_one(
        {"_id": notif_oid, "userId": user_oid},
        {"$set": {"read": True}},
    )
    if result.matched_count == 0:
        return jsonify({"success": False, "message": "Notification not found"}), 404

    return jsonify({"success": True, "message": "Notification marked as read"}), 200


@notifications_bp.route("/read-all", methods=["PUT"])
def mark_all_read():
    """
    Mark all unread notifications for the authenticated user as read.

    Response: { "success": true, "updated": <int> }
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    result = current_app.db.notification.update_many(
        {"userId": user_oid, "read": False},
        {"$set": {"read": True}},
    )
    return jsonify({"success": True, "updated": result.modified_count}), 200


@notifications_bp.route("/<notification_id>", methods=["DELETE"])
def delete_notification(notification_id):
    """
    Permanently delete a single notification owned by the authenticated user.

    Response: 200
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    notif_oid = _parse_oid(notification_id)
    if not notif_oid:
        return jsonify({"success": False, "message": "Invalid notification ID"}), 400

    user_oid = _parse_oid(user_id_str)
    result = current_app.db.notification.delete_one(
        {"_id": notif_oid, "userId": user_oid}
    )
    if result.deleted_count == 0:
        return jsonify({"success": False, "message": "Notification not found"}), 404

    return jsonify({"success": True, "message": "Notification deleted"}), 200


@notifications_bp.route("/test-push", methods=["POST"])
def test_push():
    """
    Send a test push notification to ALL registered devices of the authenticated user.
    Use this to verify the full FCM / Expo pipeline end-to-end.

    Returns a summary of how many tokens were found and whether FCM is configured.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid   = _parse_oid(user_id_str)
    db         = current_app.db
    token_docs = list(db.push_token.find({"userId": user_oid}, {"token": 1, "platform": 1}))
    tokens     = [t["token"] for t in token_docs if t.get("token")]

    fcm_configured = _init_fcm()

    if not tokens:
        return jsonify({
            "success": False,
            "message": "No registered device tokens found for your account. "
                       "Call POST /api/notifications/device-token first.",
            "fcmConfigured": fcm_configured,
        }), 400

    _send_fcm(
        tokens,
        "Test notification",
        "Push notifications are working!",
        {"type": "test"},
        badge=1,
    )

    return jsonify({
        "success": True,
        "message": f"Test notification dispatched to {len(tokens)} token(s).",
        "tokenCount": len(tokens),
        "fcmConfigured": fcm_configured,
        "tokens": [{"token": t["token"][:20] + "...", "platform": t.get("platform")} for t in token_docs],
    }), 200


@notifications_bp.route("/push-status", methods=["GET"])
def push_status():
    """
    Returns the current push notification configuration status.
    Useful for debugging why notifications are not being received.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid   = _parse_oid(user_id_str)
    db         = current_app.db
    token_docs = list(db.push_token.find({"userId": user_oid}, {"token": 1, "platform": 1, "createdAt": 1}))

    creds_path     = os.environ.get("FIREBASE_CREDENTIALS_PATH", "")
    fcm_configured = bool(creds_path and os.path.isfile(creds_path))

    return jsonify({
        "success": True,
        "data": {
            "fcmConfigured":      fcm_configured,
            "credentialsPathSet": bool(creds_path),
            "credentialsFileExists": os.path.isfile(creds_path) if creds_path else False,
            "registeredTokens": [
                {
                    "tokenPreview": t["token"][:20] + "..." if t.get("token") else None,
                    "isExpoToken":  (t.get("token") or "").startswith("ExponentPushToken"),
                    "platform":     t.get("platform"),
                    "registeredAt": t["createdAt"].isoformat() if isinstance(t.get("createdAt"), datetime) else None,
                }
                for t in token_docs
            ],
        },
    }), 200


# ── Index helper (called from create_app) ────────────────────────────────────

def ensure_notification_indexes(db):
    """Create MongoDB indexes for the notification collections (idempotent)."""
    db.notification.create_index([("userId", 1), ("createdAt", -1)])
    db.notification.create_index([("userId", 1), ("read", 1)])
    db.push_token.create_index([("userId", 1), ("deviceId", 1)], unique=True, sparse=True)
    db.push_token.create_index("token", unique=True)
