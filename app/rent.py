"""
Rent Payment Management
───────────────────────

Rent records track what a tenant owes and what has been paid for a given
period (month).  Stripe webhook events update these records automatically;
landlords can also create and manage records manually.

Status lifecycle
  pending  →  partial  (payment received but less than rentDue)
  pending  →  paid     (full payment received)
  partial  →  paid     (remainder received)

MongoDB collection:  rent_payment

Endpoints
─────────
  POST   /api/rent/                        Landlord creates a rent record (charge)
  GET    /api/rent/                        List rent records (landlord sees all theirs;
                                           tenant sees only their own)
  GET    /api/rent/<rent_id>               Single record
  PATCH  /api/rent/<rent_id>              Landlord edits any field
  DELETE /api/rent/<rent_id>              Landlord deletes a record

Internal helpers (used by stripe_finance webhook)
─────────────────
  upsert_rent_from_charge(tenant_oid, property_oid, landlord_oid,
                          amount_paid, currency, period, charge_meta, db, app)
"""

from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, jsonify, request

from .auth import decode_token

rent_bp = Blueprint("rent", __name__, url_prefix="/api/rent")

VALID_STATUSES = {"pending", "partial", "paid", "overdue"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _parse_oid(value):
    try:
        return ObjectId(str(value).strip())
    except (InvalidId, TypeError):
        return None


def _iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else dt


def _serialize(doc):
    """Serialize all fields in the document, converting ObjectId and datetime values."""
    if doc is None:
        return None
    out = {}
    for key, val in doc.items():
        out_key = "id" if key == "_id" else key
        if isinstance(val, ObjectId):
            out[out_key] = str(val)
        elif isinstance(val, datetime):
            out[out_key] = val.isoformat()
        else:
            out[out_key] = val
    return out


def _get_user(db, user_id_str):
    oid = _parse_oid(user_id_str)
    return db.user.find_one({"_id": oid}) if oid else None


def _is_landlord(user_doc):
    return (user_doc.get("userType") or user_doc.get("role") or "").lower() == "landlord"


def _notify_tenant_rent(tenant_oid, event_type, rent_doc, property_doc, db):
    """
    Send an in-app notification, SocketIO event, and FCM push to the tenant
    when a rent record is created or updated by the landlord.

    event_type: "created" | "updated"
    """
    from .notifications import _send_fcm, _fmt_notification

    try:
        from .socket import socketio as _socketio
    except Exception:
        _socketio = None

    address = (property_doc or {}).get("address", "your property")
    period  = rent_doc.get("period", "")
    due     = rent_doc.get("rentDue") or rent_doc.get("amount", 0)
    currency = rent_doc.get("currency", "USD")
    due_date = rent_doc.get("dueDate")
    due_date_str = due_date.strftime("%b %d, %Y") if isinstance(due_date, datetime) else (str(due_date)[:10] if due_date else "")

    if event_type == "created":
        title = f"New rent charge – {address}"
        body  = (
            f"Your landlord has posted a rent charge of {currency} {due:,.2f} "
            f"for {period}" + (f", due {due_date_str}" if due_date_str else "") + "."
        )
        notif_type = "rent_charge_posted"
    else:
        title = f"Rent record updated – {address}"
        body  = (
            f"Your rent record for {period} has been updated. "
            f"Amount due: {currency} {due:,.2f}"
            + (f" | Due: {due_date_str}" if due_date_str else "") + "."
        )
        notif_type = "rent_charge_updated"

    rent_id     = str(rent_doc["_id"])
    property_id = str(rent_doc.get("propertyId", ""))

    notif_doc = {
        "userId":    tenant_oid,
        "type":      notif_type,
        "title":     title,
        "body":      body,
        "data": {
            "rentId":     rent_id,
            "propertyId": property_id,
            "period":     period,
            "amount":     str(due),
            "currency":   currency,
        },
        "read":      False,
        "createdAt": _now(),
    }
    result = db.notification.insert_one(notif_doc)
    notif_doc["_id"] = result.inserted_id

    if _socketio:
        try:
            _socketio.emit("notification", _fmt_notification(notif_doc), to=str(tenant_oid))
        except Exception:
            pass

    token_docs = list(db.push_token.find({"userId": tenant_oid}, {"token": 1}))
    tokens = [t["token"] for t in token_docs if t.get("token")]
    if tokens:
        badge = db.notification.count_documents({"userId": tenant_oid, "read": False})
        _send_fcm(
            tokens, title, body,
            {"rentId": rent_id, "propertyId": property_id, "type": notif_type},
            badge=badge,
        )


# ── MongoDB index setup ───────────────────────────────────────────────────────

def ensure_rent_indexes(db):
    db.rent_payment.create_index("tenantId")
    db.rent_payment.create_index("landlordId")
    db.rent_payment.create_index("propertyId")
    db.rent_payment.create_index([("tenantId", 1), ("period", 1)])
    db.rent_payment.create_index("status")

    # The stripeChargeId index must be sparse+unique so that multiple documents
    # with stripeChargeId=null (manual rent records) are allowed.
    # Drop the index if it already exists without the sparse flag.
    existing = db.rent_payment.index_information()
    idx = existing.get("stripeChargeId_1")
    if idx and not idx.get("sparse"):
        db.rent_payment.drop_index("stripeChargeId_1")
    db.rent_payment.create_index(
        "stripeChargeId", sparse=True, unique=True, name="stripeChargeId_1"
    )


# ── Internal helper called by stripe_finance webhook ─────────────────────────

def upsert_rent_from_charge(
    tenant_oid, property_oid, landlord_oid,
    amount_paid, currency, period, charge_meta, db, app
):
    """
    Find an existing pending/partial rent record for the tenant in this period
    and update it; create a fresh record if none exists.

    Returns the final (inserted or updated) rent_payment document.
    """
    from .transactions import create_transaction_for_rent

    # Search for ANY open record for this tenant+property regardless of period.
    # A landlord typically creates the record for next month (e.g. 2026-04)
    # but the payment arrives in the current month (e.g. 2026-03), so
    # restricting by the charge-date period causes a false miss and a duplicate.
    # If multiple open records exist, pick the one with the earliest period.
    existing = db.rent_payment.find_one(
        {
            "tenantId":   tenant_oid,
            "propertyId": property_oid,
            "status":     {"$in": ["pending", "partial"]},
        },
        sort=[("period", 1)],
    )

    now = _now()

    if existing:
        rent_due   = existing.get("rentDue") or existing.get("amount") or 0
        prev_paid  = existing.get("partialPaid", 0)
        total_paid = round(prev_paid + amount_paid, 2)

        if total_paid >= rent_due:
            new_status    = "paid"
            new_partial   = 0
            new_amount    = rent_due
        else:
            new_status    = "partial"
            new_partial   = total_paid
            new_amount    = existing.get("amount", total_paid)

        updates = {
            "status":                new_status,
            "amount":                new_amount,
            "partialPaid":           new_partial,
            "paidAt":                now,
            "paymentMethod":         charge_meta.get("pm_type"),
            "paymentMethodDetails":  charge_meta.get("pm_sub"),
            "stripeChargeId":        charge_meta.get("charge_id"),
            "stripePaymentIntentId": charge_meta.get("payment_intent"),
            "stripeCustomerId":      charge_meta.get("customer"),
            "updatedAt":             now,
        }
        db.rent_payment.update_one({"_id": existing["_id"]}, {"$set": updates})
        rent_doc = db.rent_payment.find_one({"_id": existing["_id"]})

        # Update the linked transaction if one exists, else create
        txn = db.transaction.find_one({"rentId": existing["_id"], "userId": landlord_oid})
        if txn:
            db.transaction.update_one(
                {"_id": txn["_id"]},
                {"$set": {"updatedAt": now}},
            )
        else:
            create_transaction_for_rent(rent_doc, landlord_oid, db)

    else:
        rent_doc_new = {
            "propertyId":            property_oid,
            "tenantId":              tenant_oid,
            "landlordId":            landlord_oid,
            "rentDue":               amount_paid,
            "amount":                amount_paid,
            "partialPaid":           0,
            "currency":              currency,
            "period":                period,
            "status":                "paid",
            "paidAt":                now,
            "description":           "Rent Payment",
            "transactionType":       "credit",
            "paymentMethod":         charge_meta.get("pm_type"),
            "paymentMethodDetails":  charge_meta.get("pm_sub"),
            "stripeChargeId":        charge_meta.get("charge_id"),
            "stripePaymentIntentId": charge_meta.get("payment_intent"),
            "stripeCustomerId":      charge_meta.get("customer"),
            "createdAt":             now,
            "updatedAt":             now,
        }
        result = db.rent_payment.insert_one(rent_doc_new)
        rent_doc_new["_id"] = result.inserted_id
        rent_doc = rent_doc_new
        create_transaction_for_rent(rent_doc, landlord_oid, db)

    # Sync rentStatus on property tenants array and user document
    status_display = rent_doc.get("status", "pending").capitalize()
    db.property.update_one(
        {"_id": property_oid, "tenants.tenantId": tenant_oid},
        {"$set": {"tenants.$.rentStatus": status_display, "updatedAt": now}},
    )
    db.user.update_one(
        {"_id": tenant_oid},
        {"$set": {"rentStatus": status_display}},
    )

    from .activities import _log_activity
    _log_activity(db, landlord_oid, "RENT_PAYMENT_RECEIVED",
                  {"rentId": str(rent_doc["_id"]), "period": rent_doc.get("period"),
                   "amount": amount_paid, "currency": currency,
                   "status": rent_doc.get("status"),
                   "tenantId": str(tenant_oid), "propertyId": str(property_oid)})

    return rent_doc


# ═════════════════════════════════════════════════════════════════════════════
# CRUD Endpoints
# ═════════════════════════════════════════════════════════════════════════════

@rent_bp.route("/", methods=["POST"])
def create_rent():
    """
    Landlord manually creates a rent record (charge) for a tenant.

    Requires: Bearer token (landlord).

    JSON body:
      tenantId    str    required
      propertyId  str    required
      rentDue     float  required   Total amount owed
      period      str    required   "YYYY-MM"
      currency    str    optional   default "USD"
      dueDate     str    optional   ISO-8601 date
      description str    optional
    """
    user_id, err = decode_token(request)
    if err:
        return err

    db = current_app.db
    caller = _get_user(db, user_id)
    if not caller or not _is_landlord(caller):
        return jsonify({"success": False, "message": "Only landlords can create rent records"}), 403

    data = request.get_json(silent=True) or {}

    tenant_id_raw   = (data.get("tenantId") or "").strip()
    property_id_raw = (data.get("propertyId") or "").strip()
    # Accept rentDue, or fall back to amount or mainRent so callers don't have
    # to rename their fields.
    rent_due_raw = (
        data.get("rentDue")
        if data.get("rentDue") is not None
        else (
            data.get("amount")
            if data.get("amount") is not None
            else data.get("mainRent")
        )
    )
    period = (data.get("period") or "").strip()

    if not tenant_id_raw:
        return jsonify({"success": False, "message": "tenantId is required"}), 400
    if not property_id_raw:
        return jsonify({"success": False, "message": "propertyId is required"}), 400
    if rent_due_raw is None:
        return jsonify({"success": False, "message": "rentDue (or amount) is required"}), 400
    try:
        rent_due = float(rent_due_raw)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "rentDue must be a number"}), 400
    if not period:
        return jsonify({"success": False, "message": "period is required (YYYY-MM)"}), 400

    tenant_oid   = _parse_oid(tenant_id_raw)
    property_oid = _parse_oid(property_id_raw)
    landlord_oid = _parse_oid(user_id)

    if not tenant_oid:
        return jsonify({"success": False, "message": "Invalid tenantId"}), 400
    if not property_oid:
        return jsonify({"success": False, "message": "Invalid propertyId"}), 400

    # Verify the property belongs to this landlord
    prop = db.property.find_one({"_id": property_oid, "landlordId": landlord_oid})
    if not prop:
        return jsonify({"success": False, "message": "Property not found or not yours"}), 404

    # Prevent duplicate record for the same tenant + period
    if db.rent_payment.find_one({"tenantId": tenant_oid, "period": period}):
        return jsonify({
            "success": False,
            "message": f"A rent record for tenant {tenant_id_raw} in period {period} already exists",
        }), 409

    due_date = None
    raw_due = (data.get("dueDate") or "").strip()
    if raw_due:
        try:
            due_date = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"success": False, "message": "Invalid dueDate format, use ISO-8601"}), 400

    # Start with everything the client sent, then set/override server-controlled fields.
    # _id, createdAt, updatedAt, landlordId are always server-owned.
    _server_owned = {"_id", "createdAt", "updatedAt", "landlordId"}
    rent_doc = {k: v for k, v in data.items() if k not in _server_owned}

    # Required / computed overrides
    rent_doc["propertyId"]  = property_oid
    rent_doc["tenantId"]    = tenant_oid
    rent_doc["landlordId"]  = landlord_oid
    rent_doc["rentDue"]     = rent_due
    rent_doc["currency"]    = (data.get("currency") or "USD").upper().strip()
    rent_doc["period"]      = period
    rent_doc["dueDate"]     = due_date

    # Apply defaults only when the client did not supply a value
    rent_doc.setdefault("amount",          0)
    rent_doc.setdefault("partialPaid",     0)
    rent_doc.setdefault("status",          "pending")
    rent_doc.setdefault("description",     "Rent Payment")
    rent_doc.setdefault("transactionType", "credit")

    # Guard: ensure status is valid even if client sent something unexpected
    if rent_doc.get("status") not in VALID_STATUSES:
        rent_doc["status"] = "pending"

    # Remove Stripe / payment fields if they are None / falsy so that the
    # sparse unique index on stripeChargeId is not violated by null values.
    # MongoDB sparse indexes still index documents where the field is
    # explicitly null — only *absent* fields are excluded from the index.
    for _stripe_field in (
        "stripeChargeId", "stripePaymentIntentId", "stripeCustomerId",
        "paymentMethod", "paymentMethodDetails", "paidAt",
    ):
        if not rent_doc.get(_stripe_field):
            rent_doc.pop(_stripe_field, None)

    rent_doc["createdAt"] = _now()
    rent_doc["updatedAt"] = _now()
    result = db.rent_payment.insert_one(rent_doc)
    rent_doc["_id"] = result.inserted_id

    from .activities import _log_activity
    _log_activity(db, landlord_oid, "RENT_RECORD_CREATED",
                  {"rentId": str(rent_doc["_id"]), "period": period,
                   "rentDue": rent_due, "tenantId": str(tenant_oid),
                   "propertyId": str(property_oid)})

    # Notify the tenant that a rent charge has been posted
    try:
        prop = db.property.find_one({"_id": property_oid})
        _notify_tenant_rent(tenant_oid, "created", rent_doc, prop, db)
    except Exception:
        pass

    return jsonify({
        "success": True,
        "message": "Rent record created",
        "data": _serialize(rent_doc),
    }), 201


@rent_bp.route("/", methods=["GET"])
def list_rents():
    """
    List rent records.

    Landlords see all records for properties they own; optionally filter
    by tenantId or propertyId via query params.

    Tenants see only their own records.

    Query params (all optional):
      tenantId    str   (landlord only) filter by tenant
      propertyId  str   filter by property
      status      str   filter by status: pending|partial|paid|overdue
      period      str   filter by period, e.g. "2026-03"
      page        int   default 1
      limit       int   default 20, max 100
    """
    user_id, err = decode_token(request)
    if err:
        return err

    db = current_app.db
    caller = _get_user(db, user_id)
    if not caller:
        return jsonify({"success": False, "message": "User not found"}), 404

    caller_oid = caller["_id"]
    is_ll = _is_landlord(caller)

    filt = {}

    if is_ll:
        # Find all properties owned by this landlord
        prop_oids = [p["_id"] for p in db.property.find(
            {"landlordId": caller_oid}, {"_id": 1}
        )]
        filt["propertyId"] = {"$in": prop_oids}

        # Landlord may narrow by tenantId
        raw_tid = (request.args.get("tenantId") or "").strip()
        if raw_tid:
            tid = _parse_oid(raw_tid)
            if tid:
                filt["tenantId"] = tid
    else:
        # Tenant can only see their own records
        filt["tenantId"] = caller_oid

    # Shared optional filters
    raw_pid = (request.args.get("propertyId") or "").strip()
    if raw_pid:
        pid = _parse_oid(raw_pid)
        if pid:
            filt["propertyId"] = pid

    raw_status = (request.args.get("status") or "").strip().lower()
    if raw_status and raw_status in VALID_STATUSES:
        filt["status"] = raw_status

    raw_period = (request.args.get("period") or "").strip()
    if raw_period:
        filt["period"] = raw_period

    try:
        page  = max(1, int(request.args.get("page", 1)))
        limit = min(100, max(1, int(request.args.get("limit", 20))))
    except (ValueError, TypeError):
        page, limit = 1, 20

    skip = (page - 1) * limit
    total = db.rent_payment.count_documents(filt)
    docs  = list(db.rent_payment.find(filt).sort("createdAt", -1).skip(skip).limit(limit))

    return jsonify({
        "success": True,
        "data": [_serialize(d) for d in docs],
        "total": total,
        "page": page,
        "limit": limit,
    }), 200


@rent_bp.route("/<rent_id>", methods=["GET"])
def get_rent(rent_id):
    """Get a single rent record by ID. Accessible to both landlord and the linked tenant."""
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(rent_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid rent ID"}), 400

    db = current_app.db
    doc = db.rent_payment.find_one({"_id": oid})
    if not doc:
        return jsonify({"success": False, "message": "Rent record not found"}), 404

    caller = _get_user(db, user_id)
    caller_oid = caller["_id"]

    if not (_is_landlord(caller) or doc.get("tenantId") == caller_oid):
        return jsonify({"success": False, "message": "Access denied"}), 403

    return jsonify({"success": True, "data": _serialize(doc)}), 200


@rent_bp.route("/<rent_id>", methods=["PATCH"])
def update_rent(rent_id):
    """
    Landlord edits any field on a rent record.

    Requires: Bearer token (landlord who owns the linked property).

    JSON body — any subset of:
      rentDue, amount, partialPaid, currency, period, status,
      dueDate, paidAt, description, paymentMethod
    """
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(rent_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid rent ID"}), 400

    db = current_app.db
    caller = _get_user(db, user_id)
    if not caller or not _is_landlord(caller):
        return jsonify({"success": False, "message": "Only landlords can edit rent records"}), 403

    doc = db.rent_payment.find_one({"_id": oid})
    if not doc:
        return jsonify({"success": False, "message": "Rent record not found"}), 404

    # Landlord must own the property
    if doc.get("landlordId") != caller["_id"]:
        return jsonify({"success": False, "message": "Access denied"}), 403

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "message": "No fields provided"}), 400

    # Accept all incoming fields except immutable identity / audit fields.
    IMMUTABLE = {"_id", "createdAt", "tenantId", "landlordId", "propertyId"}
    NUMERIC   = {"rentDue", "amount", "partialPaid"}
    DATETIME  = {"dueDate", "paidAt"}

    updates = {}

    for field, val in data.items():
        if field in IMMUTABLE:
            continue
        if field == "status":
            if val not in VALID_STATUSES:
                return jsonify({
                    "success": False,
                    "message": f"status must be one of: {', '.join(sorted(VALID_STATUSES))}",
                }), 400
            updates[field] = val
        elif field in NUMERIC:
            try:
                updates[field] = float(val)
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": f"{field} must be a number"}), 400
        elif field in DATETIME:
            if not val:
                updates[field] = None
            else:
                try:
                    updates[field] = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                except ValueError:
                    return jsonify({
                        "success": False,
                        "message": f"Invalid {field} format, use ISO-8601",
                    }), 400
        else:
            updates[field] = val

    if not updates:
        return jsonify({"success": False, "message": "No fields provided to update"}), 400

    updates["updatedAt"] = _now()
    db.rent_payment.update_one({"_id": oid}, {"$set": updates})

    # Sync rentStatus on property and user if status changed
    if "status" in updates:
        status_display = updates["status"].capitalize()
        tenant_oid = doc.get("tenantId")
        property_oid = doc.get("propertyId")
        if tenant_oid and property_oid:
            db.property.update_one(
                {"_id": property_oid, "tenants.tenantId": tenant_oid},
                {"$set": {"tenants.$.rentStatus": status_display, "updatedAt": _now()}},
            )
            db.user.update_one(
                {"_id": tenant_oid},
                {"$set": {"rentStatus": status_display}},
            )

    updated = db.rent_payment.find_one({"_id": oid})

    from .activities import _log_activity
    _log_activity(db, caller["_id"], "RENT_RECORD_UPDATED",
                  {"rentId": str(oid), "changes": list(updates.keys())})

    # Notify the tenant that their rent record was updated
    try:
        tenant_oid_notif = doc.get("tenantId")
        prop_notif = db.property.find_one({"_id": doc.get("propertyId")})
        if tenant_oid_notif:
            _notify_tenant_rent(tenant_oid_notif, "updated", updated, prop_notif, db)
    except Exception:
        pass

    return jsonify({
        "success": True,
        "message": "Rent record updated",
        "data": _serialize(updated),
    }), 200


@rent_bp.route("/<rent_id>", methods=["DELETE"])
def delete_rent(rent_id):
    """
    Delete a rent record.

    Requires: Bearer token (landlord who owns the linked property).
    Also removes any linked transactions.
    """
    user_id, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(rent_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid rent ID"}), 400

    db = current_app.db
    caller = _get_user(db, user_id)
    if not caller or not _is_landlord(caller):
        return jsonify({"success": False, "message": "Only landlords can delete rent records"}), 403

    doc = db.rent_payment.find_one({"_id": oid})
    if not doc:
        return jsonify({"success": False, "message": "Rent record not found"}), 404

    if doc.get("landlordId") != caller["_id"]:
        return jsonify({"success": False, "message": "Access denied"}), 403

    # Remove linked transactions
    db.transaction.delete_many({"rentId": oid})

    db.rent_payment.delete_one({"_id": oid})

    return jsonify({"success": True, "message": "Rent record deleted"}), 200
