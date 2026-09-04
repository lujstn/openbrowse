"""Structured-output store tools, forced individually: the system prompt steers the
model towards the easiest bulk paths, so each scenario names its target tool and
forbids the shortcuts that would rob it of coverage."""

import pytest

from tests.live.fixture_site import (
    CORRECTIONS_PAGE,
    EXPEDITION,
    RATE_DISCOUNT,
    RATES,
    STAFF,
    TWO_ITEMS,
)
from tests.live.harness import (
    assert_no_doom_loop,
    assert_not_used,
    assert_output,
    assert_success,
    assert_used,
)

pytestmark = pytest.mark.live


def _items_schema(fields: dict, array: str = "items", required: list | None = None) -> dict:
    return {
        "type": "object",
        "properties": {
            array: {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": fields,
                    "required": required if required is not None else list(fields),
                },
            }
        },
        "required": [array],
    }


def test_add_item_micro(run_scenario, fixture_url):
    trace = run_scenario(
        "add_item_micro",
        f"Go to {fixture_url}/two_items.html. There are exactly two cases for sale. "
        "Add each one to the output with its own add_item call — no scripts and no "
        "bulk file loading (do not use add_items_from_file).",
        output_schema=_items_schema(
            {"name": {"type": "string"}, "priceGbp": {"type": "integer"}}
        ),
    )
    assert_success(trace)
    assert_used(trace, "add_item", at_least=2)
    assert_not_used(trace, "add_items_from_file", "run_code_file")
    assert_no_doom_loop(trace)
    assert_output(trace, {"items": TWO_ITEMS})


def test_update_item_micro(run_scenario, fixture_url):
    expected = [dict(r) for r in CORRECTIONS_PAGE["rows"]]
    for row in expected:
        if row["name"] == CORRECTIONS_PAGE["corrected_name"]:
            row["dailyRateGbp"] = CORRECTIONS_PAGE["corrected_rate"]
    trace = run_scenario(
        "update_item_micro",
        f"Go to {fixture_url}/corrections.html. First add the three rows exactly as "
        "the table shows them. Then apply the correction notice printed beneath the "
        "table to the relevant row using a single update_item call.",
        output_schema=_items_schema(
            {"name": {"type": "string"}, "dailyRateGbp": {"type": "integer"}},
            array="rows",
        ),
    )
    assert_success(trace)
    assert_used(trace, "update_item")
    assert_no_doom_loop(trace)
    assert_output(trace, {"rows": expected})


def test_update_from_file(run_scenario, fixture_url):
    expected = [
        {
            "name": r["name"],
            "rateGbp": r["rateGbp"],
            "discountedRateGbp": round(r["rateGbp"] * (1 - RATE_DISCOUNT)),
        }
        for r in RATES
    ]
    trace = run_scenario(
        "update_from_file",
        f"Go to {fixture_url}/rates.html. Step 1: add the five jobs to the output "
        "(name and rateGbp). Step 2: compute each job's discounted rate — exactly "
        "10% off, rounded to the nearest whole pound — with run_code_file: build a "
        'list of {"index": n, "fields": {"discountedRateGbp": value}} objects, save '
        "it with save_json('updates.json'), then merge it with "
        "update_items_from_file('updates.json'). Finish when every row has all "
        "three fields.",
        output_schema=_items_schema(
            {
                "name": {"type": "string"},
                "rateGbp": {"type": "integer"},
                "discountedRateGbp": {"type": "integer"},
            },
            array="jobs",
            required=["name", "rateGbp"],
        ),
        max_cost_usd=0.15,
    )
    assert_success(trace)
    assert_used(trace, "run_code_file")
    assert_used(trace, "update_items_from_file")
    assert_no_doom_loop(trace)
    assert_output(trace, {"jobs": expected})


@pytest.mark.sonnet_smoke
def test_schema_scalars(run_scenario, fixture_url):
    trace = run_scenario(
        "schema_scalars",
        f"Go to {fixture_url}/expedition.html. Fill the output: title, curator and "
        "foundedYear with set_field, and the members list — add every member row "
        "shown on the page including any duplicates, then check the output with "
        "read_output or search_output and remove any duplicate rows with "
        "remove_items. The census office publishes no telephone number: confirm "
        "that on the page, then record phoneNumber as absent with mark_absent.",
        output_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "curator": {"type": "string"},
                "foundedYear": {"type": "integer"},
                "phoneNumber": {"type": "string"},
                "members": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                },
            },
            "required": ["title", "curator", "foundedYear", "members"],
        },
        max_cost_usd=0.15,
    )
    assert_success(trace)
    assert_used(trace, "set_field")
    assert_used(trace, "mark_absent")
    assert_used(trace, "remove_items")
    assert trace.used["read_output"] + trace.used["search_output"] >= 1, trace.describe()
    assert_no_doom_loop(trace)
    out = trace.output or {}
    assert out.get("title") == EXPEDITION["title"], trace.describe()
    assert out.get("curator") == EXPEDITION["curator"], trace.describe()
    assert out.get("foundedYear") == EXPEDITION["foundedYear"], trace.describe()
    members = sorted(m.get("name") for m in out.get("members") or [])
    assert members == sorted(set(EXPEDITION["members"])), trace.describe()


def test_schema_enrich(run_scenario, fixture_url):
    expected = [
        {"name": s["name"], "role": s["role"], "dailyRateGbp": s["dailyRateGbp"]}
        for s in STAFF[:4]
    ]
    trace = run_scenario(
        "schema_enrich",
        f"Go to {fixture_url}/listing_small.html — four staff links. First add the "
        "four names as rows. Then read the four profile pages and merge role and "
        "dailyRateGbp into the existing rows using ONE update_items call, not "
        "repeated update_item calls.",
        output_schema=_items_schema(
            {
                "name": {"type": "string"},
                "role": {"type": "string"},
                "dailyRateGbp": {"type": "integer"},
            },
            array="staff",
            required=["name"],
        ),
        max_cost_usd=0.15,
    )
    assert_success(trace)
    assert_used(trace, "update_items")
    assert_no_doom_loop(trace)
    assert_output(trace, {"staff": expected})


def _conditional_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "reason": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "firstItem": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["found"],
        "if": {"properties": {"found": {"const": False}}},
        "then": {"required": ["reason"]},
        "else": {"not": {"required": ["reason"]}},
    }


def test_schema_conditional_field(run_scenario, fixture_url):
    """`reason` only applies when the page was not found. Without the conditional
    the gate bounces a complete run and the agent spends four steps settling a
    field the schema itself says does not apply."""
    trace = run_scenario(
        "schema_conditional",
        f"Go to {fixture_url}/two_items.html. Set found to true and put the name of "
        "the first case for sale in firstItem. Leave reason alone: it applies only "
        "when a page could not be retrieved.",
        output_schema=_conditional_schema(),
        max_cost_usd=0.15,
    )
    assert_success(trace)
    assert_no_doom_loop(trace)
    assert_not_used(trace, "mark_absent")
    out = trace.output()
    assert out.get("found") is True, trace.describe()
    assert out.get("reason") is None, trace.describe()
    assert TWO_ITEMS[0]["name"] in (out.get("firstItem") or ""), trace.describe()


def test_schema_seeded_defaults(run_scenario, fixture_url):
    """A field the caller already knows arrives filled, and the agent is asked only
    for the one it does not."""
    seeded = "Wardian cases, sold as seen"
    schema = {
        "type": "object",
        "properties": {
            "catalogueNote": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": seeded,
            },
            "secondItem": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["secondItem"],
    }
    trace = run_scenario(
        "schema_seeded_defaults",
        f"Go to {fixture_url}/two_items.html and put the name of the SECOND case for "
        "sale in secondItem. catalogueNote is already filled in for you — leave it "
        "exactly as it is.",
        output_schema=schema,
        max_cost_usd=0.15,
    )
    assert_success(trace)
    assert_no_doom_loop(trace)
    out = trace.output()
    assert out.get("catalogueNote") == seeded, trace.describe()
    assert TWO_ITEMS[1]["name"] in (out.get("secondItem") or ""), trace.describe()
