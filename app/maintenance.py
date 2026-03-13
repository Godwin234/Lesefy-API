import os
import re
import jwt
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, send_file, current_app
from bson import ObjectId
from bson.errors import InvalidId
from .auth import decode_token

maintenance_bp = Blueprint("maintenance", __name__, url_prefix="/api/maintenance")

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic"}
ALLOWED_STATUSES = {"open", "in_progress", "resolved", "closed"}
ALLOWED_PRIORITIES = {"low", "medium", "high", "urgent"}
MAX_PICTURES = 10


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _uploads_root():
    return os.path.abspath(os.path.join(current_app.root_path, "..", "uploads"))


def _maintenance_dir():
    path = os.path.join(_uploads_root(), "MaintenancePictures")
    os.makedirs(path, exist_ok=True)
    return path


def _sanitize(value):
    return re.sub(r"[^\w\-]", "_", value)


def _safe_realpath(path, base_dir):
    return os.path.realpath(path).startswith(os.path.realpath(base_dir))


def _parse_oid(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _serialize(doc):
    """Stringify all ObjectId fields for JSON output."""
    if doc is None:
        return None
    doc["id"] = str(doc.pop("_id"))
    if "propertyId" in doc:
        doc["propertyId"] = str(doc["propertyId"])
    if "requestedBy" in doc:
        doc["requestedBy"] = str(doc["requestedBy"])
    if "assignedTo" in doc and doc["assignedTo"]:
        doc["assignedTo"] = str(doc["assignedTo"])
    for note in doc.get("notes", []):
        if "addedBy" in note and note["addedBy"]:
            note["addedBy"] = str(note["addedBy"])
    return doc


def _not_found():
    return jsonify({"success": False, "message": "Maintenance request not found"}), 404


# ── Create request ────────────────────────────────────────────────────────────

@maintenance_bp.route("", methods=["POST"])
def create_request():
    """
    Create a maintenance request. Pictures can be attached separately via the
    upload endpoint, or included in a multipart/form-data submission here.

    Auth: Bearer token (tenant or landlord).

    Accepts multipart/form-data OR application/json.
    Form / JSON fields:
      - propertyId  (string, required)
      - title       (string, required)
      - description (string, optional)
      - priority    (string, optional) — Low | Medium | High | Urgent  (default: Medium)

    Optionally attach up to 10 files under the key "pictures".

    POST /api/maintenance
    """
    
    user_id, err = decode_token(request)
    
    if err:
        return err

    # Support both JSON and multipart
    if request.content_type and "application/json" in request.content_type:
        data = request.get_json(silent=True) or {}
    
        get = lambda k: (data.get(k) or "").strip()
    else:
        get = lambda k: (request.form.get(k) or "").strip()
        
    print(get("priority"))
    property_id_raw = get("propertyId")
    title = get("title")
    description = get("description")
    priority = get("priority") or "medium"

    if not title:
        return jsonify({"success": False, "message": "title is required"}), 400

    if priority not in ALLOWED_PRIORITIES:
        return jsonify({
            "success": False,
            "message": f"priority must be one of: {', '.join(sorted(ALLOWED_PRIORITIES))}",
        }), 400

    # Resolve propertyId — use the value sent, or fall back to the user's assigned property
    property_oid = _parse_oid(property_id_raw) if property_id_raw else None
    if not property_oid:
        user_doc = current_app.db.user.find_one(
            {"_id": ObjectId(user_id)}, {"propertyId": 1}
        )
        property_oid = user_doc.get("propertyId") if user_doc else None
    if not property_oid:
        return jsonify({"success": False, "message": "propertyId is required and no property is assigned to this user"}), 400

    if not current_app.db.property.find_one({"_id": property_oid}):
        return jsonify({"success": False, "message": "Property not found"}), 404

    doc = {
        "propertyId": property_oid,
        "requestedBy": ObjectId(user_id),
        "title": title,
        "description": description,
        "priority": priority,
        "status": "Open",
        "pictures": [],
        "assignedTo": None,
        "notes": [],
        "createdAt": _now(),
        "updatedAt": _now(),
    }
    result = current_app.db.maintenance.insert_one(doc)
    request_id = result.inserted_id

    # Handle pictures uploaded alongside the request
    pictures = request.files.getlist("pictures")
    saved_files = _save_pictures(str(request_id), user_id, pictures)
    if saved_files:
        current_app.db.maintenance.update_one(
            {"_id": request_id},
            {"$push": {"pictures": {"$each": saved_files}}},
        )
        doc["pictures"] = saved_files

    doc["_id"] = request_id
    return jsonify({
        "success": True,
        "message": "Maintenance request created",
        "data": _serialize(doc),
    }), 201


# ── List / filter requests ────────────────────────────────────────────────────

@maintenance_bp.route("", methods=["GET"])
def list_requests():
    """
    List maintenance requests.

    Auth: Bearer token.

    Query params (all optional):
      - propertyId  — filter by property
      - status      — filter by status
      - priority    — filter by priority

    Resolution order when propertyId is not provided:
      1. Look up the user document; if it has a propertyId field, use that.
      2. Fall back to returning all requests the user submitted OR for any
         property they own as a landlord.

    GET /api/maintenance
    """
    user_id, err = decode_token(request)
    
    if err:
        return err

    query = {}
    property_id_raw = (request.args.get("propertyId") or "").strip()
    status = (request.args.get("status") or "").strip()
    priority = (request.args.get("priority") or "").strip()

    if property_id_raw:
        oid = _parse_oid(property_id_raw)
        if not oid:
            # Invalid ObjectId — fall back to the property on the user's document
            user_doc = current_app.db.user.find_one(
                {"_id": ObjectId(user_id)}, {"propertyId": 1}
            )
            oid = user_doc.get("propertyId") if user_doc else None
            if not oid:
                return jsonify({"success": False, "message": "Invalid propertyId and no property found for this user"}), 400
        query["propertyId"] = oid
    else:
        # Try to resolve the property from the user's own document
        user_doc = current_app.db.user.find_one(
            {"_id": ObjectId(user_id)}, {"propertyId": 1}
        )
        user_property_id = user_doc.get("propertyId") if user_doc else None

        if user_property_id:
            # Tenant path: scope to the property they are assigned to
            query["propertyId"] = user_property_id
        else:
            # Landlord / fallback path: requests they submitted OR properties they own
            owned_ids = [
                p["_id"]
                for p in current_app.db.property.find(
                    {"landlordId": ObjectId(user_id)}, {"_id": 1}
                )
            ]
            query["$or"] = [
                {"requestedBy": ObjectId(user_id)},
                {"propertyId": {"$in": owned_ids}},
            ]

    if status:
        query["status"] = status
    if priority:
        query["priority"] = priority

    requests_list = list(current_app.db.maintenance.find(query).sort("createdAt", -1))
    return jsonify({
        "success": True,
        "data": [_serialize(r) for r in requests_list],
    }), 200


# ── Get single request ────────────────────────────────────────────────────────

@maintenance_bp.route("/<request_id>", methods=["GET"])
def get_request(request_id):
    """
    Get a single maintenance request by ID.

    Auth: Bearer token.

    GET /api/maintenance/<request_id>
    """
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(request_id)
    if not oid:
        return _not_found()

    req = current_app.db.maintenance.find_one({"_id": oid})
    if not req:
        return _not_found()

    return jsonify({"success": True, "data": _serialize(req)}), 200


# ── Update request ────────────────────────────────────────────────────────────

@maintenance_bp.route("/<request_id>", methods=["PUT"])
def update_request(request_id):
    """
    Update a maintenance request.

    Auth: Bearer token.
      - The requester can update title, description, priority.
      - The landlord (owner of the linked property) can additionally update
        status, assignedTo, and add notes.

    JSON body (all optional):
      - title
      - description
      - priority    — Low | Medium | High | Urgent
      - status      — Open | In Progress | Resolved | Closed
      - assignedTo  (user id string)
      - note        (string — appended to notes array with timestamp)

    PUT /api/maintenance/<request_id>
    """
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(request_id)
    if not oid:
        return _not_found()

    req = current_app.db.maintenance.find_one({"_id": oid})
    if not req:
        return _not_found()

    # Determine caller's role for this request
    prop = current_app.db.property.find_one({"_id": req["propertyId"]})
    is_landlord = prop and str(prop.get("landlordId")) == user_id
    is_requester = str(req["requestedBy"]) == user_id

    if not is_landlord and not is_requester:
        return jsonify({"success": False, "message": "Not authorised"}), 403

    data = request.get_json(silent=True) or {}
    updates = {}
    array_ops = {}

    # Fields anyone involved can change
    if "title" in data:
        updates["title"] = (data["title"] or "").strip()
    if "description" in data:
        updates["description"] = (data["description"] or "").strip()
    if "priority" in data:
        p = (data["priority"] or "").strip()
        if p not in ALLOWED_PRIORITIES:
            return jsonify({
                "success": False,
                "message": f"priority must be one of: {', '.join(sorted(ALLOWED_PRIORITIES))}",
            }), 400
        updates["priority"] = p

    # Landlord-only fields
    if is_landlord:
        if "status" in data:
            s = (data["status"] or "").strip()
            print(s)
            if s not in ALLOWED_STATUSES:
                return jsonify({
                    "success": False,
                    "message": f"status must be one of: {', '.join(sorted(ALLOWED_STATUSES))}",
                }), 400
            updates["status"] = s

        if "assignedTo" in data:
            at = (data["assignedTo"] or "").strip()
            updates["assignedTo"] = ObjectId(at) if at else None

        if "note" in data and data["note"]:
            array_ops["$push"] = {
                "notes": {
                    "text": data["note"].strip(),
                    "addedBy": ObjectId(user_id),
                    "addedAt": _now(),
                }
            }

    if not updates and not array_ops:
        return jsonify({"success": False, "message": "No valid fields to update"}), 400

    updates["updatedAt"] = _now()
    mongo_update = {"$set": updates}
    if array_ops:
        mongo_update.update(array_ops)

    current_app.db.maintenance.update_one({"_id": oid}, mongo_update)
    req = current_app.db.maintenance.find_one({"_id": oid})
    return jsonify({"success": True, "message": "Request updated", "data": _serialize(req)}), 200


# ── Delete request ────────────────────────────────────────────────────────────

@maintenance_bp.route("/<request_id>", methods=["DELETE"])
def delete_request(request_id):
    """
    Delete a maintenance request and all its stored pictures.

    Auth: Bearer token (requester or landlord only).

    DELETE /api/maintenance/<request_id>
    """
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(request_id)
    if not oid:
        return _not_found()

    req = current_app.db.maintenance.find_one({"_id": oid})
    if not req:
        return _not_found()

    prop = current_app.db.property.find_one({"_id": req["propertyId"]})
    is_landlord = prop and str(prop.get("landlordId")) == user_id
    is_requester = str(req["requestedBy"]) == user_id

    if not is_landlord and not is_requester:
        return jsonify({"success": False, "message": "Not authorised"}), 403

    # Delete stored picture files
    maint_dir = _maintenance_dir()
    for pic in req.get("pictures", []):
        path = os.path.join(maint_dir, pic)
        if os.path.isfile(path) and _safe_realpath(path, maint_dir):
            os.remove(path)

    current_app.db.maintenance.delete_one({"_id": oid})
    return jsonify({"success": True, "message": "Maintenance request deleted"}), 200


# ── Upload pictures ───────────────────────────────────────────────────────────

@maintenance_bp.route("/<request_id>/pictures", methods=["POST"])
def upload_pictures(request_id):
    """
    Upload one or more pictures for an existing maintenance request.

    Auth: Bearer token (requester or landlord).

    Multipart/form-data:
      - pictures  (one or more image files)

    POST /api/maintenance/<request_id>/pictures
    """
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(request_id)
    if not oid:
        return _not_found()

    req = current_app.db.maintenance.find_one({"_id": oid})
    if not req:
        return _not_found()

    prop = current_app.db.property.find_one({"_id": req["propertyId"]})
    is_landlord = prop and str(prop.get("landlordId")) == user_id
    is_requester = str(req["requestedBy"]) == user_id
    if not is_landlord and not is_requester:
        return jsonify({"success": False, "message": "Not authorised"}), 403

    current_count = len(req.get("pictures", []))
    pictures = request.files.getlist("pictures")

    if not pictures or all(not f.filename for f in pictures):
        return jsonify({"success": False, "message": "No pictures provided"}), 400

    if current_count + len(pictures) > MAX_PICTURES:
        return jsonify({
            "success": False,
            "message": f"Maximum {MAX_PICTURES} pictures per request. Already has {current_count}.",
        }), 400

    saved = _save_pictures(request_id, user_id, pictures)
    if not saved:
        return jsonify({"success": False, "message": "No valid image files found"}), 400

    current_app.db.maintenance.update_one(
        {"_id": oid},
        {
            "$push": {"pictures": {"$each": saved}},
            "$set": {"updatedAt": _now()},
        },
    )
    return jsonify({
        "success": True,
        "message": f"{len(saved)} picture(s) uploaded",
        "data": {"uploaded": saved},
    }), 201


# ── Retrieve a picture ────────────────────────────────────────────────────────

@maintenance_bp.route("/<request_id>/pictures/<filename>", methods=["GET"])
def get_picture(request_id, filename):
    """
    Retrieve a single maintenance picture.

    Auth: Bearer token — accepted either as:
      - Authorization: Bearer <token>  header  (API clients)
      - ?token=<token>                 query param (image tags / mobile Image components)

    GET /api/maintenance/<request_id>/pictures/<filename>
    GET /api/maintenance/<request_id>/pictures/<filename>?token=<jwt>
    """
    # Accept token from the Authorization header OR from a query parameter so
    # that image tags / mobile Image components can load pictures without
    # needing to inject a custom header.
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        query_token = (request.args.get("token") or "").strip()
        if query_token:
            try:
                payload = jwt.decode(
                    query_token, current_app.config["SECRET_KEY"], algorithms=["HS256"]
                )
                user_id = payload["sub"]
            except jwt.ExpiredSignatureError:
                return jsonify({"success": False, "message": "Token has expired"}), 401
            except jwt.InvalidTokenError:
                return jsonify({"success": False, "message": "Invalid token"}), 401
                "Token error ignored for image access — allowing unauthenticated access to images with a valid token in the query param"
        else:
            return jsonify({"success": False, "message": "Authorization token required"}), 401
    else:
        user_id, err = decode_token(request)
        if err:
            return err

    oid = _parse_oid(request_id)
    if not oid:
        return _not_found()

    req = current_app.db.maintenance.find_one({"_id": oid})
    if not req:
        return _not_found()

    # Validate filename contains only safe characters (word chars, hyphens, one dot).
    # A regex is used instead of _sanitize() so the extension dot is preserved.
    if not re.match(r'^[\w\-]+\.[\w]+$', filename):
        return jsonify({"success": False, "message": "Invalid filename"}), 400

    if filename not in req.get("pictures", []):
        return jsonify({"success": False, "message": "Picture not found"}), 404

    maint_dir = _maintenance_dir()
    path = os.path.join(maint_dir, filename)
    if not _safe_realpath(path, maint_dir) or not os.path.isfile(path):
        return jsonify({"success": False, "message": "Picture not found"}), 404

    return send_file(path)


# ── Delete a picture ──────────────────────────────────────────────────────────

@maintenance_bp.route("/<request_id>/pictures/<filename>", methods=["DELETE"])
def delete_picture(request_id, filename):
    """
    Delete a single picture from a maintenance request.

    Auth: Bearer token (requester or landlord).

    DELETE /api/maintenance/<request_id>/pictures/<filename>
    """
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(request_id)
    if not oid:
        return _not_found()

    req = current_app.db.maintenance.find_one({"_id": oid})
    if not req:
        return _not_found()

    prop = current_app.db.property.find_one({"_id": req["propertyId"]})
    is_landlord = prop and str(prop.get("landlordId")) == user_id
    is_requester = str(req["requestedBy"]) == user_id
    if not is_landlord and not is_requester:
        return jsonify({"success": False, "message": "Not authorised"}), 403

    if not re.match(r'^[\w\-]+\.[\w]+$', filename):
        return jsonify({"success": False, "message": "Invalid filename"}), 400

    if filename not in req.get("pictures", []):
        return jsonify({"success": False, "message": "Picture not found"}), 404

    maint_dir = _maintenance_dir()
    path = os.path.join(maint_dir, filename)
    if _safe_realpath(path, maint_dir) and os.path.isfile(path):
        os.remove(path)

    current_app.db.maintenance.update_one(
        {"_id": oid},
        {
            "$pull": {"pictures": filename},
            "$set": {"updatedAt": _now()},
        },
    )
    return jsonify({"success": True, "message": "Picture deleted"}), 200


# ── Internal helper ───────────────────────────────────────────────────────────

def _save_pictures(request_id, user_id, files):
    """
    Save validated image files to MaintenancePictures/.
    Filename format: <request_id>__<sanitized_original_name>.<ext>
    The user_id is intentionally excluded so all participants on a request
    (tenant, landlord, property manager) can retrieve pictures by filename alone.
    Returns list of stored filenames.
    """
    maint_dir = _maintenance_dir()
    saved = []
    for f in files:
        if not f or not f.filename:
            continue
        orig = f.filename
        if "." not in orig:
            continue
        ext = orig.rsplit(".", 1)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue
        filename = f"{_sanitize(request_id)}__{_sanitize(orig)}.{ext}"
        path = os.path.join(maint_dir, filename)
        if not _safe_realpath(path, maint_dir):
            continue
        f.save(path)
        saved.append(filename)
    return saved
