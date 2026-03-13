from datetime import date, datetime, timedelta, timezone
from flask import Blueprint, request, jsonify, current_app
from werkzeug.security import check_password_hash, generate_password_hash
from bson import ObjectId
import jwt
from .cache import cache

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def decode_token(request):
    """
    Decode the Bearer JWT from the Authorization header.
    Returns (user_id_str, None) on success or (None, error_response_tuple) on failure.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, (jsonify({"success": False, "message": "Authorization token required"}), 401)
    token = auth_header.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
        return payload["sub"], None
    except jwt.ExpiredSignatureError:
        return None, (jsonify({"success": False, "message": "Token has expired"}), 401)
    except jwt.InvalidTokenError:
        return None, (jsonify({"success": False, "message": "Invalid token"}), 401)


@cache.memoize(timeout=300)
def _get_user_by_email(email):
    """Cached user lookup by email. Returns user dict with _id as str."""
    user = current_app.db.user.find_one({"email": email})
    if user is None:
        return None
    user["_id"] = str(user["_id"])
    return user


@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    password = data.get("password") or ""
    dob_raw = data.get("dateOfBirth") or data.get("dob") or ""

    # Required field check
    if not email:
        return jsonify({"success": False, "message": "Email is required"}), 400

    db = current_app.db

    # Duplicate email check
    if db.user.find_one({"email": email}):
        return jsonify({"success": False, "message": "Email already exists"}), 409

    # Duplicate phone check
    if phone and db.user.find_one({"phone": phone}):
        return jsonify({"success": False, "message": "Phone number already exists"}), 409

    # Age validation (must be 18+)
    if dob_raw:
        try:
            dob = datetime.strptime(dob_raw, "%Y-%m-%d").date()
            today = date.today()
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            if age < 18:
                return jsonify({"success": False, "message": "User must be at least 18 years old"}), 400
        except ValueError:
            return jsonify({"success": False, "message": "Invalid dateOfBirth format. Use YYYY-MM-DD"}), 400

    # Build the user document from all submitted fields, normalising email
    user_doc = {k: v for k, v in data.items() if k != "password"}
    user_doc["email"] = email
    if phone:
        user_doc["phone"] = phone

    # If a valid Bearer token is present, set createdBy to that user's ID.
    # This takes precedence over any createdBy value sent in the request body.
    token_user_id, _ = decode_token(request)
    if token_user_id:
        user_doc["createdBy"] = ObjectId(token_user_id)
    elif user_doc.get("createdBy"):
        # No token, but a createdBy was explicitly sent — coerce it to ObjectId
        try:
            user_doc["createdBy"] = ObjectId(str(user_doc["createdBy"]))
        except Exception:
            return jsonify({"success": False, "message": "Invalid createdBy value"}), 400

    result = db.user.insert_one(user_doc)
    user_id = result.inserted_id

    # Store hashed password only if one was provided
    if password:
        db.password.insert_one({
            "user_id": user_id,
            "password_hash": generate_password_hash(password),
        })

    return jsonify({
        "success": True,
        "message": "User created successfully",
        "data": {
            "userId": str(user_id),
            "firstName": user_doc.get("firstName"),
            "lastName": user_doc.get("lastName"),
            "email": email,
        }
    }), 201


@auth_bp.route("/tenants", methods=["GET"])
def list_tenants():
    """
    Return all users with userType 'tenant' that were created by the
    authenticated landlord (matched via the createdBy field).

    Requires: Bearer token.

    GET /api/auth/tenants
    """
    landlord_id, err = decode_token(request)
    if err:
        return err

    tenants = list(
        current_app.db.user.find(
            {"createdBy": ObjectId(landlord_id)}
        )
    )

    for t in tenants:
        t["_id"] = str(t["_id"])
        if "createdBy" in t:
            t["createdBy"] = str(t["createdBy"])
        if "propertyId" in t:
            t["propertyId"] = str(t["propertyId"])
    print(f"Found {len(tenants)} tenants for landlord {landlord_id}")

    return jsonify({"success": True, "data": tenants}), 200


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email:
        return jsonify({"success": False, "message": "Email is required"}), 400

    db = current_app.db

    # Cached user lookup by email
    user = _get_user_by_email(email)
    if not user:
        return jsonify({"success": False, "message": "Invalid email or password"}), 401

    # Look up the password record linked to this user (not cached — security sensitive)
    password_record = db.password.find_one({"user_id": ObjectId(user["_id"])})
    if not password_record:
        return jsonify({"success": False, "message": "No password found"}), 401

    if not password:
        return jsonify({"success": False, "message": "Password is required"}), 400

    # Verify the submitted password against the stored hash
    if not check_password_hash(password_record["password_hash"], password):
        return jsonify({"success": False, "message": "Invalid email or password"}), 401

    # Build user object (all fields except internal MongoDB _id),
    # converting any ObjectId values to strings so they are JSON-serialisable.
    user_data = {
        k: str(v) if isinstance(v, ObjectId) else v
        for k, v in user.items()
        if k != "_id"
    }

    # Generate JWT token
    payload = {
        "sub": str(user["_id"]),
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    token = jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")

    return jsonify({
        "user": user_data,
        "token": token,
    }), 200


@auth_bp.route("/change-password", methods=["POST"])
def change_password():
    data = request.get_json(silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    new_password = data.get("newPassword") or ""

    if not email or not new_password:
        return jsonify({"success": False, "message": "Email and newPassword are required"}), 400

    db = current_app.db

    # Cached user lookup
    user = _get_user_by_email(email)
    if not user:
        return jsonify({"success": False, "message": "No account found with that email"}), 404

    new_hash = generate_password_hash(new_password)

    # Update existing password record, or insert one if it doesn't exist yet
    db.password.update_one(
        {"user_id": ObjectId(user["_id"])},
        {"$set": {"password_hash": new_hash}},
        upsert=True,
    )

    # Invalidate the cached user entry so stale data isn't served
    cache.delete_memoized(_get_user_by_email, email)

    return jsonify({"success": True, "message": "Password updated successfully"}), 200


@auth_bp.route("/deleteUser", methods=["DELETE"])
def delete_user():
    data = request.get_json(silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"success": False, "message": "Email and password are required"}), 400

    db = current_app.db

    # Fetch user directly (bypass cache for security-sensitive deletion)
    user = db.user.find_one({"email": email})
    if not user:
        return jsonify({"success": False, "message": "Invalid email or password"}), 401

    # Verify password before allowing deletion
    password_record = db.password.find_one({"user_id": user["_id"]})
    if not password_record or not check_password_hash(password_record["password_hash"], password):
        return jsonify({"success": False, "message": "Invalid email or password"}), 401

    user_id = user["_id"]

    # Wipe all user-related data
    db.user.delete_one({"_id": user_id})
    db.password.delete_one({"user_id": user_id})

    # Flush the cached entry for this user
    cache.delete_memoized(_get_user_by_email, email)

    return jsonify({"success": True, "message": "User account deleted successfully"}), 200
