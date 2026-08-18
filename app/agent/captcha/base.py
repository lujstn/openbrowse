"""Core contracts for the captcha subsystem.

One captcha type is one CaptchaStrategy subclass. The base defines detection,
task building, solution redemption and honest verification; the token and
recognition sub-bases share the machinery each family needs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar

from browser_use import BrowserSession

from app.agent.captcha import cdp


@dataclass(frozen=True)
class Detection:
    """What a strategy read off the page, ready to solve."""

    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    interstitial: bool = False
    served_host: str = ""
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

    async def verify(
        self, det: Detection, ctx: SolveContext, before_url: str
    ) -> bool:
        """Honest success check: the page moved on, or my challenge is gone."""
        return await cdp.page_advanced(ctx.session, before_url, self)


class TokenStrategy(CaptchaStrategy):
    """Family that injects a returned token (or fields) into the page."""

    async def redeem(self, solution, det, ctx):
        await self._place(ctx.session, solution, det)
        if det.interstitial:
            await cdp.submit_widget_form(ctx.session)

    @abstractmethod
    async def _place(
        self, session: BrowserSession, solution: dict[str, Any], det: Detection
    ) -> None:
        """Write the token/fields into the page and fire any declared callback."""


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
