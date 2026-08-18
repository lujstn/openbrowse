"""Importing this package registers every captcha strategy via @register."""

from __future__ import annotations

from app.agent.captcha.strategies import (  # noqa: F401
    awswaf,
    datadome,
    geetest,
    hcaptcha,
    imagetotext,
    mtcaptcha,
    recaptcha,
    turnstile,
)
