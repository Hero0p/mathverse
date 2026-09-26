from flask import Blueprint

whiteboard_bp = Blueprint("whiteboard", __name__, template_folder="../../templates/whiteboard")

from app.blueprints.whiteboard import routes  # noqa: E402,F401
