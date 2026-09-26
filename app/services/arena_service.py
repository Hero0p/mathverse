"""Match-level orchestration for the Arena: question selection, scoring,
and match finalization (Elo + tier + aggregate stats). Kept separate from
the Socket.IO transport layer (app/sockets/arena_events.py) so it can be
unit tested and reused from the REST match-review endpoint.
"""
import json as _json
import random
from datetime import datetime, timezone

from app.extensions import db, get_redis
from app.models.match import Match, MatchAnswer
from app.models.question import Question
from app.models.user import User
from app.services import elo_service, question_service
from app.tiers import tier_by_name, tier_for_elo

CORRECT_POINTS = 3
WRONG_POINTS = -1
SKIP_POINTS = 0
DEFAULT_UNRATED_ELO = 1200  # Phantom floor: sensible default for un-placed casual players


def _utcnow():
    return datetime.now(timezone.utc)


def effective_elo(user: User | None) -> int:
    if user is None or user.elo is None:
        return DEFAULT_UNRATED_ELO
    return user.elo


def question_level_for_match(elo_a: int, elo_b: int) -> int:
    """Spec 7.4: questions are drawn from the HIGHER-rated player's tier level."""
    return tier_for_elo(max(elo_a, elo_b)).level


def build_question_sequence(level: int, count: int = 40) -> list[int]:
    """Return `count` question ids at `level`, weighted toward questions
    served less than the level's median serve count. Cycles if the pool is
    smaller than `count` -- a 30-60s match can't realistically exhaust a
    healthy pool, but this keeps things correct if it's thin."""
    pool = Question.query.filter_by(level=level).all()
    if not pool:
        raise ValueError(f"no questions available at level {level}")

    served_counts = sorted(q.times_served for q in pool)
    median = served_counts[len(served_counts) // 2]
    low = [q for q in pool if q.times_served <= median]
    high = [q for q in pool if q.times_served > median]
    random.shuffle(low)
    random.shuffle(high)
    ordered = low + high

    ids = []
    i = 0
    while len(ids) < count:
        q = ordered[i % len(ordered)]
        ids.append(q.id)
        i += 1
    return ids


def mark_questions_served(question_ids: list[int]) -> None:
    unique_ids = set(question_ids)
    Question.query.filter(Question.id.in_(unique_ids)).update(
        {Question.times_served: Question.times_served + 1}, synchronize_session=False
    )
    db.session.commit()


def record_answer(
    match: Match, user: User, question: Question, answer: str | None, is_skip: bool, ms_elapsed: int
) -> bool:
    """Grade and persist one answer. Returns whether it was correct."""
    is_correct = False if is_skip else question_service.grade_answer(question, answer)

    ma = MatchAnswer(
        match_id=match.id,
        user_id=user.id,
        question_id=question.id,
        answer_given=answer,
        is_correct=is_correct,
        is_skip=is_skip,
        ms_elapsed=ms_elapsed,
    )
    db.session.add(ma)

    points = SKIP_POINTS if is_skip else (CORRECT_POINTS if is_correct else WRONG_POINTS)
    is_player_a = user.id == match.player_a_id
    if is_player_a:
        match.score_a += points
        match.time_ms_a += ms_elapsed
        if not is_skip and not is_correct:
            match.wrong_a += 1
    else:
        match.score_b += points
        match.time_ms_b += ms_elapsed
        if not is_skip and not is_correct:
            match.wrong_b += 1

    question.times_correct += 1 if is_correct else 0
    db.session.commit()
    return is_correct


def _apply_elo(user: User, opponent_elo: int, outcome: str, match: Match) -> int:
    player_elo = effective_elo(user)
    at_floor = elo_service.is_at_tier_floor(player_elo)
    tier_name = tier_for_elo(player_elo).name
    shield_available = at_floor and not user.has_used_shield(tier_name)

    result = elo_service.apply_match_result(
        player_elo=player_elo,
        opponent_elo=opponent_elo,
        outcome=outcome,
        is_provisional=match.is_provisional,
        at_floor=at_floor,
        shield_available=shield_available,
    )
    if result.shield_used:
        user.mark_shield_used(tier_name)

    user.elo = result.new_elo
    user.tier = tier_for_elo(result.new_elo).name
    user.peak_elo = max(user.peak_elo or 0, result.new_elo)
    user.matches_played += 1
    if outcome == "win":
        user.wins += 1
    elif outcome == "loss":
        user.losses += 1
    else:
        user.draws += 1
    return result.delta


def finalize_match(match: Match, forfeiting_user_id: int | None = None, final_status: str = "finished") -> None:
    """Determine the winner, apply Elo (if ranked and not vs. a bot), persist
    aggregate user stats, and mark the match finished (or `final_status`,
    e.g. "abandoned", if the caller already knows how it ended)."""
    match.status = final_status
    match.ended_at = _utcnow()

    player_a = db.session.get(User, match.player_a_id)
    player_b = db.session.get(User, match.player_b_id) if match.player_b_id else None

    if forfeiting_user_id is not None:
        outcome_a = "loss" if forfeiting_user_id == match.player_a_id else "win"
    else:
        result = elo_service.resolve_tie_break(
            match.score_a, match.score_b, match.wrong_a, match.wrong_b,
            match.time_ms_a, match.time_ms_b,
        )
        if result == "draw":
            outcome_a = "draw"
        else:
            outcome_a = "win" if result == "a" else "loss"

    if outcome_a == "draw":
        match.is_draw = True
    else:
        match.winner_id = match.player_a_id if outcome_a == "win" else match.player_b_id

    elo_a_before = effective_elo(player_a)
    elo_b_before = effective_elo(player_b) if player_b else DEFAULT_UNRATED_ELO
    match.elo_before_a = elo_a_before
    match.elo_before_b = elo_b_before if player_b else None

    is_bot_match = player_b is None
    if match.is_ranked and not is_bot_match:
        outcome_b = {"win": "loss", "loss": "win", "draw": "draw"}[outcome_a]
        delta_a = _apply_elo(player_a, elo_b_before, outcome_a, match)
        delta_b = _apply_elo(player_b, elo_a_before, outcome_b, match)
        match.elo_delta_a = delta_a
        match.elo_delta_b = delta_b
    else:
        # Casual and bot matches still count toward matches_played/W-L for
        # profile stats, but never move Elo.
        player_a.matches_played += 1
        if outcome_a == "win":
            player_a.wins += 1
        elif outcome_a == "loss":
            player_a.losses += 1
        else:
            player_a.draws += 1
        match.elo_delta_a = 0
        if player_b is not None:
            player_b.matches_played += 1
            outcome_b = {"win": "loss", "loss": "win", "draw": "draw"}[outcome_a]
            if outcome_b == "win":
                player_b.wins += 1
            elif outcome_b == "loss":
                player_b.losses += 1
            else:
                player_b.draws += 1
            match.elo_delta_b = 0

    db.session.commit()


def ranked_matches_played(user_id: int) -> int:
    return Match.query.filter(
        Match.is_ranked.is_(True),
        Match.status == "finished",
        db.or_(Match.player_a_id == user_id, Match.player_b_id == user_id),
    ).count()


def is_provisional_pair(user_a_id: int, user_b_id: int | None) -> bool:
    """Spec 7.2: a player's first 5 RANKED matches move Elo at 1.5x so a
    mis-seeded placement converges fast. True if either side qualifies."""
    if ranked_matches_played(user_a_id) < 5:
        return True
    if user_b_id is not None and ranked_matches_played(user_b_id) < 5:
        return True
    return False


def abandon_match(match: Match, leaver_user_id: int) -> None:
    """15s disconnect grace has expired: the leaver forfeits the full loss,
    the opponent takes the full win."""
    finalize_match(match, forfeiting_user_id=leaver_user_id, final_status="abandoned")


# --- Placement quiz ------------------------------------------------------
PLACEMENT_QUESTION_COUNT = 5
PLACEMENT_TTL_SEC = 900


def _placement_key(user_id: int) -> str:
    return f"mv:placement:{user_id}"


def start_placement(user: User, tier_name: str) -> tuple[list[Question], str]:
    tier = tier_by_name(tier_name)  # raises ValueError for an invalid tier name
    pool = Question.query.filter_by(level=tier.level).all()
    if len(pool) < PLACEMENT_QUESTION_COUNT:
        raise ValueError(f"not enough questions at level {tier.level} to run placement")
    chosen = random.sample(pool, PLACEMENT_QUESTION_COUNT)

    r = get_redis()
    r.set(
        _placement_key(user.id),
        _json.dumps({"tier": tier.name, "question_ids": [q.id for q in chosen]}),
        ex=PLACEMENT_TTL_SEC,
    )
    return chosen, tier.name


def submit_placement(user: User, answers: dict[int, str]) -> dict:
    """`answers` maps question_id -> submitted answer string."""
    r = get_redis()
    raw = r.get(_placement_key(user.id))
    if raw is None:
        raise ValueError("No placement quiz in progress (it may have expired). Start a new one.")
    session = _json.loads(raw)
    tier = tier_by_name(session["tier"])
    question_ids = session["question_ids"]

    questions = {q.id: q for q in Question.query.filter(Question.id.in_(question_ids)).all()}
    correct_count = 0
    for qid in question_ids:
        question = questions.get(qid)
        given = answers.get(qid) or answers.get(str(qid))
        if question is not None and question_service.grade_answer(question, given):
            correct_count += 1

    seed = elo_service.placement_seed(tier, correct_count)
    user.elo = seed
    user.tier = tier_for_elo(seed).name
    user.peak_elo = max(user.peak_elo or 0, seed)
    user.placement_done = True
    db.session.commit()

    r.delete(_placement_key(user.id))
    return {
        "correct_count": correct_count,
        "total": len(question_ids),
        "seeded_elo": seed,
        "tier": user.tier,
    }
