import os

from flask import Flask

from app.config import get_config
from app.extensions import csrf, db, login_manager, migrate, socketio


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(get_config(config_name))

    os.makedirs(app.config["MEDIA_ROOT"], exist_ok=True)
    os.makedirs(os.path.join(app.config["MEDIA_ROOT"], "videos"), exist_ok=True)
    os.makedirs(os.path.join(app.config["MEDIA_ROOT"], "thumbnails"), exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    socketio.init_app(app)

    from app.models.user import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    register_blueprints(app)
    register_error_handlers(app)
    register_cli(app)
    register_media_routes(app)

    return app


def register_blueprints(app: Flask) -> None:
    from app.blueprints.main import main_bp
    from app.blueprints.auth import auth_bp
    from app.blueprints.chat import chat_bp
    from app.blueprints.whiteboard import whiteboard_bp
    from app.blueprints.arena import arena_bp
    from app.sockets import arena_events  # noqa: F401  (registers socket handlers)

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(chat_bp)
    app.register_blueprint(whiteboard_bp)
    app.register_blueprint(arena_bp)


def register_error_handlers(app: Flask) -> None:
    from flask import jsonify, render_template, request

    def _wants_json():
        return request.path.startswith("/api/")

    @app.errorhandler(404)
    def not_found(e):
        if _wants_json():
            return jsonify(ok=False, data=None, error="Not found"), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        if _wants_json():
            return jsonify(ok=False, data=None, error="Internal server error"), 500
        return render_template("errors/500.html"), 500


def register_media_routes(app: Flask) -> None:
    from flask import send_from_directory

    @app.route("/media/videos/<path:filename>")
    def media_video(filename):
        return send_from_directory(
            os.path.join(app.config["MEDIA_ROOT"], "videos"), filename
        )

    @app.route("/media/thumbnails/<path:filename>")
    def media_thumbnail(filename):
        return send_from_directory(
            os.path.join(app.config["MEDIA_ROOT"], "thumbnails"), filename
        )


def register_cli(app: Flask) -> None:
    @app.cli.command("seed-dev")
    def seed_dev():
        """Seed the dev database with sample data."""
        from scripts.seed_dev import run

        run()
