import base64

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from app.blueprints.whiteboard import whiteboard_bp
from app.extensions import db
from app.models.whiteboard import Whiteboard
from app.services import gemini_service, rate_limit_service


@whiteboard_bp.route("/whiteboard")
@login_required
def whiteboard_page():
    boards = Whiteboard.query.filter_by(user_id=current_user.id).order_by(
        Whiteboard.updated_at.desc()
    ).all()
    return render_template("whiteboard/whiteboard.html", boards=boards)


@whiteboard_bp.route("/whiteboard/<int:board_id>")
@login_required
def whiteboard_editor(board_id):
    board = Whiteboard.query.filter_by(id=board_id, user_id=current_user.id).first_or_404()
    return render_template("whiteboard/editor.html", board=board)


@whiteboard_bp.route("/api/boards", methods=["GET"])
@login_required
def list_boards():
    boards = Whiteboard.query.filter_by(user_id=current_user.id).order_by(
        Whiteboard.updated_at.desc()
    ).all()
    return jsonify(ok=True, data=[b.to_dict() for b in boards], error=None)


@whiteboard_bp.route("/api/boards", methods=["POST"])
@login_required
def create_board():
    title = (request.json or {}).get("title") or "Untitled board"
    board = Whiteboard(user_id=current_user.id, title=title, canvas_json="{}")
    db.session.add(board)
    db.session.commit()
    return jsonify(ok=True, data=board.to_dict(), error=None), 201


@whiteboard_bp.route("/api/boards/<int:board_id>", methods=["GET"])
@login_required
def get_board(board_id):
    board = Whiteboard.query.filter_by(id=board_id, user_id=current_user.id).first_or_404()
    return jsonify(ok=True, data=board.to_dict(include_canvas=True), error=None)


@whiteboard_bp.route("/api/boards/<int:board_id>", methods=["PUT"])
@login_required
def update_board(board_id):
    board = Whiteboard.query.filter_by(id=board_id, user_id=current_user.id).first_or_404()
    payload = request.json or {}
    if "canvas_json" in payload:
        board.canvas_json = payload["canvas_json"]
    if "title" in payload:
        board.title = payload["title"] or board.title
    db.session.commit()
    return jsonify(ok=True, data=board.to_dict(), error=None)


@whiteboard_bp.route("/api/boards/<int:board_id>/ask", methods=["POST"])
@login_required
def ask_board(board_id):
    board = Whiteboard.query.filter_by(id=board_id, user_id=current_user.id).first_or_404()
    payload = request.json or {}
    mode = payload.get("mode", "ask")
    if mode not in ("ask", "explain_selection", "check_work", "solve_on_board"):
        return jsonify(ok=False, data=None, error="Invalid mode."), 400

    image_b64 = payload.get("image_png_base64")
    if not image_b64:
        return jsonify(ok=False, data=None, error="A PNG snapshot of the board is required."), 400

    if not rate_limit_service.check_and_increment(current_user, "whiteboard"):
        return jsonify(
            ok=False, data=None, error="Daily whiteboard AI limit reached."
        ), 429

    try:
        image_bytes = base64.b64decode(image_b64.split(",")[-1])
    except (ValueError, base64.binascii.Error):
        return jsonify(ok=False, data=None, error="Invalid image data."), 400

    question_text = payload.get("question")
    try:
        answer = gemini_service.ask_whiteboard(
            mode, image_bytes, question_text=question_text, job_id=f"board-{board_id}"
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, data=None, error=f"AI assistant is unavailable: {exc}"), 502

    if mode == "solve_on_board":
        import json

        try:
            steps = json.loads(answer)
        except json.JSONDecodeError:
            steps = [answer]
        return jsonify(ok=True, data={"mode": mode, "steps": steps}, error=None)

    return jsonify(ok=True, data={"mode": mode, "text": answer}, error=None)
