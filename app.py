# Run with: python app.py  (dev server on 5001)

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, render_template, jsonify
from sqlalchemy import text
from config import Config
from extensions import db, login_manager, cache
from routes import main as main_bp
from api import api_bp


def _configure_logging(app):
    """Log to stderr with a simple format."""
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"
    )
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    stream.setLevel(logging.DEBUG if app.debug else logging.INFO)

    app.logger.addHandler(stream)
    app.logger.setLevel(logging.DEBUG if app.debug else logging.INFO)

    logging.getLogger("swimcloud_scraper").setLevel(logging.INFO)


def create_app(test_config=None):
    """Flask app factory. test_config overrides for pytest."""
    app = Flask(__name__)
    app.config.from_object(Config)

    if test_config:
        app.config.update(test_config)

    _configure_logging(app)

    db.init_app(app)
    login_manager.init_app(app)
    cache.init_app(app)

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    with app.app_context():
        import time as _time
        for attempt in range(5):
            try:
                db.create_all()
                break
            except Exception as e:
                app.logger.warning("db.create_all() attempt %d failed: %s", attempt + 1, e)
                db.session.rollback()
                _time.sleep(1)

    @app.route("/health")
    def health():
        """Kubernetes/Docker liveness — just checks DB is reachable."""
        try:
            db.session.execute(text("SELECT 1"))
            return jsonify(status="ok", db="connected"), 200
        except Exception as e:
            return jsonify(status="error", db=str(e)), 503

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        app.logger.error("Internal server error: %s", e)
        return render_template("errors/500.html"), 500

    return app


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "true").lower() in ("1", "true", "yes")
    create_app().run(debug=debug, port=5001)
