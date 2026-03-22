from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from bson import ObjectId
from bson.errors import InvalidId
from .auth import decode_token

activities_bp = Blueprint("activities", __name__, url_prefix="/api/activities")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _parse_dt(value, field):
    """Parse an ISO-8601 datetime string. Returns (datetime, None) or (None, error_response)."""
    if not value:
        return None, None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt, None
    except ValueError:
        return None, (
            jsonify({"success": False, "message": f"Invalid {field} format. Use ISO-8601, e.g. 2026-03-01T00:00:00Z"}),
            400,
        )


def _serialize(doc):
    doc["id"] = str(doc.pop("_id"))
    doc["userId"] = str(doc["userId"])
    return doc


# ── Shared backend helper ─────────────────────────────────────────────────────

def _log_activity(db, user_oid, activity_type, metadata=None):
    """
    Persist an activity record from anywhere in the backend.
    Silently swallows errors so it never breaks the caller.

    Parameters
    ----------
    db            : pymongo database object
    user_oid      : ObjectId of the user performing the action
    activity_type : str  e.g. "LOGIN", "RENT_PAYMENT_RECEIVED"
    metadata      : dict  optional extra data stored under "metadata" key
    """
    try:
        db.activity.insert_one({
            "userId":    user_oid,
            "type":      activity_type,
            "metadata":  metadata or {},
            "createdAt": _now(),
        })
    except Exception:
        pass


# ── Save activity ─────────────────────────────────────────────────────────────

@activities_bp.route("", methods=["POST"])
def save_activity():
    """
    Save an activity for the authenticated user.
    All fields sent in the request body are stored as-is alongside the userId
    and a server-side timestamp.

    Auth: Bearer token.

    JSON body:
      - type        (string, recommended) — e.g. "login", "payment", "maintenance_request"
      - description (string, optional)
      - metadata    (object, optional)    — any extra key/value data
      - <any other fields>               — stored verbatim

    POST /api/activities
    """
    user_id, err = decode_token(request)
    if err:
        return err

    data = request.get_json(silent=True) or {}

    doc = {k: v for k, v in data.items()}
    doc["userId"] = ObjectId(user_id)
    doc["createdAt"] = _now()

    result = current_app.db.activity.insert_one(doc)
    doc["_id"] = result.inserted_id

    return jsonify({
        "success": True,
        "message": "Activity saved",
        "data": _serialize(doc),
    }), 201


# ── Get activities ────────────────────────────────────────────────────────────

@activities_bp.route("", methods=["GET"])
def list_activities():
    """
    Retrieve activities for the authenticated user.

    Auth: Bearer token.

    Query params (all optional):
      - from    ISO-8601 datetime — inclusive start of time range
      - to      ISO-8601 datetime — inclusive end of time range
      - type    filter by activity type string
      - limit   max number of results (default 100, max 500)
      - skip    number of records to skip for pagination (default 0)

    GET /api/activities
    GET /api/activities?from=2026-03-01T00:00:00Z&to=2026-03-31T23:59:59Z
    GET /api/activities?type=payment&limit=20
    """
    user_id, err = decode_token(request)
    if err:
        return err

    query = {"userId": ObjectId(user_id)}

    # Time range filters
    from_raw = (request.args.get("from") or "").strip()
    to_raw = (request.args.get("to") or "").strip()

    from_dt, err = _parse_dt(from_raw, "from")
    if err:
        return err
    to_dt, err = _parse_dt(to_raw, "to")
    if err:
        return err

    if from_dt or to_dt:
        time_filter = {}
        if from_dt:
            time_filter["$gte"] = from_dt
        if to_dt:
            time_filter["$lte"] = to_dt
        query["createdAt"] = time_filter

    # Optional type filter
    activity_type = (request.args.get("type") or "").strip()
    if activity_type:
        query["type"] = activity_type

    # Pagination
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
        skip = max(int(request.args.get("skip", 0)), 0)
    except ValueError:
        return jsonify({"success": False, "message": "limit and skip must be integers"}), 400

    cursor = (
        current_app.db.activity
        .find(query)
        .sort("createdAt", -1)
        .skip(skip)
        .limit(limit)
    )
    activities = [_serialize(a) for a in cursor]

    return jsonify({
        "success": True,
        "count": len(activities),
        "data": activities,
    }), 200
