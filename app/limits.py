"""Small in-memory limiters. Every table has a hard size bound.

They live in one process, which is enough for a PoC. A real deployment would
keep the counters in a shared store and identify clients by more than an address.
"""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from datetime import date

MAX_KEYS = 5000


class _LRU(OrderedDict):
    """Dict that never holds more than `max_keys`: the least recently used key is dropped."""

    def __init__(self, max_keys: int):
        super().__init__()
        self.max_keys = max_keys

    def touch(self, key, default):
        if key in self:
            self.move_to_end(key)
            return self[key]
        while len(self) >= self.max_keys:
            self.popitem(last=False)
        self[key] = default
        return default


class WindowLimiter:
    """At most `limit` events per `window_s` seconds for each key."""

    def __init__(self, limit: int, window_s: float, max_keys: int = MAX_KEYS):
        self.limit, self.window_s = limit, window_s
        self._calls = _LRU(max_keys)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        calls = self._calls.get(key)
        if calls is not None:
            while calls and now - calls[0] > self.window_s:
                calls.popleft()
            if len(calls) >= self.limit:
                self._calls.move_to_end(key)
                return False
        # the key is stored only once a request is actually admitted
        self._calls.touch(key, deque()).append(now)
        return True

    def reset(self) -> None:
        self._calls.clear()

    def __len__(self) -> int:
        return len(self._calls)


class DailyQuota:
    """A daily budget for the whole service plus a smaller one per client.

    The per-client share stops one client from spending the whole budget.
    `reserve` before the paid call, `refund` if the call turned out not to be billable.
    """

    def __init__(self, total: int, per_client: int, max_keys: int = MAX_KEYS):
        self.total, self.per_client = total, per_client
        self._day: date | None = None
        self._used = 0
        self._by_client = _LRU(max_keys)

    def _roll(self) -> None:
        today = date.today()
        if self._day != today:
            self._day, self._used = today, 0
            self._by_client.clear()

    def reserve(self, client: str) -> str | None:
        """None when the call may proceed, otherwise which budget is exhausted."""
        self._roll()
        if self._used >= self.total:
            return "service"
        if self._by_client.get(client, 0) >= self.per_client:
            return "client"
        self._by_client[client] = self._by_client.touch(client, 0) + 1
        self._used += 1
        return None

    def refund(self, client: str) -> None:
        self._roll()
        if self._by_client.get(client, 0) > 0:
            self._by_client[client] -= 1
            self._used = max(0, self._used - 1)

    def reset(self) -> None:
        self._day, self._used = None, 0
        self._by_client.clear()
