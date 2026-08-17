"""In-memory per-IP exponential backoff for failed authentication attempts."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import HTTPException
from starlette.requests import HTTPConnection

FREE_ATTEMPTS = 5
BASE_LOCKOUT_SECONDS = 1.0
MAX_LOCKOUT_SECONDS = 900.0
ENTRY_TTL_SECONDS = 3600.0
MAX_TRACKED_IPS = 10_000


def client_ip(conn: HTTPConnection) -> str:
    # @nonobvious(forced-by): behind a reverse proxy or tunnel the socket peer
    # is the proxy itself for every visitor, so the standard forwarding header
    # is the only per-visitor identity available.
    raw = conn.headers.get("x-forwarded-for", "")
    forwarded = raw.split(",")[0].strip() if raw else ""
    if forwarded:
        return forwarded
    if conn.client and conn.client.host:
        return conn.client.host
    return "unknown"


@dataclass
class _Record:
    failures: int = 0
    locked_until: float = 0.0
    last_seen: float = field(default_factory=time.monotonic)


class AuthThrottle:
    """Tracks consecutive failed auth attempts per client IP.

    The first ``FREE_ATTEMPTS`` failures are free; each further failure locks
    the IP out for a doubling interval starting at ``BASE_LOCKOUT_SECONDS`` and
    capped at ``MAX_LOCKOUT_SECONDS``. A successful authentication clears the
    record. State is in-memory only and resets on process restart.
    """

    def __init__(self) -> None:
        self._records: dict[str, _Record] = {}

    def _prune(self, now: float) -> None:
        if len(self._records) < MAX_TRACKED_IPS:
            stale = [
                ip
                for ip, rec in self._records.items()
                if now - rec.last_seen > ENTRY_TTL_SECONDS and rec.locked_until <= now
            ]
            for ip in stale:
                del self._records[ip]
            return
        by_age = sorted(self._records.items(), key=lambda item: item[1].last_seen)
        for ip, _ in by_age[: len(by_age) // 2]:
            del self._records[ip]

    def retry_after(self, ip: str) -> float:
        rec = self._records.get(ip)
        if rec is None:
            return 0.0
        return max(0.0, rec.locked_until - time.monotonic())

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        self._prune(now)
        rec = self._records.setdefault(ip, _Record())
        rec.failures += 1
        rec.last_seen = now
        excess = rec.failures - FREE_ATTEMPTS
        if excess > 0:
            lockout = min(
                MAX_LOCKOUT_SECONDS, BASE_LOCKOUT_SECONDS * (2 ** (excess - 1))
            )
            rec.locked_until = now + lockout

    def record_success(self, ip: str) -> None:
        self._records.pop(ip, None)

    def reset(self) -> None:
        self._records.clear()


throttle = AuthThrottle()


def enforce(ip: str) -> None:
    """Raise 429 with a Retry-After header while the IP is locked out."""
    remaining = throttle.retry_after(ip)
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail="Too many failed authentication attempts. Try again later.",
            headers={"Retry-After": str(int(remaining) + 1)},
        )
