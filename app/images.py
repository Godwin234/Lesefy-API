import os
import re
from flask import Blueprint, request, jsonify, send_file, current_app
from .cache import cache

images_bp = Blueprint("images", __name__, url_prefix="/api/images")

# ── Allowed picture types and their storage sub-folders ───────────────────────
PICTURE_TYPES = {
    "ProfilePicture",
    "MoveInPictures",
    "MoveOutPictures",
    "RecieptPictures",
    "ListingPictures",
    "MaintenancePictures",
}

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic"}

MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uploads_root():
    """Absolute path to the top-level uploads/ directory."""
    return os.path.abspath(os.path.join(current_app.root_path, "..", "uploads"))


def _type_dir(picture_type):
    """Return (and create if needed) the folder for a given picture type."""
    path = os.path.join(_uploads_root(), picture_type)
    os.makedirs(path, exist_ok=True)
    return path


def _allowed_ext(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _sanitize(value):
    """Replace any non-alphanumeric/underscore/hyphen chars with underscores."""
    return re.sub(r"[^\w\-]", "_", value)


def _build_filename(email, imagename, ext):
    """
    Construct the storage filename.
    Format:  <sanitized_email>__<sanitized_imagename>.<ext>
    """
    return f"{_sanitize(email.lower())}__{_sanitize(imagename)}.{ext.lower()}"


def _safe_realpath(path, base_dir):
    """Return True only if *path* resolves to a location inside *base_dir*."""
    return os.path.realpath(path).startswith(os.path.realpath(base_dir))


@cache.memoize(timeout=300)
def _find_file(picture_type, email, imagename):
    """
    Search the picture-type folder for a file matching email + imagename
    (any allowed extension).  Returns the absolute path or None.
    Cached for 5 minutes.
    """
    type_dir = os.path.join(
        os.path.abspath(
            os.path.join(current_app.root_path, "..", "uploads")
        ),
        picture_type,
    )
    if not os.path.isdir(type_dir):
        return None
    prefix = f"{_sanitize(email.lower())}__{_sanitize(imagename)}."
    for fname in os.listdir(type_dir):
        if fname.startswith(prefix):
            full = os.path.join(type_dir, fname)
            if _safe_realpath(full, type_dir):
                return full
    return None


def _invalidate(picture_type, email, imagename):
    cache.delete_memoized(_find_file, picture_type, email, imagename)


def _get_uploaded_file(picture_type):
    """
    Find the uploaded file in request.files.
    Tries the camelCase key derived from picture_type first
    (e.g. ProfilePicture -> profilePicture), then falls back to any file.
    """
    camel_key = picture_type[0].lower() + picture_type[1:]
    if camel_key in request.files and request.files[camel_key].filename:
        return request.files[camel_key]
    for f in request.files.values():
        if f.filename:
            return f
    return None


def _validate_type(picture_type):
    if picture_type not in PICTURE_TYPES:
        return jsonify({
            "success": False,
            "message": f"Unknown picture type '{picture_type}'. "
                       f"Allowed: {', '.join(sorted(PICTURE_TYPES))}",
        }), 400
    return None


def _require_form_fields(*fields):
    missing = [f for f in fields if not (request.form.get(f) or "").strip()]
    if missing:
        return jsonify({"success": False, "message": f"Missing fields: {', '.join(missing)}"}), 400
    return None


# ── Save ─────────────────────────────────────────────────────────────────────

@images_bp.route("/<picture_type>/save", methods=["POST"])
def save_picture(picture_type):
    """
    Upload a new image.

    Form fields:
      - email      (string)
      - imagename  (string, full filename incl. extension, e.g. "photo.jpg")
      - <camelCaseType>  (multipart file, e.g. 'profilePicture' for ProfilePicture)

    Example:
      POST /api/images/ProfilePicture/save
    """
    err = _validate_type(picture_type)
    if err:
        return err

    err = _require_form_fields("email", "imagename")
    if err:
        return err

    file = _get_uploaded_file(picture_type)
    if not file:
        return jsonify({"success": False, "message": "No file provided"}), 400

    imagename_raw = request.form["imagename"].strip()
    if "." in imagename_raw:
        imagename_base, ext = imagename_raw.rsplit(".", 1)
        ext = ext.lower()
    else:
        imagename_base = imagename_raw
        mime = (file.content_type or "").lower()
        mime_to_ext = {
            "image/jpeg": "jpeg",
            "image/jpg": "jpeg",
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
            "image/heic": "heic",
        }
        ext = mime_to_ext.get(mime, "jpeg")

    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({
            "success": False,
            "message": f"File type not allowed. Allowed extensions: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        }), 415

    email = request.form["email"].strip().lower()
    imagename = imagename_base
    filename = _build_filename(email, imagename, ext)
    save_path = os.path.join(_type_dir(picture_type), filename)

    if not _safe_realpath(save_path, _type_dir(picture_type)):
        return jsonify({"success": False, "message": "Invalid file path"}), 400

    if os.path.exists(save_path):
        return jsonify({
            "success": False,
            "message": "Image already exists. Use the update endpoint to replace it.",
        }), 409

    file.save(save_path)
    _invalidate(picture_type, email, imagename)

    return jsonify({
        "success": True,
        "message": "Image saved successfully",
        "data": {"filename": filename, "pictureType": picture_type},
    }), 201


# ── Get ───────────────────────────────────────────────────────────────────────

@images_bp.route("/<picture_type>/get", methods=["GET"])
def get_picture(picture_type):
    """
    Retrieve an image file.

    Query params:
      - email
      - imagename

    Example:
      GET /api/images/ProfilePicture/get?email=jane@example.com&imagename=avatar
    """
    err = _validate_type(picture_type)
    if err:
        return err

    email = (request.args.get("email") or "").strip().lower()
    imagename = (request.args.get("imagename") or "").strip()
    uri = (request.args.get("uri") or "").strip()

    if uri and not imagename:
        imagename = uri.rsplit(".", 1)[0] if "." in uri else uri

    if not email or not imagename:
        return jsonify({"success": False, "message": "email and imagename query params are required"}), 400

    file_path = _find_file(picture_type, email, imagename)
    if not file_path or not os.path.exists(file_path):
        return jsonify({"success": False, "message": "Image not found"}), 404

    return send_file(file_path)


# ── Update ────────────────────────────────────────────────────────────────────

@images_bp.route("/<picture_type>/update", methods=["POST", "PUT"])
def update_picture(picture_type):
    """
    Replace an existing image with a new file.

    Form fields:
      - email
      - imagename  (full filename incl. extension, e.g. "photo.jpg")
      - <camelCaseType>  (multipart file, e.g. 'profilePicture' for ProfilePicture)

    Example:
      POST /api/images/ProfilePicture/update
    """
    err = _validate_type(picture_type)
    if err:
        return err

    err = _require_form_fields("email", "imagename")
    if err:
        return err

    file = _get_uploaded_file(picture_type)
    if not file:
        return jsonify({"success": False, "message": "No file provided"}), 400

    imagename_raw = request.form["imagename"].strip()
    if "." in imagename_raw:
        imagename_base, ext = imagename_raw.rsplit(".", 1)
        ext = ext.lower()
    else:
        # No extension in imagename — derive from the uploaded file's MIME type
        imagename_base = imagename_raw
        mime = (file.content_type or "").lower()
        mime_to_ext = {
            "image/jpeg": "jpeg",
            "image/jpg": "jpeg",
            "image/png": "png",
            "image/gif": "gif",
            "image/webp": "webp",
            "image/heic": "heic",
        }
        ext = mime_to_ext.get(mime, "jpeg")

    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({
            "success": False,
            "message": f"File type not allowed. Allowed extensions: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        }), 415

    email = request.form["email"].strip().lower()
    imagename = imagename_base

    # Remove the old file (may have a different extension)
    old_path = _find_file(picture_type, email, imagename)
    if old_path and os.path.exists(old_path):
        os.remove(old_path)

    filename = _build_filename(email, imagename, ext)
    save_path = os.path.join(_type_dir(picture_type), filename)

    if not _safe_realpath(save_path, _type_dir(picture_type)):
        return jsonify({"success": False, "message": "Invalid file path"}), 400

    file.save(save_path)
    _invalidate(picture_type, email, imagename)

    return jsonify({
        "success": True,
        "message": "Image updated successfully",
        "data": {"filename": filename, "pictureType": picture_type},
    }), 200


# ── Delete ────────────────────────────────────────────────────────────────────

@images_bp.route("/<picture_type>/delete", methods=["DELETE"])
def delete_picture(picture_type):
    """
    Delete an image.

    JSON body or query params:
      - email
      - imagename

    Example:
      DELETE /api/images/ProfilePicture/delete
      { "email": "jane@example.com", "imagename": "avatar" }
    """
    err = _validate_type(picture_type)
    if err:
        return err

    body = request.get_json(silent=True) or {}
    email = (body.get("email") or request.args.get("email") or "").strip().lower()
    imagename = (body.get("imagename") or request.args.get("imagename") or "").strip()
    uri = (body.get("uri") or request.args.get("uri") or "").strip()

    if uri and not imagename:
        imagename = uri.rsplit(".", 1)[0] if "." in uri else uri

    if not email or not imagename:
        return jsonify({"success": False, "message": "email and imagename are required"}), 400

    file_path = _find_file(picture_type, email, imagename)
    if not file_path or not os.path.exists(file_path):
        return jsonify({"success": False, "message": "Image not found"}), 404

    os.remove(file_path)
    _invalidate(picture_type, email, imagename)

    return jsonify({"success": True, "message": "Image deleted successfully"}), 200


# ── List ──────────────────────────────────────────────────────────────────────

@images_bp.route("/<picture_type>/list", methods=["GET"])
def list_pictures(picture_type):
    """
    List all stored image filenames for a given email under a picture type.

    Query params:
      - email

    Example:
      GET /api/images/MoveInPictures/list?email=jane@example.com
    """
    err = _validate_type(picture_type)
    if err:
        return err

    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"success": False, "message": "email query param is required"}), 400

    type_dir = os.path.join(_uploads_root(), picture_type)
    if not os.path.isdir(type_dir):
        return jsonify({"success": True, "data": []}), 200

    prefix = f"{_sanitize(email)}__"
    files = sorted(f for f in os.listdir(type_dir) if f.startswith(prefix))

    return jsonify({"success": True, "data": files}), 200
