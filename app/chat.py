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


def _fmt_message(msg):
    return {
        "id": str(msg["_id"]),
        "conversationId": str(msg["conversationId"]),
        "senderId": str(msg["senderId"]),
        "text": msg["text"],
        "readBy": [str(uid) for uid in msg.get("readBy", [])],
        "createdAt": msg["createdAt"].isoformat(),
    }


def _fmt_conversation(conv, current_uid_str=None, db=None):
    data = {
        "id": str(conv["_id"]),
        "participants": [str(p) for p in conv["participants"]],
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
    sender_role    = (sender_doc.get("role") or "tenant").lower()
    recipient_role = (recipient_doc.get("role") or "tenant").lower()
    sender_id      = sender_doc["_id"]
    recipient_id   = recipient_doc["_id"]

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
            rec_prop_id = recipient_doc.get("propertyId")
            if not rec_prop_id:
                return False, "That user is not linked to any property"

            if sender_role == "landlord":
                prop = db.property.find_one(
                    {"_id": rec_prop_id, "landlordId": sender_id}
                )
            else:  # property_manager
                prop = db.property.find_one(
                    {"_id": rec_prop_id, "propertyManagerId": sender_id}
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

def get_or_create_conversation(sender_oid, recipient_oid, db, property_id=None):
    """
    Return (conv_doc, created: bool) for the 1-on-1 thread between two users.
    Participants are stored sorted so the lookup is always deterministic.
    """
    pair = sorted([sender_oid, recipient_oid], key=lambda x: str(x))
    conv = db.conversation.find_one({"participants": {"$all": pair, "$size": 2}})
    if conv:
        return conv, False

    now = _now()
    doc = {
        "participants": pair,
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
    # Conversation: fast participant-pair lookup
    db.conversation.create_index("participants")
    db.conversation.create_index([("updatedAt", -1)])
    # Message: paginated history and unread counts
    db.message.create_index([("conversationId", 1), ("createdAt", -1)])
    db.message.create_index([("conversationId", 1), ("readBy", 1)])


# ── REST endpoints ────────────────────────────────────────────────────────────

@chat_bp.route("/conversations", methods=["POST"])
def start_conversation():
    """
    Start or retrieve a 1-on-1 conversation with another user.

    Permission is validated before a conversation is created.

    JSON body:
      - recipientId  (string, required)
      - propertyId   (string, optional) — contextual property reference

    Returns the conversation document (201 if new, 200 if existing).
    """
    sender_id_str, err = decode_token(request)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    recipient_id_str = (data.get("recipientId") or "").strip()
    if not recipient_id_str:
        return jsonify({"success": False, "message": "recipientId is required"}), 400

    sender_oid    = _parse_oid(sender_id_str)
    recipient_oid = _parse_oid(recipient_id_str)
    if not recipient_oid:
        return jsonify({"success": False, "message": "Invalid recipientId"}), 400
    if sender_oid == recipient_oid:
        return jsonify({"success": False, "message": "Cannot start a conversation with yourself"}), 400

    db            = current_app.db
    sender_doc    = db.user.find_one({"_id": sender_oid})
    recipient_doc = db.user.find_one({"_id": recipient_oid})

    if not sender_doc or not recipient_doc:
        return jsonify({"success": False, "message": "User not found"}), 404

    allowed, reason = can_chat(sender_doc, recipient_doc, db)
    if not allowed:
        # A conversation may already exist — allow retrieval even if the
        # direction check currently fails (e.g. roles changed after creation).
        pair = sorted([sender_oid, recipient_oid], key=lambda x: str(x))
        existing = db.conversation.find_one(
            {"participants": {"$all": pair, "$size": 2}}
        )
        if not existing:
            return jsonify({"success": False, "message": reason}), 403

    prop_oid = _parse_oid(data.get("propertyId") or "")
    conv, created = get_or_create_conversation(
        sender_oid, recipient_oid, db, prop_oid
    )
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
                {"firstName": 1, "lastName": 1, "role": 1, "profilePicture": 1},
            )
            if other:
                fmt["recipient"] = {
                    "id": str(other["_id"]),
                    "name": f"{other.get('firstName', '')} {other.get('lastName', '')}".strip(),
                    "role": other.get("role", "tenant"),
                    "profilePicture": other.get("profilePicture"),
                }
        result.append(fmt)

    return jsonify({"success": True, "data": result}), 200


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
        "data": [_fmt_message(m) for m in messages],
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
