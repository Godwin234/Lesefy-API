"""
Background Check System
────────────────────────

Landlords can request a background check on a tenant before or during tenancy.
Tenants must consent before any check is processed.

Status flow:
  pending_consent  →  consented  →  processing  →  completed
                   ↘  declined  (tenant refuses)
                              ↘  failed       (processing error)

Endpoints
─────────
  POST   /api/background-checks/             Landlord requests a check on a tenant
  GET    /api/background-checks/             List checks (landlord sees theirs; tenant sees checks on them)
  GET    /api/background-checks/<id>         Get a single check
  PATCH  /api/background-checks/<id>/consent Tenant accepts or declines the request
  PATCH  /api/background-checks/<id>/result  Landlord / system records the result
  DELETE /api/background-checks/<id>         Landlord cancels a pending-consent check

MongoDB collection:  background_check
"""

from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, jsonify, request

from .auth import decode_token

background_checks_bp = Blueprint(
    "background_checks", __name__, url_prefix="/api/background-checks"
)

VALID_STATUSES = {"pending_consent", "consented", "processing", "completed", "failed", "declined"}
VALID_CHECK_TYPES = {"credit", "criminal", "eviction", "employment", "full"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _parse_oid(value):
    try:
        return ObjectId(str(value).strip())
    except (InvalidId, TypeError):
        return None


def _iso(dt):
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt


def _serialize(check: dict) -> dict:
    out = {
        "id":             str(check["_id"]),
        "landlordId":     str(check.get("landlordId", "")),
        "tenantId":       str(check.get("tenantId", "")),
        "propertyId":     str(check["propertyId"]) if check.get("propertyId") else "",
        "checkType":      check.get("checkType", "full"),
        "status":         check.get("status", "pending_consent"),
        "notes":          check.get("notes") or "",
        "result":         check.get("result") or None,
        "resultSummary":  check.get("resultSummary") or None,
        "consentedAt":    _iso(check.get("consentedAt")),
        "declinedAt":     _iso(check.get("declinedAt")),
        "completedAt":    _iso(check.get("completedAt")),
        "createdAt":      _iso(check.get("createdAt")),
        "updatedAt":      _iso(check.get("updatedAt")),
    }
    # Attach resolved user profiles if populated
    if "_landlordDoc" in check:
        d = check["_landlordDoc"]
        out["landlord"] = {
            "id":        str(d["_id"]),
            "firstName": d.get("firstName", ""),
            "lastName":  d.get("lastName", ""),
            "email":     d.get("email", ""),
        }
    if "_tenantDoc" in check:
        d = check["_tenantDoc"]
        out["tenant"] = {
            "id":          str(d["_id"]),
            "firstName":   d.get("firstName", ""),
            "lastName":    d.get("lastName", ""),
            "email":       d.get("email", ""),
            "phone":       d.get("phone", ""),
            "dateOfBirth": d.get("dateOfBirth") or None,
        }
    return out


def _notify(notif_type, recipient_oids, check_id_str, actor_doc, actor_name, extra=None):
    """Fire a background-check notification without crashing the main flow."""
    templates = {
        "bgcheck_requested": (
            "Background check requested",
            f"{actor_name} has requested a background check.",
        ),
        "bgcheck_consented": (
            "Tenant consented to background check",
            f"{actor_name} has agreed to the background check.",
        ),
        "bgcheck_declined": (
            "Tenant declined background check",
            f"{actor_name} declined the background check request.",
        ),
        "bgcheck_completed": (
            "Background check completed",
            "The background check results are ready.",
        ),
    }
    title, body = templates.get(notif_type, ("Background check update", ""))
    try:
        from .socket import socketio as _socketio
    except Exception:
        _socketio = None

    db = current_app.db

    for recipient_oid in recipient_oids:
        notif_doc = {
            "userId":    recipient_oid,
            "type":      notif_type,
            "title":     title,
            "body":      body,
            "data": {
                "backgroundCheckId": check_id_str,
                "actorId":    str(actor_doc["_id"]),
                "actorEmail": actor_doc.get("email", ""),
                "actorName":  actor_name,
                **(extra or {}),
            },
            "read":      False,
            "createdAt": _now(),
        }
        result = db.notification.insert_one(notif_doc)
        notif_doc["_id"] = result.inserted_id

        if _socketio:
            try:
                from .notifications import _fmt_notification
                _socketio.emit("notification", _fmt_notification(notif_doc), to=str(recipient_oid))
            except Exception:
                pass

        # FCM push
        try:
            from .notifications import _send_fcm
            token_docs = list(db.push_token.find({"userId": recipient_oid}, {"token": 1}))
            tokens = [t["token"] for t in token_docs if t.get("token")]
            if tokens:
                _send_fcm(tokens, title, body, {"backgroundCheckId": check_id_str, "type": notif_type})
        except Exception:
            pass


# ── Endpoints ─────────────────────────────────────────────────────────────────

@background_checks_bp.route("/", methods=["POST"])
def request_background_check():
    """
    Landlord requests a background check on a tenant.

    JSON body:
      - tenantId    (string, required)  ObjectId of the tenant to check
      - checkType   (string, optional)  "credit" | "criminal" | "eviction" |
                                        "employment" | "full"  (default "full")
      - propertyId  (string, optional)  associated property
      - notes       (string, optional)  reason for the check / instructions
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    db           = current_app.db
    landlord_oid = _parse_oid(user_id_str)
    landlord_doc = db.user.find_one({"_id": landlord_oid})
    if not landlord_doc:
        return jsonify({"success": False, "message": "User not found"}), 404

    if (landlord_doc.get("userType") or "").lower() != "landlord":
        return jsonify({"success": False, "message": "Only landlords can request background checks"}), 403

    data = request.get_json(silent=True) or {}

    tenant_oid = _parse_oid(data.get("tenantId") or "")
    if not tenant_oid:
        return jsonify({"success": False, "message": "tenantId is required"}), 400

    tenant_doc = db.user.find_one({"_id": tenant_oid})
    if not tenant_doc:
        return jsonify({"success": False, "message": "Tenant not found"}), 404
    if (tenant_doc.get("userType") or "").lower() != "tenant":
        return jsonify({"success": False, "message": "The specified user is not a tenant"}), 400

    check_type  = (data.get("checkType") or "full").lower()
    if check_type not in VALID_CHECK_TYPES:
        return jsonify({
            "success": False,
            "message": f"Invalid checkType. Must be one of: {', '.join(sorted(VALID_CHECK_TYPES))}",
        }), 400

    property_oid = _parse_oid(data.get("propertyId") or "") or None
    notes        = (data.get("notes") or "").strip() or None

    now = _now()
    doc = {
        "landlordId":  landlord_oid,
        "tenantId":    tenant_oid,
        "propertyId":  property_oid,
        "checkType":   check_type,
        "status":      "pending_consent",
        "notes":       notes,
        "result":      None,
        "resultSummary": None,
        "consentedAt": None,
        "declinedAt":  None,
        "completedAt": None,
        "createdAt":   now,
        "updatedAt":   now,
    }

    result   = db.background_check.insert_one(doc)
    doc["_id"] = result.inserted_id
    doc["_landlordDoc"] = landlord_doc
    doc["_tenantDoc"]   = tenant_doc

    actor_name = (
        f"{landlord_doc.get('firstName', '')} {landlord_doc.get('lastName', '')}".strip()
        or landlord_doc.get("email", "Landlord")
    )
    _notify("bgcheck_requested", [tenant_oid], str(result.inserted_id), landlord_doc, actor_name)

    return jsonify({"success": True, "data": _serialize(doc)}), 201


@background_checks_bp.route("/", methods=["GET"])
def list_background_checks():
    """
    List background checks visible to the authenticated user.

    - Landlord: sees all checks they initiated.
    - Tenant:   sees all checks requested on them.

    Query params:
      - status      (optional)  filter by status
      - propertyId  (optional)  filter by property
      - page        (default 1)
      - limit       (default 20, max 100)
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    db       = current_app.db
    user_oid = _parse_oid(user_id_str)
    user_doc = db.user.find_one({"_id": user_oid}, {"userType": 1})
    if not user_doc:
        return jsonify({"success": False, "message": "User not found"}), 404

    user_type = (user_doc.get("userType") or "").lower()
    if user_type == "landlord":
        filt = {"landlordId": user_oid}
    elif user_type == "tenant":
        filt = {"tenantId": user_oid}
    else:
        return jsonify({"success": False, "message": "Only landlords and tenants can access background checks"}), 403

    status_filter = (request.args.get("status") or "").lower()
    if status_filter in VALID_STATUSES:
        filt["status"] = status_filter

    prop_oid = _parse_oid(request.args.get("propertyId") or "")
    if prop_oid:
        filt["propertyId"] = prop_oid

    try:
        page  = max(1, int(request.args.get("page", 1)))
        limit = min(100, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        return jsonify({"success": False, "message": "page and limit must be integers"}), 400

    skip   = (page - 1) * limit
    checks = list(db.background_check.find(filt).sort("createdAt", -1).skip(skip).limit(limit))
    total  = db.background_check.count_documents(filt)

    # Batch-resolve landlord and tenant user docs
    landlord_oids = list({c["landlordId"] for c in checks if c.get("landlordId")})
    tenant_oids   = list({c["tenantId"]   for c in checks if c.get("tenantId")})
    all_oids      = list({*landlord_oids, *tenant_oids})
    users_by_id   = {u["_id"]: u for u in db.user.find({"_id": {"$in": all_oids}}, {"password": 0})}

    serialized = []
    for c in checks:
        c["_landlordDoc"] = users_by_id.get(c.get("landlordId"), {})
        c["_tenantDoc"]   = users_by_id.get(c.get("tenantId"), {})
        serialized.append(_serialize(c))

    return jsonify({
        "success": True,
        "data": serialized,
        "pagination": {
            "page": page, "limit": limit, "total": total,
            "pages": (total + limit - 1) // limit,
        },
    }), 200


@background_checks_bp.route("/<check_id>", methods=["GET"])
def get_background_check(check_id):
    """Get a single background check (must belong to the user as landlord or tenant)."""
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid      = _parse_oid(check_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid background check ID"}), 400

    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    check = db.background_check.find_one({
        "_id": oid,
        "$or": [{"landlordId": user_oid}, {"tenantId": user_oid}],
    })
    if not check:
        return jsonify({"success": False, "message": "Background check not found"}), 404

    check["_landlordDoc"] = db.user.find_one({"_id": check["landlordId"]}, {"password": 0}) or {}
    check["_tenantDoc"]   = db.user.find_one({"_id": check["tenantId"]},   {"password": 0}) or {}

    return jsonify({"success": True, "data": _serialize(check)}), 200


@background_checks_bp.route("/<check_id>/consent", methods=["PATCH"])
def respond_to_background_check(check_id):
    """
    Tenant accepts or declines a background check request.

    JSON body:
      - consent  (boolean, required)  true = accept, false = decline
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid      = _parse_oid(check_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid background check ID"}), 400

    db       = current_app.db
    user_oid = _parse_oid(user_id_str)

    check = db.background_check.find_one({"_id": oid, "tenantId": user_oid})
    if not check:
        return jsonify({"success": False, "message": "Background check not found"}), 404

    if check.get("status") != "pending_consent":
        return jsonify({
            "success": False,
            "message": f"Cannot respond — check is already '{check.get('status')}'",
        }), 400

    data    = request.get_json(silent=True) or {}
    consent = data.get("consent")
    if consent is None:
        return jsonify({"success": False, "message": "'consent' (boolean) is required"}), 400

    now    = _now()
    tenant_doc = db.user.find_one({"_id": user_oid})
    actor_name = (
        f"{tenant_doc.get('firstName', '')} {tenant_doc.get('lastName', '')}".strip()
        or tenant_doc.get("email", "Tenant")
    ) if tenant_doc else "Tenant"

    if consent:
        new_status = "consented"
        updates    = {"status": new_status, "consentedAt": now, "updatedAt": now}
        notif_type = "bgcheck_consented"
    else:
        new_status = "declined"
        updates    = {"status": new_status, "declinedAt": now, "updatedAt": now}
        notif_type = "bgcheck_declined"

    db.background_check.update_one({"_id": oid}, {"$set": updates})
    _notify(notif_type, [check["landlordId"]], str(oid), tenant_doc or {}, actor_name)

    updated = db.background_check.find_one({"_id": oid})
    updated["_landlordDoc"] = db.user.find_one({"_id": updated["landlordId"]}, {"password": 0}) or {}
    updated["_tenantDoc"]   = db.user.find_one({"_id": updated["tenantId"]},   {"password": 0}) or {}

    return jsonify({"success": True, "data": _serialize(updated)}), 200


@background_checks_bp.route("/<check_id>/result", methods=["PATCH"])
def record_result(check_id):
    """
    Landlord records the outcome of a background check.

    The check must be in 'consented' or 'processing' status.

    JSON body:
      - status         ("completed" | "failed", required)
      - result         (object, optional)   raw result payload from a 3rd-party service
      - resultSummary  (string, optional)   human-readable summary for the UI
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid          = _parse_oid(check_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid background check ID"}), 400

    db           = current_app.db
    landlord_oid = _parse_oid(user_id_str)

    check = db.background_check.find_one({"_id": oid, "landlordId": landlord_oid})
    if not check:
        return jsonify({"success": False, "message": "Background check not found"}), 404

    if check.get("status") not in ("consented", "processing"):
        return jsonify({
            "success": False,
            "message": f"Cannot record result — check is '{check.get('status')}'",
        }), 400

    data   = request.get_json(silent=True) or {}
    status = (data.get("status") or "").lower()
    if status not in ("completed", "failed"):
        return jsonify({"success": False, "message": "status must be 'completed' or 'failed'"}), 400

    now     = _now()
    updates = {
        "status":        status,
        "result":        data.get("result") or None,
        "resultSummary": (data.get("resultSummary") or "").strip() or None,
        "completedAt":   now,
        "updatedAt":     now,
    }

    db.background_check.update_one({"_id": oid}, {"$set": updates})

    if status == "completed":
        landlord_doc = db.user.find_one({"_id": landlord_oid})
        actor_name   = (
            f"{landlord_doc.get('firstName', '')} {landlord_doc.get('lastName', '')}".strip()
            or landlord_doc.get("email", "Landlord")
        ) if landlord_doc else "Landlord"
        _notify("bgcheck_completed", [check["tenantId"]], str(oid), landlord_doc or {}, actor_name)

    updated = db.background_check.find_one({"_id": oid})
    updated["_landlordDoc"] = db.user.find_one({"_id": updated["landlordId"]}, {"password": 0}) or {}
    updated["_tenantDoc"]   = db.user.find_one({"_id": updated["tenantId"]},   {"password": 0}) or {}

    return jsonify({"success": True, "data": _serialize(updated)}), 200


@background_checks_bp.route("/<check_id>", methods=["DELETE"])
def cancel_background_check(check_id):
    """
    Landlord cancels a background check that is still pending tenant consent.
    Checks in any other status cannot be deleted.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid          = _parse_oid(check_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid background check ID"}), 400

    db           = current_app.db
    landlord_oid = _parse_oid(user_id_str)

    check = db.background_check.find_one({"_id": oid, "landlordId": landlord_oid})
    if not check:
        return jsonify({"success": False, "message": "Background check not found"}), 404

    if check.get("status") != "pending_consent":
        return jsonify({
            "success": False,
            "message": "Only pending-consent checks can be cancelled",
        }), 400

    db.background_check.delete_one({"_id": oid})
    return jsonify({"success": True, "message": "Background check cancelled"}), 200


# ── Index helper ──────────────────────────────────────────────────────────────

def ensure_background_check_indexes(db):
    db.background_check.create_index("landlordId")
    db.background_check.create_index("tenantId")
    db.background_check.create_index("status")
    db.background_check.create_index("propertyId", sparse=True)
    db.background_check.create_index("createdAt")
