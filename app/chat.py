"""
Chat REST API
─────────────
Permission matrix
  tenant           → landlord or property_manager of their linked property
  landlord         → tenants of their properties  OR  contractors
  property_manager → tenants of their managed properties  OR  contractors
  contractor       → any landlord or property_manager

Endpoints
  POST /api/chat/conversations                              start or get a 1-on-1 conversation
  GET  /api/chat/conversations                              list conversations for the current user
  GET  /api/chat/conversations/<conv_id>/messages           paginated message history
  PUT  /api/chat/conversations/<conv_id>/read               mark all messages as read
  GET  /api/chat/conversations/<conv_id>/unread             unread message count

MongoDB collections used
  conversation  – one document per 1-on-1 thread
  message       – individual messages
"""

from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, jsonify, request

from .auth import decode_token

chat_bp = Blueprint("chat", __name__, url_prefix="/api/chat")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _parse_oid(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _is_email(value):
    """Return True if value looks like an email address."""
    at = value.find("@")
    return at > 0 and "." in value[at + 1:]


def _resolve_recipient(value, db):
    """
    Resolve a recipientId string to a user document.

    Resolution order
    ────────────────
    1. If value contains '@' → look up user by email.
    2. Otherwise treat as ObjectId:
       a. Search user collection first.
       b. If not found, search property collection and follow landlordId.

    Returns (user_doc, error_message).  Exactly one of the two will be None.
    """
    if _is_email(value):
        user = db.user.find_one({"email": value.lower().strip()})
        if not user:
            return None, f"No user found with email '{value}'"
        return user, None

    oid = _parse_oid(value)
    if not oid:
        return None, "Invalid recipientId: must be a valid email address or ObjectId"

    # Try the user collection first
    user = db.user.find_one({"_id": oid})
    if user:
        return user, None

    # Fall back to property collection → landlord
    prop = db.property.find_one({"_id": oid})
    if prop:
        landlord_id = prop.get("landlordId")
        if not landlord_id:
            return None, "The property has no landlord assigned"
        landlord = db.user.find_one({"_id": landlord_id})
        if not landlord:
            return None, "The property's landlord account was not found"
        return landlord, None

    return None, "No user or property found with the given ID"


def _auto_detect_recipient(sender_doc, db):
    """
    For a tenant user, find their landlord via the linked property.
    Returns a user doc or None.
    """
    prop_id = sender_doc.get("propertyId")
    if not prop_id:
        return None
    prop = db.property.find_one({"_id": prop_id})
    if not prop or not prop.get("landlordId"):
        return None
    return db.user.find_one({"_id": prop["landlordId"]})


def _user_info(oid, db):
    """Return a lightweight dict {email, name} for a user ObjectId."""
    if not oid or db is None:
        return {"email": None, "name": None}
    u = db.user.find_one(
        {"_id": oid},
        {"email": 1, "firstName": 1, "lastName": 1},
    )
    if not u:
        return {"email": None, "name": None}
    name = f"{u.get('firstName', '')} {u.get('lastName', '')}".strip() or None
    return {"email": u.get("email"), "name": name}


def _fmt_message(msg, db=None):
    sender_info = _user_info(msg.get("senderId"), db)

    read_by_emails = []
    if db is not None:
        for uid in msg.get("readBy", []):
            info = _user_info(uid, db)
            if info["email"]:
                read_by_emails.append(info["email"])
    else:
        read_by_emails = [str(uid) for uid in msg.get("readBy", [])]

    return {
        "id": str(msg["_id"]),
        "conversationId": str(msg["conversationId"]),
        "senderId": sender_info["email"] or str(msg["senderId"]),
        "senderName": sender_info["name"],
        "text": msg.get("text") or msg.get("content") or "",
        "readBy": read_by_emails,
        "createdAt": msg["createdAt"].isoformat(),
    }


def _fmt_conversation(conv, current_uid_str=None, db=None):
    data = {
        "id": str(conv["_id"]),
        "participants": [str(p) for p in conv["participants"]],
        "participantEmails": conv.get("participantEmails") or [],
        "propertyId": str(conv["propertyId"]) if conv.get("propertyId") else None,
        "lastMessage": conv.get("lastMessage"),
        "updatedAt": conv["updatedAt"].isoformat(),
        "createdAt": conv["createdAt"].isoformat(),
    }
    if current_uid_str and db is not None:
        user_oid = _parse_oid(current_uid_str)
        data["unreadCount"] = db.message.count_documents(
            {"conversationId": conv["_id"], "readBy": {"$ne": user_oid}}
        )
    return data


# ── Permission engine ─────────────────────────────────────────────────────────

def can_chat(sender_doc, recipient_doc, db):
    """
    Return (allowed: bool, reason: str | None).

    Rules
    ─────
    tenant           → landlord or property_manager of their linked property only
    landlord         → tenants of a property they own  OR  contractors
    property_manager → tenants of a property they manage  OR  contractors
    contractor       → any landlord or property_manager
    """
    sender_role    = (sender_doc.get("userType") or "").lower()
    recipient_role = (recipient_doc.get("userType") or "").lower()
    sender_id      = sender_doc["_id"]
    recipient_id   = recipient_doc["_id"]

    if not sender_role:
        return False, "Sender account has no userType configured"

    # ── tenant ────────────────────────────────────────────────────────────────
    if sender_role == "tenant":
        prop_id = sender_doc.get("propertyId")
        if not prop_id:
            return False, "Tenant is not linked to any property"
        prop = db.property.find_one({"_id": prop_id})
        if not prop:
            return False, "Tenant's property does not exist"
        allowed_ids = set()
        if prop.get("landlordId"):
            allowed_ids.add(prop["landlordId"])
        if prop.get("propertyManagerId"):
            allowed_ids.add(prop["propertyManagerId"])
        if recipient_id not in allowed_ids:
            return False, "Tenants can only message their landlord or property manager"
        return True, None

    # ── landlord / property_manager ───────────────────────────────────────────
    if sender_role in ("landlord", "property_manager"):
        # Always allow messaging contractors
        if recipient_role == "contractor":
            return True, None

        # Allow messaging tenants who belong to a property they control
        if recipient_role == "tenant":
            # Primary check: use the tenant's propertyId field
            rec_prop_id = recipient_doc.get("propertyId")
            if rec_prop_id:
                if sender_role == "landlord":
                    prop = db.property.find_one(
                        {"_id": rec_prop_id, "landlordId": sender_id}
                    )
                else:  # property_manager
                    prop = db.property.find_one(
                        {"_id": rec_prop_id, "propertyManagerId": sender_id}
                    )
                if prop:
                    return True, None

            # Fallback: check the property's tenants array directly (handles
            # cases where the tenant's propertyId field is not yet synced)
            if sender_role == "landlord":
                prop = db.property.find_one(
                    {"landlordId": sender_id, "tenants.tenantId": recipient_id}
                )
            else:  # property_manager
                prop = db.property.find_one(
                    {"propertyManagerId": sender_id, "tenants.tenantId": recipient_id}
                )
            if not prop:
                return False, "You can only message tenants within properties you manage"
            return True, None

        return False, "Landlords and property managers can only message tenants or contractors"

    # ── contractor ────────────────────────────────────────────────────────────
    if sender_role == "contractor":
        if recipient_role in ("landlord", "property_manager"):
            return True, None
        return False, "Contractors can only message landlords or property managers"

    return False, "Unsupported role combination"


# ── Shared conversation helper (also used by socket_events) ──────────────────

def get_or_create_conversation(sender_doc, recipient_doc, db, property_id=None):
    """
    Return (conv_doc, created: bool) for the 1-on-1 thread between two users.

    Primary lookup key: sorted pair of participant emails (deterministic and
    stable even if ObjectIds change).  Falls back to the sorted ObjectId pair
    for backward-compatibility with pre-existing documents.
    """
    sender_oid    = sender_doc["_id"]
    recipient_oid = recipient_doc["_id"]
    sender_email    = (sender_doc.get("email") or "").lower().strip()
    recipient_email = (recipient_doc.get("email") or "").lower().strip()
    email_pair = sorted([sender_email, recipient_email]) if sender_email and recipient_email else None

    # 1. Lookup by email pair (preferred)
    if email_pair:
        conv = db.conversation.find_one({"participantEmails": email_pair})
        if conv:
            return conv, False

    # 2. Fallback lookup by ObjectId pair (backward compat)
    pair = sorted([sender_oid, recipient_oid], key=lambda x: str(x))
    conv = db.conversation.find_one({"participants": {"$all": pair, "$size": 2}})
    if conv:
        # Migrate: backfill participantEmails if missing
        if email_pair and not conv.get("participantEmails"):
            db.conversation.update_one(
                {"_id": conv["_id"]},
                {"$set": {"participantEmails": email_pair}},
            )
            conv["participantEmails"] = email_pair
        return conv, False

    # 3. Create a new conversation
    now = _now()
    doc = {
        "participants": pair,
        "participantEmails": email_pair or [],
        "propertyId": property_id,
        "lastMessage": None,
        "createdAt": now,
        "updatedAt": now,
    }
    result = db.conversation.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc, True


def ensure_indexes(db):
    """Create MongoDB indexes for the chat collections (idempotent)."""
    # Conversation: fast participant-pair and email-pair lookups
    db.conversation.create_index("participants")
    db.conversation.create_index("participantEmails")
    db.conversation.create_index([("updatedAt", -1)])
    # Message: paginated history and unread counts
    db.message.create_index([("conversationId", 1), ("createdAt", -1)])
    db.message.create_index([("conversationId", 1), ("readBy", 1)])


# ── REST endpoints ────────────────────────────────────────────────────────────

@chat_bp.route("/conversations", methods=["POST"])
def start_conversation():
    """
    Start or retrieve a 1-on-1 conversation with another user.

    JSON body:
      - recipientId  (string, optional) — email address, user ObjectId, or
                     property ObjectId (the property's landlord is used).
                     May be omitted for tenants: the landlord of their linked
                     property is detected automatically.
      - propertyId   (string, optional) — contextual property reference

    Returns the conversation document (201 if new, 200 if existing).
    """
    sender_id_str, err = decode_token(request)
    if err:
        return err

    db        = current_app.db
    sender_oid = _parse_oid(sender_id_str)
    sender_doc = db.user.find_one({"_id": sender_oid})
    if not sender_doc:
        return jsonify({"success": False, "message": "Sender account not found"}), 404

    data          = request.get_json(silent=True) or {}
    recipient_raw = (data.get("recipientId") or "").strip()

    if not recipient_raw:
        # Auto-detect: only supported for tenants
        if (sender_doc.get("userType") or "").lower() != "tenant":
            return jsonify({"success": False, "message": "recipientId is required"}), 400
        recipient_doc = _auto_detect_recipient(sender_doc, db)
        if not recipient_doc:
            return jsonify({
                "success": False,
                "message": "Could not auto-detect a landlord for this tenant — ensure the tenant is linked to a property",
            }), 404
    else:
        recipient_doc, resolve_err = _resolve_recipient(recipient_raw, db)
        if not recipient_doc:
            return jsonify({"success": False, "message": resolve_err}), 404

    if sender_doc["_id"] == recipient_doc["_id"]:
        return jsonify({"success": False, "message": "Cannot start a conversation with yourself"}), 400

    allowed, reason = can_chat(sender_doc, recipient_doc, db)
    if not allowed:
        # A conversation may already exist — allow retrieval even if the
        # direction check currently fails (e.g. roles changed after creation).
        existing = None
        s_email = (sender_doc.get("email") or "").lower().strip()
        r_email = (recipient_doc.get("email") or "").lower().strip()
        if s_email and r_email:
            existing = db.conversation.find_one(
                {"participantEmails": sorted([s_email, r_email])}
            )
        if not existing:
            pair = sorted([sender_doc["_id"], recipient_doc["_id"]], key=lambda x: str(x))
            existing = db.conversation.find_one(
                {"participants": {"$all": pair, "$size": 2}}
            )
        if not existing:
            return jsonify({"success": False, "message": reason}), 403

    prop_oid = _parse_oid(data.get("propertyId") or "")
    conv, created = get_or_create_conversation(sender_doc, recipient_doc, db, prop_oid)
    return (
        jsonify({"success": True, "data": _fmt_conversation(conv, sender_id_str, db)}),
        201 if created else 200,
    )


@chat_bp.route("/conversations", methods=["GET"])
def list_conversations():
    """
    Return all conversations that include the current user, sorted by most
    recently updated.  Each entry includes the other participant's basic
    profile and the number of unread messages.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    convs = list(
        db.conversation.find({"participants": user_oid}).sort("updatedAt", -1)
    )

    result = []
    for conv in convs:
        fmt = _fmt_conversation(conv, user_id_str, db)
        other_ids = [p for p in conv["participants"] if p != user_oid]
        if other_ids:
            other = db.user.find_one(
                {"_id": other_ids[0]},
                {"firstName": 1, "lastName": 1, "userType": 1, "profilePicture": 1},
            )
            if other:
                fmt["recipient"] = {
                    "id": str(other["_id"]),
                    "name": f"{other.get('firstName', '')} {other.get('lastName', '')}".strip(),
                    "role": other.get("userType", ""),
                    "profilePicture": other.get("profilePicture"),
                }
        result.append(fmt)

    return jsonify({"success": True, "data": result}), 200


@chat_bp.route("/conversations/<conv_id>/messages", methods=["POST"])
def send_message(conv_id):
    """
    Send a message to a conversation via HTTP.

    JSON body:
      - text  (string, required) — message content

    Returns the created message document (201).
    Also updates conversation.lastMessage and broadcasts via SocketIO
    if connected clients are present.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    conv_oid = _parse_oid(conv_id)
    if not conv_oid:
        return jsonify({"success": False, "message": "Invalid conversation ID"}), 400

    db   = current_app.db
    conv = db.conversation.find_one({"_id": conv_oid, "participants": user_oid})
    if not conv:
        return jsonify({"success": False, "message": "Conversation not found"}), 404

    data = request.get_json(silent=True) or {}
    text = (data.get("content") or data.get("text") or "").strip()
    if not text:
        return jsonify({"success": False, "message": "content is required"}), 400
    if len(text) > 4000:
        return jsonify({"success": False, "message": "Message must not exceed 4000 characters"}), 400

    now = _now()
    msg_doc = {
        "conversationId": conv_oid,
        "senderId":       user_oid,
        "text":           text,
        "readBy":         [user_oid],
        "createdAt":      now,
    }
    result = db.message.insert_one(msg_doc)
    msg_doc["_id"] = result.inserted_id

    db.conversation.update_one(
        {"_id": conv_oid},
        {"$set": {
            "lastMessage": {
                "text":      text[:120],
                "senderId":  user_id_str,
                "createdAt": now.isoformat(),
            },
            "updatedAt": now,
        }},
    )

    payload = _fmt_message(msg_doc, db)

    # Broadcast via SocketIO so connected clients receive it in real time
    try:
        from .socket import socketio
        socketio.emit("new_message", payload, to=conv_id)
        for participant_oid in conv["participants"]:
            p_str = str(participant_oid)
            if p_str != user_id_str:
                socketio.emit("new_message", payload, to=p_str)
    except Exception:
        pass  # SocketIO not available; HTTP response is still returned

    # Push notifications to all recipients
    try:
        from .notifications import trigger_message_notification
        sender_doc = db.user.find_one({"_id": user_oid})
        recipient_oids = [p for p in conv["participants"] if p != user_oid]
        trigger_message_notification(
            sender_doc, recipient_oids, conv_id, text, current_app._get_current_object()
        )
    except Exception:
        pass

    return jsonify({"success": True, "data": payload}), 201


@chat_bp.route("/conversations/<conv_id>/messages", methods=["GET"])
def get_messages(conv_id):
    """
    Return paginated message history for a conversation (newest page first,
    messages within a page returned oldest-first).

    Query params:
      - page  (int, default 1)
      - limit (int, default 30, max 100)
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    conv_oid = _parse_oid(conv_id)
    if not conv_oid:
        return jsonify({"success": False, "message": "Invalid conversation ID"}), 400

    db      = current_app.db
    conv    = db.conversation.find_one(
        {"_id": conv_oid, "participants": user_oid}
    )
    if not conv:
        return jsonify({"success": False, "message": "Conversation not found"}), 404

    try:
        page  = max(1, int(request.args.get("page", 1)))
        limit = min(100, max(1, int(request.args.get("limit", 30))))
    except ValueError:
        return jsonify({"success": False, "message": "page and limit must be integers"}), 400

    skip     = (page - 1) * limit
    messages = list(
        db.message.find({"conversationId": conv_oid})
        .sort("createdAt", -1)
        .skip(skip)
        .limit(limit)
    )
    messages.reverse()  # oldest-first within the page

    total = db.message.count_documents({"conversationId": conv_oid})
    return jsonify({
        "success": True,
        "data": [_fmt_message(m, db) for m in messages],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit,
        },
    }), 200


@chat_bp.route("/conversations/<conv_id>/read", methods=["PUT"])
def mark_read(conv_id):
    """
    Mark every unread message in a conversation as read by the current user.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    conv_oid = _parse_oid(conv_id)
    if not conv_oid:
        return jsonify({"success": False, "message": "Invalid conversation ID"}), 400

    db   = current_app.db
    conv = db.conversation.find_one({"_id": conv_oid, "participants": user_oid})
    if not conv:
        return jsonify({"success": False, "message": "Conversation not found"}), 404

    result = db.message.update_many(
        {"conversationId": conv_oid, "readBy": {"$ne": user_oid}},
        {"$addToSet": {"readBy": user_oid}},
    )
    return jsonify({
        "success": True,
        "message": "Messages marked as read",
        "updated": result.modified_count,
    }), 200


@chat_bp.route("/conversations/<conv_id>/unread", methods=["GET"])
def unread_count(conv_id):
    """Return the number of unread messages in a conversation for the current user."""
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    conv_oid = _parse_oid(conv_id)
    if not conv_oid:
        return jsonify({"success": False, "message": "Invalid conversation ID"}), 400

    db   = current_app.db
    conv = db.conversation.find_one({"_id": conv_oid, "participants": user_oid})
    if not conv:
        return jsonify({"success": False, "message": "Conversation not found"}), 404

    count = db.message.count_documents(
        {"conversationId": conv_oid, "readBy": {"$ne": user_oid}}
    )
    return jsonify({"success": True, "data": {"unreadCount": count}}), 200
