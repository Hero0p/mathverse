"""Redis-backed matchmaking queue.

One sorted-set key per (format, ranked) pair, scored by Elo, so ZRANGEBYSCORE
gives us everyone in a widening Elo window in one call. Queue entries are
JSON blobs with a timestamp so we can widen search windows over time and
expire stale entries.
"""
import json
import time

from app.extensions import get_redis

INITIAL_WINDOW = 75
WINDOW_GROWTH_PER_TICK = 50
WINDOW_GROWTH_INTERVAL_SEC = 5
MAX_WINDOW = 400
BOT_OFFER_AFTER_SEC = 45
QUEUE_ENTRY_TTL_SEC = 120


def _queue_key(fmt: int, ranked: bool) -> str:
    return f"mv:queue:{fmt}:{'ranked' if ranked else 'casual'}"


def _recent_opponents_key(user_id: int) -> str:
    return f"mv:recent_opponents:{user_id}"


def join_queue(user_id: int, elo: int, fmt: int, ranked: bool) -> None:
    r = get_redis()
    key = _queue_key(fmt, ranked)
    entry = json.dumps({"user_id": user_id, "elo": elo, "joined_at": time.time()})
    r.hset(f"{key}:meta", str(user_id), entry)
    r.zadd(key, {str(user_id): elo})
    r.expire(key, QUEUE_ENTRY_TTL_SEC)


def leave_queue(user_id: int, fmt: int, ranked: bool) -> None:
    r = get_redis()
    key = _queue_key(fmt, ranked)
    r.zrem(key, str(user_id))
    r.hdel(f"{key}:meta", str(user_id))


def _entry_meta(fmt: int, ranked: bool, user_id: int) -> dict | None:
    r = get_redis()
    raw = r.hget(f"{_queue_key(fmt, ranked)}:meta", str(user_id))
    return json.loads(raw) if raw else None


def current_window(joined_at: float) -> int:
    elapsed = time.time() - joined_at
    window = INITIAL_WINDOW + WINDOW_GROWTH_PER_TICK * int(elapsed // WINDOW_GROWTH_INTERVAL_SEC)
    return min(window, MAX_WINDOW)


def should_offer_bot(joined_at: float) -> bool:
    return (time.time() - joined_at) >= BOT_OFFER_AFTER_SEC


def _recently_matched_too_often(user_id: int, candidate_id: int) -> bool:
    r = get_redis()
    history = r.lrange(_recent_opponents_key(user_id), 0, 4)
    return history.count(str(candidate_id)) >= 2


def record_pairing(user_a: int, user_b: int) -> None:
    r = get_redis()
    for a, b in ((user_a, user_b), (user_b, user_a)):
        key = _recent_opponents_key(a)
        r.lpush(key, str(b))
        r.ltrim(key, 0, 9)
        r.expire(key, 3600)


def find_match(user_id: int, fmt: int, ranked: bool) -> int | None:
    """Try to pair `user_id` with someone already queued within their
    current widening Elo window. Returns the opponent's user_id, or None."""
    r = get_redis()
    key = _queue_key(fmt, ranked)
    meta = _entry_meta(fmt, ranked, user_id)
    if meta is None:
        return None

    window = current_window(meta["joined_at"])
    lo, hi = meta["elo"] - window, meta["elo"] + window
    candidates = r.zrangebyscore(key, lo, hi)

    for candidate in candidates:
        candidate_id = int(candidate)
        if candidate_id == user_id:
            continue
        if _recently_matched_too_often(user_id, candidate_id):
            continue
        # Atomically claim both slots so two schedulers can't double-pair.
        removed = r.zrem(key, str(user_id), str(candidate_id))
        if removed == 2:
            r.hdel(f"{key}:meta", str(user_id), str(candidate_id))
            record_pairing(user_id, candidate_id)
            return candidate_id
        # Someone else grabbed one of us first; bail and let the caller retry.
        return None
    return None


def queue_position_info(user_id: int, fmt: int, ranked: bool) -> dict:
    meta = _entry_meta(fmt, ranked, user_id)
    if meta is None:
        return {"queued": False}
    elapsed = time.time() - meta["joined_at"]
    return {
        "queued": True,
        "elapsed_sec": round(elapsed, 1),
        "window": current_window(meta["joined_at"]),
        "bot_available": should_offer_bot(meta["joined_at"]),
    }
