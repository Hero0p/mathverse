from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from app.blueprints.arena import arena_bp
from app.extensions import db
from app.models.match import Match
from app.services import arena_service, leaderboard_service
from app.tiers import PLACEMENT_TIER_NAMES, TIERS


@arena_bp.route("/arena")
@login_required
def arena_page():
    return render_template(
        "arena/arena.html",
        user=current_user,
        placement_tiers=PLACEMENT_TIER_NAMES,
        tiers=TIERS,
    )


@arena_bp.route("/leaderboard")
def leaderboard_page():
    return render_template("arena/leaderboard.html")


@arena_bp.route("/api/arena/placement", methods=["POST"])
@login_required
def placement():
    payload = request.json or {}
    action = payload.get("action", "start")

    if current_user.placement_done and action == "start":
        return jsonify(ok=False, data=None, error="Placement already completed."), 400

    if action == "start":
        tier_name = payload.get("tier")
        if tier_name not in PLACEMENT_TIER_NAMES:
            return jsonify(
                ok=False, data=None,
                error=f"tier must be one of {PLACEMENT_TIER_NAMES}",
            ), 400
        try:
            questions, tier = arena_service.start_placement(current_user, tier_name)
        except ValueError as exc:
            return jsonify(ok=False, data=None, error=str(exc)), 400
        return jsonify(
            ok=True,
            data={"tier": tier, "questions": [q.to_client_dict() for q in questions]},
            error=None,
        )

    if action == "submit":
        answers = payload.get("answers") or {}
        try:
            result = arena_service.submit_placement(current_user, answers)
        except ValueError as exc:
            return jsonify(ok=False, data=None, error=str(exc)), 400
        return jsonify(ok=True, data=result, error=None)

    return jsonify(ok=False, data=None, error="action must be 'start' or 'submit'"), 400


@arena_bp.route("/api/arena/profile", methods=["GET"])
@login_required
def profile():
    matches = (
        Match.query.filter(
            Match.status == "finished",
            db.or_(Match.player_a_id == current_user.id, Match.player_b_id == current_user.id),
        )
        .order_by(Match.ended_at.asc())
        .all()
    )

    elo_history = []
    for m in matches:
        if m.player_a_id == current_user.id:
            before, delta = m.elo_before_a, m.elo_delta_a
        else:
            before, delta = m.elo_before_b, m.elo_delta_b
        if before is None:
            continue
        elo_history.append({
            "match_id": m.id,
            "ended_at": m.ended_at.isoformat() if m.ended_at else None,
            "elo_before": before,
            "elo_after": before + (delta or 0),
        })

    from app.models.match import MatchAnswer

    answer_rows = MatchAnswer.query.filter_by(user_id=current_user.id).all()
    correct = sum(1 for a in answer_rows if a.is_correct)
    total = len(answer_rows) or 1
    avg_ms = sum(a.ms_elapsed for a in answer_rows) / total if answer_rows else 0

    return jsonify(
        ok=True,
        data={
            "user": current_user.to_public_dict(),
            "is_infinite": leaderboard_service.holds_infinite_title(current_user.id),
            "elo_history": elo_history,
            "accuracy": round(correct / total, 3) if answer_rows else None,
            "avg_answer_ms": round(avg_ms, 1),
            "match_count": len(matches),
        },
        error=None,
    )


@arena_bp.route("/api/arena/leaderboard", methods=["GET"])
def leaderboard():
    tier = request.args.get("tier")
    window = request.args.get("window", "all")
    page = request.args.get("page", 1, type=int)
    data = leaderboard_service.get_leaderboard(tier=tier, window=window, page=page)
    return jsonify(ok=True, data=data, error=None)


@arena_bp.route("/api/arena/match/<int:match_id>", methods=["GET"])
@login_required
def match_detail(match_id):
    match = Match.query.filter(
        Match.id == match_id,
        db.or_(Match.player_a_id == current_user.id, Match.player_b_id == current_user.id),
    ).first_or_404()

    from app.models.match import MatchAnswer
    from app.models.question import Question

    answers = MatchAnswer.query.filter_by(match_id=match.id).all()
    questions = {q.id: q for q in Question.query.filter(
        Question.id.in_([a.question_id for a in answers])
    ).all()}

    review = [
        {
            "question": questions[a.question_id].statement if a.question_id in questions else None,
            "correct_answer": questions[a.question_id].answer if a.question_id in questions else None,
            "user_id": a.user_id,
            "answer_given": a.answer_given,
            "is_correct": a.is_correct,
            "is_skip": a.is_skip,
            "ms_elapsed": a.ms_elapsed,
        }
        for a in answers
    ]

    return jsonify(ok=True, data={**match.to_dict(), "review": review}, error=None)
