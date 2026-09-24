"""An in-memory Chrome that speaks the slice of CDP profile sync uses.

It keeps a cookie jar and per-origin localStorage the way Chrome does, answers Fetch
interception, and records what would have reached the network, so tests can check both
what a profile put into the browser and that nothing leaked to a real site.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any
from urllib.parse import urlsplit

from openbrowse.profiles.cdp import CdpError


def _origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "null"
    return f"{parts.scheme}://{parts.netloc}"


class FakeChrome:
    def __init__(self) -> None:
        self.cookies: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.storage: dict[str, dict[str, str]] = {}
        self.targets: dict[str, dict[str, Any]] = {
            "initial": {"targetId": "initial", "type": "page", "url": "about:blank"}
        }
        self.sessions: dict[str, str] = {}
        self.fetching: set[str] = set()
        self.bypassing_workers: set[str] = set()
        self.network: list[str] = []
        self.refuse_cookie_names: set[str] = set()
        self.fail_origins: set[str] = set()
        self.slow_origins: dict[str, float] = {}
        self.open_seed_tabs = 0
        self.peak_seed_tabs = 0
        self.closed = False
        self._handlers: dict[str, list[Any]] = {}
        self._ids = itertools.count(1)
        self._paused: dict[str, asyncio.Future] = {}

    # The Cdp interface.

    def on(self, method: str, handler: Any):
        self._handlers.setdefault(method, []).append(handler)
        return lambda: self._handlers[method].remove(handler)

    async def close(self) -> None:
        self.closed = True

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        params = params or {}
        handler = getattr(self, "_" + method.replace(".", "_"), None)
        if handler is None:
            return {}
        return await asyncio.wait_for(handler(params, session_id), timeout)

    # Helpers for tests: what a site or the agent does to the browser.

    def visit(self, url: str) -> str:
        """The agent opens ``url`` in a tab of its own."""
        target_id = f"agent-{next(self._ids)}"
        self.targets[target_id] = {"targetId": target_id, "type": "page", "url": url}
        self._emit("Target.targetCreated", {"targetInfo": dict(self.targets[target_id])})
        return target_id

    def site_sets_cookie(self, name: str, value: str, domain: str) -> None:
        self.cookies[(name, domain, "/")] = self._as_chrome_returns(
            {"name": name, "value": value, "domain": domain, "path": "/"}
        )

    def jar(self) -> dict[str, str]:
        return {c["name"]: c["value"] for c in self.cookies.values()}

    # Internals.

    def _emit(self, method: str, params: dict[str, Any], session_id: str | None = None) -> None:
        for handler in list(self._handlers.get(method, ())):
            result = handler(params, session_id)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)

    @staticmethod
    def _as_chrome_returns(cookie: dict[str, Any]) -> dict[str, Any]:
        out = {
            "path": "/",
            "expires": -1,
            "size": len(cookie["name"]) + len(cookie["value"]),
            "httpOnly": False,
            "secure": False,
            "session": "expires" not in cookie,
            "priority": "Medium",
            "sameParty": False,
            "sourceScheme": "Secure",
            "sourcePort": 443,
        }
        out.update(cookie)
        return out

    async def _Target_setDiscoverTargets(self, params, session_id):
        return {}

    async def _Target_getTargets(self, params, session_id):
        return {"targetInfos": [dict(t) for t in self.targets.values()]}

    async def _Storage_setCookies(self, params, session_id):
        cookies = params["cookies"]
        for cookie in cookies:
            if cookie["name"] in self.refuse_cookie_names:
                raise CdpError("Invalid cookie fields")
            if "expires" in cookie and cookie["expires"] <= 0:
                raise CdpError("cookie already expired")
        for cookie in cookies:
            key = (cookie["name"], cookie.get("domain", ""), cookie.get("path", "/"))
            self.cookies[key] = self._as_chrome_returns(dict(cookie))
        return {}

    async def _Storage_getCookies(self, params, session_id):
        return {"cookies": [dict(c) for c in self.cookies.values()]}

    async def _Target_createTarget(self, params, session_id):
        target_id = f"tab-{next(self._ids)}"
        self.targets[target_id] = {"targetId": target_id, "type": "page", "url": params["url"]}
        self.open_seed_tabs += 1
        self.peak_seed_tabs = max(self.peak_seed_tabs, self.open_seed_tabs)
        self._emit("Target.targetCreated", {"targetInfo": dict(self.targets[target_id])})
        return {"targetId": target_id}

    async def _Target_attachToTarget(self, params, session_id):
        session = f"session-{next(self._ids)}"
        self.sessions[session] = params["targetId"]
        return {"sessionId": session}

    async def _Target_closeTarget(self, params, session_id):
        if self.targets.pop(params["targetId"], None) is not None:
            self.open_seed_tabs -= 1
        for session, target in list(self.sessions.items()):
            if target == params["targetId"]:
                del self.sessions[session]
                self.fetching.discard(session)
        return {"success": True}

    async def _Network_setBypassServiceWorker(self, params, session_id):
        if params.get("bypass"):
            self.bypassing_workers.add(session_id)
        return {}

    async def _Fetch_enable(self, params, session_id):
        if session_id not in self.bypassing_workers:
            raise AssertionError("a site's service worker would see requests before Fetch does")
        self.fetching.add(session_id)
        return {}

    async def _Fetch_fulfillRequest(self, params, session_id):
        future = self._paused.pop(params["requestId"], None)
        if future is not None and not future.done():
            future.set_result(params)
        return {}

    async def _Page_navigate(self, params, session_id):
        url = params["url"]
        origin = _origin(url)
        if origin in self.slow_origins:
            await asyncio.sleep(self.slow_origins[origin])
        if origin in self.fail_origins:
            return {"errorText": "net::ERR_FAILED"}
        if session_id in self.fetching:
            request_id = f"req-{next(self._ids)}"
            future = asyncio.get_running_loop().create_future()
            self._paused[request_id] = future
            self._emit("Fetch.requestPaused", {"requestId": request_id, "request": {"url": url}}, session_id)
            await future
        else:
            self.network.append(url)
        target = self.targets[self.sessions[session_id]]
        target["url"] = url
        self._emit("Target.targetInfoChanged", {"targetInfo": dict(target)})
        return {"frameId": "f", "loaderId": "l"}

    async def _Runtime_evaluate(self, params, session_id):
        expression = params["expression"]
        origin = _origin(self.targets[self.sessions[session_id]]["url"])
        if expression == "location.origin":
            return {"result": {"type": "string", "value": origin}}
        area = self.storage.setdefault(origin, {})
        if "localStorage.clear()" in expression:
            items = json.loads(expression.split("const items = ", 1)[1].split(";\n", 1)[0])
            area.clear()
            area.update({k: v for k, v in items})
            return {"result": {"type": "number", "value": len(area)}}
        return {"result": {"type": "string", "value": json.dumps(list(area.items()))}}
