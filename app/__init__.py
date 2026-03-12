import os
from flask import Flask
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()


def create_app():
    app = Flask(__name__)
    CORS(app)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-production")

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

    return app
