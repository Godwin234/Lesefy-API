"""
Receipt OCR & Classification System
────────────────────────────────────

OCR engine  : EasyOCR  (open-source, CPU-friendly, 22k+ GitHub stars)
              github.com/JaidedAI/EasyOCR
Image utils : Pillow + numpy  (both pulled in by EasyOCR anyway)
MongoDB     : `receipt` collection

Endpoints
─────────
  POST   /api/receipts/               Upload image → OCR → extract → store
  GET    /api/receipts/               List receipts for current user
  GET    /api/receipts/<id>           Single receipt with all extracted fields
  DELETE /api/receipts/<id>           Delete receipt + its image file
  GET    /api/receipts/<id>/image     Serve the stored receipt image
"""

import hashlib
import io
import os
import re
import threading
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, jsonify, request, send_file

from .auth import decode_token

receipts_bp = Blueprint("receipts", __name__, url_prefix="/api/receipts")

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "heic"}
MAX_FILE_SIZE      = 15 * 1024 * 1024   # 15 MB


# ── Lazy EasyOCR singleton ────────────────────────────────────────────────────
# EasyOCR downloads ~80 MB model files on first initialisation.
# The singleton is share across requests; the lock makes it thread-safe.

_ocr_reader = None
_ocr_lock   = threading.Lock()


def _get_reader():
    """Return the EasyOCR Reader, initialising it on first call (thread-safe)."""
    global _ocr_reader
    if _ocr_reader is None:
        with _ocr_lock:
            if _ocr_reader is None:
                try:
                    import easyocr
                    _ocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)
                except Exception as exc:
                    raise RuntimeError(f"Failed to initialise EasyOCR: {exc}") from exc
    return _ocr_reader


# ── Small helpers ─────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _parse_oid(value):
    try:
        return ObjectId(str(value).strip())
    except (InvalidId, TypeError):
        return None


def _receipts_dir():
    path = os.path.abspath(
        os.path.join(current_app.root_path, "..", "uploads", "Receipts")
    )
    os.makedirs(path, exist_ok=True)
    return path


def _safe_realpath(path, base_dir):
    return os.path.realpath(path).startswith(os.path.realpath(base_dir))


# ── Image pre-processing ──────────────────────────────────────────────────────

def _preprocess_image(raw_bytes: bytes):
    """
    Return a numpy RGB array suitable for EasyOCR, sharpened and contrast-boosted
    for typical smartphone-photographed receipts.
    """
    import numpy as np
    from PIL import Image, ImageEnhance, ImageFilter

    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")

    # Down-scale if enormous — EasyOCR is slow and memory-hungry above 4000 px
    max_dim = 3000
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Grayscale → sharpen → boost contrast → back to RGB for EasyOCR
    img = img.convert("L")
    img = img.filter(ImageFilter.SHARPEN)
    img = ImageEnhance.Contrast(img).enhance(1.8)
    img = img.convert("RGB")

    return np.array(img)


# ── Entity-extraction helpers ─────────────────────────────────────────────────

# merchant-category keyword map (lower-case keys only)
_CATEGORIES: dict[str, list[str]] = {
    "Grocery": [
        "grocery", "supermarket", "kroger", "walmart", "safeway", "whole foods",
        "trader joe", "publix", "aldi", "costco", "sam's club", "food market",
        "fresh market", "sprouts", "wegmans", "heb", "food lion", "piggly",
    ],
    "Restaurant / Food Service": [
        "restaurant", "cafe", "café", "diner", "grill", "kitchen", "bistro",
        "pizza", "burger", "mcdonald", "subway", "taco", "sushi", "bbq",
        "steakhouse", "bar & grill", "eatery", "bakery", "donut", "dunkin",
        "starbucks", "coffee", "wing", "noodle", "panda express", "chipotle",
        "chick-fil-a", "wendy", "popeyes", "five guys", "olive garden",
    ],
    "Pharmacy / Health": [
        "pharmacy", "cvs", "walgreens", "rite aid", "drug", "health",
        "vitamin", "supplement", "rx", "medical", "clinic",
    ],
    "Gas / Fuel": [
        "shell", "bp", "chevron", "exxon", "mobil", "sunoco", "marathon",
        "valero", "citgo", "gas", "fuel", "petrol", "service station",
        "quick trip", "casey's", "speedway",
    ],
    "Clothing / Apparel": [
        "h&m", "zara", "gap", "old navy", "forever 21", "nordstrom",
        "tj maxx", "ross", "marshalls", "clothing", "apparel", "fashion",
        "shoes", "foot locker", "nike", "adidas", "under armour",
    ],
    "Electronics / Tech": [
        "best buy", "apple store", "samsung", "electronics", "tech",
        "computer", "phone", "micro center", "newegg", "b&h photo",
    ],
    "Home Improvement": [
        "home depot", "lowe's", "lowes", "ace hardware", "hardware",
        "supply", "lumber", "menards", "true value",
    ],
    "Entertainment": [
        "cinema", "movie", "theater", "theatre", "amc", "regal", "cinemark",
        "entertainment", "bowling", "arcade", "concert",
    ],
    "Transportation": [
        "uber", "lyft", "taxi", "transit", "metro", "bus", "train", "amtrak",
        "parking", "toll", "enterprise", "hertz", "avis", "rental",
    ],
    "Shopping / Department Store": [
        "target", "amazon", "department", "outlet", "mall", "dollar tree",
        "five below", "dollar general", "family dollar", "big lots",
    ],
    "Hotel / Lodging": [
        "hotel", "inn", "suites", "marriott", "hilton", "hyatt", "motel",
        "airbnb", "lodge",
    ],
    "Utilities / Bills": [
        "electric", "utility", "water", "internet", "cable",
        "at&t", "verizon", "t-mobile", "sprint",
    ],
}

# Grand-total patterns: what the customer actually paid (ordered most→least specific).
# These intentionally capture ONLY the numeric part; the sign is detected separately
# via _NEGATIVE_AMOUNT_RE so that amounts printed as -$23.21 or ($23.21) are caught.
_TOTAL_PATTERNS = [
    re.compile(
        r"(?:grand\s+total|total\s+amount|amount\s+due|balance\s+due|total\s+due"
        r"|amount\s+paid|total\s+paid|you\s+paid|charge\s+total)"
        r"\s*:?\s*[-\(]?\s*\$?\s*(\d{1,6}[.,]\d{2})",
        re.IGNORECASE,
    ),
    re.compile(r"\btotal\b\s*:?\s*[-\(]?\s*\$?\s*(\d{1,6}[.,]\d{2})", re.IGNORECASE),
]

# Detect a negative total on the receipt itself.
# Handles:  -$23.21  |  -23.21  |  ($23.21)  |  (23.21)
# We require the keywords used in _TOTAL_PATTERNS to avoid false positives.
_NEGATIVE_TOTAL_RE = re.compile(
    r"(?:grand\s+total|total\s+amount|amount\s+due|balance\s+due|total\s+due"
    r"|amount\s+paid|total\s+paid|you\s+paid|charge\s+total|\btotal\b)"
    r"\s*:?\s*(?:-\s*\$?|\(\$?)\s*\d{1,6}[.,]\d{2}\)?",
    re.IGNORECASE,
)

# Sale / subtotal patterns: pre-tax amount
_SUBTOTAL_PATTERNS = [
    re.compile(
        r"(?:sub\s*total|sub-total|sale\s+(?:amount|total)|net\s+(?:amount|total)"
        r"|merchandise\s+total|items?\s+total|goods\s+total|purchase\s+subtotal)"
        r"\s*:?\s*\$?\s*(\d{1,6}[.,]\d{2})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bsubtotal\b\s*:?\s*\$?\s*(\d{1,6}[.,]\d{2})",
        re.IGNORECASE,
    ),
]

# Tax patterns
_TAX_PATTERNS = [
    re.compile(
        r"(?:sales?\s+tax|state\s+tax|local\s+tax|hst|gst|vat|pst"
        r"|tax\s+amount|total\s+tax|estimated\s+tax)"
        r"\s*:?\s*(?:\d+\.?\d*\s*%)?\s*\$?\s*(\d{1,5}[.,]\d{2})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\btax\b\s*:?\s*\$?\s*(\d{1,5}[.,]\d{2})",
        re.IGNORECASE,
    ),
]

_DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4})\b"),   # 03/14/2026
    re.compile(r"\b(\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2})\b"),   # 2026-03-14
    re.compile(r"\b(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2})\b"),   # 03/14/26
    re.compile(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*"
        r"\.?\s+\d{1,2},?\s+\d{4})\b",
        re.IGNORECASE,
    ),
]

_DATE_FORMATS = [
    "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y",
    "%m.%d.%Y", "%d.%m.%Y", "%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d",
    "%m/%d/%y", "%d/%m/%y",
    "%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y",
    "%b. %d, %Y", "%B. %d, %Y",
]

_ADDRESS_RE = re.compile(
    r"\d+\s+[A-Za-z0-9\s\.]+?"
    r"(?:street|st|avenue|ave|boulevard|blvd|road|rd|drive|dr|"
    r"lane|ln|way|court|ct|place|pl|highway|hwy|parkway|pkwy|suite|ste)"
    r"[A-Za-z0-9\s\.,#\-]*(?:\d{5}(?:-\d{4})?)?",
    re.IGNORECASE,
)


def _parse_amount(text: str) -> float | None:
    """Extract grand total (what was paid)."""
    for pattern in _TOTAL_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    # Fallback: largest dollar-style amount in the document
    candidates = re.findall(r"\$?\s*(\d{1,6}[.,]\d{2})", text)
    if candidates:
        try:
            return max(float(v.replace(",", "")) for v in candidates)
        except ValueError:
            pass
    return None


def _parse_subtotal(text: str) -> float | None:
    """Extract pre-tax sale amount (subtotal)."""
    for pattern in _SUBTOTAL_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _parse_tax(text: str) -> float | None:
    """Extract tax amount."""
    for pattern in _TAX_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                # Sanity-check: a tax amount almost never exceeds 9999
                if val <= 9999:
                    return val
            except ValueError:
                continue
    return None


def _parse_date(lines: list[str]) -> datetime | None:
    for line in lines:
        for pat in _DATE_PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            raw = m.group(1).strip()
            for fmt in _DATE_FORMATS:
                try:
                    parsed = datetime.strptime(raw, fmt)
                    if parsed.year >= 2000:
                        return parsed
                except ValueError:
                    continue
    return None


def _classify_category(text: str, store_name: str) -> str:
    combined = (text + " " + (store_name or "")).lower()
    for category, keywords in _CATEGORIES.items():
        if any(kw in combined for kw in keywords):
            return category
    return "General Purchase"


def _extract_receipt_data(lines: list[str]) -> dict:
    """
    Parse structured fields from raw EasyOCR output lines.

    Returns a dict with keys:
      storeName, storeAddress, subtotalAmount, taxAmount, totalAmount, currency,
      transactionType ("debit" | "credit"), description, receiptDate, rawText
    """
    full_text = "\n".join(lines)

    # ── Store name ────────────────────────────────────────────────────────────
    # Typically the first readable line near the top of the receipt.
    store_name = None
    for line in lines[:8]:
        line = line.strip()
        if (
            len(line) >= 3
            and not re.match(r"^\d", line)
            and not re.match(r"^\$", line)
            and not re.match(r"^[\d\s()\-+.]+$", line)   # pure phone/number
            and "http" not in line.lower()
        ):
            store_name = line.strip()
            break

    # ── Store address ─────────────────────────────────────────────────────────
    store_address = None
    m = _ADDRESS_RE.search(full_text)
    if m:
        store_address = re.sub(r"\s{2,}", " ", m.group(0)).strip()

    # ── Subtotal (pre-tax sale amount) ────────────────────────────────────────
    subtotal_amount = _parse_subtotal(full_text)

    # ── Tax amount ────────────────────────────────────────────────────────────
    tax_amount = _parse_tax(full_text)

    # ── Grand total (what was actually paid) ──────────────────────────────────
    total_amount = _parse_amount(full_text)

    # If we have subtotal + tax but no grand total, derive it
    if total_amount is None and subtotal_amount is not None and tax_amount is not None:
        total_amount = round(subtotal_amount + tax_amount, 2)

    # ── Transaction type: credit ONLY when the receipt total is negative ───────
    # A negative total is printed as -$23.21, -23.21, ($23.21), or (23.21).
    # Words like "return" or "refund" elsewhere on the receipt are ignored.
    is_negative_on_receipt = bool(_NEGATIVE_TOTAL_RE.search(full_text))
    transaction_type = "credit" if is_negative_on_receipt else "debit"

    # Store negative values for credit receipts so arithmetic is consistent
    if transaction_type == "credit":
        if total_amount is not None and total_amount > 0:
            total_amount = -total_amount
        if subtotal_amount is not None and subtotal_amount > 0:
            subtotal_amount = -subtotal_amount
        if tax_amount is not None and tax_amount > 0:
            tax_amount = -tax_amount

    # ── Currency ──────────────────────────────────────────────────────────────
    currency = "USD"
    if re.search(r"£|\bGBP\b", full_text):
        currency = "GBP"
    elif re.search(r"€|\bEUR\b", full_text):
        currency = "EUR"
    elif re.search(r"₦|\bNGN\b|\bnaira\b", full_text, re.IGNORECASE):
        currency = "NGN"
    elif re.search(r"₹|\bINR\b|\brupee\b", full_text, re.IGNORECASE):
        currency = "INR"
    elif re.search(r"\bCAD\b|C\$", full_text):
        currency = "CAD"

    # ── Receipt date ──────────────────────────────────────────────────────────
    receipt_date = _parse_date(lines)

    # ── Category / description ────────────────────────────────────────────────
    description = _classify_category(full_text, store_name or "")

    return {
        "storeName":       store_name,
        "storeAddress":    store_address,
        "subtotalAmount":  subtotal_amount,
        "taxAmount":       tax_amount,
        "totalAmount":     total_amount,
        "currency":        currency,
        "transactionType": transaction_type,
        "description":     description,
        "receiptDate":     receipt_date,
        "rawText":         full_text,
    }


# ── Serialiser ────────────────────────────────────────────────────────────────

def _serialize(doc: dict) -> dict:
    doc["id"]         = str(doc.pop("_id"))
    doc["userId"]     = str(doc.get("userId", ""))
    prop_oid          = doc.get("propertyId")
    doc["propertyId"] = str(prop_oid) if prop_oid else ""
    doc.pop("imagePath", None)   # never expose internal disk path
    for key in ("receiptDate", "createdAt", "updatedAt"):
        if isinstance(doc.get(key), datetime):
            doc[key] = doc[key].isoformat()
    return doc


# ── MongoDB indexes ───────────────────────────────────────────────────────────

def ensure_receipt_indexes(db):
    db.receipt.create_index("userId")
    db.receipt.create_index("createdAt")
    db.receipt.create_index("transactionType")
    db.receipt.create_index("propertyId", sparse=True)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@receipts_bp.route("/", methods=["POST"])
def upload_receipt():
    """
    Upload a receipt image. Runs OCR and stores extracted data + image.

    Multipart/form-data file key: "receipt"

    Returns 201 with the full created receipt document, including:
      storeName, storeAddress, totalAmount, currency,
      transactionType ("debit" | "credit"), description, receiptDate, imageUrl
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    # Accept either key="receipt" or the first uploaded file
    file = request.files.get("receipt") or next(iter(request.files.values()), None)
    if not file or not file.filename:
        return jsonify({"success": False, "message": "No file provided under key 'receipt'"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({
            "success": False,
            "message": f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        }), 400

    raw_bytes = file.read()
    if len(raw_bytes) > MAX_FILE_SIZE:
        return jsonify({"success": False, "message": "File exceeds 15 MB limit"}), 413

    # ── Save original image ───────────────────────────────────────────────────
    user_oid = _parse_oid(user_id_str)
    ts       = int(_now().timestamp() * 1000)
    filename = f"{user_id_str}_{ts}.{ext}"
    base_dir = _receipts_dir()
    save_path = os.path.join(base_dir, filename)

    if not _safe_realpath(save_path, base_dir):
        return jsonify({"success": False, "message": "Invalid file path"}), 400

    with open(save_path, "wb") as f:
        f.write(raw_bytes)

    image_hash = hashlib.sha256(raw_bytes).hexdigest()

    # ── OCR ──────────────────────────────────────────────────────────────────
    ocr_lines = []
    ocr_error = None
    try:
        reader       = _get_reader()
        img_array    = _preprocess_image(raw_bytes)
        raw_results  = reader.readtext(img_array, detail=0, paragraph=False)
        ocr_lines    = [str(r).strip() for r in raw_results if str(r).strip()]
    except Exception as exc:
        ocr_error = str(exc)

    # ── Extract structured fields ─────────────────────────────────────────────
    if ocr_lines:
        extracted = _extract_receipt_data(ocr_lines)
    else:
        extracted = {
            "storeName": None, "storeAddress": None,
            "subtotalAmount": None, "taxAmount": None, "totalAmount": None,
            "currency": "USD", "transactionType": "debit",
            "description": "General Purchase", "receiptDate": None, "rawText": "",
        }

    now = _now()
    print(extracted)
    receipt_doc = {
        "userId":          user_oid,
        "propertyId":      _parse_oid(request.form.get("propertyId") or "") or None,
        "imagePath":       save_path,
        "imageUrl":        None,          # filled in after insert gives us the _id
        "imageHash":       image_hash,
        "storeName":       extracted["storeName"],
        "storeAddress":    extracted["storeAddress"],
        "subtotalAmount":  extracted["subtotalAmount"],
        "taxAmount":       extracted["taxAmount"],
        "totalAmount":     extracted["totalAmount"],
        "currency":        extracted["currency"],
        "transactionType": extracted["transactionType"],
        "description":     extracted["description"],
        "receiptDate":     extracted["receiptDate"],
        "rawText":         extracted["rawText"],
        "ocrError":        ocr_error,
        "createdAt":       now,
        "updatedAt":       now,
    }

    result  = current_app.db.receipt.insert_one(receipt_doc)
    doc_id  = str(result.inserted_id)
    img_url = f"/api/receipts/{doc_id}/image"

    current_app.db.receipt.update_one(
        {"_id": result.inserted_id},
        {"$set": {"imageUrl": img_url}},
    )

    receipt_doc["_id"]      = result.inserted_id
    receipt_doc["imageUrl"] = img_url

    from .activities import _log_activity
    _log_activity(current_app.db, user_oid, "RECEIPT_UPLOADED",
                  {"receiptId": doc_id, "totalAmount": extracted.get("totalAmount"),
                   "currency": extracted.get("currency"), "storeName": extracted.get("storeName")})
    try:
        from .transactions import create_transaction_for_receipt
        create_transaction_for_receipt(receipt_doc, current_app.db)
    except Exception as e:
        current_app.logger.error("Failed to create transaction for receipt %s: %s", result.inserted_id, e)

    return jsonify({"success": True, "data": _serialize(receipt_doc)}), 201


@receipts_bp.route("/", methods=["GET"])
def list_receipts():
    """
    List receipts for the current user.

    Query params:
      - type    "debit" | "credit"  (optional filter)
      - page    int  (default 1)
      - limit   int  (default 20, max 100)

    The response includes imageUrl for each receipt so the frontend can display
    the image directly (e.g. <Image source={{uri: imageUrl}} />).
    rawText is excluded from the list view to keep payloads small.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id_str)
    db       = current_app.db

    filt = {"userId": user_oid}
    txn_type = (request.args.get("type") or "").lower()
    if txn_type in ("debit", "credit"):
        filt["transactionType"] = txn_type

    try:
        page  = max(1, int(request.args.get("page",  1)))
        limit = min(100, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        return jsonify({"success": False, "message": "page and limit must be integers"}), 400

    skip  = (page - 1) * limit
    docs  = list(
        db.receipt
        .find(filt, {"rawText": 0, "imagePath": 0})
        .sort("createdAt", -1)
        .skip(skip)
        .limit(limit)
    )
    total = db.receipt.count_documents(filt)

    return jsonify({
        "success": True,
        "data": [_serialize(d) for d in docs],
        "pagination": {
            "page": page, "limit": limit, "total": total,
            "pages": (total + limit - 1) // limit,
        },
    }), 200


@receipts_bp.route("/<receipt_id>", methods=["GET"])
def get_receipt(receipt_id):
    """
    Get a single receipt with all extracted fields (rawText included for debugging).
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(receipt_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid receipt ID"}), 400

    doc = current_app.db.receipt.find_one(
        {"_id": oid, "userId": _parse_oid(user_id_str)},
        {"imagePath": 0},
    )
    if not doc:
        return jsonify({"success": False, "message": "Receipt not found"}), 404

    return jsonify({"success": True, "data": _serialize(doc)}), 200


@receipts_bp.route("/<receipt_id>", methods=["DELETE"])
def delete_receipt(receipt_id):
    """Delete a receipt and remove its stored image from disk."""
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(receipt_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid receipt ID"}), 400

    doc = current_app.db.receipt.find_one(
        {"_id": oid, "userId": _parse_oid(user_id_str)},
    )
    if not doc:
        return jsonify({"success": False, "message": "Receipt not found"}), 404

    img_path = doc.get("imagePath")
    if img_path and os.path.isfile(img_path):
        base = os.path.abspath(
            os.path.join(current_app.root_path, "..", "uploads", "Receipts")
        )
        if _safe_realpath(img_path, base):
            os.remove(img_path)

    current_app.db.receipt.delete_one({"_id": oid})
    return jsonify({"success": True, "message": "Receipt deleted"}), 200


@receipts_bp.route("/<receipt_id>", methods=["PATCH"])
def update_receipt(receipt_id):
    """
    Correct OCR-extracted fields on a receipt.

    Only the fields listed below are accepted — everything else (userId, imageUrl,
    imagePath, imageHash, ocrError, createdAt) is immutable.

    JSON body (all fields optional — only send what you want to change):
      - storeName        (string)
      - storeAddress     (string)
      - subtotalAmount   (number)
      - taxAmount        (number)
      - totalAmount      (number)
      - currency         (string, e.g. "USD")
      - transactionType  ("debit" | "credit")
      - description      (string)
      - receiptDate      (ISO-8601 string, e.g. "2026-03-15T00:00:00")

    Returns the full updated receipt document.
    """
    user_id_str, err = decode_token(request)
    if err:
        return err

    oid = _parse_oid(receipt_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid receipt ID"}), 400

    doc = current_app.db.receipt.find_one(
        {"_id": oid, "userId": _parse_oid(user_id_str)},
        {"imagePath": 0},
    )
    if not doc:
        return jsonify({"success": False, "message": "Receipt not found"}), 404

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"success": False, "message": "No fields provided to update"}), 400

    # Whitelist of user-editable fields
    EDITABLE = {
        "storeName", "storeAddress", "subtotalAmount",
        "taxAmount", "totalAmount", "currency",
        "transactionType", "description", "receiptDate",
        "propertyId",
    }

    updates = {}
    errors  = []

    for key, value in data.items():
        if key not in EDITABLE:
            continue   # silently ignore non-editable fields

        if key == "transactionType":
            if value not in ("debit", "credit"):
                errors.append("transactionType must be 'debit' or 'credit'")
                continue
            updates[key] = value

        elif key in ("subtotalAmount", "taxAmount", "totalAmount"):
            if value is None:
                updates[key] = None
            else:
                try:
                    updates[key] = float(value)
                except (TypeError, ValueError):
                    errors.append(f"{key} must be a number")

        elif key == "receiptDate":
            if value is None:
                updates[key] = None
            else:
                parsed_date = None
                for fmt in _DATE_FORMATS:
                    try:
                        parsed_date = datetime.strptime(str(value).strip(), fmt)
                        break
                    except ValueError:
                        continue
                # Also accept ISO-8601 with time component
                if parsed_date is None:
                    try:
                        parsed_date = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
                    except ValueError:
                        pass
                if parsed_date is None:
                    errors.append(
                        "receiptDate must be a valid date string "
                        "(e.g. '2026-03-15' or '2026-03-15T00:00:00')"
                    )
                else:
                    updates[key] = parsed_date

        elif key == "currency":
            updates[key] = str(value).upper().strip()[:3]

        elif key == "propertyId":
            updates[key] = _parse_oid(value or "") or None

        else:
            # storeName, storeAddress, description
            updates[key] = str(value).strip() if value is not None else None

    if errors:
        return jsonify({"success": False, "message": "; ".join(errors)}), 400

    if not updates:
        return jsonify({"success": False, "message": "No valid fields provided to update"}), 400

    updates["updatedAt"] = _now()

    current_app.db.receipt.update_one({"_id": oid}, {"$set": updates})

    updated = current_app.db.receipt.find_one({"_id": oid}, {"imagePath": 0})
    return jsonify({"success": True, "data": _serialize(updated)}), 200


@receipts_bp.route("/<receipt_id>/image", methods=["GET"])
def get_receipt_image(receipt_id):
    """
    Serve the original receipt image file.

    No auth header required — the opaque ObjectId URL acts as a capability.
    This allows React Native <Image source={{uri: imageUrl}} /> to work without
    adding Authorization headers to every image request.
    """
    oid = _parse_oid(receipt_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid receipt ID"}), 400

    doc = current_app.db.receipt.find_one({"_id": oid}, {"imagePath": 1})
    if not doc:
        return jsonify({"success": False, "message": "Receipt not found"}), 404

    img_path = doc.get("imagePath")
    if not img_path or not os.path.isfile(img_path):
        return jsonify({"success": False, "message": "Image file not found on server"}), 404

    base = os.path.abspath(
        os.path.join(current_app.root_path, "..", "uploads", "Receipts")
    )
    if not _safe_realpath(img_path, base):
        return jsonify({"success": False, "message": "Forbidden"}), 403

    ext = img_path.rsplit(".", 1)[-1].lower()
    mime_map = {
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
        "png":  "image/png",
        "webp": "image/webp",
        "heic": "image/heic",
    }
    return send_file(img_path, mimetype=mime_map.get(ext, "application/octet-stream"))
