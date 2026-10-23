"""Small security helpers: session tokens, CSRF tokens, login throttling."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Session tokens are stored hashed so a leaked DB cannot be replayed as cookies."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode(), b.encode())


class LoginThrottle:
    """In-memory sliding-window limiter for failed logins.

    Keys are e.g. ``ip:10.0.0.5`` and ``user:alice`` — throttling on both means a
    spoofed X-Forwarded-For cannot be used to brute-force a single account.
    Limits are read on every call so Settings changes apply immediately.
    """

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, window_s: float, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > window_s:
            q.popleft()
        if not q:
            self._failures.pop(key, None)
        return q

    def retry_after(self, keys: list[str], max_attempts: int, window_s: float) -> int:
        """Seconds until any of ``keys`` may try again (0 = allowed now)."""
        now = time.monotonic()
        wait = 0.0
        for key in keys:
            q = self._prune(key, window_s, now)
            if len(q) >= max_attempts:
                wait = max(wait, window_s - (now - q[0]))
        return int(wait) + (1 if wait else 0)

    def record_failure(self, keys: list[str]) -> None:
        now = time.monotonic()
        for key in keys:
            self._failures[key].append(now)

    def reset(self, keys: list[str]) -> None:
        for key in keys:
            self._failures.pop(key, None)
