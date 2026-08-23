"""v3-compatible Sessions API — mirrors cloud.browser-use.com."""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from openbrowse.agent import live
from openbrowse.api.errors import error_responses
from openbrowse.agent.pool import pool
from openbrowse.agent.runner import _resolve_model, effort_when_unset, validate_effort
from openbrowse.auth import require_api_key
from openbrowse.config import settings
from openbrowse.db import crud

router = APIRouter(prefix="/v3/sessions", tags=["sessions"])

# @nonobvious(means): BU Cloud's thinkingLevel enum, mapped so v3 SDK callers
# keep working; every other spelling is steered to reasoningEffort.
_THINKING_LEVEL_MAP = {
    "disabled": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
}


# ── Pydantic models (match SDK types exactly) ────────────────────────


class RunTaskRequest(BaseModel):
    task: str | None = None
    model: str = Field(default_factory=lambda: settings.resolved_default_model)
    sessionId: str | None = None
    keepAlive: bool = False
    maxCostUsd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    profileId: str | None = None
    outputSchema: dict[str, Any] | None = None
    sensitiveData: dict[str, str] | None = None
    systemPromptExtension: str | None = None
    skills: bool = True
    enableRecording: bool = False
    proxyCountryCode: str | None = None
    reasoningEffort: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_effort_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for legacy in ("thinkingEffort", "modelThinkingEffort"):
            if legacy in data:
                raise ValueError(
                    f"'{legacy}' is no longer supported; use 'reasoningEffort'."
                )
        if "thinkingLevel" in data:
            if "reasoningEffort" in data:
                raise ValueError(
                    "send only one of 'thinkingLevel' and 'reasoningEffort'."
                )
            level = data.pop("thinkingLevel")
            mapped = _THINKING_LEVEL_MAP.get(level)
            if mapped is None:
                raise ValueError(
                    f"'{level}' is not a supported thinkingLevel; use 'reasoningEffort'."
                )
            data["reasoningEffort"] = mapped
        return data


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
    proxyUsedMb: str = "0"
    totalCostUsd: str = "0"
    screenshotUrl: str | None = None
    reasoningEffort: str = "default"
    failureKind: str | None = None
    failureStatusCode: int | None = None
    agentmailEmail: str | None = None
    integrationsUsed: list[str] = []
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
        model=row.get("model") or settings.resolved_default_model,
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
        reasoningEffort=row.get("reasoning_effort") or "default",
        failureKind=row.get("failure_kind"),
        failureStatusCode=row.get("failure_status_code"),
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


def _local_budget(cloud_max: float | None) -> float | None:
    if cloud_max is None:
        return None
    # @nonobvious(must-hold): rounding up keeps a small cap off zero, which is
    # read as "no budget at all" where the cap is enforced.
    return math.ceil(cloud_max * settings.cloud_max_cost_factor * 100 - 1e-9) / 100


def _resolved_effort(model: str, requested: str | None) -> str:
    try:
        effort = validate_effort(model, requested or "default")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return effort_when_unset(model) if effort == "default" else effort


def _same_model(asked: str, stored: str | None) -> bool:
    """Whether two model names mean the same model, in either version spelling, so
    respelling claude-sonnet-4.6 as claude-sonnet-4-6 does not read as a change of
    model and cost a live session its browser.
    """
    try:
        return _resolve_model(asked) == _resolve_model(stored or "")
    except ValueError:
        return asked == (stored or "")


def _restart_fields(
    sent: set[str], body: "RunTaskRequest", existing: dict[str, Any]
) -> list[str]:
    """Which requested changes a running agent cannot adopt.

    The model, its reasoning effort, the output schema, the sensitive-data map and
    the system-prompt extension are all fixed when the agent is built, so a
    follow-up that changes any of them has to close the browser and start over.
    Anything else — the task itself, the budget, keep-alive — a live session takes
    in its stride.
    """
    changed: list[str] = []
    if "model" in sent and not _same_model(body.model, existing.get("model")):
        changed.append("model")
    if "reasoningEffort" in sent:
        resolved = _resolved_effort(
            body.model if "model" in sent else existing.get("model") or body.model,
            body.reasoningEffort,
        )
        if resolved != (existing.get("reasoning_effort") or ""):
            changed.append("reasoningEffort")
    if "outputSchema" in sent:
        schema = json.dumps(body.outputSchema) if body.outputSchema else None
        if schema != existing.get("output_schema"):
            changed.append("outputSchema")
    if "sensitiveData" in sent:
        secrets = json.dumps(body.sensitiveData) if body.sensitiveData else None
        if secrets != existing.get("sensitive_data"):
            changed.append("sensitiveData")
    if "systemPromptExtension" in sent and body.systemPromptExtension != (
        existing.get("system_prompt_extension") or None
    ):
        changed.append("systemPromptExtension")
    return changed


@router.post(
    "",
    response_model=SessionResponse,
    responses=error_responses(401, 404, 422, 429),
    operation_id="createSession",
)
async def create_session(
    body: RunTaskRequest | None = None,
    _: str = Depends(require_api_key),
):
    """Start a browser session and give the agent its task. The run begins immediately, and the response carries the session id you poll for progress."""
    body = body or RunTaskRequest()
    sent = body.model_fields_set

    if body.sessionId:
        existing = await crud.get_session(body.sessionId)
        if not existing:
            raise HTTPException(status_code=404, detail="Session not found")
        status = existing["status"]
        busy = status == "running" or (status == "created" and existing.get("task"))
        # @nonobvious(means): a keep-alive session is a conversation, so it takes
        # follow-ups after its browser has been released too — every other session
        # is still only addressable while idle.
        if busy or (status not in ("idle", "created") and not existing.get("keep_alive")):
            raise HTTPException(
                status_code=422, detail=f"Session is {status}, not idle"
            )
        if not body.task:
            raise HTTPException(
                status_code=422, detail="Task is required when targeting an existing session"
            )

        # @nonobvious(must-hold): each column named here is overwritten, so a
        # follow-up run may only name the fields its caller actually sent.
        updates: dict[str, Any] = {"task": body.task}
        if "model" in sent:
            updates["model"] = body.model
        if "outputSchema" in sent:
            updates["output_schema"] = (
                json.dumps(body.outputSchema) if body.outputSchema else None
            )
        if "sensitiveData" in sent:
            updates["sensitive_data"] = (
                json.dumps(body.sensitiveData) if body.sensitiveData else None
            )
        if "systemPromptExtension" in sent:
            updates["system_prompt_extension"] = body.systemPromptExtension
        # @nonobvious(means): a named budget is the session's new absolute
        # ceiling for this dispatch only — it does not become the allowance, so
        # the turn after it tops up from what the session was created with.
        if "maxCostUsd" in sent:
            updates["max_cost_usd"] = _local_budget(body.maxCostUsd)
        else:
            topped_up = crud.topped_up_budget(existing)
            if topped_up is not None:
                updates["max_cost_usd"] = topped_up
        if "keepAlive" in sent:
            updates["keep_alive"] = int(body.keepAlive)
        if sent & {"model", "reasoningEffort"}:
            updates["reasoning_effort"] = _resolved_effort(
                body.model if "model" in sent else existing.get("model") or body.model,
                body.reasoningEffort,
            )

        restart = _restart_fields(sent, body, existing)
        if restart:
            await live.request_release(
                body.sessionId,
                "Browser released to apply new " + ", ".join(restart),
            )
            outcome = live.COLD
        else:
            outcome = await pool.follow_up(body.sessionId, body.task)
        # @nonobvious(must-hold): nothing is written until the outcome is known —
        # a session that turned out to be mid-task must keep the task it is
        # actually working on.
        if outcome == live.BUSY:
            raise HTTPException(status_code=422, detail="Session is running, not idle")
        updates["status"] = "running" if outcome == live.DELIVERED else "created"
        session = await crud.update_session(body.sessionId, **updates)
        await crud.create_message(
            session_id=body.sessionId,
            role="user",
            msg_type="user_message",
            summary=body.task[:2000],
            count_step=False,
        )
        if outcome != live.DELIVERED:
            pool.submit_nowait(body.sessionId)
        return _to_session_response(session)

    budget = _local_budget(body.maxCostUsd)
    session = await crud.create_session(
        task=body.task,
        model=body.model,
        profile_id=body.profileId,
        output_schema=body.outputSchema,
        sensitive_data=body.sensitiveData,
        system_prompt_extension=body.systemPromptExtension,
        max_cost_usd=budget,
        default_max_cost_usd=budget,
        keep_alive=body.keepAlive,
        reasoning_effort=_resolved_effort(body.model, body.reasoningEffort),
    )

    if body.task:
        await crud.create_message(
            session_id=session["id"],
            role="user",
            msg_type="user_message",
            summary=body.task[:2000],
            count_step=False,
        )
        pool.submit_nowait(session["id"])

    return _to_session_response(session)


@router.get(
    "",
    response_model=SessionListResponse,
    responses=error_responses(401, 422, 429),
    operation_id="listSessions",
)
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _: str = Depends(require_api_key),
):
    """List sessions with their current status, paginated. Running and finished sessions are both included."""
    sessions, total = await crud.list_sessions(page=page, page_size=page_size)
    return SessionListResponse(
        sessions=[_to_session_response(s) for s in sessions],
        total=total,
        page=page,
        pageSize=page_size,
    )


@router.get(
    "/{session_id}",
    response_model=SessionResponse,
    responses=error_responses(401, 404, 429),
    operation_id="getSession",
)
async def get_session(session_id: str, _: str = Depends(require_api_key)):
    """Fetch one session by id, with its status, step count, live view URL and structured output as they stand right now."""
    session = await crud.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return _to_session_response(session)


@router.post(
    "/{session_id}/stop",
    response_model=SessionResponse,
    responses=error_responses(401, 404, 429),
    operation_id="stopSession",
)
async def stop_session(
    session_id: str,
    body: StopSessionRequest | None = None,
    _: str = Depends(require_api_key),
):
    """Cancel a running session and close its browser. The default leaves it stopped; strategy "task" leaves it idle, so a later call can give it new work."""
    session = await crud.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    body = body or StopSessionRequest()

    # @nonobvious(mirrors): either strategy cancels the run, which closes the
    # browser with it. They differ only in the status left behind, and "task"
    # leaves the session addressable so a later call can give it new work.
    await pool.cancel(session_id)
    await crud.update_session(
        session_id, status="idle" if body.strategy == "task" else "stopped"
    )

    return _to_session_response(await crud.get_session(session_id))


@router.delete(
    "/{session_id}",
    status_code=204,
    responses=error_responses(401, 404, 429),
    operation_id="deleteSession",
)
async def delete_session(session_id: str, _: str = Depends(require_api_key)):
    """Cancel the session if it is still running, then delete it and everything stored against it."""
    session = await crud.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await pool.cancel(session_id)
    await crud.delete_session(session_id)


@router.get(
    "/{session_id}/messages",
    response_model=MessageListResponse,
    responses=error_responses(401, 404, 422, 429),
    operation_id="listSessionMessages",
)
async def list_messages(
    session_id: str,
    after: str | None = Query(None),
    before: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    _: str = Depends(require_api_key),
):
    """Read the messages a session has produced, paged with after, before and limit, for following a run live or diagnosing one afterwards."""
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
