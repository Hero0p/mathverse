from flask import redirect, render_template, url_for
from flask_login import current_user, login_required

from app.blueprints.main import main_bp
from app.models.chat import Conversation
from app.models.whiteboard import Whiteboard


@main_bp.route("/")
def landing():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return render_template("main/landing.html")


@main_bp.route("/dashboard")
@login_required
def dashboard():
    conversations = (
        Conversation.query.filter_by(user_id=current_user.id)
        .order_by(Conversation.updated_at.desc())
        .limit(10)
        .all()
    )
    boards = (
        Whiteboard.query.filter_by(user_id=current_user.id)
        .order_by(Whiteboard.updated_at.desc())
        .limit(10)
        .all()
    )
    return render_template("main/dashboard.html", conversations=conversations, boards=boards)


@main_bp.route("/profile")
@login_required
def profile():
    return render_template("main/profile.html", user=current_user)
