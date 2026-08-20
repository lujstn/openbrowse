"""Thin CapSolver HTTP client."""

from __future__ import annotations

from typing import Any

import httpx

from openbrowse.config import settings

CAPSOLVER_API = "https://api.capsolver.com"


async def create_task(
    client: httpx.AsyncClient, task_payload: dict[str, Any]
) -> dict[str, Any]:
    resp = await client.post(
        f"{CAPSOLVER_API}/createTask",
        json={"clientKey": settings.capsolver_api_key, "task": task_payload},
        timeout=30.0,
    )
    return resp.json()


async def get_task_result(
    client: httpx.AsyncClient, task_id: str
) -> dict[str, Any]:
    resp = await client.post(
        f"{CAPSOLVER_API}/getTaskResult",
        json={"clientKey": settings.capsolver_api_key, "taskId": task_id},
        timeout=30.0,
    )
    return resp.json()


def parse_cost(result: dict[str, Any]) -> float:
    """The per-solve USD cost CapSolver returns in a task result."""
    try:
        return float(result.get("cost") or 0)
    except (ValueError, TypeError):
        return 0.0
