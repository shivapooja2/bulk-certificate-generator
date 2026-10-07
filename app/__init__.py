import logging
import os

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from .db import Database
from .routes import bp
from .service import GenerationService


def create_app(config=None):
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get("DATABASE", os.path.join(base, "data", "certgen.db")),
        STORAGE_DIR=os.environ.get("STORAGE_DIR", os.path.join(base, "data", "certificates")),
        WORKERS=int(os.environ.get("WORKERS", 4)),
        MAX_RECIPIENTS=int(os.environ.get("MAX_RECIPIENTS", 5000)),
        MAX_ATTEMPTS=3,
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # reject oversized request bodies (413)
    )
    if config:
        app.config.update(config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    db = Database(app.config["DATABASE"])
    db.init_schema()
    gen = GenerationService(db, app.config["STORAGE_DIR"], app.config["WORKERS"], app.config["MAX_ATTEMPTS"])
    gen.recover()
    app.extensions["generator"] = gen

    app.register_blueprint(bp)

    @app.errorhandler(HTTPException)
    def http_error(e):
        return jsonify({"error": e.description}), e.code

    @app.errorhandler(Exception)
    def unexpected(e):
        app.logger.exception("Unhandled error")
        return jsonify({"error": "Internal server error"}), 500

    return app
