from flask import Blueprint

arena_bp = Blueprint("arena", __name__, template_folder="../../templates/arena")

from app.blueprints.arena import routes  # noqa: E402,F401
