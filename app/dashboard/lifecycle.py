"""Graceful service restart, shared by the settings page and the setup wizard."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess

from app.agent.pool import pool
from app.browser.factory import display_manager
from app.hostinfo import UNIT_NAME

logger = logging.getLogger(__name__)

_restart_tasks: set[asyncio.Task[None]] = set()


def schedule_restart() -> None:
    async def _go() -> None:
        await asyncio.sleep(0.7)
        try:
            await asyncio.wait_for(pool.shutdown(), timeout=20)
        except Exception:
            logger.warning("pool shutdown before restart failed", exc_info=True)
        try:
            await display_manager.cleanup_all()
        except Exception:
            logger.warning("display cleanup before restart failed", exc_info=True)
        try:
            subprocess.Popen(["sudo", "-n", "systemctl", "restart", UNIT_NAME])
        except Exception:
            logger.warning("systemctl restart failed", exc_info=True)
        # @nonobvious(forced-by): under systemd a non-zero exit revives the
        # service even where sudo is unavailable; without systemd the process
        # simply stops, which is what "restart" honestly means there.
        await asyncio.sleep(5)
        os._exit(1)

    task = asyncio.get_running_loop().create_task(_go())
    _restart_tasks.add(task)
    task.add_done_callback(_restart_tasks.discard)
