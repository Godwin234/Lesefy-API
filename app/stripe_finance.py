"""
Stripe Financial Connections & Payments
────────────────────────────────────────

Allows landlords and tenants to:
  - Link bank accounts via Stripe Financial Connections (OAuth + non-OAuth)
  - (Tenants) Pay rent via card, US bank account (ACH), or other Stripe methods

Flow
────
  1. Client calls POST /api/stripe/customer   → creates/retrieves Stripe Customer
  2. Client calls POST /api/stripe/financial-connections/session
       → server creates a Financial Connections Session, returns client_secret
  3. Client launches the Stripe.js authentication flow using client_secret
  4. After the flow completes, client calls
       POST /api/stripe/financial-connections/accounts/save  with the session_id
       → server fetches the linked accounts from Stripe and persists them
  5. For payments:
       a. POST /api/stripe/setup-intent   → save a payment method for future use
       b. POST /api/stripe/payment-intent → charge immediately
       c. GET  /api/stripe/payment-methods → list saved cards / bank accounts

MongoDB collection:  stripe_data
  user_id             ObjectId   (unique index)
  stripe_customer_id  str        Stripe Customer ID
  linked_accounts     list       Financial Connections account objects
  payment_methods     list       (informational — source of truth is Stripe)
  created_at          datetime
  updated_at          datetime

Endpoints
─────────
  POST   /api/stripe/customer
  POST   /api/stripe/financial-connections/session
  POST   /api/stripe/financial-connections/accounts/save
  GET    /api/stripe/financial-connections/accounts
  DELETE /api/stripe/financial-connections/accounts/<fc_account_id>
  POST   /api/stripe/setup-intent
  POST   /api/stripe/payment-intent
  GET    /api/stripe/payment-methods
  DELETE /api/stripe/payment-methods/<payment_method_id>
  POST   /api/stripe/webhook
"""

import json
import os
from datetime import datetime, timezone

import stripe
from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, current_app, jsonify, request

from .auth import decode_token

stripe_bp = Blueprint("stripe_finance", __name__, url_prefix="/api/stripe")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _parse_oid(value):
    try:
        return ObjectId(str(value).strip())
    except (InvalidId, TypeError):
        return None


def _configure_stripe():
    """Set stripe.api_key from environment. Raises RuntimeError if missing."""
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured in environment")
    stripe.api_key = key


def _stripe_error_message(exc):
    """Return a safe user-facing message from a StripeError."""
    msg = getattr(exc, "user_message", None)
    return str(msg) if msg else str(exc)


def _get_or_create_stripe_record(db, user_oid):
    """Return the stripe_data document for a user, creating it if absent."""
    record = db.stripe_data.find_one({"user_id": user_oid})
    if record is None:
        now = _now()
        result = db.stripe_data.insert_one(
            {
                "user_id": user_oid,
                "stripe_customer_id": None,
                "linked_accounts": [],
                "created_at": now,
                "updated_at": now,
            }
        )
        record = db.stripe_data.find_one({"_id": result.inserted_id})
    return record


def _build_customer_params(user_doc, user_oid):
    """Build Stripe Customer creation params from a user document."""
    params = {"metadata": {"user_id": str(user_oid)}}
    email = (user_doc.get("email") or "").strip()
    if email:
        params["email"] = email
    first = (user_doc.get("firstName") or "").strip()
    last = (user_doc.get("lastName") or "").strip()
    full_name = " ".join(p for p in [first, last] if p)
    if full_name:
        params["name"] = full_name
    return params


def _ensure_stripe_customer(db, user_oid, user_doc):
    """
    Return (customer_id, error_response).
    Looks up or creates a Stripe Customer, persisting the ID in stripe_data.
    """
    record = _get_or_create_stripe_record(db, user_oid)
    customer_id = record.get("stripe_customer_id")

    if customer_id:
        # Verify it still exists in Stripe
        try:
            _configure_stripe()
            stripe.Customer.retrieve(customer_id)
            return customer_id, None
        except stripe.error.InvalidRequestError:
            customer_id = None  # Customer deleted in Stripe — recreate

    # Create fresh customer
    try:
        _configure_stripe()
        customer = stripe.Customer.create(**_build_customer_params(user_doc, user_oid))
        customer_id = customer["id"]
        db.stripe_data.update_one(
            {"user_id": user_oid},
            {"$set": {"stripe_customer_id": customer_id, "updated_at": _now()}},
        )
        return customer_id, None
    except stripe.error.StripeError as exc:
        return None, (
            jsonify({"success": False, "message": _stripe_error_message(exc)}),
            502,
        )


def _serialize_account(a):
    a = dict(a)
    if isinstance(a.get("linked_at"), datetime):
        a["linked_at"] = a["linked_at"].isoformat()
    return a


# ── MongoDB index setup ───────────────────────────────────────────────────────

def ensure_stripe_indexes(db):
    db.stripe_data.create_index("user_id", unique=True)


# ═════════════════════════════════════════════════════════════════════════════
# Customer
# ═════════════════════════════════════════════════════════════════════════════

@stripe_bp.route("/customer", methods=["POST"])
def create_or_get_customer():
    """
    Create or retrieve a Stripe Customer for the authenticated user.
    A Stripe Customer ID is required before using Financial Connections or
    saving payment methods.
    """
    user_id, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id)
    if not user_oid:
        return jsonify({"success": False, "message": "Invalid user ID"}), 400

    db = current_app.db
    user = db.user.find_one({"_id": user_oid})
    if not user:
        return jsonify({"success": False, "message": "User not found"}), 404

    record = _get_or_create_stripe_record(db, user_oid)
    existing_customer_id = record.get("stripe_customer_id")

    _configure_stripe()

    if existing_customer_id:
        try:
            customer = stripe.Customer.retrieve(existing_customer_id)
            return jsonify(
                {
                    "success": True,
                    "customer_id": customer["id"],
                    "message": "Existing Stripe customer retrieved",
                }
            ), 200
        except stripe.error.InvalidRequestError:
            pass  # Customer deleted in Stripe — fall through to create

    # Create a new Stripe Customer
    try:
        customer = stripe.Customer.create(**_build_customer_params(user, user_oid))
    except stripe.error.StripeError as exc:
        return jsonify({"success": False, "message": _stripe_error_message(exc)}), 502

    db.stripe_data.update_one(
        {"user_id": user_oid},
        {"$set": {"stripe_customer_id": customer["id"], "updated_at": _now()}},
    )

    return jsonify(
        {
            "success": True,
            "customer_id": customer["id"],
            "message": "Stripe customer created",
        }
    ), 201


# ═════════════════════════════════════════════════════════════════════════════
# Financial Connections — Bank Account Linking
# ═════════════════════════════════════════════════════════════════════════════

@stripe_bp.route("/financial-connections/session", methods=["POST"])
def create_fc_session():
    """
    Create a Stripe Financial Connections Session.

    Returns a client_secret that the client uses to launch the Stripe.js
    authentication flow (collectFinancialConnectionsAccounts).

    Body (JSON, all optional):
      permissions  list[str]  Data permissions to request.
                              Allowed: "payment_method", "balances",
                                       "ownership", "transactions"
                              Default: ["payment_method", "balances"]
    """
    user_id, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id)
    if not user_oid:
        return jsonify({"success": False, "message": "Invalid user ID"}), 400

    db = current_app.db
    user = db.user.find_one({"_id": user_oid})
    if not user:
        return jsonify({"success": False, "message": "User not found"}), 404

    customer_id, err = _ensure_stripe_customer(db, user_oid, user)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    _allowed_perms = {"payment_method", "balances", "ownership", "transactions"}
    raw_perms = data.get("permissions", ["payment_method", "balances"])
    if not isinstance(raw_perms, list):
        return jsonify({"success": False, "message": "permissions must be a list"}), 400
    permissions = [p for p in raw_perms if p in _allowed_perms] or ["payment_method", "balances"]

    _configure_stripe()
    try:
        session = stripe.financial_connections.Session.create(
            account_holder={"type": "customer", "customer": customer_id},
            permissions=permissions,
        )
    except stripe.error.StripeError as exc:
        return jsonify({"success": False, "message": _stripe_error_message(exc)}), 502

    return jsonify(
        {
            "success": True,
            "client_secret": session["client_secret"],
            "session_id": session["id"],
        }
    ), 200


@stripe_bp.route("/financial-connections/accounts/save", methods=["POST"])
def save_fc_accounts():
    """
    After the client completes the Financial Connections authentication flow,
    call this endpoint with the session_id to persist the linked accounts in
    the database.

    Body (JSON):
      session_id  str  required  The Financial Connections Session ID (fcsess_…)
    """
    user_id, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id)
    if not user_oid:
        return jsonify({"success": False, "message": "Invalid user ID"}), 400

    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "message": "session_id is required"}), 400

    _configure_stripe()
    try:
        session = stripe.financial_connections.Session.retrieve(session_id)
    except stripe.error.InvalidRequestError:
        return jsonify({"success": False, "message": "Invalid or expired session_id"}), 400
    except stripe.error.StripeError as exc:
        return jsonify({"success": False, "message": _stripe_error_message(exc)}), 502

    accounts_data = (session.get("accounts") or {}).get("data") or []
    if not accounts_data:
        return jsonify(
            {"success": True, "linked_accounts": [], "message": "No accounts were linked"}
        ), 200

    db = current_app.db
    _get_or_create_stripe_record(db, user_oid)

    new_count = 0
    for acct in accounts_data:
        entry = {
            "fc_account_id": acct["id"],
            "institution_name": acct.get("institution_name") or "",
            "display_name": acct.get("display_name") or "",
            "last4": acct.get("last4") or "",
            "category": acct.get("category") or "",
            "subcategory": acct.get("subcategory") or "",
            "status": acct.get("status") or "",
            "balance": acct.get("balance"),
            "linked_at": _now(),
        }
        # Conditional push — only add if this fc_account_id is not already present
        result = db.stripe_data.update_one(
            {
                "user_id": user_oid,
                "linked_accounts.fc_account_id": {"$ne": entry["fc_account_id"]},
            },
            {"$push": {"linked_accounts": entry}, "$set": {"updated_at": _now()}},
        )
        if result.modified_count:
            new_count += 1

    record = db.stripe_data.find_one({"user_id": user_oid})
    linked = [_serialize_account(a) for a in record.get("linked_accounts", [])]

    return jsonify(
        {
            "success": True,
            "linked_accounts": linked,
            "new_accounts_added": new_count,
            "message": f"{new_count} new account(s) linked",
        }
    ), 200


@stripe_bp.route("/financial-connections/accounts", methods=["GET"])
def list_fc_accounts():
    """List all linked Financial Connections bank accounts for the current user."""
    user_id, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id)
    if not user_oid:
        return jsonify({"success": False, "message": "Invalid user ID"}), 400

    db = current_app.db
    record = db.stripe_data.find_one({"user_id": user_oid})
    if not record:
        return jsonify({"success": True, "linked_accounts": []}), 200

    linked = [_serialize_account(a) for a in record.get("linked_accounts", [])]
    return jsonify({"success": True, "linked_accounts": linked}), 200


@stripe_bp.route("/financial-connections/accounts/<fc_account_id>", methods=["DELETE"])
def disconnect_fc_account(fc_account_id):
    """
    Disconnect a linked Financial Connections bank account.
    Calls Stripe to revoke access and removes the account from the database.
    """
    user_id, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id)
    if not user_oid:
        return jsonify({"success": False, "message": "Invalid user ID"}), 400

    # Verify the account actually belongs to this user before disconnecting
    db = current_app.db
    record = db.stripe_data.find_one(
        {"user_id": user_oid, "linked_accounts.fc_account_id": fc_account_id}
    )
    if not record:
        return jsonify({"success": False, "message": "Account not found"}), 404

    _configure_stripe()
    try:
        stripe.financial_connections.Account.disconnect(fc_account_id)
    except stripe.error.InvalidRequestError:
        pass  # Already disconnected — continue to remove from DB
    except stripe.error.StripeError as exc:
        return jsonify({"success": False, "message": _stripe_error_message(exc)}), 502

    db.stripe_data.update_one(
        {"user_id": user_oid},
        {
            "$pull": {"linked_accounts": {"fc_account_id": fc_account_id}},
            "$set": {"updated_at": _now()},
        },
    )

    return jsonify({"success": True, "message": "Bank account disconnected"}), 200


# ═════════════════════════════════════════════════════════════════════════════
# Setup Intent — Save a Payment Method for Future Use
# ═════════════════════════════════════════════════════════════════════════════

@stripe_bp.route("/setup-intent", methods=["POST"])
def create_setup_intent():
    """
    Create a SetupIntent so the client can securely save a payment method
    (card or US bank account) for future off-session charges.

    NOTE: Cash App Pay is a redirect-based wallet and cannot be saved via
    SetupIntent — use /api/stripe/payment-intent for one-time Cash App payments.

    Body (JSON, all optional):
      payment_method_types  list[str]
          Allowed: "card", "us_bank_account"
          Default: ["card", "us_bank_account"]

    Frontend: initialise the Payment Element with
      wallets: { link: 'never' }
    to prevent Stripe from auto-surfacing Link based on customer email.
    """
    user_id, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id)
    if not user_oid:
        return jsonify({"success": False, "message": "Invalid user ID"}), 400

    db = current_app.db
    user = db.user.find_one({"_id": user_oid})
    if not user:
        return jsonify({"success": False, "message": "User not found"}), 404

    customer_id, err = _ensure_stripe_customer(db, user_oid, user)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    _allowed_types = {"card", "us_bank_account"}
    raw_types = data.get("payment_method_types", ["card", "us_bank_account"])
    pm_types = [t for t in raw_types if t in _allowed_types] or ["card", "us_bank_account"]

    _configure_stripe()
    try:
        setup_intent = stripe.SetupIntent.create(
            customer=customer_id,
            payment_method_types=pm_types,
            usage="off_session",
            # us_bank_account always needs these options for the Payment Element
            # to render it — setting them unconditionally is harmless for card-only.
            payment_method_options={
                "us_bank_account": {
                    "financial_connections": {
                        "permissions": ["payment_method"],
                    },
                    "verification_method": "automatic",
                }
            },
        )
    except stripe.error.StripeError as exc:
        return jsonify({"success": False, "message": _stripe_error_message(exc)}), 502

    return jsonify(
        {
            "success": True,
            "client_secret": setup_intent["client_secret"],
            "setup_intent_id": setup_intent["id"],
            # Tell the frontend to initialise the Payment Element with
            # wallets: { link: 'never' } to suppress Link.
        }
    ), 200


# ═════════════════════════════════════════════════════════════════════════════
# Payment Intent — Charge a Tenant (Immediate Payment)
# ═════════════════════════════════════════════════════════════════════════════

@stripe_bp.route("/payment-intent", methods=["POST"])
def create_payment_intent():
    """
    Create a PaymentIntent for a tenant paying rent or fees.

    Supported payment methods: card, us_bank_account (ACH), cashapp.
    Link is excluded — suppress it on the frontend with wallets: { link: 'never' }.

    Body (JSON):
      amount                int      required  Amount in smallest currency unit (e.g. cents for USD)
      currency              str      optional  default: "usd"
      payment_method_id     str      optional  Pre-existing saved payment method to use
      payment_method_types  list     optional  default: ["card", "us_bank_account", "cashapp"]
      property_id           str      optional  Stored in metadata for reconciliation
      description           str      optional  Shown on Stripe Dashboard / receipts
      confirm               bool     optional  Confirm immediately (requires payment_method_id)
                                               default: false
    """
    user_id, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id)
    if not user_oid:
        return jsonify({"success": False, "message": "Invalid user ID"}), 400

    data = request.get_json(silent=True) or {}

    amount = data.get("amount")
    if not isinstance(amount, int) or amount <= 0:
        return jsonify(
            {
                "success": False,
                "message": "amount must be a positive integer (smallest currency unit, e.g. cents)",
            }
        ), 400

    currency = (data.get("currency") or "usd").lower().strip()
    description = (data.get("description") or "").strip()
    property_id = (data.get("property_id") or "").strip()
    pm_id = (data.get("payment_method_id") or "").strip() or None
    confirm = bool(data.get("confirm", False))

    _allowed_types = {"card", "us_bank_account", "cashapp"}
    raw_types = data.get("payment_method_types", ["card", "us_bank_account", "cashapp"])
    pm_types = [t for t in raw_types if t in _allowed_types] or ["card", "us_bank_account", "cashapp"]

    db = current_app.db
    user = db.user.find_one({"_id": user_oid})
    if not user:
        return jsonify({"success": False, "message": "User not found"}), 404

    customer_id, err = _ensure_stripe_customer(db, user_oid, user)
    if err:
        return err

    intent_params = {
        "amount": amount,
        "currency": currency,
        "customer": customer_id,
        "payment_method_types": pm_types,
        "metadata": {
            "user_id": str(user_oid),
            "property_id": property_id,
        },
        # us_bank_account always needs financial_connections permissions for the
        # Payment Element to render the ACH option — harmless when not using ACH.
        "payment_method_options": {
            "us_bank_account": {
                "financial_connections": {
                    "permissions": ["payment_method"],
                },
                "verification_method": "automatic",
            }
        },
    }
    if description:
        intent_params["description"] = description
    if pm_id:
        intent_params["payment_method"] = pm_id
    if confirm:
        if not pm_id:
            return jsonify(
                {
                    "success": False,
                    "message": "payment_method_id is required when confirm is true",
                }
            ), 400
        intent_params["confirm"] = True
        intent_params["off_session"] = True

    _configure_stripe()
    try:
        intent = stripe.PaymentIntent.create(**intent_params)
    except stripe.error.CardError as exc:
        return jsonify({"success": False, "message": _stripe_error_message(exc)}), 402
    except stripe.error.StripeError as exc:
        return jsonify({"success": False, "message": _stripe_error_message(exc)}), 502

    return jsonify(
        {
            "success": True,
            "client_secret": intent["client_secret"],
            "payment_intent_id": intent["id"],
            "status": intent["status"],
            "amount": intent["amount"],
            "currency": intent["currency"],
            # Tell the frontend to initialise the Payment Element with
            # wallets: { link: 'never' } to suppress Link.
        }
    ), 200


# ═════════════════════════════════════════════════════════════════════════════
# Payment Methods — List & Detach
# ═════════════════════════════════════════════════════════════════════════════

@stripe_bp.route("/payment-methods", methods=["GET"])
def list_payment_methods():
    """
    List all saved payment methods (cards and US bank accounts) for the
    current user.

    Query params:
      type  str  optional  Filter by type: "card" | "us_bank_account"
                           Returns both if omitted.
    """
    user_id, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id)
    if not user_oid:
        return jsonify({"success": False, "message": "Invalid user ID"}), 400

    db = current_app.db
    record = db.stripe_data.find_one({"user_id": user_oid})
    customer_id = (record or {}).get("stripe_customer_id")
    if not customer_id:
        return jsonify({"success": True, "payment_methods": []}), 200

    filter_type = request.args.get("type", "").strip().lower()
    fetch_types = (
        [filter_type] if filter_type in {"card", "us_bank_account", "cashapp"} else ["card", "us_bank_account", "cashapp"]
    )

    _configure_stripe()
    all_methods = []
    try:
        for pm_type in fetch_types:
            page = stripe.PaymentMethod.list(customer=customer_id, type=pm_type)
            all_methods.extend(page.get("data", []))
    except stripe.error.StripeError as exc:
        return jsonify({"success": False, "message": _stripe_error_message(exc)}), 502

    def _serialize_pm(pm):
        out = {
            "id": pm["id"],
            "type": pm["type"],
            "created": pm.get("created"),
        }
        if pm["type"] == "card":
            card = pm.get("card") or {}
            out.update(
                {
                    "brand": card.get("brand"),
                    "last4": card.get("last4"),
                    "exp_month": card.get("exp_month"),
                    "exp_year": card.get("exp_year"),
                }
            )
        elif pm["type"] == "us_bank_account":
            bank = pm.get("us_bank_account") or {}
            out.update(
                {
                    "bank_name": bank.get("bank_name"),
                    "last4": bank.get("last4"),
                    "account_type": bank.get("account_type"),
                    "account_holder_type": bank.get("account_holder_type"),
                }
            )
        elif pm["type"] == "cashapp":
            cashapp = pm.get("cashapp") or {}
            out.update(
                {
                    "buyer_id": cashapp.get("buyer_id"),
                    "cashtag": cashapp.get("cashtag"),
                }
            )
        return out

    return jsonify(
        {"success": True, "payment_methods": [_serialize_pm(pm) for pm in all_methods]}
    ), 200


@stripe_bp.route("/payment-methods/<payment_method_id>", methods=["DELETE"])
def detach_payment_method(payment_method_id):
    """
    Detach (remove) a saved payment method from the current user's Stripe
    Customer.  The user can only detach methods that belong to their own
    Customer — ownership is verified before detaching.
    """
    user_id, err = decode_token(request)
    if err:
        return err

    user_oid = _parse_oid(user_id)
    if not user_oid:
        return jsonify({"success": False, "message": "Invalid user ID"}), 400

    db = current_app.db
    record = db.stripe_data.find_one({"user_id": user_oid})
    customer_id = (record or {}).get("stripe_customer_id")
    if not customer_id:
        return jsonify({"success": False, "message": "No payment methods found"}), 404

    _configure_stripe()
    try:
        pm = stripe.PaymentMethod.retrieve(payment_method_id)
        if pm.get("customer") != customer_id:
            # Ownership mismatch — treat as not found to avoid information leakage
            return jsonify({"success": False, "message": "Payment method not found"}), 404
        stripe.PaymentMethod.detach(payment_method_id)
    except stripe.error.InvalidRequestError:
        return jsonify({"success": False, "message": "Payment method not found"}), 404
    except stripe.error.StripeError as exc:
        return jsonify({"success": False, "message": _stripe_error_message(exc)}), 502

    return jsonify({"success": True, "message": "Payment method removed"}), 200


# ═════════════════════════════════════════════════════════════════════════════
# Webhook helpers
# ═════════════════════════════════════════════════════════════════════════════

def _trigger_tenant_payment_confirmation(
    tenant_oid, amount, currency, period,
    rent_status, property_address, rent_id, property_id, db, app
):
    """
    Send a payment-confirmation notification to the tenant after a successful
    Stripe charge:  "Your payment was received."
    """
    from .notifications import _send_fcm, _fmt_notification

    status_label = {"paid": "fully paid", "partial": "received (partial)"}.get(
        rent_status, "received"
    )
    title = f"Payment {status_label} – {property_address}"
    body  = (
        f"Your payment of {currency} {amount:,.2f} for {period} "
        f"has been {status_label}."
    )
    notif_type = "rent_payment_confirmation"

    try:
        from .socket import socketio as _socketio
    except Exception:
        _socketio = None

    notif_doc = {
        "userId":    tenant_oid,
        "type":      notif_type,
        "title":     title,
        "body":      body,
        "data": {
            "rentId":     rent_id,
            "propertyId": property_id,
            "period":     period,
            "amount":     str(amount),
            "currency":   currency,
            "status":     rent_status,
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


def _trigger_rent_payment_notification(
    recipient_oids, tenant_name, amount, currency, period,
    property_address, rent_id, property_id, db, app
):
    """
    Persist an in-app notification, emit a SocketIO event, and send FCM to
    each recipient (intended for the landlord) when rent is received.
    """
    from .notifications import _send_fcm, _fmt_notification

    title = f"Rent payment received – {property_address}"
    body = (
        f"{tenant_name} paid {currency} {amount:,.2f} "
        f"for {period} via bank transfer."
    )

    try:
        from .socket import socketio as _socketio
    except Exception:
        _socketio = None

    for recipient_oid in recipient_oids:
        notif_doc = {
            "userId": recipient_oid,
            "type": "rent_payment_received",
            "title": title,
            "body": body,
            "data": {
                "rentId": rent_id,
                "propertyId": property_id,
                "tenantName": tenant_name,
                "amount": str(amount),
                "currency": currency,
                "period": period,
            },
            "read": False,
            "createdAt": _now(),
        }
        result = db.notification.insert_one(notif_doc)
        notif_doc["_id"] = result.inserted_id

        if _socketio:
            try:
                _socketio.emit(
                    "notification",
                    _fmt_notification(notif_doc),
                    to=str(recipient_oid),
                )
            except Exception:
                pass

        token_docs = list(db.push_token.find({"userId": recipient_oid}, {"token": 1}))
        tokens = [t["token"] for t in token_docs if t.get("token")]
        if tokens:
            badge = db.notification.count_documents({"userId": recipient_oid, "read": False})
            _send_fcm(
                tokens, title, body,
                {"rentId": rent_id, "propertyId": property_id, "type": "rent_payment_received"},
                badge=badge,
            )


def _handle_charge_succeeded(charge, db, app):
    """
    Process a charge.succeeded webhook event.

    Delegates rent record upsert logic to rent.upsert_rent_from_charge so
    that partial-payment tracking lives in one place.

    Steps:
      1. Identify tenant via metadata.user_id or billing_details.email.
      2. Find property via metadata.property_id or tenant's propertyId.
      3. Get landlord from property.
      4. Delegate to upsert_rent_from_charge (handles pending→partial→paid).
      5. Notify the landlord.
    """
    from .rent import upsert_rent_from_charge

    metadata  = charge.get("metadata") or {}
    billing   = charge.get("billing_details") or {}
    charge_id = charge.get("id", "")

    # ── Idempotency ───────────────────────────────────────────────────────────
    if charge_id and db.rent_payment.find_one({"stripeChargeId": charge_id}):
        app.logger.info(f"charge.succeeded: already recorded charge {charge_id}, skipping")
        return

    # ── 1. Identify tenant ────────────────────────────────────────────────────
    tenant_user = None
    user_id_str = (metadata.get("user_id") or "").strip()
    if user_id_str:
        try:
            tenant_user = db.user.find_one({"_id": ObjectId(user_id_str)})
        except Exception:
            pass

    if not tenant_user:
        email = (billing.get("email") or "").strip().lower()
        if email:
            tenant_user = db.user.find_one({"email": email})

    if not tenant_user:
        app.logger.warning(f"charge.succeeded: cannot identify tenant for charge {charge_id}")
        return

    tenant_oid = tenant_user["_id"]

    # ── 2. Find property ──────────────────────────────────────────────────────
    property_doc = None
    prop_id_str = (metadata.get("property_id") or "").strip()
    if prop_id_str:
        try:
            property_doc = db.property.find_one({"_id": ObjectId(prop_id_str)})
        except Exception:
            pass

    if not property_doc and tenant_user.get("propertyId"):
        property_doc = db.property.find_one({"_id": tenant_user["propertyId"]})

    if not property_doc:
        app.logger.warning(
            f"charge.succeeded: cannot find property for tenant {tenant_oid}, charge {charge_id}"
        )
        return

    property_oid = property_doc["_id"]
    landlord_oid = property_doc.get("landlordId")

    if not landlord_oid:
        app.logger.warning(f"charge.succeeded: property {property_oid} has no landlordId")
        return

    # ── 3. Charge details ─────────────────────────────────────────────────────
    created_ts   = charge.get("created")
    paid_dt      = (
        datetime.fromtimestamp(created_ts, tz=timezone.utc) if created_ts else _now()
    )
    period        = paid_dt.strftime("%Y-%m")
    amount_cents  = charge.get("amount", 0)
    amount_dollars = round(amount_cents / 100.0, 2)
    currency      = (charge.get("currency") or "usd").upper()

    pm_details_obj = charge.get("payment_method_details") or {}
    pm_type = pm_details_obj.get("type", "")
    pm_sub  = pm_details_obj.get(pm_type) if pm_type else None

    charge_meta = {
        "charge_id":      charge_id,
        "payment_intent": charge.get("payment_intent", ""),
        "customer":       charge.get("customer", ""),
        "pm_type":        pm_type,
        "pm_sub":         pm_sub,
    }

    # ── 4. Upsert rent record (handles partial / full payment logic) ──────────
    rent_doc = upsert_rent_from_charge(
        tenant_oid=tenant_oid,
        property_oid=property_oid,
        landlord_oid=landlord_oid,
        amount_paid=amount_dollars,
        currency=currency,
        period=period,
        charge_meta=charge_meta,
        db=db,
        app=app,
    )

    # ── 5. Notify landlord ────────────────────────────────────────────────────
    tenant_name = (
        f"{tenant_user.get('firstName', '')} {tenant_user.get('lastName', '')}".strip()
        or tenant_user.get("email", "Your tenant")
    )
    _trigger_rent_payment_notification(
        recipient_oids=[landlord_oid],
        tenant_name=tenant_name,
        amount=amount_dollars,
        currency=currency,
        period=period,
        property_address=property_doc.get("address", "your property"),
        rent_id=str(rent_doc["_id"]),
        property_id=str(property_oid),
        db=db,
        app=app,
    )

    # ── 6. Notify tenant (payment confirmation) ───────────────────────────────
    _trigger_tenant_payment_confirmation(
        tenant_oid=tenant_oid,
        amount=amount_dollars,
        currency=currency,
        period=period,
        rent_status=rent_doc.get("status", "paid"),
        property_address=property_doc.get("address", "your property"),
        rent_id=str(rent_doc["_id"]),
        property_id=str(property_oid),
        db=db,
        app=app,
    )

    app.logger.info(
        f"charge.succeeded processed: rent_payment={rent_doc['_id']} | status={rent_doc.get('status')} | "
        f"tenant={tenant_oid} | landlord={landlord_oid} | "
        f"property={property_oid} | amount_paid={amount_dollars} {currency}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Webhook — Receive Stripe Events
# ═════════════════════════════════════════════════════════════════════════════

@stripe_bp.route("/webhook", methods=["POST"])
def stripe_webhook():
    """
    Stripe webhook receiver.

    Configure this URL in your Stripe Dashboard under
    Developers → Webhooks → Add endpoint:
      https://<your-domain>/api/stripe/webhook

    Set STRIPE_WEBHOOK_SECRET in your .env to the signing secret (whsec_…).
    Recommended events to subscribe to:
      - charge.succeeded
      - charge.failed
      - financial_connections.account.disconnected
      - payment_intent.succeeded
      - payment_intent.payment_failed
      - setup_intent.succeeded
      - customer.deleted
    """
    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

    if webhook_secret:
        try:
            _configure_stripe()
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except ValueError:
            current_app.logger.warning("Stripe webhook: invalid JSON payload")
            return jsonify({"success": False, "message": "Invalid payload"}), 400
        except stripe.error.SignatureVerificationError:
            current_app.logger.warning("Stripe webhook: signature verification failed")
            return jsonify({"success": False, "message": "Invalid signature"}), 400
    else:
        # No webhook secret — only acceptable in local development
        if not current_app.debug:
            current_app.logger.error(
                "Stripe webhook received but STRIPE_WEBHOOK_SECRET is not set in production"
            )
            return jsonify({"success": False, "message": "Webhook secret not configured"}), 500
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return jsonify({"success": False, "message": "Invalid payload"}), 400

    event_type = event.get("type", "")
    event_obj = (event.get("data") or {}).get("object") or {}
    db = current_app.db
    app = current_app._get_current_object()

    # ── Handle events ────────────────────────────────────────────────────────

    if event_type == "charge.succeeded":
        _handle_charge_succeeded(event_obj, db, app)

    elif event_type == "charge.failed":
        charge_id = event_obj.get("id")
        failure_msg = event_obj.get("failure_message", "unknown")
        current_app.logger.warning(
            f"charge.failed: {charge_id} | reason={failure_msg}"
        )

    elif event_type == "financial_connections.account.disconnected":
        fc_account_id = event_obj.get("id")
        if fc_account_id:
            db.stripe_data.update_many(
                {},
                {"$pull": {"linked_accounts": {"fc_account_id": fc_account_id}}},
            )
            current_app.logger.info(
                f"Financial Connections account disconnected via webhook: {fc_account_id}"
            )

    elif event_type == "payment_intent.succeeded":
        pi_id = event_obj.get("id")
        metadata = event_obj.get("metadata") or {}
        current_app.logger.info(
            f"PaymentIntent succeeded: {pi_id} | "
            f"user={metadata.get('user_id')} | property={metadata.get('property_id')} | "
            f"amount={event_obj.get('amount_received')} {event_obj.get('currency', '').upper()}"
        )

    elif event_type == "payment_intent.payment_failed":
        pi_id = event_obj.get("id")
        last_err = event_obj.get("last_payment_error") or {}
        current_app.logger.warning(
            f"PaymentIntent failed: {pi_id} | reason={last_err.get('message', 'unknown')}"
        )

    elif event_type == "setup_intent.succeeded":
        si_id = event_obj.get("id")
        current_app.logger.info(
            f"SetupIntent succeeded: {si_id} | "
            f"customer={event_obj.get('customer')} | pm={event_obj.get('payment_method')}"
        )

    elif event_type == "customer.deleted":
        customer_id = event_obj.get("id")
        if customer_id:
            db.stripe_data.update_many(
                {"stripe_customer_id": customer_id},
                {"$set": {"stripe_customer_id": None, "updated_at": _now()}},
            )
            current_app.logger.info(f"Stripe Customer deleted: {customer_id}")

    return jsonify({"success": True}), 200
