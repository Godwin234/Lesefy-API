import os
import warnings
from flask import Flask
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()


def create_app():
    app = Flask(__name__)
    CORS(app, resources={r"/*": {"origins": "*"}})

    secret_key = os.environ.get("SECRET_KEY", "lesefy-default-secret-key-change-me!!")
    if len(secret_key.encode()) < 32:
        warnings.warn(
            "SECRET_KEY is shorter than 32 bytes. Set a strong SECRET_KEY in your .env file.",
            stacklevel=2,
        )
    app.config["SECRET_KEY"] = secret_key

    # Cache configuration — uses Redis when available, falls back to simple in-memory
    app.config["CACHE_TYPE"] = os.environ.get("CACHE_TYPE", "RedisCache")
    app.config["CACHE_REDIS_URL"] = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    app.config["CACHE_DEFAULT_TIMEOUT"] = int(os.environ.get("CACHE_DEFAULT_TIMEOUT", 300))  # 5 minutes

    # Max upload size: 10 MB
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

    from .cache import cache
    cache.init_app(app)

    from .database import init_db
    init_db(app)

    from .routes import main_bp
    app.register_blueprint(main_bp)

    from .auth import auth_bp
    app.register_blueprint(auth_bp)

    from .images import images_bp
    app.register_blueprint(images_bp)

    from .properties import properties_bp
    app.register_blueprint(properties_bp)

    from .maintenance import maintenance_bp
    app.register_blueprint(maintenance_bp)

    from .activities import activities_bp
    app.register_blueprint(activities_bp)

    from .chat import chat_bp, ensure_indexes
    app.register_blueprint(chat_bp)

    # ── Flask-SocketIO ────────────────────────────────────────────────────────
    from .socket import socketio
    socketio.init_app(
        app,
        cors_allowed_origins="*",
        async_mode="threading",
        # Allow the werkzeug development server (set False in production)
        allow_unsafe_werkzeug=True,
        logger=False,
        engineio_logger=False,
    )

    # Register real-time event handlers (import triggers @socketio.on decorators)
    from . import socket_events  # noqa: F401

    # Ensure MongoDB indexes exist for the chat collections
    with app.app_context():
        ensure_indexes(app.db)

    return app
