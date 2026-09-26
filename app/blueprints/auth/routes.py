from datetime import timedelta

from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.blueprints.auth import auth_bp
from app.extensions import db
from app.models.user import User


def _set_guest_cookie(response, token: str):
    response.set_cookie(
        current_app.config["GUEST_TOKEN_COOKIE"],
        token,
        max_age=int(timedelta(days=current_app.config["GUEST_TOKEN_MAX_AGE_DAYS"]).total_seconds()),
        httponly=True,
        samesite="Lax",
    )
    return response


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("auth/register.html")

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    username = (request.form.get("username") or "").strip()

    if not email or not password or len(password) < 8:
        flash("Please provide a valid email and a password of at least 8 characters.", "error")
        return redirect(url_for("auth.register"))

    if User.query.filter_by(email=email).first():
        flash("An account with that email already exists.", "error")
        return redirect(url_for("auth.register"))

    user = User(username=username or email.split("@")[0], display_name=username or email.split("@")[0])
    user.email = email
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    login_user(user, remember=True)
    return redirect(url_for("main.dashboard"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("auth/login.html")

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    user = User.query.filter_by(email=email).first()
    if user is None or not user.check_password(password):
        flash("Invalid email or password.", "error")
        return redirect(url_for("auth.login"))

    user.touch()
    db.session.commit()
    login_user(user, remember=True)
    return redirect(url_for("main.dashboard"))


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("main.landing"))


@auth_bp.route("/guest", methods=["POST"])
def guest():
    """Create (or resume) a guest session. Never forces auth."""
    token = request.cookies.get(current_app.config["GUEST_TOKEN_COOKIE"])
    user = User.query.filter_by(guest_token=token).first() if token else None

    if user is None:
        user = User.create_guest()
        db.session.add(user)
        db.session.commit()

    user.touch()
    db.session.commit()
    login_user(user, remember=True)

    response = redirect(url_for("main.dashboard"))
    return _set_guest_cookie(response, user.guest_token)


@auth_bp.route("/claim", methods=["POST"])
@login_required
def claim():
    """Upgrade the current guest account into a full registered account,
    preserving all conversations, boards, and Elo history in place."""
    if not current_user.is_guest:
        return jsonify(ok=False, data=None, error="Account is already registered."), 400

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    username = (request.form.get("username") or "").strip() or None

    if not email or not password or len(password) < 8:
        return jsonify(ok=False, data=None, error="Valid email and 8+ character password required."), 400

    if User.query.filter(User.email == email, User.id != current_user.id).first():
        return jsonify(ok=False, data=None, error="An account with that email already exists."), 400

    current_user.claim(email=email, password=password, username=username)
    db.session.commit()

    response = jsonify(ok=True, data=current_user.to_public_dict(), error=None)
    response.delete_cookie(current_app.config["GUEST_TOKEN_COOKIE"])
    return response
