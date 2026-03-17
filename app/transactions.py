"""
Transaction System
──────────────────

A transaction is a lightweight record that either:
  (a) links to an existing resource (receipt, rent) via its ID, or
  (b) holds its own data for manually entered transactions.

Linked transactions (type "receipt" | "rent") are read-only from this
endpoint — their display data is always pulled live from the source document,
so any update to the source is automatically reflected here.

Manual transactions (type "manual") can be fully created, edited and deleted
from this endpoint.

MongoDB collection:  transaction

Endpoints
─────────
  POST   /api/transactions/           Create a manual transaction
  GET    /api/transactions/           List the current user's transactions (with live data)
  GET    /api/transactions/<id>       Get one transaction (with live data)
  PATCH  /api/transactions/<id>       Update a MANUAL transaction only
  DELETE /api/transactions/<id>       Delete any transaction (never deletes the source)

Internal helpers (used by other modules to auto-create linked transactions)
─────────────────
  create_transaction_for_receipt(receipt_doc, db, app_context=None)
  create_transaction_for_rent(rent_doc, user_oid, db)
"""

import hashlib
import os
import re
import threading
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, jsonify, request, send_file

from .auth import decode_token

transactions_bp = Blueprint("transactions", __name__, url_prefix="/api/transactions")

VALID_TRANSACTION_TYPES = {"debit", "credit"}
VALID_TYPES             = {"manual", "receipt", "rent"}
ALLOWED_IMG_EXT         = {"jpg", "jpeg", "png", "webp", "heic"}
MAX_IMG_SIZE            = 15 * 1024 * 1024   # 15 MB
MAX_IMAGES_PER_TXN      = 10


# ── Small helpers ─────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _parse_oid(value):
    try:
        return ObjectId(str(value).strip())
    except (InvalidId, TypeError):
        return None


def _txn_images_dir():
    path = os.path.abspath(
        os.path.join(current_app.root_path, "..", "uploads", "TransactionImages")
    )
    os.makedirs(path, exist_ok=True)
    return path


def _safe_realpath(path, base_dir):
    return os.path.realpath(path).startswith(os.path.realpath(base_dir))


def _sanitize(value):
    return re.sub(r"[^\w\-]", "_", str(value))


def _iso(dt):
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt


# ── Serialisers ───────────────────────────────────────────────────────────────

def _serialize_transaction(txn: dict, linked_data: dict | None = None) -> dict:
    """
    Turn a raw `transaction` MongoDB doc into a clean response dict.

    `linked_data` is the fully-serialised source document (receipt / rent)
    fetched live — it is merged under key `linkedData` in the response.
    """
    prop_oid = txn.get("propertyId")
    out = {
        "id":          str(txn["_id"]),
        "userId":      str(txn.get("userId", "")),
        "type":        txn.get("type", "manual"),
        "propertyId":  str(prop_oid) if prop_oid else "",
        "createdAt":   _iso(txn.get("createdAt")),
        "updatedAt":   _iso(txn.get("updatedAt")),
    }

    if txn.get("type") == "receipt":
        out["receiptId"] = str(txn["receiptId"]) if txn.get("receiptId") else None
        out["linkedData"] = linked_data  # None if receipt was deleted

    elif txn.get("type") == "rent":
        out["rentId"] = str(txn["rentId"]) if txn.get("rentId") else None
        out["linkedData"] = linked_data

    else:  # manual
        out.update({
            "title":           txn.get("title"),
            "amount":          txn.get("amount"),
            "currency":        txn.get("currency", "USD"),
            "transactionType": txn.get("transactionType", "debit"),
            "description":     txn.get("description"),
            "transactionDate": _iso(txn.get("transactionDate")),
            "notes":           txn.get("notes"),
            "imageUrls":       txn.get("imageUrls") or [],
        })

    return out


def _serialize_receipt(doc: dict) -> dict:
    """Condense a receipt doc to the fields useful as a transaction's linkedData."""
    if doc is None:
        return None
    return {
        "storeName":       doc.get("storeName"),
        "storeAddress":    doc.get("storeAddress"),
        "subtotalAmount":  doc.get("subtotalAmount"),
        "taxAmount":       doc.get("taxAmount"),
        "totalAmount":     doc.get("totalAmount"),
        "currency":        doc.get("currency", "USD"),
        "transactionType": doc.get("transactionType", "debit"),
        "description":     doc.get("description"),
        "receiptDate":     _iso(doc.get("receiptDate")),
        "imageUrl":        doc.get("imageUrl"),
    }


def _serialize_rent(doc: dict) -> dict:
    """Condense a rent-payment doc to the fields useful as a transaction's linkedData."""
    if doc is None:
        return None
    return {
        "propertyId":      str(doc.get("propertyId", "")),
        "tenantId":        str(doc.get("tenantId", "")),
        "amount":          doc.get("amount"),
        "currency":        doc.get("currency", "USD"),
        "period":          doc.get("period"),       # e.g. "2026-03"
        "status":          doc.get("status"),
        "paidAt":          _iso(doc.get("paidAt")),
        "description":     doc.get("description", "Rent Payment"),
        "transactionType": doc.get("transactionType", "credit"),
    }


# ── Live data population ──────────────────────────────────────────────────────

def _populate(txn: dict, db) -> dict:
    """
    Fetch the live source document for a linked transaction and return
    the serialised transaction with populated `linkedData`.
    propertyId is backfilled from the linked doc when not stored on the txn itself.
    """
    txn_type = txn.get("type", "manual")
    txn = dict(txn)  # shallow copy so we can safely mutate

    if txn_type == "receipt":
        receipt_oid = txn.get("receiptId")
        raw = db.receipt.find_one({"_id": receipt_oid}, {"imagePath": 0, "rawText": 0}) if receipt_oid else None
        if raw and not txn.get("propertyId") and raw.get("propertyId"):
            txn["propertyId"] = raw["propertyId"]
        linked = _serialize_receipt(raw)
        return _serialize_transaction(txn, linked)

    if txn_type == "rent":
        rent_oid = txn.get("rentId")
        raw = db.rent_payment.find_one({"_id": rent_oid}) if rent_oid else None
        if raw and not txn.get("propertyId") and raw.get("propertyId"):
            txn["propertyId"] = raw["propertyId"]
        linked = _serialize_rent(raw) if raw else None
        return _serialize_transaction(txn, linked)

    # manual — no linked data needed
    return _serialize_transaction(txn)


# ── MongoDB indexes ───────────────────────────────────────────────────────────

def ensure_transaction_indexes(db):
    db.transaction.create_index("userId")
    db.transaction.create_index("type")
    db.transaction.create_index("receiptId", sparse=True)
    db.transaction.create_index("rentId", sparse=True)
    db.transaction.create_index("propertyId", sparse=True)
    db.transaction.create_index("createdAt")


# ── Internal auto-creation helpers ───────────────────────────────────────────

def create_transaction_for_receipt(receipt_doc: dict, db, app=None):
    """
    Create a linked 'receipt' transaction immediately after a receipt is saved.
    Safe to call from within an active request context.
    """
    receipt_id = receipt_doc.get("_id")
    user_id    = receipt_doc.get("userId")

    if not receipt_id or not user_id:
        return

    existing = db.transaction.find_one({"receiptId": receipt_id})
    if existing:
        return

    property_id = receipt_doc.get("propertyId") or None
    db.transaction.insert_one({
        "userId":     user_id,
        "type":       "receipt",
        "receiptId":  receipt_id,
        "rentId":     None,
        "propertyId": property_id,
        "createdAt":  _now(),
        "updatedAt":  _now(),
    })


def create_transaction_for_rent(rent_doc: dict, user_oid: ObjectId, db):
    """
    Create a linked 'rent' transaction after a rent payment is recorded.

    `rent_doc` must already be inserted and contain `_id`.
    """
    existing = db.transaction.find_one({"rentId": rent_doc["_id"]})
    if existing:
        return
    db.transaction.insert_one({
        "userId":     user_oid,
        "type":       "rent",
        "receiptId":  None,
        "rentId":     rent_doc["_id"],
        "propertyId": rent_doc.get("propertyId") or None,
        "createdAt":  _now(),
        "updatedAt":  _now(),
    })


# ── Endpoints ─────────────────────────────────────────────────────────────────

@transactions_bp.route("/", methods=["POST"])
def create_transaction():
    """
    Create a manual transaction.

    Accepts EITHER:
      • application/json        { title, amount, transactionType, ... }
      • multipart/form-data     same fields as form values + optional image files
                                under key "images" (repeat the key for multiple files)

    Fields (all required unless marked optional):
      - title           (string, required)
      - amount          (number, required)
      - transactionType ("debit" | "credit", required)
      - currency        (string, optional, default "USD")
      - description     (string, optional)
      - transactionDate (ISO-8601 string, optional — defaults to now)
      - notes           (string, optional)
      - propertyId      (ObjectId string, optional)

    Returns 201 with the created transaction (including imageUrls if images were attached).
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    is_multipart = request.content_type and "multipart/form-data" in request.content_type
    if is_multipart:
        get = lambda k, d="": (request.form.get(k) or d)
    else:
        _body = request.get_json(silent=True) or {}
        get = lambda k, d="": (_body.get(k) or d)

    title    = get("title").strip()
    amount   = get("amount", None)
    txn_type = get("transactionType").lower()

    if not title:
        return jsonify({"success": False, "message": "title is required"}), 400
    if amount is None or str(amount).strip() == "":
        return jsonify({"success": False, "message": "amount is required"}), 400
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "amount must be a number"}), 400
    if txn_type not in VALID_TRANSACTION_TYPES:
        return jsonify({"success": False, "message": "transactionType must be 'debit' or 'credit'"}), 400

    txn_date = _now()
    raw_date = get("transactionDate", None)
    if raw_date:
        try:
            txn_date = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
        except ValueError:
            return jsonify({"success": False, "message": "Invalid transactionDate. Use ISO-8601."}), 400

    # Start from everything the client sent, then set/override server-owned fields.
    now = _now()
    _server_owned = {"_id", "createdAt", "updatedAt", "userId", "type", "receiptId", "rentId", "imageUrls", "imagePaths"}
    if is_multipart:
        # form-data: only well-known fields are accessible; pass them all through
        all_form_keys = set(request.form.keys())
        extra_fields = {k: request.form.get(k) for k in all_form_keys if k not in _server_owned}
    else:
        extra_fields = {k: v for k, v in _body.items() if k not in _server_owned}

    doc = extra_fields
    doc["userId"]          = _parse_oid(user_id_str)
    doc["type"]            = "manual"
    doc["receiptId"]       = None
    doc["rentId"]          = None
    doc["title"]           = title
    doc["amount"]          = amount
    doc["currency"]        = str(get("currency", "USD")).upper().strip()[:3]
    doc["transactionType"] = txn_type
    doc["transactionDate"] = txn_date
    doc["imageUrls"]       = []
    doc["imagePaths"]      = []
    doc["createdAt"]       = now
    doc["updatedAt"]       = now

    result = current_app.db.transaction.insert_one(doc)
    doc["_id"] = result.inserted_id
    txn_id_str = str(result.inserted_id)

    # Handle optional images attached at creation time
    if is_multipart:
        files = request.files.getlist("images")
        saved_urls, saved_paths, img_errors = _save_images(files, txn_id_str)
        if img_errors:
            return jsonify({"success": False, "message": img_errors[0]}), 400
        if saved_urls:
            current_app.db.transaction.update_one(
                {"_id": doc["_id"]},
                {"$set": {"imageUrls": saved_urls, "imagePaths": saved_paths}},
            )
            doc["imageUrls"]  = saved_urls
            doc["imagePaths"] = saved_paths

    return jsonify({"success": True, "data": _serialize_transaction(doc)}), 201


@transactions_bp.route("/", methods=["GET"])
def list_transactions():
    """
    List all transactions for the current user, with linked data populated live.

    Query params:
      - type    "manual" | "receipt" | "rent"  (optional filter)
      - page    int  (default 1)
      - limit   int  (default 20, max 100)
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    filt = {"userId": user_oid}
    type_filter = (request.args.get("type") or "").lower()
    if type_filter in VALID_TYPES:
        filt["type"] = type_filter

    try:
        page  = max(1, int(request.args.get("page",  1)))
        limit = min(100, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        return jsonify({"success": False, "message": "page and limit must be integers"}), 400

    skip  = (page - 1) * limit
    txns  = list(
        db.transaction.find(filt)
        .sort("createdAt", -1)
        .skip(skip)
        .limit(limit)
    )
    total = db.transaction.count_documents(filt)

    # Batch-fetch all linked receipts in a single query to avoid N+1
    receipt_ids = [t["receiptId"] for t in txns if t.get("type") == "receipt" and t.get("receiptId")]
    rent_ids    = [t["rentId"]    for t in txns if t.get("type") == "rent"    and t.get("rentId")]

    receipts_by_id = {}
    if receipt_ids:
        for r in db.receipt.find({"_id": {"$in": receipt_ids}}, {"imagePath": 0, "rawText": 0}):
            receipts_by_id[r["_id"]] = r

    rents_by_id = {}
    if rent_ids:
        for r in db.rent_payment.find({"_id": {"$in": rent_ids}}):
            rents_by_id[r["_id"]] = r

    serialized = []
    for txn in txns:
        t   = txn.get("type", "manual")
        txn = dict(txn)  # shallow copy so we can backfill propertyId safely
        if t == "receipt":
            raw = receipts_by_id.get(txn.get("receiptId"))
            if raw and not txn.get("propertyId") and raw.get("propertyId"):
                txn["propertyId"] = raw["propertyId"]
            serialized.append(_serialize_transaction(txn, _serialize_receipt(raw)))
        elif t == "rent":
            raw = rents_by_id.get(txn.get("rentId"))
            if raw and not txn.get("propertyId") and raw.get("propertyId"):
                txn["propertyId"] = raw["propertyId"]
            serialized.append(_serialize_transaction(txn, _serialize_rent(raw) if raw else None))
        else:
            serialized.append(_serialize_transaction(txn))

    return jsonify({
        "success": True,
        "data": serialized,
        "pagination": {
            "page": page, "limit": limit, "total": total,
            "pages": (total + limit - 1) // limit,
        },
    }), 200


@transactions_bp.route("/<txn_id>", methods=["GET"])
def get_transaction(txn_id):
    """Get a single transaction with live linked data populated."""
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(txn_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid transaction ID"}), 400

    txn = current_app.db.transaction.find_one({"_id": oid, "userId": _parse_oid(user_id_str)})
    if not txn:
        return jsonify({"success": False, "message": "Transaction not found"}), 404

    return jsonify({"success": True, "data": _populate(txn, current_app.db)}), 200


@transactions_bp.route("/<txn_id>", methods=["PATCH"])
def update_transaction(txn_id):
    """
    Update a MANUAL transaction only.
    Linked transactions (receipt / rent) are read-only here — edit the source instead.

    JSON body (all optional — send only what changes):
      - title
      - amount          (number)
      - transactionType ("debit" | "credit")
      - currency        (string)
      - description     (string)
      - transactionDate (ISO-8601 string)
      - notes           (string)
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(txn_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid transaction ID"}), 400

    db  = current_app.db
    txn = db.transaction.find_one({"_id": oid, "userId": _parse_oid(user_id_str)})
    if not txn:
        return jsonify({"success": False, "message": "Transaction not found"}), 404

    if txn.get("type") != "manual":
        return jsonify({
            "success": False,
            "message": (
                f"This is a linked '{txn.get('type')}' transaction. "
                "Edit the source receipt or rent payment to change its details."
            ),
        }), 403

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "message": "No fields provided to update"}), 400

    updates = {}
    errors  = []

    if "title" in data:
        v = (data["title"] or "").strip()
        if not v:
            errors.append("title cannot be empty")
        else:
            updates["title"] = v

    if "amount" in data:
        try:
            updates["amount"] = float(data["amount"])
        except (TypeError, ValueError):
            errors.append("amount must be a number")

    if "transactionType" in data:
        v = (data["transactionType"] or "").lower()
        if v not in VALID_TRANSACTION_TYPES:
            errors.append("transactionType must be 'debit' or 'credit'")
        else:
            updates["transactionType"] = v

    if "currency" in data:
        updates["currency"] = str(data["currency"]).upper().strip()[:3]

    if "description" in data:
        updates["description"] = (data["description"] or "").strip() or None

    if "transactionDate" in data:
        raw = data["transactionDate"]
        if raw is None:
            updates["transactionDate"] = None
        else:
            try:
                updates["transactionDate"] = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                errors.append("Invalid transactionDate. Use ISO-8601.")

    if "notes" in data:
        updates["notes"] = (data["notes"] or "").strip() or None

    if "propertyId" in data:
        updates["propertyId"] = _parse_oid(data["propertyId"] or "") or None

    if errors:
        return jsonify({"success": False, "message": "; ".join(errors)}), 400
    if not updates:
        return jsonify({"success": False, "message": "No valid fields provided to update"}), 400

    updates["updatedAt"] = _now()
    db.transaction.update_one({"_id": oid}, {"$set": updates})

    updated = db.transaction.find_one({"_id": oid})
    return jsonify({"success": True, "data": _serialize_transaction(updated)}), 200


@transactions_bp.route("/<txn_id>", methods=["DELETE"])
def delete_transaction(txn_id):
    """
    Delete a transaction record.

    This only deletes the transaction entry — it never deletes the linked
    receipt or rent payment.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(txn_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid transaction ID"}), 400

    db = current_app.db
    txn = db.transaction.find_one({"_id": oid, "userId": _parse_oid(user_id_str)})
    if not txn:
        return jsonify({"success": False, "message": "Transaction not found"}), 404

    # Clean up any attached image files from disk
    if txn.get("imagePaths"):
        base_dir = os.path.abspath(
            os.path.join(current_app.root_path, "..", "uploads", "TransactionImages")
        )
        for p in txn["imagePaths"]:
            if os.path.isfile(p) and _safe_realpath(p, base_dir):
                try:
                    os.remove(p)
                except OSError:
                    pass

    db.transaction.delete_one({"_id": oid})
    return jsonify({"success": True, "message": "Transaction deleted"}), 200


# ── Image upload / serve / delete ─────────────────────────────────────────────

def _save_images(files, txn_id_str: str) -> tuple[list, list, list]:
    """
    Validate and save a list of FileStorage objects.
    Returns (url_list, path_list, error_list).
    Stops and returns errors on the first problem.
    """
    saved_urls  = []
    saved_paths = []
    base_dir    = _txn_images_dir()

    for f in files:
        if not f or not f.filename:
            continue
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in ALLOWED_IMG_EXT:
            return [], [], [f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_IMG_EXT))}"]

        data = f.read()
        if len(data) > MAX_IMG_SIZE:
            return [], [], [f"File '{f.filename}' exceeds 15 MB limit"]

        ts       = int(_now().timestamp() * 1000)
        filename = f"{_sanitize(txn_id_str)}_{ts}_{hashlib.sha256(data).hexdigest()[:8]}.{ext}"
        save_path = os.path.join(base_dir, filename)

        if not _safe_realpath(save_path, base_dir):
            return [], [], ["Invalid file path"]

        with open(save_path, "wb") as fh:
            fh.write(data)

        saved_urls.append(f"/api/transactions/images/{filename}")
        saved_paths.append(save_path)

    return saved_urls, saved_paths, []


@transactions_bp.route("/<txn_id>/images", methods=["POST"])
def upload_transaction_images(txn_id):
    """
    Add one or more images to an existing MANUAL transaction.

    Multipart/form-data, repeat key "images" for multiple files.
    Up to 10 images total per transaction.

    Returns the updated transaction.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(txn_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid transaction ID"}), 400

    db  = current_app.db
    txn = db.transaction.find_one({"_id": oid, "userId": _parse_oid(user_id_str)})
    if not txn:
        return jsonify({"success": False, "message": "Transaction not found"}), 404
    if txn.get("type") != "manual":
        return jsonify({"success": False, "message": "Images can only be attached to manual transactions"}), 403

    existing_urls  = txn.get("imageUrls")  or []
    existing_paths = txn.get("imagePaths") or []

    if len(existing_urls) >= MAX_IMAGES_PER_TXN:
        return jsonify({"success": False, "message": f"Maximum {MAX_IMAGES_PER_TXN} images per transaction"}), 400

    files = request.files.getlist("images")
    if not files or not any(f.filename for f in files):
        return jsonify({"success": False, "message": "No files provided under key 'images'"}), 400

    # Respect per-transaction cap
    remaining = MAX_IMAGES_PER_TXN - len(existing_urls)
    files = files[:remaining]

    new_urls, new_paths, errors = _save_images(files, txn_id)
    if errors:
        return jsonify({"success": False, "message": errors[0]}), 400

    all_urls  = existing_urls  + new_urls
    all_paths = existing_paths + new_paths

    db.transaction.update_one(
        {"_id": oid},
        {"$set": {"imageUrls": all_urls, "imagePaths": all_paths, "updatedAt": _now()}},
    )

    updated = db.transaction.find_one({"_id": oid})
    return jsonify({"success": True, "data": _serialize_transaction(updated)}), 200


@transactions_bp.route("/images/<filename>", methods=["GET"])
def serve_transaction_image(filename):
    """
    Serve a transaction image file by filename.
    URL is returned inside imageUrls on the transaction document.
    No auth required — the opaque filename acts as a capability token.
    """
    # Reject any path traversal attempts
    safe_filename = os.path.basename(filename)
    base_dir  = os.path.abspath(
        os.path.join(current_app.root_path, "..", "uploads", "TransactionImages")
    )
    file_path = os.path.join(base_dir, safe_filename)

    if not _safe_realpath(file_path, base_dir) or not os.path.isfile(file_path):
        return jsonify({"success": False, "message": "Image not found"}), 404

    ext = safe_filename.rsplit(".", 1)[-1].lower()
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "webp": "image/webp", "heic": "image/heic"}
    return send_file(file_path, mimetype=mime_map.get(ext, "application/octet-stream"))


@transactions_bp.route("/<txn_id>/images/<filename>", methods=["DELETE"])
def delete_transaction_image(txn_id, filename):
    """
    Remove a single image from a manual transaction.

    `filename` is the bare filename as it appears at the end of the imageUrl
    (e.g. "abc123_1710000000000_abcd1234.jpg").
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(txn_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid transaction ID"}), 400

    db  = current_app.db
    txn = db.transaction.find_one({"_id": oid, "userId": _parse_oid(user_id_str)})
    if not txn:
        return jsonify({"success": False, "message": "Transaction not found"}), 404
    if txn.get("type") != "manual":
        return jsonify({"success": False, "message": "Only manual transactions have images"}), 403

    safe_filename  = os.path.basename(filename)
    target_url     = f"/api/transactions/images/{safe_filename}"
    existing_urls  = txn.get("imageUrls")  or []
    existing_paths = txn.get("imagePaths") or []

    if target_url not in existing_urls:
        return jsonify({"success": False, "message": "Image not found on this transaction"}), 404

    idx = existing_urls.index(target_url)
    new_urls  = [u for i, u in enumerate(existing_urls)  if i != idx]
    new_paths = [p for i, p in enumerate(existing_paths) if i != idx]

    # Remove file from disk
    if idx < len(existing_paths):
        disk_path = existing_paths[idx]
        base_dir  = os.path.abspath(
            os.path.join(current_app.root_path, "..", "uploads", "TransactionImages")
        )
        if os.path.isfile(disk_path) and _safe_realpath(disk_path, base_dir):
            os.remove(disk_path)

    db.transaction.update_one(
        {"_id": oid},
        {"$set": {"imageUrls": new_urls, "imagePaths": new_paths, "updatedAt": _now()}},
    )

    updated = db.transaction.find_one({"_id": oid})
    return jsonify({"success": True, "data": _serialize_transaction(updated)}), 200
