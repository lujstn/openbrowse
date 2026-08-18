"""Core contracts for the captcha subsystem.

One captcha type is one CaptchaStrategy subclass. The base defines detection,
task building, solution redemption and honest verification; the token and
recognition sub-bases share the machinery each family needs.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar

from browser_use import BrowserSession

from app.agent.browser_cdp import _eval_js
from app.agent.captcha import cdp

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Detection:
    """What a strategy read off the page, ready to solve."""

    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    interstitial: bool = False
    confidence: int = 10


@dataclass
class SolveContext:
    """Everything a solve needs about the current page and session."""

    session: BrowserSession
    page_url: str
    host: str
    cookies: str
    emit: Callable[[str], Awaitable[None]]
    cost_sink: list[float] | None = None
    proxy: str = ""


@dataclass
class Action:
    """One page action a recognition strategy wants performed."""

    kind: str
    x: float = 0.0
    y: float = 0.0
    selector: str = ""
    text: str = ""

    @classmethod
    def click(cls, x: float, y: float) -> "Action":
        return cls(kind="click", x=x, y=y)

    @classmethod
    def type_into(cls, selector: str, text: str) -> "Action":
        return cls(kind="type", selector=selector, text=text)


def _first_present(solution: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if solution.get(k) not in (None, ""):
            return solution[k]
    return None


class CaptchaStrategy(ABC):
    """A single captcha type: detect it, build its task, redeem it, verify it."""

    kind: ClassVar[str] = ""
    priority: ClassVar[int] = 0
    requires_proxy: ClassVar[bool] = False
    # @nonobvious(means): detection params this strategy cannot work without. A
    # solve missing any of them is refused before a task is created, so a hint-only
    # strategy called without its hint costs nothing rather than being billed and
    # then doing nothing.
    required_params: ClassVar[tuple[str, ...]] = ()
    # @nonobvious(means): set when the solving service offers no task for this
    # challenge, so it is still recognised and reported rather than silently
    # missed, but no task is created and nothing is charged.
    unsupported_reason: ClassVar[str] = ""
    # @nonobvious(means): solution keys this strategy reads from the CapSolver
    # result, most-specific first; the poll loop uses these instead of guessing.
    solution_keys: ClassVar[tuple[str, ...]] = ("token",)

    @abstractmethod
    def detect(self, probe: dict[str, Any]) -> Detection | None:
        """Pure: given a probe snapshot, is this my challenge and what are its params?"""

    @abstractmethod
    def build_task(self, det: Detection, ctx: SolveContext) -> dict[str, Any]:
        """The CapSolver createTask payload for this detection."""

    async def capture(self, det: Detection, ctx: SolveContext) -> dict[str, Any]:
        """Optional pre-task capture (recognition strategies screenshot here)."""
        return {}

    @abstractmethod
    async def redeem(
        self, solution: dict[str, Any], det: Detection, ctx: SolveContext
    ) -> None:
        """Apply the CapSolver solution to the page."""

    async def verify(self, det: Detection, ctx: SolveContext) -> bool:
        """Honest success check: my challenge is gone from the page.

        @nonobvious(forced-by): an in-page widget stays in the DOM after a solve by
        design, because the token is written into it rather than clicked into it, so
        waiting the full interstitial budget for it to vanish can only ever time out.
        Only a full-page interstitial can prove itself by clearing.
        """
        timeout = 25.0 if det.interstitial else 4.0
        return await cdp.page_advanced(ctx.session, timeout_s=timeout)


_PLACE_JS = r"""(function (token, names, tag, widgetSel) {
  var out = { fields: 0, inForm: false, valueLen: 0, callback: "" };
  var parts = [];
  for (var i = 0; i < names.length; i++) { parts.push('[name="' + names[i] + '"]'); }
  var sel = parts.join(",");
  var widget = widgetSel ? document.querySelector(widgetSel) : null;
  var form = widget && widget.closest ? widget.closest("form") : null;
  if (!form) {
    var any = document.querySelector(sel);
    form = any && any.closest ? any.closest("form") : null;
  }
  var found = document.querySelectorAll(sel);
  for (var i = 0; i < found.length; i++) {
    found[i].value = token;
    if (found[i].tagName === "TEXTAREA") { found[i].innerHTML = token; }
  }
  // @nonobvious(forced-by): only fields inside the form element are serialised on
  // submit, so a response box the widget has not rendered yet, or one rendered
  // outside the form, must be replaced by one the submit will actually carry.
  function make(parent) {
    var el = document.createElement(tag);
    if (tag === "input") { el.type = "hidden"; }
    el.name = names[0];
    el.id = names[0];
    el.style.display = "none";
    el.value = token;
    if (tag === "textarea") { el.innerHTML = token; }
    parent.appendChild(el);
  }
  if (form && !form.querySelector(sel)) { make(form); }
  else if (!form && !found.length) { make(document.body); }
  var all = document.querySelectorAll(sel);
  out.fields = all.length;
  out.valueLen = all.length ? (all[0].value || "").length : 0;
  out.inForm = !!(form && form.querySelector(sel));
  try {
    var cb = widget && widget.getAttribute("data-callback");
    if (cb && typeof window[cb] === "function") { window[cb](token); out.callback = cb; }
  } catch (e) {}
  return out;
})(%s, %s, %s, %s)"""


class TokenStrategy(CaptchaStrategy):
    """Family that injects a returned token (or fields) into the page.

    A subclass normally only declares which response field the page expects and
    how to find its widget; the shared placement below then writes the token
    where a submit will actually carry it and reports what it did.
    """

    response_fields: ClassVar[tuple[str, ...]] = ()
    widget_selector: ClassVar[str] = ""
    response_tag: ClassVar[str] = "textarea"

    async def redeem(self, solution, det, ctx):
        await self._place(ctx.session, solution, det)
        if det.interstitial:
            await cdp.submit_widget_form(
                ctx.session, self.response_fields, self.widget_selector
            )

    async def _place(
        self, session: BrowserSession, solution: dict[str, Any], det: Detection
    ) -> None:
        if not self.response_fields:
            raise NotImplementedError(f"{self.kind} declares no response field")
        token = _first_present(solution, self.solution_keys) or ""
        placed = await _eval_js(
            session,
            _PLACE_JS
            % (
                json.dumps(token),
                json.dumps(list(self.response_fields)),
                json.dumps(self.response_tag),
                json.dumps(self.widget_selector),
            ),
        ) or {}
        logger.info(
            "solve_captcha: placed %s token (len=%d fields=%s in_form=%s "
            "written=%s callback=%s)",
            self.kind, len(str(token)), placed.get("fields"), placed.get("inForm"),
            placed.get("valueLen"), placed.get("callback") or "none",
        )
        await self._after_place(session, str(token), det)

    async def _after_place(
        self, session: BrowserSession, token: str, det: Detection
    ) -> None:
        """Any provider-specific step once the token is in the page."""
        return None


class RecognitionStrategy(CaptchaStrategy):
    """Family that acts on returned text or coordinates (click grids, type text)."""

    @abstractmethod
    def plan_actions(
        self, solution: dict[str, Any], det: Detection
    ) -> list[Action]:
        """Turn the recognition solution into concrete page actions."""

    async def redeem(self, solution, det, ctx):
        for a in self.plan_actions(solution, det):
            if a.kind == "click":
                await cdp.click_coordinate(ctx.session, a.x, a.y)
            elif a.kind == "type":
                await cdp.type_text(ctx.session, a.selector, a.text)
        await self._commit(ctx.session, det)

    async def _commit(self, session: BrowserSession, det: Detection) -> None:
        """Press the challenge's verify/submit control, if any. Default: none."""
        return None
