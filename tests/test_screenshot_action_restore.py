"""browser-use's Agent constructor strips the screenshot action whenever
use_vision != 'auto' — after openbrowse has already generated the system
prompt's action inventory from the intact registry. The model is then promised
a tool the schema lacks, tries it (a live scenario names it outright), gets
"mis-typed arguments" retries from pydantic's union noise, and truthfully
reports the tool as unavailable. The restore puts the entry back and rebuilds
the action models; the correction helpers stop calling an absent action a
typing problem."""

from browser_use import Tools

from openbrowse.agent.runner import (
    _actions_absent_from_schema,
    _mistyped_correction,
    _restore_screenshot_action,
    _unknown_action_correction,
)


def _union_members(tools: Tools) -> set[str]:
    model = tools.registry.create_action_model()
    variants = model.model_fields["root"].annotation.__args__
    return {next(iter(v.model_fields)) for v in variants}


class _FakeAgent:
    def __init__(self) -> None:
        self.rebuilds = 0

    def _setup_action_models(self) -> None:
        self.rebuilds += 1


def test_restore_reverses_the_constructor_strip() -> None:
    tools = Tools()
    saved = tools.registry.registry.actions.get("screenshot")
    tools.exclude_action("screenshot")
    assert "screenshot" not in _union_members(tools)
    agent = _FakeAgent()
    _restore_screenshot_action(tools, saved, agent)
    assert "screenshot" in _union_members(tools)
    assert agent.rebuilds == 1


def test_restore_is_a_noop_when_nothing_was_stripped() -> None:
    tools = Tools()
    saved = tools.registry.registry.actions.get("screenshot")
    agent = _FakeAgent()
    _restore_screenshot_action(tools, saved, agent)
    assert agent.rebuilds == 0
    assert "screenshot" in _union_members(tools)


def test_restore_tolerates_a_missing_entry() -> None:
    tools = Tools()
    tools.exclude_action("screenshot")
    agent = _FakeAgent()
    _restore_screenshot_action(tools, None, agent)
    assert agent.rebuilds == 0
    assert "screenshot" not in _union_members(tools)


def test_restore_reconverts_initial_actions_for_the_new_classes() -> None:
    # The constructor parses initial_actions with the pre-rebuild classes; the
    # live failure was those stale instances failing validation inside the new
    # AgentOutput at run start. The restore must re-convert them.
    from browser_use.agent.views import AgentOutput

    tools = Tools()
    saved = tools.registry.registry.actions.get("screenshot")
    tools.exclude_action("screenshot")

    class _Agent:
        def __init__(self) -> None:
            self.tools = tools
            self._setup_action_models()
            self.initial_actions = self._convert_initial_actions(
                [{"navigate": {"url": "http://127.0.0.1:8621/a.html", "new_tab": False}}]
            )

        def _setup_action_models(self) -> None:
            self.ActionModel = tools.registry.create_action_model()
            self.AgentOutput = AgentOutput.type_with_custom_actions(self.ActionModel)

        def _convert_initial_actions(self, dicts: list) -> list:
            return [self.ActionModel.model_validate(d) for d in dicts]

    agent = _Agent()
    _restore_screenshot_action(tools, saved, agent)
    # The exact validation that exploded live: initial actions validated by the
    # rebuilt AgentOutput before the first step runs.
    out = agent.AgentOutput(
        evaluation_previous_goal="Start",
        memory="",
        next_goal="Start",
        action=agent.initial_actions,
    )
    assert out.action
    assert "screenshot" in _union_members(tools)


# ── correction selection ─────────────────────────────────────────────────────

# Abridged from a real failed run: every variant flags the name as extra and
# none has a variant of its own — the action is absent from the schema.
_ABSENT_DETAIL = (
    "action.0.DoneActionModel.done: Field required; "
    "action.0.DoneActionModel.screenshot: Extra inputs are not permitted; "
    "action.0.SearchActionModel.search: Field required; "
    "action.0.SearchActionModel.screenshot: Extra inputs are not permitted"
)

# The same action name, but its own variant reports a field-level error —
# the action exists and only its argument shape is wrong.
_MISSHAPED_DETAIL = (
    "action.0.ScreenshotActionModel.screenshot.file_name: Input should be a "
    "valid string; "
    "action.0.DoneActionModel.done: Field required; "
    "action.0.DoneActionModel.screenshot: Extra inputs are not permitted"
)


def test_absent_action_is_detected() -> None:
    assert _actions_absent_from_schema(_ABSENT_DETAIL) == ["screenshot"]


def test_misshaped_but_present_action_is_not_flagged_absent() -> None:
    assert _actions_absent_from_schema(_MISSHAPED_DETAIL) == []


def test_unknown_action_correction_never_claims_the_action_exists() -> None:
    text = _unknown_action_correction(["screenshot"])
    assert "not in this session's action schema" in text
    assert "do not retry it" in text
    assert "exists and is valid" not in text


def test_mistyped_correction_still_defends_real_actions() -> None:
    text = _mistyped_correction(_MISSHAPED_DETAIL)
    assert "exists and is valid" in text
