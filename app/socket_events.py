"""
Real-time chat via Flask-SocketIO
──────────────────────────────────

CLIENT CONNECTION
  // Pass JWT in the handshake auth object
  const socket = io("http://localhost:5000", {
      auth: { token: "<jwt>" }
  });

CLIENT → SERVER events
  ┌──────────────────────┬──────────────────────────────────────────────────┐
  │ Event                │ Payload                                          │
  ├──────────────────────┼──────────────────────────────────────────────────┤
  │ join_conversation    │ { conversationId: string }                       │
  │ leave_conversation   │ { conversationId: string }                       │
  │ send_message         │ { conversationId: string, text: string }         │
  │ typing               │ { conversationId: string, isTyping: bool }       │
  │ mark_read            │ { conversationId: string }                       │
  └──────────────────────┴──────────────────────────────────────────────────┘

SERVER → CLIENT events
  ┌──────────────────────┬──────────────────────────────────────────────────┐
  │ Event                │ Payload                                          │
  ├──────────────────────┼──────────────────────────────────────────────────┤
  │ error                │ { message: string }                              │
  │ joined               │ { conversationId: string }                       │
  │ new_message          │ { id, conversationId, senderId, text, readBy,    │
  │                      │   createdAt }                                    │
  │ typing               │ { conversationId, userId, isTyping }             │
  │ messages_read        │ { conversationId, userId }                       │
  └──────────────────────┴──────────────────────────────────────────────────┘

ROOMS
  • Each user automatically joins a personal room keyed by their user ID.
    This allows pushing events to a user regardless of which conversation
    they have open.
  • Conversation rooms are keyed by the conversation's string _id.
    Clients join/leave them explicitly via join_conversation /
    leave_conversation.
"""

from datetime import datetime, timezone

import jwt
from bson import ObjectId
from bson.errors import InvalidId
from flask import request
from flask_socketio import disconnect, emit, join_room, leave_room

from .socket import socketio

# sid → user_id_str: maintained for the lifetime of a socket connection.
# For multi-process deployments replace this with a Redis-backed store.
_connected: dict[str, str] = {}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _safe_oid(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _resolve_user(token: str, app):
    """Decode *token* and return the matching user document, or None."""
    try:
        payload = jwt.decode(
            token, app.config["SECRET_KEY"], algorithms=["HS256"]
        )
        user_oid = ObjectId(payload["sub"])
        return app.db.user.find_one({"_id": user_oid})
    except Exception:
        return None


def _current_user_id() -> str | None:
    """Return the user_id_str for the current socket connection."""
    return _connected.get(request.sid)


# ── Connection lifecycle ──────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect(auth):
    """
    Authenticate the connecting socket.

    The client must supply the JWT either in the handshake auth object
    ({ token: "..." }) or as a query parameter (?token=...).
    """
    from flask import current_app  # imported here; app context is active

    token = ""
    if isinstance(auth, dict):
        token = auth.get("token", "")
    if not token:
        token = request.args.get("token", "")

    if not token:
        emit("error", {"message": "Authentication token required"})
        disconnect()
        return False

    app  = current_app._get_current_object()
    user = _resolve_user(token, app)
    if not user:
        emit("error", {"message": "Invalid or expired token"})
        disconnect()
        return False

    user_id_str = str(user["_id"])
    _connected[request.sid] = user_id_str

    # Join the personal room so we can push targeted notifications
    join_room(user_id_str)


@socketio.on("disconnect")
def on_disconnect():
    _connected.pop(request.sid, None)


# ── Conversation rooms ────────────────────────────────────────────────────────

@socketio.on("join_conversation")
def on_join(data):
    """
    Subscribe the socket to a conversation room.
    Validates that the authenticated user is a participant.
    """
    from flask import current_app

    uid = _current_user_id()
    if not uid:
        emit("error", {"message": "Not authenticated"})
        return

    conv_id  = (data or {}).get("conversationId", "")
    conv_oid = _safe_oid(conv_id)
    if not conv_oid:
        emit("error", {"message": "Invalid conversationId"})
        return

    db   = current_app.db
    conv = db.conversation.find_one(
        {"_id": conv_oid, "participants": ObjectId(uid)}
    )
    if not conv:
        emit("error", {"message": "Conversation not found or access denied"})
        return

    join_room(conv_id)
    emit("joined", {"conversationId": conv_id})


@socketio.on("leave_conversation")
def on_leave(data):
    conv_id = (data or {}).get("conversationId", "")
    if conv_id:
        leave_room(conv_id)


# ── Messaging ─────────────────────────────────────────────────────────────────

@socketio.on("send_message")
def on_send_message(data):
    """
    Persist a message and broadcast it to every participant.

    The message is emitted to:
      1. The conversation room  (clients actively viewing the thread).
      2. Each participant's personal room (for notification badges when the
         conversation is not open).
    """
    from flask import current_app

    uid = _current_user_id()
    if not uid:
        emit("error", {"message": "Not authenticated"})
        return

    conv_id = (data or {}).get("conversationId", "")
    text    = ((data or {}).get("content") or (data or {}).get("text") or "").strip()

    if not conv_id or not text:
        emit("error", {"message": "conversationId and content are required"})
        return

    if len(text) > 4000:
        emit("error", {"message": "Message must not exceed 4000 characters"})
        return

    conv_oid = _safe_oid(conv_id)
    user_oid = ObjectId(uid)
    db       = current_app.db

    conv = db.conversation.find_one(
        {"_id": conv_oid, "participants": user_oid}
    )
    if not conv:
        emit("error", {"message": "Conversation not found or access denied"})
        return

    now     = _now()
    msg_doc = {
        "conversationId": conv_oid,
        "senderId":       user_oid,
        "text":           text,
        "readBy":         [user_oid],  # sender has implicitly read it
        "createdAt":      now,
    }
    result       = db.message.insert_one(msg_doc)
    msg_doc["_id"] = result.inserted_id

    # Persist a lightweight preview on the conversation document
    db.conversation.update_one(
        {"_id": conv_oid},
        {"$set": {
            "lastMessage": {
                "text":      text[:120],
                "senderId":  uid,
                "createdAt": now.isoformat(),
            },
            "updatedAt": now,
        }},
    )

    from .chat import _fmt_message
    payload = _fmt_message(msg_doc, current_app.db)

    # Broadcast to everyone in the conversation room
    emit("new_message", payload, to=conv_id)

    # Push to each participant's personal room for clients not in the room
    for participant_oid in conv["participants"]:
        p_str = str(participant_oid)
        if p_str != uid:
            socketio.emit("new_message", payload, to=p_str)

    # Push notifications to offline / background recipients
    try:
        from .notifications import trigger_message_notification
        sender_doc = db.user.find_one({"_id": user_oid})
        recipient_oids = [p for p in conv["participants"] if p != user_oid]
        trigger_message_notification(
            sender_doc, recipient_oids, conv_id, text, current_app._get_current_object()
        )
    except Exception:
        pass


# ── Typing indicators ─────────────────────────────────────────────────────────

@socketio.on("typing")
def on_typing(data):
    uid = _current_user_id()
    if not uid:
        return

    conv_id   = (data or {}).get("conversationId", "")
    is_typing = bool((data or {}).get("isTyping", False))

    if not conv_id:
        return

    # Broadcast to the room, excluding the sender
    emit(
        "typing",
        {"conversationId": conv_id, "userId": uid, "isTyping": is_typing},
        to=conv_id,
        include_self=False,
    )


# ── Read receipts ─────────────────────────────────────────────────────────────

@socketio.on("mark_read")
def on_mark_read(data):
    from flask import current_app

    uid = _current_user_id()
    if not uid:
        return

    conv_id  = (data or {}).get("conversationId", "")
    conv_oid = _safe_oid(conv_id)
    user_oid = ObjectId(uid)

    db   = current_app.db
    conv = db.conversation.find_one(
        {"_id": conv_oid, "participants": user_oid}
    )
    if not conv:
        return

    db.message.update_many(
        {"conversationId": conv_oid, "readBy": {"$ne": user_oid}},
        {"$addToSet": {"readBy": user_oid}},
    )

    emit(
        "messages_read",
        {"conversationId": conv_id, "userId": uid},
        to=conv_id,
    )
