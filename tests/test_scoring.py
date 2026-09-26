import pytest

from app.extensions import db as _db
from app.models.match import Match
from app.models.question import Question
from app.models.user import User
from app.services import arena_service


def _make_user(username, elo=1000):
    user = User(username=username, display_name=username, elo=elo, tier="Genius", placement_done=True)
    _db.session.add(user)
    return user


def _make_question(answer="4", answer_type="numeric", level=2):
    q = Question(
        statement_hash=f"hash-{answer}-{level}-{id(object())}",
        level=level,
        statement="What is 2+2?",
        answer=answer,
        answer_type=answer_type,
    )
    _db.session.add(q)
    return q


def test_record_answer_correct_scores_plus_three(app):
    a = _make_user("alice")
    b = _make_user("bob")
    _db.session.commit()
    match = Match(format=30, status="live", is_ranked=True, player_a_id=a.id, player_b_id=b.id, question_ids_json="[]")
    _db.session.add(match)
    _db.session.commit()
    q = _make_question()
    _db.session.commit()

    correct = arena_service.record_answer(match, a, q, "4", is_skip=False, ms_elapsed=1200)
    assert correct is True
    assert match.score_a == 3
    assert match.wrong_a == 0


def test_record_answer_wrong_scores_minus_one(app):
    a = _make_user("alice")
    b = _make_user("bob")
    _db.session.commit()
    match = Match(format=30, status="live", is_ranked=True, player_a_id=a.id, player_b_id=b.id, question_ids_json="[]")
    _db.session.add(match)
    _db.session.commit()
    q = _make_question()
    _db.session.commit()

    correct = arena_service.record_answer(match, b, q, "5", is_skip=False, ms_elapsed=900)
    assert correct is False
    assert match.score_b == -1
    assert match.wrong_b == 1


def test_record_answer_skip_scores_zero_and_not_counted_wrong(app):
    a = _make_user("alice")
    b = _make_user("bob")
    _db.session.commit()
    match = Match(format=30, status="live", is_ranked=True, player_a_id=a.id, player_b_id=b.id, question_ids_json="[]")
    _db.session.add(match)
    _db.session.commit()
    q = _make_question()
    _db.session.commit()

    correct = arena_service.record_answer(match, a, q, None, is_skip=True, ms_elapsed=500)
    assert correct is False
    assert match.score_a == 0
    assert match.wrong_a == 0


def test_finalize_match_ranked_applies_elo_symmetrically(app):
    a = _make_user("alice", elo=1000)
    b = _make_user("bob", elo=1000)
    _db.session.commit()
    match = Match(
        format=30, status="live", is_ranked=True, is_provisional=False,
        player_a_id=a.id, player_b_id=b.id, question_ids_json="[]",
        score_a=10, score_b=4,
    )
    _db.session.add(match)
    _db.session.commit()

    arena_service.finalize_match(match)

    assert match.status == "finished"
    assert match.winner_id == a.id
    assert match.elo_delta_a > 0
    assert match.elo_delta_b < 0
    assert a.elo == 1000 + match.elo_delta_a
    assert b.elo == 1000 + match.elo_delta_b
    assert a.wins == 1 and b.losses == 1


def test_finalize_match_draw_sets_is_draw(app):
    a = _make_user("alice", elo=1000)
    b = _make_user("bob", elo=1000)
    _db.session.commit()
    match = Match(
        format=30, status="live", is_ranked=True,
        player_a_id=a.id, player_b_id=b.id, question_ids_json="[]",
        score_a=5, score_b=5,
    )
    _db.session.add(match)
    _db.session.commit()

    arena_service.finalize_match(match)
    assert match.is_draw is True
    assert match.winner_id is None
    assert a.draws == 1 and b.draws == 1


def test_finalize_match_casual_never_moves_elo(app):
    a = _make_user("alice", elo=1000)
    b = _make_user("bob", elo=1000)
    _db.session.commit()
    match = Match(
        format=30, status="live", is_ranked=False,
        player_a_id=a.id, player_b_id=b.id, question_ids_json="[]",
        score_a=8, score_b=2,
    )
    _db.session.add(match)
    _db.session.commit()

    arena_service.finalize_match(match)
    assert a.elo == 1000 and b.elo == 1000
    assert match.elo_delta_a == 0 and match.elo_delta_b == 0


def test_finalize_match_bot_never_moves_elo(app):
    a = _make_user("alice", elo=1000)
    _db.session.commit()
    match = Match(
        format=30, status="live", is_ranked=True,
        player_a_id=a.id, player_b_id=None, question_ids_json="[]",
        score_a=8, score_b=2,
    )
    _db.session.add(match)
    _db.session.commit()

    arena_service.finalize_match(match)
    assert a.elo == 1000
    assert a.matches_played == 1 and a.wins == 1


def test_abandon_match_leaver_forfeits_full_loss(app):
    a = _make_user("alice", elo=1000)
    b = _make_user("bob", elo=1000)
    _db.session.commit()
    match = Match(
        format=30, status="live", is_ranked=True,
        player_a_id=a.id, player_b_id=b.id, question_ids_json="[]",
        score_a=5, score_b=1,  # a was winning on points, but a disconnects
    )
    _db.session.add(match)
    _db.session.commit()

    arena_service.abandon_match(match, leaver_user_id=a.id)

    assert match.status == "abandoned"
    assert match.winner_id == b.id
    assert match.elo_delta_a < 0
    assert match.elo_delta_b > 0


def test_question_level_for_match_uses_higher_rated_player():
    assert arena_service.question_level_for_match(500, 1900) == 4  # Titan
    assert arena_service.question_level_for_match(1900, 500) == 4


def test_build_question_sequence_prefers_less_served(app):
    # 4 fresh questions (times_served=0) and 2 heavily-served ones (=100).
    # The median of [0,0,0,0,100,100] is 0, so the "low" (<=median) group is
    # exactly the 4 fresh questions and must be ordered before the 2 heavy ones.
    for i in range(6):
        q = Question(
            statement_hash=f"seq-{i}",
            level=3,
            statement=f"Question {i}",
            answer="1",
            answer_type="numeric",
            times_served=100 if i >= 4 else 0,
        )
        _db.session.add(q)
    _db.session.commit()

    fresh_ids = {q.id for q in Question.query.filter_by(level=3, times_served=0).all()}
    heavy_ids = {q.id for q in Question.query.filter_by(level=3, times_served=100).all()}

    ids = arena_service.build_question_sequence(level=3, count=6)
    assert set(ids[:4]) == fresh_ids
    assert set(ids[4:6]) == heavy_ids
