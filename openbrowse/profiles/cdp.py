"""A browser-level CDP connection that several callers can share.

Profile restore and capture talk to Chrome on their own socket rather than through
browser-use's session, so they can run before the agent attaches and after it lets go,
and so nothing they open or intercept is ever mistaken for one of the agent's tabs.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from typing import Any, Awaitable, Callable, Protocol

import httpx
import websockets

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any], str | None], Awaitable[None] | None]


class Cdp(Protocol):
    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]: ...

    def on(self, method: str, handler: Handler) -> Callable[[], None]: ...


class CdpError(RuntimeError):
    pass


class _Handlers:
    def __init__(self) -> None:
        self._by_method: dict[str, list[Handler]] = {}

    def add(self, method: str, handler: Handler) -> Callable[[], None]:
        self._by_method.setdefault(method, []).append(handler)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._by_method.get(method, []).remove(handler)

        return remove

    async def dispatch(self, method: str, params: dict[str, Any], session_id: str | None) -> None:
        for handler in list(self._by_method.get(method, ())):
            try:
                result = handler(params, session_id)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("CDP handler for %s failed", method, exc_info=True)


async def browser_ws_url(cdp_url: str) -> str:
    if cdp_url.startswith(("ws://", "wss://")):
        return cdp_url
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(cdp_url.rstrip("/") + "/json/version")
        resp.raise_for_status()
        return resp.json()["webSocketDebuggerUrl"]


class CdpConnection:
    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers = _Handlers()
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def open(cls, cdp_url: str) -> CdpConnection:
        url = await browser_ws_url(cdp_url)
        ws = await websockets.connect(url, max_size=256 * 1024 * 1024, ping_interval=None)
        return cls(ws)

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        self._next_id += 1
        msg_id = self._next_id
        message: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await self._ws.send(json.dumps(message))
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(msg_id, None)

    def on(self, method: str, handler: Handler) -> Callable[[], None]:
        return self._handlers.add(method, handler)

    async def _read(self) -> None:
        error: BaseException = ConnectionError("CDP connection closed")
        try:
            async for raw in self._ws:
                data = json.loads(raw)
                if "id" in data:
                    future = self._pending.get(data["id"])
                    if future is None or future.done():
                        continue
                    if "error" in data:
                        future.set_exception(CdpError(str(data["error"].get("message") or data["error"])))
                    else:
                        future.set_result(data.get("result") or {})
                elif "method" in data:
                    # @nonobvious(must-hold): a handler that awaits a command on
                    # this socket would deadlock the reader, so handlers run as tasks.
                    asyncio.create_task(
                        self._handlers.dispatch(data["method"], data.get("params") or {}, data.get("sessionId"))
                    )
        except Exception as exc:
            error = exc
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(str(error)))

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close()
        self._reader.cancel()
        with contextlib.suppress(BaseException):
            await self._reader


class CdpUseAdapter:
    """The same interface over a cdp_use client, for browsers browser-use connected to."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._handlers = _Handlers()
        self._registered: set[str] = set()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        return await asyncio.wait_for(
            self._client.send_raw(method, params or {}, session_id=session_id), timeout
        )

    def on(self, method: str, handler: Handler) -> Callable[[], None]:
        if method not in self._registered:
            self._registered.add(method)

            def fan_out(params: Any, session_id: str | None = None) -> None:
                asyncio.create_task(self._handlers.dispatch(method, params or {}, session_id))

            self._client._event_registry.register(method, fan_out)
        return self._handlers.add(method, handler)
