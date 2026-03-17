"""
Document E-Sign System
───────────────────────

Phase 1 endpoints (core)
  POST   /api/documents/                       Create a new document record
  GET    /api/documents/                       List documents for the current user
  GET    /api/documents/<id>                   Get single document + full signer list
  DELETE /api/documents/<id>                   Delete a draft (owner only)
  POST   /api/documents/<id>/upload            Upload base PDF (multipart key: "pdf")
  PUT    /api/documents/<id>/sign              Submit completed signature for one signer
  POST   /api/documents/<id>/send              Transition draft → pending, notify signers
  PUT    /api/documents/<id>/decline           Signer declines — notifies owner
  PUT    /api/documents/<id>/void              Owner cancels — notifies all signers

Phase 2 endpoints
  GET    /api/documents/<id>/download          Stream final merged PDF with burned-in sigs
  GET    /api/documents/<id>/audit             Full audit log
  GET    /api/documents/pending-count          Badge count of pending signing requests

MongoDB collections
  document  — one doc per document
"""

import base64
import hashlib
import io
import os
import re
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, jsonify, request, send_file

from .auth import decode_token

documents_bp = Blueprint("documents", __name__, url_prefix="/api/documents")

ALLOWED_PDF_EXT = {"pdf"}
MAX_PDF_SIZE    = 20 * 1024 * 1024   # 20 MB
VALID_STATUSES  = {"draft", "pending_signatures", "completed", "cancelled"}
VALID_SIGNER_ROLES = {"landlord", "tenant", "witness"}
VALID_FIELD_TYPES  = {"signature", "date", "text", "initials"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _parse_oid(value):
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _sanitize(value):
    return re.sub(r"[^\w\-]", "_", str(value))


def _docs_dir():
    path = os.path.abspath(
        os.path.join(current_app.root_path, "..", "uploads", "Documents")
    )
    os.makedirs(path, exist_ok=True)
    return path


def _signed_dir():
    path = os.path.abspath(
        os.path.join(current_app.root_path, "..", "uploads", "SignedDocuments")
    )
    os.makedirs(path, exist_ok=True)
    return path


def _safe_realpath(path, base_dir):
    return os.path.realpath(path).startswith(os.path.realpath(base_dir))


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _serialize_doc(doc, db=None):
    """Convert a MongoDB document document to a JSON-safe dict."""
    if doc is None:
        return None
    doc["id"] = str(doc.pop("_id"))
    if "ownerId" in doc:
        owner_oid = doc["ownerId"]
        if db is not None and owner_oid:
            owner = db.user.find_one({"_id": owner_oid}, {"email": 1, "firstName": 1, "lastName": 1})
            if owner:
                doc["ownerEmail"] = owner.get("email")
                doc["ownerName"] = f"{owner.get('firstName', '')} {owner.get('lastName', '')}".strip() or None
        doc["ownerId"] = str(owner_oid)
    if "propertyId" in doc and doc["propertyId"]:
        doc["propertyId"] = str(doc["propertyId"])
    for s in doc.get("signers", []):
        if "userId" in s and s["userId"]:
            s["userId"] = str(s["userId"])
    for e in doc.get("auditLog", []):
        if "userId" in e and e["userId"]:
            e["userId"] = str(e["userId"])
        if isinstance(e.get("at"), datetime):
            e["at"] = e["at"].isoformat()
    for f in doc.get("fields", []):
        if "assignedTo" in f and f["assignedTo"]:
            f["assignedTo"] = str(f["assignedTo"])
    if "createdAt" in doc and isinstance(doc["createdAt"], datetime):
        doc["createdAt"] = doc["createdAt"].isoformat()
    if "updatedAt" in doc and isinstance(doc["updatedAt"], datetime):
        doc["updatedAt"] = doc["updatedAt"].isoformat()
    return doc


def _audit_entry(action, user_id_str, ip=None):
    return {
        "action": action,
        "userId": _parse_oid(user_id_str),
        "at": _now(),
        "ip": ip or "",
    }


def _client_ip():
    return (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or ""
    )


def _notify(notif_type, recipient_oids, doc, actor_doc, app, extra=None):
    """Fire trigger_document_notification without crashing the main flow."""
    try:
        from .notifications import trigger_document_notification
        trigger_document_notification(
            notif_type,
            recipient_oids,
            str(doc["_id"]),
            doc.get("title", "Document"),
            actor_doc,
            app,
            extra_data=extra,
        )
    except Exception:
        pass


def _next_pending_signer(doc):
    """Return the signer dict with the lowest order whose status is pending, or None."""
    pending = [s for s in doc.get("signers", []) if s.get("status") == "pending"]
    if not pending:
        return None
    return min(pending, key=lambda s: s.get("order", 999))


def _embed_signatures(doc, db):
    """
    Use PyMuPDF to burn all signed field values into the original PDF.

    Coordinate system expected for field x/y:
      - Origin at the TOP-LEFT corner of the page (screen / PyMuPDF convention).
      - Y increases DOWNWARD, X increases rightward.
      - Units: PDF points (1 pt = 1/72 inch).  A US-Letter page is 612 × 792 pts.

    If your frontend returns PDF-native coordinates (origin at BOTTOM-LEFT, y
    increases upward), convert before storing the field:
        fitz_y = page_height_pts - pdf_native_y - field_height_pts

    Returns the path of the merged PDF file, or None on failure.
    """
    try:
        import fitz  # PyMuPDF

        base_path = doc.get("pdfPath")
        if not base_path or not os.path.isfile(base_path):
            return None

        pdf = fitz.open(base_path)
        num_pages = len(pdf)

        for field in doc.get("fields", []):
            value = field.get("value")
            if not value:
                continue

            # Convert 1-indexed page to 0-indexed and guard against out-of-range
            page_num = max(0, int(field.get("page", 1)) - 1)
            if page_num >= num_pages:
                continue

            page = pdf[page_num]
            pw = page.rect.width
            ph = page.rect.height

            x = float(field.get("x") or 0)
            y = float(field.get("y") or 0)
            w = max(float(field.get("width") or 200), 10.0)
            h = max(float(field.get("height") or 60), 10.0)

            # Clamp rect so it never spills outside the visible page area
            x = min(max(x, 0.0), pw - w)
            y = min(max(y, 0.0), ph - h)

            rect = fitz.Rect(x, y, x + w, y + h)

            LABEL_TEXT    = "Signed electronically"
            LABEL_FONT_SZ = 7          # pts — small but legible
            LABEL_HEIGHT  = 10.0       # pts — thin strip beneath the field
            LABEL_PADDING = 2.0        # pts gap between field bottom and label
            LABEL_COLOR   = (0.4, 0.4, 0.4)   # grey

            try:
                if field.get("type") in ("signature", "initials"):
                    # value is a base64 PNG, optionally with a data-URI prefix
                    img_data = value.split(",", 1)[-1] if "," in value else value
                    img_bytes = base64.b64decode(img_data)
                    # keep_proportion=False fills the defined rect exactly
                    page.insert_image(rect, stream=img_bytes, keep_proportion=False)
                else:
                    # Plain text field (date, text, initials-text, etc.)
                    page.insert_textbox(
                        rect,
                        str(value),
                        fontsize=11,
                        color=(0, 0, 0),
                        align=0,
                    )

                # ── "Signed electronically" label beneath every filled field ──
                label_top = rect.y1 + LABEL_PADDING
                label_bot = label_top + LABEL_HEIGHT
                # If the label would go off the page, shift it above the field instead
                if label_bot > ph:
                    label_top = rect.y0 - LABEL_PADDING - LABEL_HEIGHT
                    label_bot = rect.y0 - LABEL_PADDING
                label_rect = fitz.Rect(rect.x0, label_top, rect.x1, label_bot)
                page.insert_textbox(
                    label_rect,
                    LABEL_TEXT,
                    fontsize=LABEL_FONT_SZ,
                    color=LABEL_COLOR,
                    align=1,   # centred
                )
            except Exception:
                # Skip individual bad fields without aborting the whole PDF
                pass

        out_name = f"signed_{str(doc['_id'])}.pdf"
        out_path = os.path.join(_signed_dir(), out_name)
        pdf.save(out_path, garbage=4, deflate=True)
        pdf.close()
        return out_path

    except Exception:
        return None


# ── Phase 1: Core endpoints ───────────────────────────────────────────────────

@documents_bp.route("/", methods=["POST"])
def create_document():
    """
    Create a new document record. Optionally attach the base PDF in the same
    request so the frontend never needs a two-step create-then-upload flow.

    Accepts EITHER:
      • application/json          { title, type, propertyId }
      • multipart/form-data       fields: title, type, propertyId
                                  file key: "pdf"  (optional)

    Returns: 201 with document object (pdfUrl populated if PDF was included).
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    # Support both JSON and multipart in one endpoint
    is_multipart = request.content_type and "multipart/form-data" in request.content_type
    if is_multipart:
        title      = (request.form.get("title") or "").strip()
        doc_type   = (request.form.get("type") or "custom").strip()
        prop_id    = _parse_oid(request.form.get("propertyId") or "")
    else:
        data     = request.get_json(silent=True) or {}
        title    = (data.get("title") or "").strip()
        doc_type = (data.get("type") or "custom").strip()
        prop_id  = _parse_oid(data.get("propertyId") or "")

    if not title:
        return jsonify({"success": False, "message": "title is required"}), 400

    user_oid = _parse_oid(user_id_str)
    now      = _now()

    doc = {
        "title":      title,
        "type":       doc_type,
        "status":     "draft",
        "ownerId":    user_oid,
        "propertyId": prop_id,
        "pdfUrl":     None,
        "pdfPath":    None,
        "pdfHash":    None,
        "signers":    [],
        "fields":     [],
        "auditLog":   [_audit_entry("created", user_id_str, _client_ip())],
        "createdAt":  now,
        "updatedAt":  now,
    }
    result = current_app.db.document.insert_one(doc)
    doc["_id"] = result.inserted_id
    doc_id_str = str(doc["_id"])

    # If a PDF was included in the same request, process it immediately
    pdf_file = request.files.get("pdf") if is_multipart else None
    if pdf_file and pdf_file.filename:
        ext = pdf_file.filename.rsplit(".", 1)[-1].lower() if "." in pdf_file.filename else ""
        if ext not in ALLOWED_PDF_EXT:
            current_app.db.document.delete_one({"_id": doc["_id"]})
            return jsonify({"success": False, "message": "Only PDF files are accepted"}), 400

        pdf_bytes = pdf_file.read()
        if len(pdf_bytes) > MAX_PDF_SIZE:
            current_app.db.document.delete_one({"_id": doc["_id"]})
            return jsonify({"success": False, "message": "PDF must be under 20 MB"}), 413

        if not pdf_bytes.startswith(b"%PDF"):
            current_app.db.document.delete_one({"_id": doc["_id"]})
            return jsonify({"success": False, "message": "File does not appear to be a valid PDF"}), 400

        base_dir  = _docs_dir()
        filename  = f"{_sanitize(doc_id_str)}.pdf"
        save_path = os.path.join(base_dir, filename)

        if not _safe_realpath(save_path, base_dir):
            current_app.db.document.delete_one({"_id": doc["_id"]})
            return jsonify({"success": False, "message": "Invalid file path"}), 400

        with open(save_path, "wb") as f:
            f.write(pdf_bytes)

        pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
        pdf_url  = f"/api/documents/{doc_id_str}/download"

        current_app.db.document.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "pdfPath":  save_path,
                "pdfUrl":   pdf_url,
                "pdfHash":  pdf_hash,
                "updatedAt": now,
            },
            "$push": {"auditLog": _audit_entry("pdf_uploaded", user_id_str, _client_ip())}},
        )
        doc.update({"pdfPath": save_path, "pdfUrl": pdf_url, "pdfHash": pdf_hash})

    return jsonify({"success": True, "data": _serialize_doc(doc, current_app.db)}), 201


@documents_bp.route("/current-lease", methods=["GET"])
def current_lease():
    """
    Return the tenant's current active lease agreement.

    Looks for the most recently created document that:
      • type  == "lease_agreement"
      • status == "completed"
      • signers array contains an entry with userId == caller AND status == "completed"

    Only the document metadata is returned (no PDF content).

    GET /api/documents/current-lease
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    db = current_app.db

    lease = db.document.find_one(
        {
            "type":   "lease_agreement",
            "status": "completed",
            "signers": {
                "$elemMatch": {
                    "userId": user_oid,
                    "status": "completed",
                }
            },
        },
        # Exclude the raw PDF path — data only
        {"pdfPath": 0},
        sort=[("createdAt", -1)],
    )

    if not lease:
        return jsonify({"success": True, "data": None}), 200

    return jsonify({"success": True, "data": _serialize_doc(lease, db)}), 200


@documents_bp.route("/", methods=["GET"])
def list_documents():
    """
    List documents for the current user.

    Query params:
      - role    "owner" | "signer"  (default: both)
      - status  e.g. "draft" | "pending_signatures" | "completed" | "cancelled"
      - page    (int, default 1)
      - limit   (int, default 20, max 100)
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    role_filter   = (request.args.get("role") or "").lower()
    status_filter = (request.args.get("status") or "").lower()

    try:
        page  = max(1, int(request.args.get("page", 1)))
        limit = min(100, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        return jsonify({"success": False, "message": "page and limit must be integers"}), 400

    if role_filter == "owner":
        filt = {"ownerId": user_oid}
    elif role_filter == "signer":
        filt = {"signers.userId": user_oid}
    else:
        filt = {"$or": [{"ownerId": user_oid}, {"signers.userId": user_oid}]}

    if status_filter and status_filter in VALID_STATUSES:
        filt["status"] = status_filter

    skip  = (page - 1) * limit
    docs  = list(
        db.document.find(filt, {"pdfPath": 0})  # exclude internal path
        .sort("updatedAt", -1)
        .skip(skip)
        .limit(limit)
    )
    total = db.document.count_documents(filt)

    return jsonify({
        "success": True,
        "data": [_serialize_doc(d) for d in docs],
        "pagination": {
            "page": page, "limit": limit, "total": total,
            "pages": (total + limit - 1) // limit,
        },
    }), 200


@documents_bp.route("/<doc_id>", methods=["GET"])
def get_document(doc_id):
    """Get a single document with full signer list and audit log."""
    user_id_str, err = decode_token(request)
    if err:
        return err

    doc_oid  = _parse_oid(doc_id)
    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    doc = db.document.find_one(
        {"_id": doc_oid, "$or": [{"ownerId": user_oid}, {"signers.userId": user_oid}]},
        {"pdfPath": 0},
    )
    if not doc:
        return jsonify({"success": False, "message": "Document not found"}), 404

    # Log view event (fire-and-forget, no error if it fails)
    is_signer = any(str(s.get("userId")) == user_id_str for s in doc.get("signers", []))
    if is_signer:
        db.document.update_one(
            {"_id": doc_oid},
            {"$push": {"auditLog": _audit_entry("viewed", user_id_str, _client_ip())},
             "$set":  {"updatedAt": _now()}},
        )
    return jsonify({"success": True, "data": _serialize_doc(doc, db)}), 200


@documents_bp.route("/<doc_id>", methods=["DELETE"])
def delete_document(doc_id):
    """Delete a draft document. Owner only."""
    user_id_str, err = decode_token(request)
    if err:
        return err

    doc_oid  = _parse_oid(doc_id)
    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    doc = db.document.find_one({"_id": doc_oid, "ownerId": user_oid})
    if not doc:
        return jsonify({"success": False, "message": "Document not found"}), 404
    if doc.get("status") != "draft":
        return jsonify({"success": False, "message": "Only draft documents can be deleted"}), 400

    # Remove uploaded PDF file if present
    pdf_path = doc.get("pdfPath")
    if pdf_path and os.path.isfile(pdf_path):
        base = _docs_dir()
        if _safe_realpath(pdf_path, base):
            os.remove(pdf_path)

    db.document.delete_one({"_id": doc_oid})
    return jsonify({"success": True, "message": "Document deleted"}), 200


@documents_bp.route("/<doc_id>/upload", methods=["POST"])
def upload_pdf(doc_id):
    """
    Upload the base PDF for a document.

    Multipart form-data, key: "pdf".
    The document must be in draft status and owned by the caller.
    Replaces any previously uploaded PDF.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    db       = current_app.db
    doc_oid  = _parse_oid(doc_id)   # None when doc_id is "undefined" or garbage

    # ── Auto-create document if no valid ID was supplied ──────────────────────
    if not doc_oid:
        title    = (request.form.get("title") or "Untitled Document").strip()
        doc_type = (request.form.get("type") or "custom").strip()
        prop_id  = _parse_oid(request.form.get("propertyId") or "")
        now      = _now()
        new_doc  = {
            "title":      title,
            "type":       doc_type,
            "status":     "draft",
            "ownerId":    user_oid,
            "propertyId": prop_id,
            "pdfUrl":     None,
            "pdfPath":    None,
            "pdfHash":    None,
            "signers":    [],
            "fields":     [],
            "auditLog":   [_audit_entry("created", user_id_str, _client_ip())],
            "createdAt":  now,
            "updatedAt":  now,
        }
        result  = db.document.insert_one(new_doc)
        doc_oid = result.inserted_id
        new_doc["_id"] = doc_oid
        doc     = new_doc
    else:
        doc = db.document.find_one({"_id": doc_oid, "ownerId": user_oid})
        if not doc:
            return jsonify({"success": False, "message": "Document not found"}), 404
        if doc.get("status") not in ("draft", "pending_signatures"):
            return jsonify({"success": False, "message": "PDF can only be replaced on draft or pending documents"}), 400

    if "pdf" not in request.files:
        return jsonify({"success": False, "message": "No file uploaded under key 'pdf'"}), 400

    file = request.files["pdf"]
    if not file.filename:
        return jsonify({"success": False, "message": "Empty filename"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_PDF_EXT:
        return jsonify({"success": False, "message": "Only PDF files are accepted"}), 400

    # Read and size-check before writing
    pdf_bytes = file.read()
    if len(pdf_bytes) > MAX_PDF_SIZE:
        return jsonify({"success": False, "message": "PDF must be under 20 MB"}), 413

    # Validate it is actually a PDF (magic bytes)
    if not pdf_bytes.startswith(b"%PDF"):
        return jsonify({"success": False, "message": "File does not appear to be a valid PDF"}), 400

    base_dir  = _docs_dir()
    filename  = f"{_sanitize(str(doc_oid))}.pdf"
    save_path = os.path.join(base_dir, filename)

    if not _safe_realpath(save_path, base_dir):
        return jsonify({"success": False, "message": "Invalid file path"}), 400

    # Remove old file
    old_path = doc.get("pdfPath")
    if old_path and os.path.isfile(old_path) and _safe_realpath(old_path, base_dir):
        os.remove(old_path)

    with open(save_path, "wb") as f:
        f.write(pdf_bytes)

    pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
    pdf_url  = f"/api/documents/{str(doc_oid)}/download"

    now = _now()
    db.document.update_one(
        {"_id": doc_oid},
        {
            "$set": {
                "pdfPath":  save_path,
                "pdfUrl":   pdf_url,
                "pdfHash":  pdf_hash,
                "updatedAt": now,
            },
            "$push": {"auditLog": _audit_entry("pdf_uploaded", user_id_str, _client_ip())},
        },
    )

    updated_doc = db.document.find_one({"_id": doc_oid}, {"pdfPath": 0})
    return jsonify({
        "success": True,
        "message": "PDF uploaded successfully",
        "data": _serialize_doc(updated_doc),
    }), 200


@documents_bp.route("/<doc_id>/send", methods=["POST"])
def send_for_signing(doc_id):
    """
    Transition a draft document to pending_signatures and notify all signers.

    JSON body:
      - signers  (array, required)  — list of signer objects:
                   { "userId": "...", "role": "tenant|landlord|witness", "order": 1 }
                   Alternatively use "email" instead of "userId" to resolve by email.
      - fields   (array, optional) — field placement definitions:
                   { "id": "field_1", "type": "signature|date|text|initials",
                     "assignedTo": "<userId>", "page": 1, "x": 0, "y": 0,
                     "width": 200, "height": 60, "required": true }
      - message  (string, optional) — custom message included in notification body
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    doc_oid  = _parse_oid(doc_id)
    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    doc = db.document.find_one({"_id": doc_oid, "ownerId": user_oid})
    if not doc:
        return jsonify({"success": False, "message": "Document not found"}), 404
    if doc.get("status") != "draft":
        return jsonify({"success": False, "message": "Only draft documents can be sent for signing"}), 400
    if not doc.get("pdfPath"):
        return jsonify({"success": False, "message": "Upload a PDF before sending for signing"}), 400

    data    = request.get_json(silent=True) or {}
    signers_input = data.get("signers") or []
    fields_input  = data.get("fields") or []
    message       = (data.get("message") or "").strip()

    if not signers_input:
        return jsonify({"success": False, "message": "At least one signer is required"}), 400

    # Resolve signer user documents
    owner_doc  = db.user.find_one({"_id": user_oid})
    signer_docs = []
    for s in signers_input:
        role  = (s.get("role") or "tenant").lower()
        order = int(s.get("order") or 1)
        if role not in VALID_SIGNER_ROLES:
            return jsonify({"success": False, "message": f"Invalid signer role: {role}"}), 400

        user_doc = None
        if s.get("userId"):
            user_doc = db.user.find_one({"_id": _parse_oid(s["userId"])})
        elif s.get("email"):
            user_doc = db.user.find_one({"email": s["email"].lower().strip()})

        if not user_doc:
            identifier = s.get("userId") or s.get("email") or "unknown"
            return jsonify({"success": False, "message": f"User not found: {identifier}"}), 404

        name = f"{user_doc.get('firstName', '')} {user_doc.get('lastName', '')}".strip()
        signer_docs.append({
            "userId":    user_doc["_id"],
            "email":     user_doc.get("email", ""),
            "name":      name,
            "role":      role,
            "order":     order,
            "status":    "pending",
            "signedAt":  None,
            "ipAddress": None,
        })

    # Validate and build fields
    built_fields = []
    for f in fields_input:
        ftype = (f.get("type") or "signature").lower()
        if ftype not in VALID_FIELD_TYPES:
            return jsonify({"success": False, "message": f"Invalid field type: {ftype}"}), 400
        assigned_oid = _parse_oid(f.get("assignedTo") or "")
        built_fields.append({
            "id":         f.get("id") or f"field_{len(built_fields) + 1}",
            "type":       ftype,
            "assignedTo": assigned_oid,
            "page":       int(f.get("page") or 1),
            "x":          float(f.get("x") or 0),
            "y":          float(f.get("y") or 0),
            "width":      float(f.get("width") or 200),
            "height":     float(f.get("height") or 60),
            "value":      None,
            "required":   bool(f.get("required", True)),
        })

    now = _now()
    db.document.update_one(
        {"_id": doc_oid},
        {
            "$set": {
                "status":    "pending_signatures",
                "signers":   signer_docs,
                "fields":    built_fields,
                "updatedAt": now,
            },
            "$push": {"auditLog": _audit_entry("sent", user_id_str, _client_ip())},
        },
    )
    doc.update({"status": "pending_signatures", "signers": signer_docs, "fields": built_fields})

    # Notify the first signer (lowest order)
    first_signer = min(signer_docs, key=lambda s: s["order"])
    app = current_app._get_current_object()
    _notify(
        "document_signing_request",
        [first_signer["userId"]],
        doc,
        owner_doc,
        app,
        extra={"message": message} if message else None,
    )

    updated_doc = db.document.find_one({"_id": doc_oid}, {"pdfPath": 0})
    return jsonify({"success": True, "data": _serialize_doc(updated_doc)}), 200


@documents_bp.route("/<doc_id>/sign", methods=["PUT"])
def sign_document(doc_id):
    """
    Submit a completed signature for one signer.

    JSON body:
      - signatureImage  (string, optional) base64 PNG of the drawn signature
      - fields          (array, required)  list of field completions:
                          { "id": "field_1", "value": "<base64 PNG or string>" }
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    doc_oid  = _parse_oid(doc_id)
    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    doc = db.document.find_one({"_id": doc_oid})
    if not doc:
        return jsonify({"success": False, "message": "Document not found"}), 404
    if doc.get("status") != "pending_signatures":
        return jsonify({"success": False, "message": "Document is not pending signatures"}), 400

    # Locate the calling user in the signers array
    signer_entry = next(
        (s for s in doc.get("signers", []) if str(s.get("userId")) == user_id_str),
        None,
    )
    if not signer_entry:
        return jsonify({"success": False, "message": "You are not a signer on this document"}), 403
    if signer_entry.get("status") == "signed":
        return jsonify({"success": False, "message": "You have already signed this document"}), 400
    if signer_entry.get("status") == "declined":
        return jsonify({"success": False, "message": "You have declined this document"}), 400

    data          = request.get_json(silent=True) or {}
    field_values  = {fv["id"]: fv["value"] for fv in (data.get("fields") or []) if fv.get("id")}

    # Validate all required fields assigned to this signer are present
    for f in doc.get("fields", []):
        if str(f.get("assignedTo")) == user_id_str and f.get("required"):
            if not field_values.get(f["id"]):
                return jsonify({
                    "success": False,
                    "message": f"Required field '{f['id']}' is missing a value",
                }), 400

    now = _now()
    ip  = _client_ip()

    # Apply field values into the document's fields array
    updated_fields = []
    for f in doc.get("fields", []):
        if str(f.get("assignedTo")) == user_id_str and f["id"] in field_values:
            f = {**f, "value": field_values[f["id"]]}
        updated_fields.append(f)

    # Update the signer entry
    updated_signers = []
    for s in doc.get("signers", []):
        if str(s.get("userId")) == user_id_str:
            s = {**s, "status": "signed", "signedAt": now, "ipAddress": ip}
        updated_signers.append(s)

    # Check if all required signers have now signed
    all_signed = all(
        s["status"] == "signed"
        for s in updated_signers
        if s.get("role") != "witness" or True   # every signer must sign
    )
    new_status = "completed" if all_signed else "pending_signatures"

    db.document.update_one(
        {"_id": doc_oid},
        {
            "$set": {
                "signers":   updated_signers,
                "fields":    updated_fields,
                "status":    new_status,
                "updatedAt": now,
            },
            "$push": {"auditLog": _audit_entry("signed", user_id_str, ip)},
        },
    )
    doc.update({
        "signers": updated_signers,
        "fields":  updated_fields,
        "status":  new_status,
    })

    owner_doc  = db.user.find_one({"_id": doc["ownerId"]})
    signer_doc = db.user.find_one({"_id": user_oid})
    app        = current_app._get_current_object()

    if new_status == "completed":
        # Burn signatures into PDF asynchronously (blocking here but fast for small PDFs)
        signed_path = _embed_signatures(doc, db)
        if signed_path:
            signed_hash = _sha256_file(signed_path)
            db.document.update_one(
                {"_id": doc_oid},
                {"$set": {
                    "signedPdfPath": signed_path,
                    "signedPdfHash": signed_hash,
                    "pdfUrl": f"/api/documents/{doc_id}/download",
                }},
            )
            db.document.update_one(
                {"_id": doc_oid},
                {"$push": {"auditLog": _audit_entry("completed", user_id_str, ip)}},
            )

        # Notify all participants
        all_user_oids = [doc["ownerId"]] + [
            s["userId"] for s in updated_signers if s["userId"] != doc["ownerId"]
        ]
        _notify("document_completed", all_user_oids, doc, signer_doc, app)

    else:
        # Notify owner that this signer has signed
        _notify("document_signed", [doc["ownerId"]], doc, signer_doc, app)

        # Notify the next signer in order
        next_signer = _next_pending_signer({**doc, "signers": updated_signers})
        if next_signer:
            _notify(
                "document_signing_request",
                [next_signer["userId"]],
                doc,
                owner_doc,
                app,
            )

    updated_doc = db.document.find_one({"_id": doc_oid}, {"pdfPath": 0, "signedPdfPath": 0})
    return jsonify({"success": True, "data": _serialize_doc(updated_doc)}), 200


@documents_bp.route("/<doc_id>/decline", methods=["PUT"])
def decline_document(doc_id):
    """
    A signer declines to sign. The document stays in pending_signatures
    (other signers can still sign), but the owner is notified.

    JSON body:
      - reason  (string, optional)
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    doc_oid  = _parse_oid(doc_id)
    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    doc = db.document.find_one({"_id": doc_oid})
    if not doc:
        return jsonify({"success": False, "message": "Document not found"}), 404

    signer_entry = next(
        (s for s in doc.get("signers", []) if str(s.get("userId")) == user_id_str),
        None,
    )
    if not signer_entry:
        return jsonify({"success": False, "message": "You are not a signer on this document"}), 403
    if signer_entry.get("status") != "pending":
        return jsonify({"success": False, "message": "You can only decline a pending signature"}), 400

    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    now    = _now()
    ip     = _client_ip()

    updated_signers = [
        {**s, "status": "declined", "signedAt": now, "ipAddress": ip}
        if str(s.get("userId")) == user_id_str else s
        for s in doc.get("signers", [])
    ]

    db.document.update_one(
        {"_id": doc_oid},
        {
            "$set": {"signers": updated_signers, "updatedAt": now},
            "$push": {"auditLog": {**_audit_entry("declined", user_id_str, ip), "reason": reason}},
        },
    )
    doc["signers"] = updated_signers

    signer_doc = db.user.find_one({"_id": user_oid})
    _notify(
        "document_declined",
        [doc["ownerId"]],
        doc,
        signer_doc,
        current_app._get_current_object(),
        extra={"reason": reason} if reason else None,
    )

    updated_doc = db.document.find_one({"_id": doc_oid}, {"pdfPath": 0, "signedPdfPath": 0})
    return jsonify({"success": True, "data": _serialize_doc(updated_doc)}), 200


@documents_bp.route("/<doc_id>/void", methods=["PUT"])
def void_document(doc_id):
    """
    Owner cancels/voids the document. All signers are notified.

    JSON body:
      - reason  (string, optional)
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    doc_oid  = _parse_oid(doc_id)
    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    doc = db.document.find_one({"_id": doc_oid, "ownerId": user_oid})
    if not doc:
        return jsonify({"success": False, "message": "Document not found"}), 404
    if doc.get("status") in ("completed", "cancelled"):
        return jsonify({"success": False, "message": "Completed or already voided documents cannot be voided"}), 400

    data   = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    now    = _now()
    ip     = _client_ip()

    db.document.update_one(
        {"_id": doc_oid},
        {
            "$set": {"status": "cancelled", "updatedAt": now},
            "$push": {"auditLog": {**_audit_entry("voided", user_id_str, ip), "reason": reason}},
        },
    )

    owner_doc = db.user.find_one({"_id": user_oid})
    signer_oids = [
        s["userId"] for s in doc.get("signers", [])
        if s.get("userId") and str(s["userId"]) != user_id_str
    ]
    _notify(
        "document_voided",
        signer_oids,
        doc,
        owner_doc,
        current_app._get_current_object(),
        extra={"reason": reason} if reason else None,
    )

    updated_doc = db.document.find_one({"_id": doc_oid}, {"pdfPath": 0, "signedPdfPath": 0})
    return jsonify({"success": True, "data": _serialize_doc(updated_doc)}), 200


# ── Phase 2: Download, audit, badge count ─────────────────────────────────────

@documents_bp.route("/<doc_id>/download", methods=["GET"])
def download_document(doc_id):
    """
    Stream the signed PDF (if completed) or the original PDF to the caller,
    provided they are the owner or a listed signer.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    doc_oid  = _parse_oid(doc_id)
    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    doc = db.document.find_one(
        {"_id": doc_oid, "$or": [{"ownerId": user_oid}, {"signers.userId": user_oid}]}
    )
    if not doc:
        return jsonify({"success": False, "message": "Document not found"}), 404

    # Prefer the completed signed PDF; fall back to the original
    path = doc.get("signedPdfPath") or doc.get("pdfPath")
    if not path:
        return jsonify({"success": False, "message": "No PDF available yet"}), 404

    base_dir = _signed_dir() if doc.get("signedPdfPath") and path == doc.get("signedPdfPath") else _docs_dir()
    if not _safe_realpath(path, base_dir):
        return jsonify({"success": False, "message": "File not accessible"}), 403
    if not os.path.isfile(path):
        return jsonify({"success": False, "message": "PDF file not found on server"}), 404

    # Log view
    db.document.update_one(
        {"_id": doc_oid},
        {"$push": {"auditLog": _audit_entry("downloaded", user_id_str, _client_ip())},
         "$set":  {"updatedAt": _now()}},
    )

    filename = f"{_sanitize(doc.get('title', 'document'))}.pdf"
    return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=filename)


@documents_bp.route("/<doc_id>/audit", methods=["GET"])
def get_audit_log(doc_id):
    """
    Return the full audit log for a document. Owner only.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    doc_oid  = _parse_oid(doc_id)
    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    doc = db.document.find_one(
        {"_id": doc_oid, "ownerId": user_oid},
        {"auditLog": 1},
    )
    if not doc:
        return jsonify({"success": False, "message": "Document not found"}), 404

    log = []
    for entry in doc.get("auditLog", []):
        log.append({
            "action": entry.get("action"),
            "userId": str(entry["userId"]) if entry.get("userId") else None,
            "at":     entry["at"].isoformat() if isinstance(entry.get("at"), datetime) else entry.get("at"),
            "ip":     entry.get("ip"),
            "reason": entry.get("reason"),
        })

    return jsonify({"success": True, "data": log}), 200


@documents_bp.route("/<doc_id>/distribute", methods=["POST"])
def distribute_document(doc_id):
    """
    Share a document with one or more users for reading only — no signature required.

    The document must have a PDF uploaded. It does NOT need to be in a specific
    status; draft, pending, completed, and cancelled documents can all be distributed.

    JSON body:
      - recipients  (array, required) — each item is either:
                      { "userId": "<objectId>" }  OR
                      { "email": "user@example.com" }
      - message     (string, optional) — extra note included in the notification

    Returns the document (without pdfPath) on success.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    doc_oid  = _parse_oid(doc_id)
    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    doc = db.document.find_one({"_id": doc_oid, "ownerId": user_oid})
    if not doc:
        return jsonify({"success": False, "message": "Document not found"}), 404
    if not doc.get("pdfPath"):
        return jsonify({"success": False, "message": "Upload a PDF before distributing"}), 400

    data            = request.get_json(silent=True) or {}
    recipients_raw  = data.get("recipients") or []
    message         = (data.get("message") or "").strip()

    if not recipients_raw:
        return jsonify({"success": False, "message": "At least one recipient is required"}), 400

    # Resolve recipient user documents
    recipient_oids = []
    for r in recipients_raw:
        user_doc = None
        if r.get("userId"):
            user_doc = db.user.find_one({"_id": _parse_oid(r["userId"])}, {"_id": 1})
        elif r.get("email"):
            user_doc = db.user.find_one({"email": r["email"].lower().strip()}, {"_id": 1})

        if not user_doc:
            identifier = r.get("userId") or r.get("email") or "unknown"
            return jsonify({"success": False, "message": f"User not found: {identifier}"}), 404

        recipient_oids.append(user_doc["_id"])

    owner_doc = db.user.find_one({"_id": user_oid})
    now       = _now()

    db.document.update_one(
        {"_id": doc_oid},
        {
            "$set": {"updatedAt": now},
            "$push": {"auditLog": _audit_entry("distributed", user_id_str, _client_ip())},
        },
    )

    app = current_app._get_current_object()
    _notify(
        "document_distributed",
        recipient_oids,
        doc,
        owner_doc,
        app,
        extra={"message": message} if message else None,
    )

    updated_doc = db.document.find_one({"_id": doc_oid}, {"pdfPath": 0})
    return jsonify({"success": True, "data": _serialize_doc(updated_doc)}), 200


@documents_bp.route("/pending-count", methods=["GET"])
def pending_count():
    """
    Return the number of documents waiting for the authenticated user's signature.
    Use this to drive badge counters in the UI.

    Response: { "success": true, "data": { "count": N } }
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    count = current_app.db.document.count_documents({
        "status": "pending_signatures",
        "signers": {"$elemMatch": {"userId": user_oid, "status": "pending"}},
    })
    return jsonify({"success": True, "data": {"count": count}}), 200


# ── Index helper ──────────────────────────────────────────────────────────────

def ensure_document_indexes(db):
    """Create MongoDB indexes for the document collection (idempotent)."""
    db.document.create_index([("ownerId", 1), ("status", 1)])
    db.document.create_index([("signers.userId", 1), ("status", 1)])
    db.document.create_index([("propertyId", 1)])
    db.document.create_index([("updatedAt", -1)])
