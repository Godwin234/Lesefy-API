from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from bson import ObjectId
from bson.errors import InvalidId
from .auth import decode_token

properties_bp = Blueprint("properties", __name__, url_prefix="/api/properties")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _to_str_id(doc):
    """Convert a MongoDB document's ObjectId fields to strings for JSON output."""
    if doc is None:
        return None
    doc["id"] = str(doc.pop("_id"))
    if "landlordId" in doc:
        doc["landlordId"] = str(doc["landlordId"])
    for t in doc.get("tenants", []):
        t["tenantId"] = str(t["tenantId"])
        if "userId" in t:
            t["userId"] = str(t["userId"])
    return doc


def _parse_oid(value, field="id"):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _property_not_found():
    return jsonify({"success": False, "message": "Property not found"}), 404


# ── Property endpoints ────────────────────────────────────────────────────────

@properties_bp.route("", methods=["POST"])
def create_property():
    """
    Create a new property.

    Requires: Bearer token (landlord).

    JSON body:
      - address        (string, required)
      - city           (string, required)
      - units          (int, required)
      - monthlyRevenue (number, optional, default 0)

    POST /api/properties
    """
    user_id, err = decode_token(request)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    address = (data.get("address") or "").strip()
    city = (data.get("city") or "").strip()
    units = data.get("units")

    if not address or not city:
        return jsonify({"success": False, "message": "address and city are required"}), 400
    if units is None or not isinstance(units, int) or units < 1:
        return jsonify({"success": False, "message": "units must be a positive integer"}), 400

    # Start from everything the client sent, then set/override server-controlled fields.
    _server_owned = {"_id", "createdAt", "updatedAt", "landlordId", "tenants"}
    doc = {k: v for k, v in data.items() if k not in _server_owned}
    doc["landlordId"]   = ObjectId(user_id)
    doc["address"]      = address
    doc["city"]         = city
    doc["units"]        = units
    doc.setdefault("monthlyRevenue", 0)
    doc.setdefault("description", "")
    doc["tenants"]  = []
    doc["createdAt"] = _now()
    doc["updatedAt"] = _now()
    result = current_app.db.property.insert_one(doc)
    doc["_id"] = result.inserted_id

    return jsonify({
        "success": True,
        "message": "Property created successfully",
        "data": _to_str_id(doc),
    }), 201


@properties_bp.route("", methods=["GET"])
def list_properties():
    """
    List all properties owned by the authenticated landlord.

    Requires: Bearer token.

    GET /api/properties
    """
    user_id, err = decode_token(request)
    if err:
        return err

    props = list(current_app.db.property.find({"landlordId": ObjectId(user_id)}))
    return jsonify({
        "success": True,
        "data": [_to_str_id(p) for p in props],
    }), 200


@properties_bp.route("/<property_id>", methods=["GET"])
def get_property(property_id):
    """
    Get a single property by ID.

    Requires: Bearer token.

    GET /api/properties/<property_id>
    """
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(property_id)
    if not oid:
        return _property_not_found()

    prop = current_app.db.property.find_one({"_id": oid, "landlordId": ObjectId(user_id)})
    if not prop:
        return _property_not_found()

    return jsonify({"success": True, "data": _to_str_id(prop)}), 200


@properties_bp.route("/<property_id>", methods=["PUT"])
def update_property(property_id):
    """
    Update top-level property fields (address, city, units, monthlyRevenue).
    Does NOT modify the tenants array — use the tenant sub-endpoints for that.

    Requires: Bearer token (must be the landlord).

    PUT /api/properties/<property_id>
    """
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(property_id)
    if not oid:
        return _property_not_found()

    data = request.get_json(silent=True) or {}
    _IMMUTABLE = {"_id", "createdAt", "landlordId", "tenants"}
    _NUMERIC   = {"units", "monthlyRevenue"}
    updates = {}
    for k, v in data.items():
        if k in _IMMUTABLE:
            continue
        if k == "units":
            if not isinstance(v, int) or v < 1:
                return jsonify({"success": False, "message": "units must be a positive integer"}), 400
        if k in _NUMERIC:
            try:
                updates[k] = type(v)(v)  # keep original numeric type
            except (TypeError, ValueError):
                updates[k] = v
        else:
            updates[k] = v

    if not updates:
        return jsonify({"success": False, "message": "No valid fields to update"}), 400

    updates["updatedAt"] = _now()
    result = current_app.db.property.update_one(
        {"_id": oid, "landlordId": ObjectId(user_id)},
        {"$set": updates},
    )
    if result.matched_count == 0:
        return _property_not_found()

    prop = current_app.db.property.find_one({"_id": oid})
    return jsonify({"success": True, "message": "Property updated", "data": _to_str_id(prop)}), 200


@properties_bp.route("/<property_id>", methods=["DELETE"])
def delete_property(property_id):
    """
    Delete a property and remove it from all linked tenants' propertyId fields.

    Requires: Bearer token (must be the landlord).

    DELETE /api/properties/<property_id>
    """
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(property_id)
    if not oid:
        return _property_not_found()

    prop = current_app.db.property.find_one({"_id": oid, "landlordId": ObjectId(user_id)})
    if not prop:
        return _property_not_found()

    # Detach all tenants that were linked to this property
    tenant_ids = [t["tenantId"] for t in prop.get("tenants", [])]
    if tenant_ids:
        current_app.db.user.update_many(
            {"_id": {"$in": tenant_ids}, "propertyId": oid},
            {"$unset": {"propertyId": "", "unit": "", "rentStatus": ""}},
        )

    current_app.db.property.delete_one({"_id": oid})
    return jsonify({"success": True, "message": "Property deleted"}), 200


# ── Tenant sub-endpoints ──────────────────────────────────────────────────────

@properties_bp.route("/<property_id>/tenants", methods=["POST"])
def add_tenant(property_id):
    """
    Add an existing user as a tenant of this property.
    The user document is updated with propertyId, unit, and rentStatus so the
    tenant–property link can be resolved from either side.

    Requires: Bearer token (landlord).

    JSON body:
      - userId     (string, required) — the user to attach as a tenant
      - unit       (string, required) — e.g. "Apt 1A"
      - rentStatus (string, optional) — "Paid" | "Overdue" | "Pending", default "Pending"

    POST /api/properties/<property_id>/tenants
    """
    landlord_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(property_id)
    if not oid:
        return _property_not_found()

    prop = current_app.db.property.find_one({"_id": oid, "landlordId": ObjectId(landlord_id)})
    if not prop:
        return _property_not_found()

    data = request.get_json(silent=True) or {}
    user_id_raw = (data.get("userId") or "").strip()
    unit = (data.get("unit") or "").strip()
    rent_status = (data.get("rentStatus") or "Pending").strip()

    if not user_id_raw or not unit:
        return jsonify({"success": False, "message": "userId and unit are required"}), 400

    valid_statuses = {"Paid", "Overdue", "Pending"}
    if rent_status not in valid_statuses:
        return jsonify({
            "success": False,
            "message": f"rentStatus must be one of: {', '.join(sorted(valid_statuses))}",
        }), 400

    user_oid = _parse_oid(user_id_raw, "userId")
    if not user_oid:
        return jsonify({"success": False, "message": "Invalid userId"}), 400

    user = current_app.db.user.find_one({"_id": user_oid})
    if not user:
        return jsonify({"success": False, "message": "User not found"}), 404

    # Prevent duplicate tenant entry in the same property
    already = any(str(t["tenantId"]) == user_id_raw for t in prop.get("tenants", []))
    if already:
        return jsonify({"success": False, "message": "User is already a tenant of this property"}), 409

    # Build the tenant entry from everything the client sent for this tenant,
    # then enforce server-computed fields on top.
    _tenant_immutable = {"tenantId", "userId"}
    tenant_entry = {k: v for k, v in data.items() if k not in _tenant_immutable}
    tenant_entry["tenantId"] = user_oid
    tenant_entry["unit"] = unit
    tenant_entry["rentStatus"] = rent_status

    # Push into property's tenants array
    current_app.db.property.update_one(
        {"_id": oid},
        {
            "$push": {"tenants": tenant_entry},
            "$set": {"updatedAt": _now()},
        },
    )

    # Attach the property reference to the user document
    current_app.db.user.update_one(
        {"_id": user_oid},
        {"$set": {
            "propertyId": oid,
            "unit": unit,
            "rentStatus": rent_status,
            "role": "tenant",
        }},
    )

    tenant_entry["tenantId"] = str(tenant_entry["tenantId"])
    return jsonify({
        "success": True,
        "message": "Tenant added successfully",
        "data": tenant_entry,
    }), 201


@properties_bp.route("/<property_id>/tenants", methods=["GET"])
def list_tenants(property_id):
    """
    List all tenants of a property, with full user details merged in.

    Requires: Bearer token.

    GET /api/properties/<property_id>/tenants
    """
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(property_id)
    if not oid:
        return _property_not_found()

    prop = current_app.db.property.find_one({"_id": oid})
    if not prop:
        return _property_not_found()

    result = []
    for t in prop.get("tenants", []):
        user = current_app.db.user.find_one({"_id": t["tenantId"]})
        entry = {
            "tenantId": str(t["tenantId"]),
            "unit": t["unit"],
            "rentStatus": t["rentStatus"],
        }
        if user:
            entry["name"] = f"{user.get('firstName', '')} {user.get('lastName', '')}".strip()
            entry["email"] = user.get("email")
        result.append(entry)

    return jsonify({"success": True, "data": result}), 200


@properties_bp.route("/<property_id>/tenants/<tenant_user_id>", methods=["PUT"])
def update_tenant(property_id, tenant_user_id):
    """
    Update a tenant's unit or rentStatus within a property.

    Requires: Bearer token (landlord).

    JSON body (one or both):
      - unit       (string)
      - rentStatus (string) — "Paid" | "Overdue" | "Pending"

    PUT /api/properties/<property_id>/tenants/<tenant_user_id>
    """
    landlord_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(property_id)
    tenant_oid = _parse_oid(tenant_user_id, "tenant_user_id")
    if not oid or not tenant_oid:
        return _property_not_found()

    prop = current_app.db.property.find_one({"_id": oid, "landlordId": ObjectId(landlord_id)})
    if not prop:
        return _property_not_found()

    data = request.get_json(silent=True) or {}
    unit = (data.get("unit") or "").strip() or None
    rent_status = (data.get("rentStatus") or "").strip() or None

    valid_statuses = {"Paid", "Overdue", "Pending"}
    if rent_status and rent_status not in valid_statuses:
        return jsonify({
            "success": False,
            "message": f"rentStatus must be one of: {', '.join(sorted(valid_statuses))}",
        }), 400

    if not unit and not rent_status:
        return jsonify({"success": False, "message": "Provide unit and/or rentStatus to update"}), 400

    # Build the array of updates from everything in the payload (except
    # identity / immutable fields). Validated fields still override.
    _immutable_tenant = {"tenantId", "userId", "_id"}
    array_updates = {}
    user_updates  = {}
    for k, v in data.items():
        if k in _immutable_tenant:
            continue
        array_updates[f"tenants.$.{k}"] = v
        user_updates[k] = v
    # Validated overrides
    if unit:
        array_updates["tenants.$.unit"] = unit
        user_updates["unit"] = unit
    if rent_status:
        array_updates["tenants.$.rentStatus"] = rent_status
        user_updates["rentStatus"] = rent_status
    array_updates["updatedAt"] = _now()

    if not array_updates:
        return jsonify({"success": False, "message": "Provide at least one field to update"}), 400

    result = current_app.db.property.update_one(
        {"_id": oid, "tenants.tenantId": tenant_oid},
        {"$set": array_updates},
    )
    if result.matched_count == 0:
        return jsonify({"success": False, "message": "Tenant not found in this property"}), 404

    # Keep the user document in sync
    current_app.db.user.update_one(
        {"_id": tenant_oid},
        {"$set": user_updates},
    )

    return jsonify({"success": True, "message": "Tenant updated"}), 200


@properties_bp.route("/<property_id>/tenants/<tenant_user_id>", methods=["DELETE"])
def remove_tenant(property_id, tenant_user_id):
    """
    Remove a tenant from a property and detach the property link from their user document.

    Requires: Bearer token (landlord).

    DELETE /api/properties/<property_id>/tenants/<tenant_user_id>
    """
    landlord_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(property_id)
    tenant_oid = _parse_oid(tenant_user_id, "tenant_user_id")
    if not oid or not tenant_oid:
        return _property_not_found()

    prop = current_app.db.property.find_one({"_id": oid, "landlordId": ObjectId(landlord_id)})
    if not prop:
        return _property_not_found()

    result = current_app.db.property.update_one(
        {"_id": oid},
        {
            "$pull": {"tenants": {"tenantId": tenant_oid}},
            "$set": {"updatedAt": _now()},
        },
    )
    if result.modified_count == 0:
        return jsonify({"success": False, "message": "Tenant not found in this property"}), 404

    # Remove property reference from the user
    current_app.db.user.update_one(
        {"_id": tenant_oid},
        {"$unset": {"propertyId": "", "unit": "", "rentStatus": "", "role": ""}},
    )

    return jsonify({"success": True, "message": "Tenant removed from property"}), 200
