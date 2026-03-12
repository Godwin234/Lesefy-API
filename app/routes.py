from flask import Blueprint, jsonify
from .cache import cache

main_bp = Blueprint("main", __name__)


@main_bp.route("/", methods=["GET"])
@cache.cached(timeout=60)
def index():
    return jsonify({"message": "Lesefy API is running"}), 200


@main_bp.route("/health", methods=["GET"])
@cache.cached(timeout=30)
def health():
    return jsonify({"status": "ok"}), 200
