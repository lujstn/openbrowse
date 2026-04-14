"""noVNC URL resolution and health checks."""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


async def wait_for_novnc(port: int, timeout: float = 10.0) -> bool:
    """Wait for the noVNC server to be ready on the given port."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with httpx.AsyncClient() as client:
        while loop.time() < deadline:
            try:
                resp = await client.get(f"http://localhost:{port}/vnc.html", timeout=2.0)
                if resp.status_code == 200:
                    return True
            except (httpx.ConnectError, httpx.ReadTimeout):
                pass
            await asyncio.sleep(0.5)
    logger.warning("noVNC on port %d did not become ready within %.0fs", port, timeout)
    return False
