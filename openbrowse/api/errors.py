"""v3 API error envelopes and OpenAPI response metadata."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorEnvelope(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable summary of the error.")
    resolution: str = Field(description="Action the caller can take to resolve the error.")
    detail: Any = Field(description="Legacy error detail retained for compatibility.")


_ERRORS: dict[tuple[int, str | None], tuple[str, str, str]] = {
    (400, None): (
        "INVALID_STORAGE_STATE",
        "The storage state is invalid.",
        "Correct the storage state and try again.",
    ),
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
    (404, None): (
        "ENDPOINT_NOT_FOUND",
        "The endpoint was not found.",
        "Check the request path and HTTP method.",
    ),
    (405, None): (
        "ENDPOINT_NOT_FOUND",
        "The endpoint was not found.",
        "Check the request path and HTTP method.",
    ),
    (422, "Task is required when targeting an existing session"): (
        "TASK_REQUIRED",
        "A task is required for an existing session.",
        "Include a non-empty task in the request.",
    ),
    (422, "Session is running, not idle"): (
        "SESSION_NOT_IDLE",
        "The session is running, not idle.",
        "Wait for the session to become idle before sending a follow-up task.",
    ),
    (422, None): (
        "INVALID_REASONING_EFFORT",
        "The requested reasoning effort is invalid.",
        "Use a reasoning effort supported by the selected model.",
    ),
    (429, None): (
        "AUTH_RATE_LIMITED",
        "Authentication attempts are temporarily rate limited.",
        "Wait for the Retry-After interval before trying again.",
    ),
}


def error_envelope(
    status_code: int, detail: Any, *, code: str | None = None
) -> ErrorEnvelope:
    if code == "REQUEST_VALIDATION_FAILED":
        return ErrorEnvelope(
            code=code,
            message="The request failed validation.",
            resolution="Correct the request and try again.",
            detail=detail,
        )
    detail_text = detail if isinstance(detail, str) else None
    if status_code == 422 and detail_text and detail_text.startswith("Session is "):
        code, message, resolution = _ERRORS[(422, "Session is running, not idle")]
        return ErrorEnvelope(
            code=code,
            message=message,
            resolution=resolution,
            detail=detail,
        )
    error = _ERRORS.get((status_code, detail_text))
    if error is None:
        error = _ERRORS[(status_code, None)]
    code, message, resolution = error
    return ErrorEnvelope(
        code=code,
        message=message,
        resolution=resolution,
        detail=detail,
    )


_ERROR_RESPONSES = {
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
    return {status_code: _ERROR_RESPONSES[status_code] for status_code in status_codes}
