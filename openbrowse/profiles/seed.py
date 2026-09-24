"""Reach a site's own storage without loading the site.

A background tab navigates to a path on the origin, and the request is answered from here
with an empty page before it leaves the browser. The tab is then a real document on that
origin, so it reads and writes the origin's localStorage natively, while no site script
runs and nothing reaches the site's servers. This is how a profile's data goes into a
fresh browser and comes back out of it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from openbrowse.profiles.cdp import Cdp

logger = logging.getLogger(__name__)

BLANK_PATH = "/__openbrowse_blank"
_BLANK_BODY = base64.b64encode(
    b"<!doctype html><html><head><title></title></head><body></body></html>"
).decode()
_ORIGIN_TIMEOUT_S = 10.0
_POLL_S = 0.02
_CLOSE_WAIT_S = 2.0

Work = Callable[[Cdp, str, str], Awaitable[Any]]


@dataclass
class Visit:
    results: dict[str, Any] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    elapsed_s: float = 0.0


def is_blank_url(url: str) -> bool:
    return BLANK_PATH in url


async def _evaluate(cdp: Cdp, session_id: str, expression: str, timeout: float) -> Any:
    reply = await cdp.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": False},
        session_id=session_id,
        timeout=timeout,
    )
    if reply.get("exceptionDetails"):
        detail = reply["exceptionDetails"]
        text = (detail.get("exception") or {}).get("description") or detail.get("text")
        raise RuntimeError(str(text))
    return (reply.get("result") or {}).get("value")


async def evaluate(cdp: Cdp, session_id: str, expression: str) -> Any:
    return await _evaluate(cdp, session_id, expression, _ORIGIN_TIMEOUT_S)


async def _open_on(cdp: Cdp, session_id: str, origin: str) -> None:
    reply = await cdp.send(
        "Page.navigate", {"url": origin + BLANK_PATH}, session_id=session_id, timeout=_ORIGIN_TIMEOUT_S
    )
    if reply.get("errorText"):
        raise RuntimeError(reply["errorText"])
    deadline = time.monotonic() + _ORIGIN_TIMEOUT_S
    while True:
        try:
            if await _evaluate(cdp, session_id, "location.origin", 2.0) == origin:
                return
        except Exception:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"blank page on {origin} did not open")
        await asyncio.sleep(_POLL_S)


async def _until_closed(cdp: Cdp, target_ids: set[str]) -> None:
    """Chrome closes a tab after answering closeTarget; wait until it has, so a client
    that attaches next never finds a half-closed tab among the browser's pages."""
    deadline = time.monotonic() + _CLOSE_WAIT_S
    while target_ids and time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            reply = await cdp.send("Target.getTargets", {})
            if not target_ids & {t.get("targetId") for t in reply.get("targetInfos") or []}:
                return
        await asyncio.sleep(0.05)


async def visit_origins(
    cdp: Cdp,
    origins: list[str],
    work: Work,
    *,
    tabs: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
    visit: Visit | None = None,
) -> Visit:
    """Run ``work(cdp, session_id, origin)`` on a blank page of each origin.

    Origins are shared across up to ``tabs`` background tabs. One origin failing never
    stops the rest; its error is recorded instead. Results land in ``visit`` as each
    origin finishes, so a caller that passes its own keeps them if it cancels the rest.
    """
    visit = visit if visit is not None else Visit()
    if not origins:
        return visit
    started = time.monotonic()
    queue: asyncio.Queue[str] = asyncio.Queue()
    for origin in origins:
        queue.put_nowait(origin)
    our_sessions: set[str] = set()
    our_tabs: set[str] = set()

    async def fulfil(event: dict[str, Any], session_id: str | None) -> None:
        if session_id not in our_sessions:
            return
        request_id = event.get("requestId")
        with contextlib.suppress(Exception):
            await cdp.send(
                "Fetch.fulfillRequest",
                {
                    "requestId": request_id,
                    "responseCode": 200,
                    "responseHeaders": [
                        {"name": "Content-Type", "value": "text/html; charset=utf-8"},
                        {"name": "Cache-Control", "value": "no-store"},
                    ],
                    "body": _BLANK_BODY,
                },
                session_id=session_id,
            )

    unsubscribe = cdp.on("Fetch.requestPaused", fulfil)

    async def worker() -> None:
        created: str | None = None
        session_id: str | None = None
        try:
            created = (
                await cdp.send("Target.createTarget", {"url": "about:blank", "background": True})
            )["targetId"]
            our_tabs.add(created)
            attached = await cdp.send(
                "Target.attachToTarget", {"targetId": created, "flatten": True}
            )
            session_id = attached["sessionId"]
            our_sessions.add(session_id)
            # @nonobvious(must-hold): every request the tab makes is answered here,
            # favicon included, so nothing from a profile visit reaches the network;
            # a site's service worker would otherwise take the navigation first and
            # fetch from its own target, where nothing is intercepted.
            await cdp.send("Network.setBypassServiceWorker", {"bypass": True}, session_id=session_id)
            await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session_id=session_id)
            while not queue.empty():
                origin = queue.get_nowait()
                try:
                    await _open_on(cdp, session_id, origin)
                    visit.results[origin] = await work(cdp, session_id, origin)
                except Exception as exc:
                    visit.failures[origin] = str(exc) or type(exc).__name__
                if on_progress:
                    on_progress(len(visit.results) + len(visit.failures), len(origins))
        except Exception as exc:
            while not queue.empty():
                visit.failures[queue.get_nowait()] = f"no tab: {exc}"
        finally:
            if session_id:
                our_sessions.discard(session_id)
            if created:
                with contextlib.suppress(Exception):
                    await cdp.send("Target.closeTarget", {"targetId": created})

    try:
        await asyncio.gather(*(worker() for _ in range(max(1, min(tabs, len(origins))))))
        await _until_closed(cdp, our_tabs)
    finally:
        unsubscribe()
    visit.elapsed_s = time.monotonic() - started
    return visit


_WRITE = """(() => {{
  const items = {items};
  localStorage.clear();
  for (const [k, v] of items) localStorage.setItem(k, v);
  return localStorage.length;
}})()"""

_READ = """(() => {
  const out = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    out.push([k, localStorage.getItem(k)]);
  }
  return JSON.stringify(out);
})()"""


def write_local_storage(items: list[dict[str, str]]) -> Work:
    payload = json.dumps([[item["name"], item["value"]] for item in items], ensure_ascii=False)

    async def work(cdp: Cdp, session_id: str, origin: str) -> int:
        return await evaluate(cdp, session_id, _WRITE.format(items=payload))

    return work


async def read_local_storage(cdp: Cdp, session_id: str, origin: str) -> list[dict[str, str]]:
    raw = await evaluate(cdp, session_id, _READ)
    return [{"name": k, "value": v} for k, v in json.loads(raw or "[]")]
