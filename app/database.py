from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
import os

client = None
db = None


def init_db(app):
    global client, db

    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DB_NAME", "lesefy")

    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        # Force a connection to verify the server is reachable
        client.admin.command("ping")
        db = client[db_name]
        app.db = db
        app.mongo_client = client
        app.logger.info(f"Connected to MongoDB: {mongo_uri} / database: {db_name}")
    except ConnectionFailure as e:
        app.logger.error(f"Could not connect to MongoDB: {e}")
        raise


def get_db():
    return db
