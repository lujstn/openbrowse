"""v3 API error envelopes and OpenAPI response metadata."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorEnvelope(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable summary of the error.")
    resolution: str = Field(description="Action the caller can take to resolve the error.")
    detail: Any = Field(description="Legacy error detail retained for compatibility.")


_ErrorSpec = tuple[str, str, str]

_VALIDATION_FAILED: _ErrorSpec = (
    "REQUEST_VALIDATION_FAILED",
    "The request failed validation.",
    "Correct the request and try again.",
)
_SESSION_NOT_IDLE: _ErrorSpec = (
    "SESSION_NOT_IDLE",
    "The session is running, not idle.",
    "Wait for the session to become idle before sending a follow-up task.",
)
# @nonobvious(must-hold): resolving an envelope runs inside an exception handler,
# so the resolver must never raise; this is the spec returned when no table has
# an entry for the status.
_UNEXPECTED: _ErrorSpec = (
    "UNEXPECTED_ERROR",
    "The request could not be completed.",
    "Retry the request, or contact support if the problem persists.",
)

_ERRORS: dict[tuple[int, str], _ErrorSpec] = {
    (401, "Server authentication is not configured"): (
        "AUTH_NOT_CONFIGURED",
        "Server authentication is not configured.",
        "Configure an API key before calling this endpoint.",
    ),
    (401, "Invalid API key"): (
        "INVALID_API_KEY",
        "The API key is invalid.",
        "Provide a valid API key and try again.",
    ),
    (404, "Session not found"): (
        "SESSION_NOT_FOUND",
        "The session was not found.",
        "Use an existing session ID or create a new session.",
    ),
    (404, "Profile not found"): (
        "PROFILE_NOT_FOUND",
        "The profile was not found.",
        "Use an existing profile ID or create a new profile.",
    ),
    (422, "Task is required when targeting an existing session"): (
        "TASK_REQUIRED",
        "A task is required for an existing session.",
        "Include a non-empty task in the request.",
    ),
}

_STATUS_FALLBACK: dict[int, _ErrorSpec] = {
    400: (
        "INVALID_STORAGE_STATE",
        "The storage state is invalid.",
        "Correct the storage state and try again.",
    ),
    404: (
        "ENDPOINT_NOT_FOUND",
        "The endpoint was not found.",
        "Check the request path and HTTP method.",
    ),
    405: (
        "ENDPOINT_NOT_FOUND",
        "The endpoint was not found.",
        "Check the request path and HTTP method.",
    ),
    422: (
        "INVALID_REASONING_EFFORT",
        "The requested reasoning effort is invalid.",
        "Use a reasoning effort supported by the selected model.",
    ),
    429: (
        "AUTH_RATE_LIMITED",
        "Authentication attempts are temporarily rate limited.",
        "Wait for the Retry-After interval before trying again.",
    ),
}


def _is_session_not_idle(status_code: int, detail_text: str | None) -> bool:
    # @nonobvious(forced-by): the idle guard raises a dynamic detail,
    # f"Session is {status}, not idle", so this matches the shape rather than an
    # exact string that only the "running" status would ever hit.
    return (
        status_code == 422
        and detail_text is not None
        and detail_text.startswith("Session is ")
        and detail_text.endswith(", not idle")
    )


def _resolve(status_code: int, detail: Any, code: str | None) -> _ErrorSpec:
    if code == "REQUEST_VALIDATION_FAILED":
        return _VALIDATION_FAILED
    detail_text = detail if isinstance(detail, str) else None
    if _is_session_not_idle(status_code, detail_text):
        return _SESSION_NOT_IDLE
    if detail_text is not None:
        exact = _ERRORS.get((status_code, detail_text))
        if exact is not None:
            return exact
    return _STATUS_FALLBACK.get(status_code, _UNEXPECTED)


def error_envelope(
    status_code: int, detail: Any, *, code: str | None = None
) -> ErrorEnvelope:
    resolved_code, message, resolution = _resolve(status_code, detail, code)
    return ErrorEnvelope(
        code=resolved_code,
        message=message,
        resolution=resolution,
        detail=detail,
    )


_GENERIC_ERROR_RESPONSE: dict[str, Any] = {
    "model": ErrorEnvelope,
    "description": "The request failed.",
}

_ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Invalid storage state."},
    401: {"model": ErrorEnvelope, "description": "Authentication failed."},
    404: {"model": ErrorEnvelope, "description": "Requested resource was not found."},
    422: {"model": ErrorEnvelope, "description": "Request validation failed."},
    429: {
        "model": ErrorEnvelope,
        "description": "Authentication attempts are rate limited.",
        "headers": {
            "Retry-After": {
                "description": "Seconds until authentication may be retried.",
                "schema": {"type": "integer"},
            }
        },
    },
}


def error_responses(*status_codes: int) -> dict[int, dict[str, Any]]:
    return {
        status_code: _ERROR_RESPONSES.get(status_code, _GENERIC_ERROR_RESPONSE)
        for status_code in status_codes
    }
