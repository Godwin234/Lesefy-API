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

import os
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, jsonify, request

from .auth import decode_token

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
    if not creds_path or not os.path.isfile(creds_path):
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials as fb_creds
        if not firebase_admin._apps:
            cred = fb_creds.Certificate(creds_path)
            firebase_admin.initialize_app(cred)
        _fcm_ready = True
        return True
    except Exception:
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


def _send_fcm(tokens: list[str], title: str, body: str, data: dict):
    """
    Send a FCM multicast message to a list of device tokens.
    Silently removes invalid/expired tokens from the push_token collection.
    Errors are swallowed so a failed push never breaks the chat flow.
    """
    if not tokens or not _init_fcm():
        return
    try:
        from firebase_admin import messaging
        message = messaging.MulticastMessage(
            tokens=tokens,
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
            android=messaging.AndroidConfig(priority="high"),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default", badge=1)
                )
            ),
        )
        response = messaging.send_each_for_multicast(message)
        # Collect tokens that are no longer valid
        bad_tokens = [
            tokens[i]
            for i, r in enumerate(response.responses)
            if not r.success
            and hasattr(r.exception, "code")
            and r.exception.code in (
                "registration-token-not-registered",
                "invalid-registration-token",
            )
        ]
        if bad_tokens:
            from flask import current_app as _app
            _app.db.push_token.delete_many({"token": {"$in": bad_tokens}})
    except Exception:
        pass


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
            _send_fcm(
                tokens,
                title,
                body,
                {
                    "conversationId": conv_id_str,
                    "senderId": str(sender_doc["_id"]),
                    "type": "new_message",
                },
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


# ── Index helper (called from create_app) ────────────────────────────────────

def ensure_notification_indexes(db):
    """Create MongoDB indexes for the notification collections (idempotent)."""
    db.notification.create_index([("userId", 1), ("createdAt", -1)])
    db.notification.create_index([("userId", 1), ("read", 1)])
    db.push_token.create_index([("userId", 1), ("deviceId", 1)], unique=True, sparse=True)
    db.push_token.create_index("token", unique=True)
