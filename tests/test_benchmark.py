"""The benchmark spec names every field once, and the scorer grades runs the way it says."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from scripts import benchmark as bench

FIXTURE = Path(__file__).parent / "fixtures" / "ashby_marshmallow.json"
PRICING = "27e7e5ca-6cf7-4c91-b922-a1cb792917e0"
BUDAPEST = "6f057170"
HEAD = "3dee50f9"
CLAIMS = "fd0acc52-8604-4648-8575-631bd155a1c0"
DEADLINE_PAGE = (
    '<html><script>window.__appData = {"posting": {"workplaceType": "Hybrid", '
    '"applicationDeadline": "2026-10-08T22:00:00.000Z"}};\n</script></html>'
)


@pytest.fixture(scope="module")
def key():
    api = json.loads(FIXTURE.read_text())
    return bench.build_key(api, {CLAIMS: bench.page_data(DEADLINE_PAGE)})


def _role(key, prefix):
    return next(r for r in key["roles"] if r["id"].startswith(prefix))


def _perfect(key) -> list[dict]:
    records = []
    for role in key["roles"]:
        f = role["fields"]
        first = lambda name: f[name]["accept"][0] if f[name] else None  # noqa: E731
        visa = f["visaSponsorship"]
        records.append({
            "title": role["title"],
            "description": role["text"],
            "sourceUrl": f"https://jobs.ashbyhq.com/marshmallow/{role['id']}",
            "applyUrl": f"https://jobs.ashbyhq.com/marshmallow/{role['id']}/application",
            "location": first("location"),
            "locationType": first("locationType"),
            "department": first("department"),
            "seniority": first("seniority") and first("seniority").title(),
            "postedAt": first("postedAt"),
            "expiresAt": first("expiresAt"),
            "skills": ["Python"] if "Python" in role["text"] else ["experience"],
            "compensationType": first("compensationType"),
            "ir35Status": None,
            "salaryMin": first("salaryMin"),
            "salaryMax": first("salaryMax"),
            "salaryCurrency": first("salaryCurrency"),
            "payPeriod": first("payPeriod"),
            "visaSponsorship": visa and visa["quote"],
            "companyName": "Marshmallow",
            "companyUrl": "https://www.marshmallow.com",
            "companyDescription": role["text"].split("\n")[0],
        })
    return records


def _run(records, variant="A", careers="https://www.marshmallow.com/jobs#ashby_embed"):
    spec = bench.variant_spec(variant)
    return {
        "id": "run", "model": "m", "reasoningEffort": "none", "_variant": variant,
        "_spec": {"task": spec["task"], "outputSchema": spec["outputSchema"]},
        "output": {"jobs": records, "careersPageUrl": careers},
    }


def _issues(score, kind):
    return [(i["field"], i["role"]) for i in score["issues"] if i["kind"] == kind]


def test_variant_b_differs_from_a_only_in_leaving_visa_unnamed():
    a, b = bench.load_spec(), bench.variant_spec("B")
    visa = bench.job_properties(a["outputSchema"])["visaSponsorship"]["description"]
    assert b["task"] == a["task"].replace(bench.bullet("visaSponsorship", visa), "")
    expected = copy.deepcopy(a["outputSchema"])
    del bench.job_properties(expected)["visaSponsorship"]["description"]
    assert b["outputSchema"] == expected
    assert "visa" not in b["task"].casefold()
    assert bench.named_fields(a) - bench.named_fields(b) == {"visaSponsorship"}


def test_every_field_has_one_bullet_matching_its_description_in_schema_order():
    spec = bench.load_spec()
    props = bench.job_properties(spec["outputSchema"])
    positions = []
    for name, node in props.items():
        line = bench.bullet(name, node["description"])
        assert spec["task"].count(line) == 1, name
        positions.append(spec["task"].index(line))
    assert positions == sorted(positions)
    assert bench.named_fields(spec) == set(props) | {"careersPageUrl"}


def test_the_spec_asks_for_no_extra_array_and_drops_retired_fields():
    spec = bench.load_spec()
    assert not re.search(r"\bextra\b", spec["task"])
    assert {"companySourceId", "address"}.isdisjoint(bench.job_properties(spec["outputSchema"]))
    assert (
        "5. Include structured data already in the page (JSON-LD, meta tags, embedded page "
        "data such as __NEXT_DATA__) if accessible, but prioritise what is visually present."
    ) in spec["task"]


def test_the_key_reads_each_field_the_way_the_page_shows_it(key):
    pricing, budapest = _role(key, PRICING)["fields"], _role(key, BUDAPEST)
    head, claims = _role(key, HEAD)["fields"], _role(key, CLAIMS)["fields"]

    assert pricing["visaSponsorship"]["accept"] == ["offers"]
    assert pricing["locationType"]["accept"] == ["HYBRID"]
    assert pricing["seniority"] is None and pricing["expiresAt"] is None

    assert budapest["salaryIn"] == "description"
    pay = budapest["fields"]
    assert pay["salaryMin"]["accept"] == [720000.0] and pay["salaryMax"] is None
    assert pay["salaryCurrency"]["accept"] == ["HUF"]
    assert pay["payPeriod"]["accept"] == ["MONTHLY"]
    assert pay["locationType"]["accept"] == ["ONSITE"]
    assert pay["visaSponsorship"] is None

    assert head["visaSponsorship"]["accept"] == ["does not offer"]
    assert head["compensationType"]["accept"] == ["SALARIED", "CONTRACT"]
    assert head["seniority"]["accept"] == ["head"]

    assert claims["expiresAt"]["accept"] == ["2026-10-08", "2026-10-09"]


def test_a_perfect_run_scores_full_marks(key):
    score = bench.score_run(_run(_perfect(key)), key)
    assert score["records"] == "4/4"
    assert score["accuracy"] == 100.0 and score["proactive"] == 100.0
    assert score["faithful"] and score["careersPageUrl"]


def test_a_missing_value_costs_accuracy_but_is_not_invented(key):
    records = _perfect(key)
    records[0]["postedAt"] = None
    score = bench.score_run(_run(records), key)
    assert score["accuracy"] < 100.0 and score["faithful"]
    assert ("postedAt", "Pricing Data Scientist") in _issues(score, "missing")


def test_a_wrong_value_and_an_apply_link_that_is_only_the_listing_are_wrong(key):
    records = _perfect(key)
    records[0]["location"] = "Paris"
    records[0]["applyUrl"] = records[0]["sourceUrl"]
    score = bench.score_run(_run(records), key)
    assert set(_issues(score, "wrong")) == {
        ("location", "Pricing Data Scientist"), ("applyUrl", "Pricing Data Scientist"),
    }
    assert score["faithful"]


def test_a_value_the_page_never_shows_is_invented(key):
    records = _perfect(key)
    records[0]["seniority"] = "Mid-level"
    records[0]["ir35Status"] = "OUTSIDE_IR35"
    score = bench.score_run(_run(records), key)
    assert set(_issues(score, "invented")) == {
        ("seniority", "Pricing Data Scientist"), ("ir35Status", "Pricing Data Scientist"),
    }
    assert not score["faithful"] and score["accuracy"] < 100.0


def test_a_word_the_page_uses_is_not_invented_and_not_stated_counts_as_empty(key):
    records = _perfect(key)
    records[0]["seniority"] = "Data Scientist"
    records[1]["visaSponsorship"] = "Not stated on the page"
    records[1]["ir35Status"] = "UNKNOWN"
    score = bench.score_run(_run(records), key)
    assert score["faithful"] and score["accuracy"] == 100.0


def test_visa_is_scored_by_meaning_not_wording(key):
    records = _perfect(key)
    records[0]["visaSponsorship"] = "Sponsorship available"
    records[2]["visaSponsorship"] = "false"
    assert bench.score_run(_run(records), key)["accuracy"] == 100.0
    records[0]["visaSponsorship"] = "We can't sponsor visas"
    score = bench.score_run(_run(records), key)
    assert ("visaSponsorship", "Pricing Data Scientist") in _issues(score, "wrong")


def test_pay_found_only_in_the_description_is_proactive(key):
    records = _perfect(key)
    budapest = next(r for r in records if BUDAPEST in r["sourceUrl"])
    for field in bench.SALARY_FIELDS:
        budapest[field] = None
    score = bench.score_run(_run(records), key)
    assert score["accuracy"] == 100.0
    assert score["proactive"] == 0.0


def test_visa_is_proactive_only_when_the_prompt_leaves_it_unnamed(key):
    records = _perfect(key)
    for record in records:
        record["visaSponsorship"] = None
    a = bench.score_run(_run(records, "A"), key)
    b = bench.score_run(_run(records, "B"), key)
    assert a["accuracy"] < 100.0
    assert b["accuracy"] == 100.0 and b["proactive"] < 100.0


def test_a_record_for_a_role_not_on_the_page_is_invented_and_the_missed_role_counts(key):
    records = _perfect(key)
    records[0]["sourceUrl"] = "https://jobs.ashbyhq.com/marshmallow/00000000-0000-0000-0000-000000000000"
    records[0]["applyUrl"] = None
    records[0]["title"] = "Chief Vibes Officer"
    score = bench.score_run(_run(records), key)
    assert score["records"] == "3/4" and score["unlisted"] == 1
    assert not score["faithful"]
    assert (None, "Pricing Data Scientist") in _issues(score, "missing record")


def test_a_record_with_no_id_is_matched_by_its_title(key):
    records = _perfect(key)
    records[0]["sourceUrl"] = records[0]["applyUrl"] = None
    score = bench.score_run(_run(records), key)
    assert score["records"] == "4/4"
    assert ("sourceUrl", "Pricing Data Scientist") in _issues(score, "missing")


def test_a_duplicate_record_costs_accuracy(key):
    records = _perfect(key)
    score = bench.score_run(_run(records + [dict(records[0])]), key)
    assert score["duplicates"] == 1 and score["accuracy"] < 100.0 and score["faithful"]


@pytest.mark.parametrize(
    "careers, ok",
    [
        ("https://www.marshmallow.com/jobs", True),
        ("https://marshmallow.com/jobs#openings", True),
        ("https://jobs.ashbyhq.com/marshmallow?embed=js", True),
        ("https://www.marshmallow.com/", False),
        (None, False),
    ],
)
def test_the_careers_page_accepts_the_board_wherever_it_is_hosted(key, careers, ok):
    assert bench.score_run(_run(_perfect(key), careers=careers), key)["careersPageUrl"] is ok


def test_an_empty_key_is_refused(key):
    with pytest.raises(ValueError, match="no roles"):
        bench.score_run(_run([]), {**key, "roles": []})


def test_a_run_saved_without_its_spec_is_scored_by_the_spec_it_names(key):
    old = bench.variant_spec("A")
    old["task"] = "Fill title, location, department, applyUrl, sourceUrl and careersPageUrl."
    for node in bench.job_properties(old["outputSchema"]).values():
        node.pop("description", None)
    old["outputSchema"]["properties"]["careersPageUrl"].pop("description")
    run = _run(_perfect(key))
    del run["_spec"]
    for record in run["output"]["jobs"]:
        record["visaSponsorship"] = None
    score = bench.score_run(run, key, old)
    assert score["accuracy"] == 100.0 and score["proactive"] < 100.0


@pytest.mark.parametrize(
    "text, number",
    [("720.000 HUF", 720000.0), ("£52,500", 52500.0), ("52k", 52000.0), ("1.5", 1.5),
     ("720 000", 720000.0), (61000, 61000.0)],
)
def test_pay_figures_read_with_either_thousands_separator(text, number):
    assert bench.parse_number(text) == number
