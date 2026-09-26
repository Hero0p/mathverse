"""Leaderboard queries and 'The Infinite' title (top 500 by Elo).

The title set is cached in the in-process store and refreshed lazily on read
once it is more than 10 minutes old, so a profile or leaderboard render is a
single set-membership check rather than a live ranking query.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.extensions import db, get_store
from app.models.match import Match
from app.models.user import User
from app.tiers import tier_by_name

INFINITE_TITLE_COUNT = 500
INFINITE_TITLE_KEY = "mv:infinite_title_holders"
INFINITE_FRESH_KEY = "mv:infinite_title_fresh"
INFINITE_TTL_SECONDS = 600  # 10 minutes, per spec 7.8
LEADERBOARD_PAGE_SIZE = 50


def recompute_infinite_titles() -> int:
    """Recompute the top-N ranked, non-guest players by Elo, returning how
    many holders were written."""
    top_users = (
        User.query.filter(User.is_guest.is_(False), User.elo.isnot(None))
        .order_by(User.elo.desc())
        .limit(INFINITE_TITLE_COUNT)
        .all()
    )
    store = get_store()
    pipe = store.pipeline()
    pipe.delete(INFINITE_TITLE_KEY)
    if top_users:
        pipe.sadd(INFINITE_TITLE_KEY, *[u.id for u in top_users])
    pipe.execute()
    # A separate short-lived key marks freshness; the holder set itself has
    # no TTL so a stale-but-usable answer survives while we recompute.
    store.set(INFINITE_FRESH_KEY, "1", ex=INFINITE_TTL_SECONDS)
    return len(top_users)


def _ensure_fresh() -> None:
    """Recompute on read when the cache has aged out.

    This replaces what was a Celery beat job. With no broker or scheduler
    process, refreshing lazily on access keeps the same 10-minute staleness
    bound without anything extra to run.
    """
    if get_store().get(INFINITE_FRESH_KEY) is None:
        recompute_infinite_titles()


def holds_infinite_title(user_id: int) -> bool:
    _ensure_fresh()
    return bool(get_store().sismember(INFINITE_TITLE_KEY, user_id))


def get_leaderboard(tier: str | None = None, window: str = "all", page: int = 1) -> dict:
    page = max(1, page)
    offset = (page - 1) * LEADERBOARD_PAGE_SIZE

    query = User.query.filter(User.is_guest.is_(False), User.elo.isnot(None))
    if tier:
        query = query.filter(User.tier == tier_by_name(tier).name)

    if window == "30d":
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        win_counts = dict(
            db.session.query(Match.winner_id, func.count(Match.id))
            .filter(Match.ended_at >= cutoff, Match.is_ranked.is_(True), Match.winner_id.isnot(None))
            .group_by(Match.winner_id)
            .all()
        )
        candidates = query.all()
        candidates.sort(key=lambda u: (win_counts.get(u.id, 0), u.elo), reverse=True)
        total = len(candidates)
        page_users = candidates[offset: offset + LEADERBOARD_PAGE_SIZE]
        rows = [
            {**u.to_public_dict(), "rank": offset + i + 1,
             "recent_wins": win_counts.get(u.id, 0),
             "is_infinite": holds_infinite_title(u.id)}
            for i, u in enumerate(page_users)
        ]
    else:
        total = query.count()
        page_users = (
            query.order_by(User.elo.desc()).offset(offset).limit(LEADERBOARD_PAGE_SIZE).all()
        )
        rows = [
            {**u.to_public_dict(), "rank": offset + i + 1, "is_infinite": holds_infinite_title(u.id)}
            for i, u in enumerate(page_users)
        ]

    return {
        "rows": rows,
        "page": page,
        "page_size": LEADERBOARD_PAGE_SIZE,
        "total": total,
        "window": window,
        "tier": tier,
    }
