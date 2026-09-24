"""Carry a profile into a session's browser and its changes back out.

Every session starts from an empty Chrome. Before the agent attaches, the profile's cookies
go into the browser's cookie store and each site's localStorage into that site's own
storage, so a page only ever sees its own site's data, loaded when it asks for it, as in a
browser that had been running all along. While the session runs, the cookie jar is
checkpointed into the profile, so an interrupted session loses at most a minute of logins.
When the session ends, the cookies and the storage of every site it opened are read back
and merged into the profile under its lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openbrowse.profiles import policy, store
from openbrowse.profiles.cdp import Cdp, CdpConnection
from openbrowse.profiles.merge import cookie_meaning, merge_storage_states
from openbrowse.profiles.seed import (
    Visit,
    is_blank_url,
    read_local_storage,
    visit_origins,
    write_local_storage,
)
from openbrowse.profiles.storage import normalise_origin, normalize_storage_state

logger = logging.getLogger(__name__)

CHECKPOINT_S = 60.0
_RESTORE_DEADLINE_S = 45.0
_CAPTURE_DEADLINE_S = 60.0
_SEED_TABS = 4


@dataclass
class Restored:
    cookies: int = 0
    cookie_failures: int = 0
    sites: int = 0
    site_failures: dict[str, str] = field(default_factory=dict)
    elapsed_s: float = 0.0


def _settable(cookie: dict[str, Any]) -> dict[str, Any]:
    out = dict(cookie)
    expires = out.get("expires")
    # @nonobvious(forced-by): Playwright writes a session cookie as expires -1 or 0,
    # and CDP reads either as already expired.
    if not isinstance(expires, (int, float)) or expires <= 0:
        out.pop("expires", None)
    return out


async def set_cookies(cdp: Cdp, cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put ``cookies`` into the browser's jar; returns the ones Chrome refused."""
    if not cookies:
        return []
    try:
        await cdp.send("Storage.setCookies", {"cookies": [_settable(c) for c in cookies]})
        return []
    except Exception:
        pass

    async def one(cookie: dict[str, Any]) -> dict[str, Any] | None:
        try:
            await cdp.send("Storage.setCookies", {"cookies": [_settable(cookie)]})
            return None
        except Exception:
            return cookie

    refused = await asyncio.gather(*(one(c) for c in cookies))
    return [c for c in refused if c is not None]


async def read_cookies(cdp: Cdp) -> list[dict[str, Any]]:
    reply = await cdp.send("Storage.getCookies", {})
    return normalize_storage_state({"cookies": reply.get("cookies") or []})["cookies"]


class ProfileSync:
    """One session's use of one profile, from restore to write-back."""

    def __init__(
        self, path: Path, cdp_url: str, *, label: str = "", ignore: set[str] | None = None
    ) -> None:
        self.path = path
        self.cdp_url = cdp_url
        self.label = label or path.stem
        self.ignore = {o for o in (normalise_origin(u) for u in ignore or ()) if o}
        self.visited: set[str] = set()
        self._cdp: CdpConnection | None = None
        self._baseline: dict[str, Any] = {"cookies": [], "origins": []}
        self._unrestored: set[str] = set()
        self._checkpoint: asyncio.Task | None = None
        self._last_cookies: set[tuple[Any, ...]] = set()
        self._restored = False

    async def restore(self) -> Restored:
        """Load the profile into the browser. Call before anything navigates."""
        started = time.monotonic()
        restored = Restored()
        async with store.lock(self.path):
            data = store.load(self.path) or store.ProfileData()
        try:
            applied = policy.apply(data.state, data.last_used, now=time.time(), added=data.added)
        except ValueError:
            logger.warning("Profile %s is not a storage state; starting empty", self.label)
            applied = policy.apply({}, {}, now=time.time())
        state = applied.state

        self._cdp = await CdpConnection.open(self.cdp_url)
        self._cdp.on("Target.targetCreated", self._on_target)
        self._cdp.on("Target.targetInfoChanged", self._on_target)
        await self._cdp.send("Target.setDiscoverTargets", {"discover": True})

        refused = await set_cookies(self._cdp, state["cookies"])
        refused_keys = {policy.cookie_key(c) for c in refused}
        restored.cookies = len(state["cookies"]) - len(refused)
        restored.cookie_failures = len(refused)

        origins = {entry["origin"]: entry["localStorage"] for entry in state["origins"]}
        visit = Visit()
        try:
            await asyncio.wait_for(self._seed(origins, visit), _RESTORE_DEADLINE_S)
        except asyncio.TimeoutError:
            pass
        seeded = set(visit.results)
        restored.site_failures = {
            o: visit.failures.get(o, "restore deadline passed") for o in origins if o not in seeded
        }
        restored.sites = len(seeded)
        self._unrestored = set(origins) - seeded

        # @nonobvious(must-hold): the baseline is only what actually reached the
        # browser. A cookie or site that failed to restore must not read as one
        # this session deleted, or writing back would erase it from the profile.
        self._baseline = {
            "cookies": [c for c in state["cookies"] if policy.cookie_key(c) not in refused_keys],
            "origins": [e for e in state["origins"] if e["origin"] not in self._unrestored],
        }
        self._last_cookies = {cookie_meaning(c) + policy.cookie_key(c) for c in self._baseline["cookies"]}
        self._restored = True
        self._checkpoint = asyncio.create_task(self._checkpoint_loop())
        restored.elapsed_s = time.monotonic() - started
        logger.info(
            "Profile %s restored in %.1fs: %d cookies (%d refused), %d sites (%d failed)",
            self.label,
            restored.elapsed_s,
            restored.cookies,
            restored.cookie_failures,
            restored.sites,
            len(restored.site_failures),
        )
        return restored

    async def _seed(self, origins: dict[str, list[dict[str, str]]], visit: Visit) -> Visit:
        assert self._cdp is not None

        async def work(cdp: Cdp, session_id: str, origin: str) -> int:
            return await write_local_storage(origins[origin])(cdp, session_id, origin)

        return await visit_origins(self._cdp, list(origins), work, tabs=_SEED_TABS, visit=visit)

    def _on_target(self, event: dict[str, Any], _session_id: str | None) -> None:
        info = event.get("targetInfo") or {}
        url = str(info.get("url") or "")
        if info.get("type") != "page" or is_blank_url(url):
            return
        origin = normalise_origin(url)
        if origin and origin not in self.ignore:
            self.visited.add(origin)

    async def _checkpoint_loop(self) -> None:
        while True:
            await asyncio.sleep(CHECKPOINT_S)
            try:
                await self.checkpoint()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Profile %s cookie checkpoint failed", self.label, exc_info=True)

    async def checkpoint(self) -> bool:
        """Write the browser's cookies into the profile if they changed; True if written."""
        assert self._cdp is not None
        cookies = await read_cookies(self._cdp)
        signature = {cookie_meaning(c) + policy.cookie_key(c) for c in cookies}
        if signature == self._last_cookies:
            return False
        await self._write_back({"cookies": cookies, "origins": []}, origins=set())
        self._last_cookies = signature
        return True

    async def abandon(self) -> None:
        """Let go of the browser without writing anything back."""
        if self._checkpoint is not None:
            self._checkpoint.cancel()
            with contextlib.suppress(BaseException):
                await self._checkpoint
            self._checkpoint = None
        if self._cdp is not None:
            await self._cdp.close()
            self._cdp = None

    async def write_back(self) -> None:
        """Read the session's changes out of the browser and merge them into the profile.
        Call after the agent has let go of the browser and before it is stopped."""
        if self._checkpoint is not None:
            self._checkpoint.cancel()
            with contextlib.suppress(BaseException):
                await self._checkpoint
        if self._cdp is None:
            return
        if not self._restored:
            # @nonobvious(must-hold): without a finished restore there is no
            # baseline, and every cookie would read as this session's change.
            await self.abandon()
            return
        try:
            await self._capture_and_merge()
        finally:
            await self._cdp.close()
            self._cdp = None

    async def _capture_and_merge(self) -> None:
        assert self._cdp is not None
        with contextlib.suppress(Exception):
            reply = await self._cdp.send("Target.getTargets", {})
            for info in reply.get("targetInfos") or []:
                self._on_target({"targetInfo": info}, None)
        # @nonobvious(must-hold): cookies go in first, on their own, so a storage
        # read that runs out of time can never cost the session its logins.
        cookies = await read_cookies(self._cdp)
        await self._write_back({"cookies": cookies, "origins": []}, origins=set())

        visit = Visit()
        wanted = sorted(self.visited - self._unrestored)
        try:
            await asyncio.wait_for(
                visit_origins(self._cdp, wanted, read_local_storage, tabs=_SEED_TABS, visit=visit),
                _CAPTURE_DEADLINE_S,
            )
        except asyncio.TimeoutError:
            logger.info(
                "Profile %s: read back %d of %d sites before the deadline",
                self.label,
                len(visit.results),
                len(wanted),
            )
        for origin, error in visit.failures.items():
            logger.info("Profile %s: could not read %s back (%s)", self.label, origin, error)
        read = dict(visit.results)
        ours = {
            "cookies": cookies,
            "origins": [
                {"origin": origin, "localStorage": items} for origin, items in read.items() if items
            ],
        }
        await self._write_back(ours, origins=set(read))

    async def _write_back(self, ours: dict[str, Any], *, origins: set[str]) -> None:
        async with store.lock(self.path):
            theirs = store.load(self.path)
            if theirs is None:
                logger.info("Profile %s is gone; nothing written back", self.label)
                return
            now = time.time()
            # @nonobvious(must-hold): the profile's limits apply before this
            # session's visits are stamped, so a site that expired before the
            # session began stays expired instead of being revived by the visit.
            current = policy.apply(theirs.state, theirs.last_used, now=now, added=theirs.added)
            merged = merge_storage_states(self._baseline, ours, current.state, origins=origins)
            last_used = dict(current.last_used)
            for origin in self.visited:
                last_used[origin] = now
            store.save(self.path, store.ProfileData(merged, last_used, current.added), now=now)
        spoken_for = {e["origin"]: e for e in ours["origins"]}
        self._baseline = {
            "cookies": ours["cookies"],
            "origins": [
                e for e in self._baseline["origins"] if e["origin"] not in origins
            ]
            + [spoken_for[o] for o in origins if o in spoken_for],
        }
