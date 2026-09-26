"""Per-user daily rate limits (video renders, whiteboard AI queries),
backed by Redis counters that expire at midnight UTC."""
from datetime import datetime, timedelta, timezone

from flask import current_app

from app.extensions import get_redis


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


def _key(user_id: int, bucket: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"mv:ratelimit:{bucket}:{day}:{user_id}"


def _limit_for(user, bucket: str) -> int:
    if bucket == "video":
        return (
            current_app.config["GUEST_DAILY_VIDEOS"]
            if user.is_guest
            else current_app.config["USER_DAILY_VIDEOS"]
        )
    if bucket in ("whiteboard", "chat"):
        # Text answers are cheap next to a render, so chat shares the more
        # generous AI-query allowance rather than the video budget.
        return (
            current_app.config["GUEST_DAILY_WHITEBOARD"]
            if user.is_guest
            else current_app.config["USER_DAILY_WHITEBOARD"]
        )
    raise ValueError(f"unknown rate limit bucket: {bucket}")


def remaining(user, bucket: str) -> int:
    r = get_redis()
    used = int(r.get(_key(user.id, bucket)) or 0)
    return max(0, _limit_for(user, bucket) - used)


def check_and_increment(user, bucket: str) -> bool:
    """Returns True and increments the counter if the user is under their
    daily limit; returns False (no increment) if they're at the limit."""
    r = get_redis()
    key = _key(user.id, bucket)
    limit = _limit_for(user, bucket)
    used = int(r.get(key) or 0)
    if used >= limit:
        return False
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, _seconds_until_utc_midnight())
    pipe.execute()
    return True
