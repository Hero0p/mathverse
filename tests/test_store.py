"""The in-process store replaces Redis for all ephemeral state, so its
semantics need to match what the calling code expects -- particularly
string-typed reads, inclusive list/zset ranges, and TTL expiry."""
import threading

import pytest

from app.services.store import LocalStore


@pytest.fixture()
def store():
    return LocalStore()


# --- strings --------------------------------------------------------------
def test_get_missing_key_returns_none(store):
    assert store.get("nope") is None


def test_values_come_back_as_strings(store):
    """Callers do int(store.get(k) or 0) and json.loads(...), matching
    redis-py with decode_responses=True."""
    store.set("n", 42)
    assert store.get("n") == "42"


def test_incr_starts_from_zero_and_accumulates(store):
    assert store.incr("hits") == 1
    assert store.incr("hits") == 2
    assert store.get("hits") == "2"


def test_delete_removes_key(store):
    store.set("k", "v")
    assert store.delete("k") == 1
    assert store.get("k") is None
    assert store.delete("k") == 0


def test_expired_key_reads_as_missing(store, monkeypatch):
    import app.services.store as store_module

    clock = {"t": 1000.0}
    monkeypatch.setattr(store_module, "_now", lambda: clock["t"])

    store.set("temp", "v", ex=60)
    assert store.get("temp") == "v"
    clock["t"] += 61
    assert store.get("temp") is None


def test_expire_sets_ttl_on_existing_key(store):
    store.set("k", "v")
    assert store.expire("k", 60) is True
    assert 0 < store.ttl("k") <= 60


def test_expire_on_missing_key_is_a_noop(store):
    assert store.expire("ghost", 60) is False


# --- hashes ---------------------------------------------------------------
def test_hash_roundtrip(store):
    store.hset("h", "field", "value")
    assert store.hget("h", "field") == "value"
    assert store.hget("h", "absent") is None
    assert store.hdel("h", "field") == 1
    assert store.hget("h", "field") is None


# --- sorted sets (matchmaking queue) -------------------------------------
def test_zrangebyscore_filters_and_orders_by_score(store):
    store.zadd("queue", {"low": 800, "mid": 1200, "high": 1900})
    assert store.zrangebyscore("queue", 1000, 1500) == ["mid"]
    assert store.zrangebyscore("queue", 0, 3000) == ["low", "mid", "high"]


def test_zrangebyscore_bounds_are_inclusive(store):
    store.zadd("queue", {"exact": 1000})
    assert store.zrangebyscore("queue", 1000, 1000) == ["exact"]


def test_zrem_reports_how_many_were_removed(store):
    """matchmaking_service relies on this count to claim both players
    atomically -- it only pairs when exactly 2 were removed."""
    store.zadd("queue", {"a": 1, "b": 2})
    assert store.zrem("queue", "a", "b") == 2
    assert store.zrem("queue", "a", "b") == 0


def test_zadd_updates_score_of_existing_member(store):
    store.zadd("queue", {"player": 1000})
    store.zadd("queue", {"player": 1500})
    assert store.zcard("queue") == 1
    assert store.zrangebyscore("queue", 1400, 1600) == ["player"]


# --- lists (recent opponents) --------------------------------------------
def test_lpush_prepends_and_lrange_is_inclusive(store):
    store.lpush("recent", "1")
    store.lpush("recent", "2")
    assert store.lrange("recent", 0, -1) == ["2", "1"]
    assert store.lrange("recent", 0, 0) == ["2"]


def test_ltrim_caps_list_length(store):
    for i in range(10):
        store.lpush("recent", str(i))
    store.ltrim("recent", 0, 4)
    assert len(store.lrange("recent", 0, -1)) == 5


def test_lrange_on_missing_key_is_empty(store):
    assert store.lrange("nothing", 0, -1) == []


# --- sets (Infinite title) ------------------------------------------------
def test_set_membership(store):
    store.sadd("titles", 1, 2, 3)
    assert store.sismember("titles", 1) is True
    assert store.sismember("titles", 99) is False


def test_sismember_compares_as_string(store):
    """Callers pass ints while members were stored from ints elsewhere."""
    store.sadd("titles", 7)
    assert store.sismember("titles", 7) is True
    assert store.sismember("titles", "7") is True


# --- pipeline -------------------------------------------------------------
def test_pipeline_applies_queued_commands_in_order(store):
    pipe = store.pipeline()
    pipe.incr("count")
    pipe.expire("count", 60)
    results = pipe.execute()

    assert results[0] == 1
    assert store.get("count") == "1"
    assert 0 < store.ttl("count") <= 60


def test_pipeline_clears_after_execute(store):
    pipe = store.pipeline()
    pipe.incr("count")
    pipe.execute()
    pipe.execute()  # nothing queued: must not double-apply
    assert store.get("count") == "1"


def test_pipeline_delete_then_sadd(store):
    """The exact shape leaderboard_service uses to swap the title set."""
    store.sadd("titles", "old")
    pipe = store.pipeline()
    pipe.delete("titles")
    pipe.sadd("titles", "new")
    pipe.execute()

    assert store.sismember("titles", "old") is False
    assert store.sismember("titles", "new") is True


# --- concurrency ----------------------------------------------------------
def test_incr_is_atomic_under_threads(store):
    """Renders and requests share this store across threads."""
    def bump():
        for _ in range(200):
            store.incr("counter")

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert store.get("counter") == str(8 * 200)


def test_flushall_clears_everything(store):
    store.set("a", "1")
    store.sadd("b", "x")
    store.flushall()
    assert store.get("a") is None
    assert store.sismember("b", "x") is False
