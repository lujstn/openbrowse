"""Gentle primaryUrl reminder — nudges the agent to set/refresh ``primaryUrl``.

The agent works from a page it should pin as ``primaryUrl`` so it can return there
after detours. ``startUrl`` is captured automatically, but ``primaryUrl`` is only
ever set by the agent. This tracker watches which pages the agent actually works
from and, when it clearly has a main page but no (or a stale) ``primaryUrl``,
injects one gentle reminder — never a forced action, and never a repeated nag.

Pure stdlib so it is unit-testable without browser-use installed.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit


def norm_url(url: str | None) -> str | None:
    """Normalise a URL for frequency tracking: drop the fragment and any trailing
    slash so the same page counts as one, keeping the query (listing pages differ
    by it).
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
    except Exception:
        return url


def links_opened(actions: Any) -> int:
    """How many URLs a step queued via open_tabs — the fan-out signal for a page."""
    count = 0
    for a in actions or []:
        try:
            dump = a.model_dump(exclude_none=True)
        except Exception:
            continue
        params = dump.get("open_tabs")
        if isinstance(params, dict):
            urls = params.get("urls")
            if isinstance(urls, list):
                count += len(urls)
    return count


class PrimaryUrlReminder:
    """Gently reminds the agent to set/refresh ``primaryUrl`` when it is clearly
    working from a page other than ``startUrl``. It never sets ``primaryUrl`` — only
    the agent does that. It nudges once if a frequent page emerges while none is set,
    then stays silent unless the agent pins one and later drifts (spends several new
    steps on a different page), so an ignored nudge is never repeated.
    """

    MIN_COUNT = 3
    MIN_STEPS_BEFORE = 4
    STALE_GAP = 4

    def __init__(self) -> None:
        self.visits: dict[str, int] = {}
        self.fanout: dict[str, int] = {}
        self.unset_reminded = False
        self.primary: str | None = None
        self.primary_set_step = 0
        self.since_primary: dict[str, int] = {}
        self.last_stale_page: str | None = None

    def _frequent_page(
        self,
        visits: dict[str, int],
        fanout: dict[str, int],
        start_url: str | None,
        exclude: set[str],
    ) -> str | None:
        best, best_score = None, 0
        for url in set(visits) | set(fanout):
            if not url or url == start_url or url in exclude or url.startswith("about:"):
                continue
            score = max(visits.get(url, 0), fanout.get(url, 0))
            if score >= self.MIN_COUNT and score > best_score:
                best, best_score = url, score
        return best

    def observe(
        self,
        step: int,
        current_url: str | None,
        opened_links: int,
        primary_url: str | None,
        start_url: str | None,
    ) -> str | None:
        """Fold in one finished step; return a reminder string to inject, or None."""
        cur = norm_url(current_url)
        start = norm_url(start_url)
        primary = norm_url(primary_url)

        if cur and not cur.startswith("about:"):
            self.visits[cur] = self.visits.get(cur, 0) + 1
            if opened_links:
                self.fanout[cur] = self.fanout.get(cur, 0) + opened_links
            if self.primary is not None:
                self.since_primary[cur] = self.since_primary.get(cur, 0) + 1

        if primary != self.primary:
            self.primary = primary
            if primary:
                self.primary_set_step = step
                self.since_primary = {}
                self.last_stale_page = None

        if primary is None:
            if self.unset_reminded or step <= self.MIN_STEPS_BEFORE:
                return None
            page = self._frequent_page(self.visits, self.fanout, start, set())
            if not page:
                return None
            self.unset_reminded = True
            return (
                f"You have been working mostly from {page} but have not set a "
                f"primaryUrl. If that is your main page, save it now so you can get "
                f"back to it: remember('primaryUrl', '{page}'). Ignore this if that "
                "page is not your base."
            )

        if step - self.primary_set_step <= self.STALE_GAP:
            return None
        page = self._frequent_page(self.since_primary, {}, start, {primary})
        if not page or page == self.last_stale_page:
            return None
        self.last_stale_page = page
        return (
            f"Your primaryUrl is set to {primary}, but you have spent several recent "
            f"steps working from {page}. If you have moved on, update it: "
            f"remember('primaryUrl', '{page}'). Keep it only if it still reflects your "
            "main page."
        )
