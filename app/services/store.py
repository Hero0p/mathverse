"""In-process key/value store used for ephemeral state.

MathVerse runs as a single local process, so the ephemeral state that would
normally live in Redis -- rate-limit counters, the matchmaking queue, live
placement quizzes, the cached "Infinite" title set -- lives here instead.

It implements the small slice of the Redis API the app actually uses
(strings, hashes, sorted sets, lists, sets, TTLs, and pipelines), with the
same string-in/string-out behaviour as `redis-py` configured with
`decode_responses=True`, so callers read the same as they would against a
real server.

Everything is guarded by a single reentrant lock. That is more than enough
for one process; it is deliberately NOT a distributed store, so state is
per-process and vanishes on restart. Anything that must survive a restart
belongs in the database, not here.
"""
from __future__ import annotations  # this class defines set()/get(), which
# would otherwise shadow the builtins inside annotations like `set[str]`

import fnmatch
import threading
import time
from typing import Any


def _now() -> float:
    return time.monotonic()


class LocalStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._values: dict[str, Any] = {}
        self._expiry: dict[str, float] = {}

    # --- expiry ---------------------------------------------------------
    def _expired(self, key: str) -> bool:
        deadline = self._expiry.get(key)
        return deadline is not None and _now() >= deadline

    def _drop_if_expired(self, key: str) -> None:
        if self._expired(key):
            self._values.pop(key, None)
            self._expiry.pop(key, None)

    def _get_container(self, key: str, kind: type):
        """Fetch (or create) the container at `key`, honouring expiry."""
        self._drop_if_expired(key)
        current = self._values.get(key)
        if current is None:
            current = kind()
            self._values[key] = current
        return current

    # --- strings --------------------------------------------------------
    def get(self, key: str) -> str | None:
        with self._lock:
            self._drop_if_expired(key)
            value = self._values.get(key)
            return None if value is None else str(value)

    def set(self, key: str, value: Any, ex: int | None = None) -> bool:
        with self._lock:
            self._values[key] = str(value)
            if ex is not None:
                self._expiry[key] = _now() + ex
            else:
                self._expiry.pop(key, None)
            return True

    def incr(self, key: str, amount: int = 1) -> int:
        with self._lock:
            self._drop_if_expired(key)
            new_value = int(self._values.get(key, 0)) + amount
            self._values[key] = str(new_value)
            return new_value

    def expire(self, key: str, seconds: int) -> bool:
        with self._lock:
            if key not in self._values:
                return False
            self._expiry[key] = _now() + seconds
            return True

    def ttl(self, key: str) -> int:
        with self._lock:
            self._drop_if_expired(key)
            if key not in self._values:
                return -2
            deadline = self._expiry.get(key)
            return -1 if deadline is None else max(0, int(deadline - _now()))

    def delete(self, *keys: str) -> int:
        with self._lock:
            removed = 0
            for key in keys:
                if self._values.pop(key, None) is not None:
                    removed += 1
                self._expiry.pop(key, None)
            return removed

    def exists(self, key: str) -> bool:
        with self._lock:
            self._drop_if_expired(key)
            return key in self._values

    def keys(self, pattern: str = "*") -> list[str]:
        with self._lock:
            for key in list(self._values):
                self._drop_if_expired(key)
            return [k for k in self._values if fnmatch.fnmatch(k, pattern)]

    # --- hashes ---------------------------------------------------------
    def hset(self, key: str, field: str, value: Any) -> int:
        with self._lock:
            table = self._get_container(key, dict)
            is_new = field not in table
            table[field] = str(value)
            return 1 if is_new else 0

    def hget(self, key: str, field: str) -> str | None:
        with self._lock:
            self._drop_if_expired(key)
            return self._values.get(key, {}).get(field)

    def hdel(self, key: str, *fields: str) -> int:
        with self._lock:
            self._drop_if_expired(key)
            table = self._values.get(key)
            if not isinstance(table, dict):
                return 0
            return sum(1 for f in fields if table.pop(f, None) is not None)

    def hgetall(self, key: str) -> dict[str, str]:
        with self._lock:
            self._drop_if_expired(key)
            return dict(self._values.get(key, {}))

    # --- sorted sets ----------------------------------------------------
    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        with self._lock:
            zset = self._get_container(key, dict)
            added = sum(1 for m in mapping if m not in zset)
            for member, score in mapping.items():
                zset[str(member)] = float(score)
            return added

    def zrem(self, key: str, *members: str) -> int:
        with self._lock:
            self._drop_if_expired(key)
            zset = self._values.get(key)
            if not isinstance(zset, dict):
                return 0
            return sum(1 for m in members if zset.pop(str(m), None) is not None)

    def zrangebyscore(self, key: str, low: float, high: float) -> list[str]:
        with self._lock:
            self._drop_if_expired(key)
            zset = self._values.get(key)
            if not isinstance(zset, dict):
                return []
            hits = [(s, m) for m, s in zset.items() if low <= s <= high]
            return [m for _, m in sorted(hits)]

    def zcard(self, key: str) -> int:
        with self._lock:
            self._drop_if_expired(key)
            zset = self._values.get(key)
            return len(zset) if isinstance(zset, dict) else 0

    # --- lists ----------------------------------------------------------
    def lpush(self, key: str, *values: Any) -> int:
        with self._lock:
            items = self._get_container(key, list)
            for value in values:
                items.insert(0, str(value))
            return len(items)

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        with self._lock:
            self._drop_if_expired(key)
            items = self._values.get(key)
            if not isinstance(items, list):
                return []
            # Redis end index is inclusive; -1 means "to the end".
            stop = len(items) if end == -1 else end + 1
            return items[start:stop]

    def ltrim(self, key: str, start: int, end: int) -> bool:
        with self._lock:
            self._drop_if_expired(key)
            items = self._values.get(key)
            if not isinstance(items, list):
                return True
            stop = len(items) if end == -1 else end + 1
            self._values[key] = items[start:stop]
            return True

    # --- sets -----------------------------------------------------------
    def sadd(self, key: str, *members: Any) -> int:
        with self._lock:
            members_set = self._get_container(key, set)
            before = len(members_set)
            members_set.update(str(m) for m in members)
            return len(members_set) - before

    def sismember(self, key: str, member: Any) -> bool:
        with self._lock:
            self._drop_if_expired(key)
            members_set = self._values.get(key)
            return isinstance(members_set, set) and str(member) in members_set

    def smembers(self, key: str) -> set[str]:
        with self._lock:
            self._drop_if_expired(key)
            members_set = self._values.get(key)
            return set(members_set) if isinstance(members_set, set) else set()

    # --- pipeline -------------------------------------------------------
    def pipeline(self) -> "LocalPipeline":
        return LocalPipeline(self)

    def ping(self) -> bool:
        return True

    def flushall(self) -> bool:
        """Reset everything. Used between tests."""
        with self._lock:
            self._values.clear()
            self._expiry.clear()
            return True


class LocalPipeline:
    """Queues commands and applies them on execute(), mirroring redis-py's
    pipeline API. Not atomic across commands -- single process, no contention
    worth guarding against."""

    def __init__(self, store: LocalStore):
        self._store = store
        self._queued: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        if not hasattr(LocalStore, name):
            raise AttributeError(name)

        def _queue(*args, **kwargs):
            self._queued.append((name, args, kwargs))
            return self

        return _queue

    def execute(self) -> list:
        with self._store._lock:
            results = [
                getattr(self._store, name)(*args, **kwargs)
                for name, args, kwargs in self._queued
            ]
        self._queued.clear()
        return results

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._queued.clear()
        return False
