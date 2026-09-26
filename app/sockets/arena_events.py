"""Arena Socket.IO protocol (spec section 7.6).

State layout:
- Each authenticated socket joins room f"user:{user_id}" so we can emit to a
  user without tracking raw sids (handles reconnects transparently).
- LIVE_MATCHES holds all server-authoritative match state (question order,
  per-player progress, timers). The server is the only source of truth for
  the clock, question order, and correctness -- clients never see answers.
- This is in-process state, which is what MathVerse runs as. Scaling to
  several web processes would mean moving LIVE_MATCHES (and the matchmaking
  queue in app/services/store.py) to a shared store and giving Socket.IO a
  message queue; the client protocol below would not change.

Client -> server: arena:queue_join, arena:queue_leave, arena:answer, arena:skip
Server -> client: arena:match_found, arena:countdown, arena:question,
                   arena:score_update, arena:match_end, arena:bot_offer, arena:error
"""
import json
import logging
import time

from flask import current_app, request
from flask_login import current_user
from flask_socketio import join_room

from app.extensions import db, socketio
from app.models.match import Match
from app.models.question import Question
from app.models.user import User
from app.services import arena_service, matchmaking_service

logger = logging.getLogger("mathverse.arena")

DISCONNECT_GRACE_SEC = 15
MIN_ANSWER_INTERVAL_SEC = 0.15  # per-socket rate limit floor
SUSPICIOUS_ANSWER_MS = 400
QUESTION_SEQUENCE_LENGTH = 40

# match_id -> state dict
LIVE_MATCHES: dict[int, dict] = {}
# user_id -> match_id currently in progress
USER_CURRENT_MATCH: dict[int, int] = {}
# user_id -> (format, ranked) they're queued for, so queue_leave/disconnect can clean up
USER_QUEUE_KEY: dict[int, tuple[int, bool]] = {}
# user_id -> last arena:answer timestamp, for rate limiting
LAST_ANSWER_AT: dict[int, float] = {}
# user_id -> monotonically increasing counter, bumped on connect/disconnect to
# invalidate stale disconnect-grace callbacks
DISCONNECT_EPOCH: dict[int, int] = {}


def _room(user_id: int) -> str:
    return f"user:{user_id}"


@socketio.on("connect")
def on_connect():
    if not current_user.is_authenticated:
        return False  # reject the connection; guests still get a real User row though
    join_room(_room(current_user.id))
    DISCONNECT_EPOCH[current_user.id] = DISCONNECT_EPOCH.get(current_user.id, 0) + 1

    match_id = USER_CURRENT_MATCH.get(current_user.id)
    if match_id is not None:
        state = LIVE_MATCHES.get(match_id)
        if state:
            state["connected"][current_user.id] = True
            logger.info("user=%s reconnected to match=%s", current_user.id, match_id)


@socketio.on("disconnect")
def on_disconnect():
    if not current_user.is_authenticated:
        return
    user_id = current_user.id
    key = USER_QUEUE_KEY.pop(user_id, None)
    if key:
        matchmaking_service.leave_queue(user_id, key[0], key[1])

    match_id = USER_CURRENT_MATCH.get(user_id)
    if match_id is None:
        return
    state = LIVE_MATCHES.get(match_id)
    if not state or state.get("finished"):
        return
    state["connected"][user_id] = False
    epoch = DISCONNECT_EPOCH.get(user_id, 0)
    app = current_app._get_current_object()
    socketio.start_background_task(_disconnect_grace, app, match_id, user_id, epoch)


@socketio.on("arena:queue_join")
def on_queue_join(data):
    if not current_user.is_authenticated:
        return
    data = data or {}
    fmt = int(data.get("format", 30))
    ranked = bool(data.get("ranked", True))
    if fmt not in (30, 60):
        socketio.emit("arena:error", {"error": "format must be 30 or 60"}, room=_room(current_user.id))
        return
    if ranked and not current_user.placement_done:
        socketio.emit(
            "arena:error",
            {"error": "Complete placement before queuing for ranked matches."},
            room=_room(current_user.id),
        )
        return
    if current_user.id in USER_CURRENT_MATCH:
        socketio.emit("arena:error", {"error": "Already in a match."}, room=_room(current_user.id))
        return

    elo = arena_service.effective_elo(current_user)
    matchmaking_service.join_queue(current_user.id, elo, fmt, ranked)
    USER_QUEUE_KEY[current_user.id] = (fmt, ranked)

    app = current_app._get_current_object()
    socketio.start_background_task(_matchmaking_loop, app, current_user.id, fmt, ranked)


@socketio.on("arena:queue_leave")
def on_queue_leave(_data=None):
    if not current_user.is_authenticated:
        return
    key = USER_QUEUE_KEY.pop(current_user.id, None)
    if key:
        matchmaking_service.leave_queue(current_user.id, key[0], key[1])


@socketio.on("arena:answer")
def on_answer(data):
    _handle_response(data, is_skip=False)


@socketio.on("arena:skip")
def on_skip(data):
    _handle_response(data, is_skip=True)


def _handle_response(data, is_skip: bool):
    if not current_user.is_authenticated:
        return
    user_id = current_user.id
    data = data or {}

    now = time.time()
    last = LAST_ANSWER_AT.get(user_id, 0)
    if now - last < MIN_ANSWER_INTERVAL_SEC:
        return  # silently drop: per-socket rate limit
    LAST_ANSWER_AT[user_id] = now

    match_id = USER_CURRENT_MATCH.get(user_id)
    state = LIVE_MATCHES.get(match_id) if match_id else None
    if not state or state.get("finished"):
        return

    dispatched_at = state["dispatched_at"].get(user_id)
    if dispatched_at is None:
        return  # no question currently in flight for this user -- ignore

    index = state["index"][user_id]
    question_ids = state["question_ids"]
    expected_qid = question_ids[index % len(question_ids)]
    if not is_skip and int(data.get("question_id", -1)) != expected_qid:
        return  # stale/spoofed response for a question that's no longer current

    ms_elapsed = int((now - dispatched_at) * 1000)
    if ms_elapsed < SUSPICIOUS_ANSWER_MS:
        logger.warning(
            "job=arena match=%s user=%s suspicious answer speed: %dms", match_id, user_id, ms_elapsed
        )

    match = db.session.get(Match, match_id)
    question = db.session.get(Question, expected_qid)
    arena_service.record_answer(
        match, current_user, question, data.get("answer"), is_skip, ms_elapsed
    )
    state["dispatched_at"][user_id] = None

    _emit_score_update(match)

    if time.time() < state["end_time"]:
        _send_question(match_id, user_id, index + 1)
    else:
        state["side_done"][user_id] = True
        if all(state["side_done"].values()):
            _end_match(current_app._get_current_object(), match_id)


def _emit_score_update(match: Match) -> None:
    payload = {"score_a": match.score_a, "score_b": match.score_b}
    socketio.emit("arena:score_update", payload, room=_room(match.player_a_id))
    if match.player_b_id:
        socketio.emit("arena:score_update", payload, room=_room(match.player_b_id))


def _send_question(match_id: int, user_id: int, index: int) -> None:
    state = LIVE_MATCHES[match_id]
    question_ids = state["question_ids"]
    question = db.session.get(Question, question_ids[index % len(question_ids)])
    state["index"][user_id] = index
    state["dispatched_at"][user_id] = time.time()
    socketio.emit(
        "arena:question",
        {"index": index, **question.to_client_dict()},
        room=_room(user_id),
    )


def _matchmaking_loop(app, user_id: int, fmt: int, ranked: bool) -> None:
    with app.app_context():
        while True:
            opponent_id = matchmaking_service.find_match(user_id, fmt, ranked)
            if opponent_id is not None:
                USER_QUEUE_KEY.pop(user_id, None)
                USER_QUEUE_KEY.pop(opponent_id, None)
                _start_match(user_id, opponent_id, fmt, ranked)
                return

            info = matchmaking_service.queue_position_info(user_id, fmt, ranked)
            if not info.get("queued"):
                return  # left the queue, or the entry expired
            if info.get("bot_available"):
                socketio.emit("arena:bot_offer", {}, room=_room(user_id))
            socketio.sleep(2)


def start_bot_match(user_id: int, fmt: int) -> None:
    """Called when a client accepts the bot offer after 45s of no match."""
    key = USER_QUEUE_KEY.pop(user_id, None)
    if key:
        matchmaking_service.leave_queue(user_id, key[0], key[1])
    _start_match(user_id, None, fmt, ranked=False)


@socketio.on("arena:accept_bot")
def on_accept_bot(data):
    if not current_user.is_authenticated:
        return
    data = data or {}
    fmt = int(data.get("format", 30))
    start_bot_match(current_user.id, fmt)


def _start_match(user_a_id: int, user_b_id: int | None, fmt: int, ranked: bool) -> None:
    player_a = db.session.get(User, user_a_id)
    player_b = db.session.get(User, user_b_id) if user_b_id else None

    elo_a = arena_service.effective_elo(player_a)
    elo_b = arena_service.effective_elo(player_b)
    level = arena_service.question_level_for_match(elo_a, elo_b)
    question_ids = arena_service.build_question_sequence(level, count=QUESTION_SEQUENCE_LENGTH)
    arena_service.mark_questions_served(question_ids)

    is_provisional = ranked and arena_service.is_provisional_pair(user_a_id, user_b_id)

    match = Match(
        format=fmt,
        status="live",
        is_ranked=ranked,
        is_provisional=is_provisional,
        player_a_id=user_a_id,
        player_b_id=user_b_id,
        question_ids_json=json.dumps(question_ids),
    )
    from datetime import datetime, timezone

    match.started_at = datetime.now(timezone.utc)
    db.session.add(match)
    db.session.commit()

    state = {
        "question_ids": question_ids,
        "index": {user_a_id: -1},
        "dispatched_at": {user_a_id: None},
        "side_done": {user_a_id: False},
        "connected": {user_a_id: True},
        "finished": False,
        "end_time": None,
    }
    if user_b_id:
        state["index"][user_b_id] = -1
        state["dispatched_at"][user_b_id] = None
        state["side_done"][user_b_id] = False
        state["connected"][user_b_id] = True

    LIVE_MATCHES[match.id] = state
    USER_CURRENT_MATCH[user_a_id] = match.id
    if user_b_id:
        USER_CURRENT_MATCH[user_b_id] = match.id

    socketio.emit(
        "arena:match_found",
        {
            "match_id": match.id,
            "format": fmt,
            "opponent": player_b.to_public_dict() if player_b else {"display_name": "Bot", "is_bot": True},
        },
        room=_room(user_a_id),
    )
    if player_b:
        socketio.emit(
            "arena:match_found",
            {"match_id": match.id, "format": fmt, "opponent": player_a.to_public_dict()},
            room=_room(user_b_id),
        )

    for n in (3, 2, 1):
        socketio.emit("arena:countdown", {"count": n}, room=_room(user_a_id))
        if user_b_id:
            socketio.emit("arena:countdown", {"count": n}, room=_room(user_b_id))
        socketio.sleep(1)

    state["end_time"] = time.time() + fmt
    _send_question(match.id, user_a_id, 0)
    if user_b_id:
        _send_question(match.id, user_b_id, 0)

    app = current_app._get_current_object()
    socketio.start_background_task(_match_timer, app, match.id, fmt)


def _match_timer(app, match_id: int, fmt: int) -> None:
    socketio.sleep(fmt)
    with app.app_context():
        _end_match(app, match_id)


def _end_match(app, match_id: int) -> None:
    state = LIVE_MATCHES.get(match_id)
    if state is None or state.get("finished"):
        return
    state["finished"] = True

    match = db.session.get(Match, match_id)
    if match is None or match.status != "live":
        return
    arena_service.finalize_match(match)

    _emit_match_end(match)

    LIVE_MATCHES.pop(match_id, None)
    USER_CURRENT_MATCH.pop(match.player_a_id, None)
    if match.player_b_id:
        USER_CURRENT_MATCH.pop(match.player_b_id, None)


def _emit_match_end(match: Match) -> None:
    player_a = db.session.get(User, match.player_a_id)
    payload_a = {
        "scores": {"you": match.score_a, "opponent": match.score_b},
        "elo_delta": match.elo_delta_a,
        "new_elo": player_a.elo,
        "new_tier": player_a.tier,
        "winner": "you" if match.winner_id == match.player_a_id else ("opponent" if match.winner_id else "draw"),
    }
    socketio.emit("arena:match_end", payload_a, room=_room(match.player_a_id))

    if match.player_b_id:
        player_b = db.session.get(User, match.player_b_id)
        payload_b = {
            "scores": {"you": match.score_b, "opponent": match.score_a},
            "elo_delta": match.elo_delta_b,
            "new_elo": player_b.elo,
            "new_tier": player_b.tier,
            "winner": "you" if match.winner_id == match.player_b_id else ("opponent" if match.winner_id else "draw"),
        }
        socketio.emit("arena:match_end", payload_b, room=_room(match.player_b_id))


def _disconnect_grace(app, match_id: int, user_id: int, epoch: int) -> None:
    socketio.sleep(DISCONNECT_GRACE_SEC)
    with app.app_context():
        if DISCONNECT_EPOCH.get(user_id, 0) != epoch:
            return  # user reconnected (or reconnected+disconnected again) since this was scheduled
        state = LIVE_MATCHES.get(match_id)
        if not state or state.get("finished"):
            return
        if state["connected"].get(user_id):
            return

        match = db.session.get(Match, match_id)
        if match is None or match.status != "live":
            return
        state["finished"] = True
        arena_service.abandon_match(match, leaver_user_id=user_id)
        _emit_match_end(match)
        LIVE_MATCHES.pop(match_id, None)
        USER_CURRENT_MATCH.pop(match.player_a_id, None)
        if match.player_b_id:
            USER_CURRENT_MATCH.pop(match.player_b_id, None)
        logger.info("match=%s abandoned by user=%s after disconnect grace", match_id, user_id)
