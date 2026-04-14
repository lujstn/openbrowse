"""v3-compatible Sessions API — mirrors cloud.browser-use.com."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.agent.pool import pool
from app.auth import require_api_key
from app.db import crud

router = APIRouter(prefix="/v3/sessions", tags=["sessions"])


# ── Pydantic models (match SDK types exactly) ────────────────────────


class RunTaskRequest(BaseModel):
    task: str | None = None
    model: str = "claude-sonnet-4.6"
    sessionId: str | None = None
    keepAlive: bool = False
    maxCostUsd: float | None = None
    profileId: str | None = None
    outputSchema: dict[str, Any] | None = None
    sensitiveData: dict[str, str] | None = None
    systemPromptExtension: str | None = None
    skills: bool = True
    enableRecording: bool = False
    proxyCountryCode: str | None = None


class SessionResponse(BaseModel):
    id: str
    status: str
    model: str
    title: str | None = None
    output: Any | None = None
    outputSchema: dict[str, Any] | None = None
    stepCount: int = 0
    lastStepSummary: str | None = None
    isTaskSuccessful: bool | None = None
    liveUrl: str | None = None
    recordingUrls: list[str] = []
    profileId: str | None = None
    workspaceId: str | None = None
    proxyCountryCode: str | None = None
    maxCostUsd: str | None = None
    totalInputTokens: int = 0
    totalOutputTokens: int = 0
    llmCostUsd: str = "0"
    proxyCostUsd: str = "0"
    browserCostUsd: str = "0"
    totalCostUsd: str = "0"
    screenshotUrl: str | None = None
    createdAt: str
    updatedAt: str


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]
    total: int
    page: int
    pageSize: int


class StopSessionRequest(BaseModel):
    strategy: str = "session"


class MessageResponseModel(BaseModel):
    id: str
    sessionId: str
    role: str
    data: str
    type: str
    summary: str
    screenshotUrl: str | None = None
    hidden: bool = False
    createdAt: str


class MessageListResponse(BaseModel):
    messages: list[MessageResponseModel]
    hasMore: bool


def _to_session_response(row: dict[str, Any]) -> SessionResponse:
    output = row.get("output")
    if output:
        try:
            output = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            pass

    output_schema = None
    if row.get("output_schema"):
        try:
            output_schema = json.loads(row["output_schema"])
        except (json.JSONDecodeError, TypeError):
            pass

    is_successful = row.get("is_task_successful")
    if is_successful is not None:
        is_successful = bool(is_successful)

    return SessionResponse(
        id=row["id"],
        status=row["status"],
        model=row.get("model", "claude-sonnet-4.6"),
        title=row.get("title"),
        output=output,
        outputSchema=output_schema,
        stepCount=row.get("step_count", 0),
        lastStepSummary=row.get("last_step_summary"),
        isTaskSuccessful=is_successful,
        liveUrl=row.get("live_url"),
        profileId=row.get("profile_id"),
        maxCostUsd=str(row["max_cost_usd"]) if row.get("max_cost_usd") else None,
        totalInputTokens=row.get("total_input_tokens", 0),
        totalOutputTokens=row.get("total_output_tokens", 0),
        llmCostUsd=str(row.get("llm_cost_usd", 0)),
        totalCostUsd=str(row.get("total_cost_usd", 0)),
        createdAt=row["created_at"],
        updatedAt=row["updated_at"],
    )


def _to_message_response(row: dict[str, Any]) -> MessageResponseModel:
    return MessageResponseModel(
        id=row["id"],
        sessionId=row["session_id"],
        role=row.get("role", "ai"),
        data=row.get("data", ""),
        type=row.get("type", ""),
        summary=row.get("summary", ""),
        screenshotUrl=None,
        hidden=bool(row.get("hidden", 0)),
        createdAt=row["created_at"],
    )


# ── Endpoints ─────────────────────────────────────────────────────────


@router.post("", response_model=SessionResponse)
async def create_session(
    body: RunTaskRequest | None = None,
    _: str = Depends(require_api_key),
):
    body = body or RunTaskRequest()

    if body.sessionId:
        existing = await crud.get_session(body.sessionId)
        if not existing:
            raise HTTPException(status_code=404, detail="Session not found")
        if existing["status"] not in ("idle", "created"):
            raise HTTPException(
                status_code=422, detail=f"Session is {existing['status']}, not idle"
            )
        if not body.task:
            raise HTTPException(
                status_code=422, detail="Task is required when targeting an existing session"
            )
        session = await crud.update_session(
            body.sessionId,
            task=body.task,
            model=body.model,
            output_schema=json.dumps(body.outputSchema) if body.outputSchema else None,
            sensitive_data=json.dumps(body.sensitiveData) if body.sensitiveData else None,
            system_prompt_extension=body.systemPromptExtension,
            max_cost_usd=body.maxCostUsd,
        )
        await pool.submit(body.sessionId)
        return _to_session_response(session)

    session = await crud.create_session(
        task=body.task,
        model=body.model,
        profile_id=body.profileId,
        output_schema=body.outputSchema,
        sensitive_data=body.sensitiveData,
        system_prompt_extension=body.systemPromptExtension,
        max_cost_usd=body.maxCostUsd,
    )

    if body.task:
        await pool.submit(session["id"])

    return _to_session_response(session)


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: str = Depends(require_api_key),
):
    sessions, total = await crud.list_sessions(page=page, page_size=page_size)
    return SessionListResponse(
        sessions=[_to_session_response(s) for s in sessions],
        total=total,
        page=page,
        pageSize=page_size,
    )


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, _: str = Depends(require_api_key)):
    session = await crud.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _to_session_response(session)


@router.post("/{session_id}/stop", response_model=SessionResponse)
async def stop_session(
    session_id: str,
    body: StopSessionRequest | None = None,
    _: str = Depends(require_api_key),
):
    session = await crud.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    body = body or StopSessionRequest()

    if body.strategy == "task":
        await pool.cancel(session_id)
        await crud.update_session(session_id, status="idle")
    else:
        await pool.cancel(session_id)
        await crud.update_session(session_id, status="stopped")

    return _to_session_response(await crud.get_session(session_id))


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: str, _: str = Depends(require_api_key)):
    session = await crud.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await pool.cancel(session_id)
    await crud.delete_session(session_id)


@router.get("/{session_id}/messages", response_model=MessageListResponse)
async def list_messages(
    session_id: str,
    after: str | None = Query(None),
    before: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    _: str = Depends(require_api_key),
):
    session = await crud.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages, has_more = await crud.list_messages(
        session_id, after=after, before=before, limit=limit
    )
    return MessageListResponse(
        messages=[_to_message_response(m) for m in messages],
        hasMore=has_more,
    )
