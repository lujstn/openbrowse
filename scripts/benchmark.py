"""OpenBrowse's careers-page benchmark: its variants, answer key, runs and scores.

Usage:
    python -m scripts.benchmark variant [--unnamed FIELD ...] [-o PATH]
    python -m scripts.benchmark key -o KEY.json [--only ID ...]
    python -m scripts.benchmark run --model M --effort E [--variant A|B] -o RUN.json
    python -m scripts.benchmark score RUN.json [...] --key KEY.json [--spec SPEC.json] [-o PATH]

A field is asked when the prompt names it: it has a schema description, or its name
appears in the task. Every other field is proactive. Pay found only in a role's
description is proactive too, because the salary fields ask for the role's salary or
pay details. Accuracy is the share of asked values the page shows that a run got
right, with every invented value, duplicate and unlisted record counted against it.
Proactive is the share of proactive values the page shows that a run got right.
"""

from __future__ import annotations

import argparse
import copy
import html as html_lib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "benchmark.json"
VARIANTS: dict[str, tuple[str, ...]] = {"A": (), "B": ("visaSponsorship",)}

BOARD = "marshmallow"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
ASHBY_PAGE = "https://jobs.ashbyhq.com/{board}/{id}"
CAREERS_PAGES = ["marshmallow.com/jobs", "jobs.ashbyhq.com/marshmallow"]
COMPANY = {"name": ["marshmallow"], "hosts": ["marshmallow.com", "marshmallow.co"]}

SALARY_FIELDS = ("salaryMin", "salaryMax", "salaryCurrency", "payPeriod")
SENIORITY_WORDS = (
    "intern", "trainee", "graduate", "junior", "associate", "mid", "senior", "staff",
    "principal", "lead", "head", "director", "vp", "vice president", "chief",
)
NULLISH = re.compile(r"^(?:none|null|n/?a|-+)\.?$")
NOT_STATED = re.compile(
    r"\b(?:not (?:stated|mentioned|specified|shown|listed|provided|disclosed|found)|"
    r"no (?:\w+ ){0,3}(?:information|mention|details?|data)|unknown|unspecified)\b"
)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PERIODS = {
    "ANNUAL": r"per annum|annual|annually|a year|per year|yearly|p\.?a\.?",
    "MONTHLY": r"monthly|a month|per month|/month",
    "WEEKLY": r"weekly|a week|per week",
    "DAILY": r"daily|a day|per day|day rate",
    "HOURLY": r"hourly|an hour|per hour|/hour",
}
ASHBY_INTERVALS = {"1 YEAR": "ANNUAL", "1 MONTH": "MONTHLY", "1 WEEK": "WEEKLY",
                   "1 DAY": "DAILY", "1 HOUR": "HOURLY"}
CURRENCIES = {"£": "GBP", "€": "EUR", "$": "USD", "ft": "HUF", "huf": "HUF",
              "gbp": "GBP", "eur": "EUR", "usd": "USD"}
MONEY = r"(?:£|€|\$|\b(?:GBP|EUR|USD|HUF)\b)?\s?\d[\d,.]*\s?(?:k\b|K\b)?\s?(?:\b(?:GBP|EUR|USD|HUF|Ft)\b)?"


def load_spec(path: Path = SPEC_PATH) -> dict:
    return json.loads(path.read_text())


def job_properties(schema: dict) -> dict:
    jobs = schema["properties"]["jobs"]
    array = next(b for b in jobs.get("anyOf", [jobs]) if b.get("type") == "array")
    return array["items"]["properties"]


def bullet(name: str, text: str) -> str:
    return f"\n   - {name}: {text}"


def make_variant(spec: dict, unnamed: tuple[str, ...] | list[str]) -> dict:
    """Leave each field out of the prompt and drop its schema description, nothing else."""
    out = copy.deepcopy(spec)
    props = job_properties(out["outputSchema"])
    for name in unnamed:
        text = props[name].pop("description")
        line = bullet(name, text)
        if out["task"].count(line) != 1:
            raise ValueError(f"{name}'s bullet is not in the task exactly once")
        out["task"] = out["task"].replace(line, "", 1)
    return out


def variant_spec(name: str, spec: dict | None = None) -> dict:
    return make_variant(spec or load_spec(), VARIANTS[name])


def named_fields(spec: dict) -> set[str]:
    props = job_properties(spec["outputSchema"])
    task = spec["task"]
    named = {
        f for f, node in props.items()
        if node.get("description") or re.search(rf"\b{re.escape(f)}\b", task)
    }
    if re.search(r"\bcareersPageUrl\b", task) or spec["outputSchema"]["properties"][
        "careersPageUrl"
    ].get("description"):
        named.add("careersPageUrl")
    return named


def norm(value) -> str:
    text = html_lib.unescape(str(value)).casefold()
    text = text.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", norm(text)) if len(t) > 1}


def is_empty(value) -> bool:
    if value is None or value == [] or value == {}:
        return True
    if not isinstance(value, str):
        return False
    text = norm(value)
    return not text or bool(NULLISH.match(text)) or (len(text) <= 60 and bool(NOT_STATED.search(text)))


def grounded_share(value: str, source: str) -> float:
    words = tokens(value)
    return len(words & tokens(source)) / len(words) if words else 0.0


def item_grounded(item: str, source: str) -> bool:
    if norm(item) in norm(source):
        return True
    words = tokens(item)
    return bool(words) and words <= tokens(source)


def visa_meaning(value) -> str | None:
    if isinstance(value, bool):
        return "offers" if value else "does not offer"
    if is_empty(value):
        return None
    text = norm(value)
    if re.search(r"\b(?:no|not|false|cannot|can't|cant|unable|unavailable|without|won't)\b", text):
        return "does not offer"
    if re.search(r"\b(?:yes|true|offers?|offered|available|sponsors?|sponsored|sponsorship)\b",
                 text):
        return "offers"
    return "unclear"


def parse_date(value) -> str | None:
    text = str(value).strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = re.search(r"\d[\d,.\u00a0 ]*\d|\d", str(value))
    if not match:
        return None
    digits = match.group(0).strip()
    if re.fullmatch(r"\d{1,3}(?:([.,\u00a0 ])\d{3})(?:\1\d{3})*", digits):
        number = float(re.sub(r"\D", "", digits))
    else:
        try:
            number = float(re.sub(r"[,\u00a0 ]", "", digits))
        except ValueError:
            return None
    return number * 1000 if re.search(r"\d\s?k\b", str(value), re.I) else number


def deadline_dates(value) -> list[str]:
    """Every calendar date a timestamp falls on in some time zone, or the date itself."""
    text = str(value)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return [d for d in [parse_date(text)] if d]
    if "T" not in text:
        return [moment.date().isoformat()]
    return sorted({(moment + timedelta(hours=h)).date().isoformat() for h in (-12, 0, 14)})


def currency(value) -> str:
    text = norm(value)
    return CURRENCIES.get(text, text.upper())


def host_path(url: str) -> tuple[str, str]:
    parsed = urlparse(url if "//" in url else f"https://{url}")
    return parsed.netloc.casefold().removeprefix("www."), parsed.path.rstrip("/")


# The answer key ---------------------------------------------------------------


def page_data(page: str) -> dict:
    """The JobPosting JSON-LD and Ashby's embedded posting data from a role's page."""
    data: dict = {}
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            parsed = json.loads(block)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("@type") == "JobPosting":
            data["jsonld"] = parsed
    match = re.search(r"window\.__appData\s*=\s*(\{.*?\});\s*\n", page, re.S)
    if match:
        try:
            data["posting"] = json.loads(match.group(1)).get("posting") or {}
        except ValueError:
            pass
    return data


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _location_type(workplace: str | None, text: str) -> list[str] | None:
    mapped = {"Hybrid": "HYBRID", "OnSite": "ONSITE", "Remote": "REMOTE"}.get(workplace or "")
    if mapped:
        return [mapped]
    if re.search(r"\bhybrid\b", text, re.I):
        return ["HYBRID"]
    if re.search(r"\b(?:on-?site|office-based) (?:position|role)\b", text, re.I):
        return ["ONSITE"]
    if re.search(r"\b(?:fully remote|remote-first|remote (?:position|role))\b", text, re.I):
        return ["REMOTE"]
    return None


def _pay_from_ashby(compensation: dict | None) -> dict | None:
    for component in (compensation or {}).get("summaryComponents") or []:
        if component.get("compensationType") != "Salary":
            continue
        return {
            "salaryMin": component.get("minValue"),
            "salaryMax": component.get("maxValue"),
            "salaryCurrency": component.get("currencyCode"),
            "payPeriod": ASHBY_INTERVALS.get(component.get("interval") or ""),
            "quote": (compensation or {}).get("compensationTierSummary"),
        }
    return None


def _pay_from_text(text: str) -> dict | None:
    for sentence in _sentences(text):
        if not re.search(r"\b(?:salary|pay|compensation|OTE)\b", sentence, re.I):
            continue
        amounts = [m.group(0) for m in re.finditer(MONEY, sentence)
                   if re.search(r"\d{3}|\dk", m.group(0), re.I)]
        if not amounts:
            continue
        found = re.search(r"£|€|\$|\b(?:GBP|EUR|USD|HUF|Ft)\b", sentence)
        period = next((p for p, pattern in PERIODS.items()
                       if re.search(rf"(?:{pattern})", sentence, re.I)), None)
        low = parse_number(amounts[0])
        high = parse_number(amounts[1]) if len(amounts) > 1 else None
        if high is None and re.search(r"\bup to\b", sentence, re.I):
            low, high = None, low
        return {
            "salaryMin": low,
            "salaryMax": high,
            "salaryCurrency": currency(found.group(0)) if found else None,
            "payPeriod": period,
            "quote": sentence,
        }
    return None


def build_role(job: dict, page: dict | None = None) -> dict:
    page = page or {}
    jsonld, posting = page.get("jsonld") or {}, page.get("posting") or {}
    text = job.get("descriptionPlain") or ""
    title = (job.get("title") or "").strip()
    fields: dict[str, dict | None] = dict.fromkeys(
        ("expiresAt", "ir35Status", "visaSponsorship", *SALARY_FIELDS)
    )

    def accept(*values, **extra) -> dict:
        return {"accept": [v for v in dict.fromkeys(values) if v], **extra}

    fields["title"] = accept(title)
    fields["description"] = accept()
    fields["sourceUrl"] = accept(job.get("jobUrl"))
    fields["applyUrl"] = accept(job.get("applyUrl"))
    locations = [job.get("location")] + [s.get("location") for s in job.get("secondaryLocations") or []]
    fields["location"] = accept(*locations)
    kind = _location_type(job.get("workplaceType") or posting.get("workplaceType"), text)
    fields["locationType"] = accept(*kind) if kind else None
    fields["department"] = accept(job.get("department"), job.get("team"))
    words = [w for w in SENIORITY_WORDS if re.search(rf"\b{w}\b", norm(title))]
    fields["seniority"] = accept(*words) if words else None
    posted = jsonld.get("datePosted") or (job.get("publishedAt") or "")[:10]
    fields["postedAt"] = accept(parse_date(posted)) if posted else None
    closing = jsonld.get("validThrough") or posting.get("applicationDeadline")
    if closing:
        fields["expiresAt"] = accept(*deadline_dates(closing))
    skills_shown = re.search(
        r"\b(?:skills?|experience|expertise|knowledge|proficien\w*|familiar\w*|ability to)\b",
        text, re.I)
    fields["skills"] = accept() if skills_shown else None
    employment = job.get("employmentType") or ""
    kinds = {"FullTime": ["SALARIED"], "PartTime": ["SALARIED"], "Contract": ["CONTRACT"],
             "Temporary": ["SALARIED", "CONTRACT"], "Intern": ["SALARIED", "CONTRACT"]}.get(employment)
    if kinds and re.search(r"\b(?:contract|fixed[- ]term|ftc)\b", title, re.I):
        kinds = list(dict.fromkeys(kinds + ["CONTRACT"]))
    fields["compensationType"] = accept(*kinds) if kinds else None
    ir35 = re.search(r"\b(inside|outside) ir35\b", text, re.I)
    if ir35:
        fields["ir35Status"] = accept(f"{ir35.group(1).upper()}_IR35")

    pay = _pay_from_ashby(job.get("compensation"))
    salary_in = "pay details" if pay else None
    if not pay:
        pay = _pay_from_text(text)
        salary_in = "description" if pay else None
    if pay:
        for f in SALARY_FIELDS:
            if pay.get(f) is not None:
                fields[f] = accept(pay[f], quote=pay.get("quote"))

    visa = [s for s in _sentences(text) if re.search(r"\bvisa|sponsor", s, re.I)]
    if visa:
        meanings = sorted({m for m in map(visa_meaning, visa) if m})
        fields["visaSponsorship"] = accept(*meanings, quote=" ".join(visa))

    fields["companyName"] = accept(*COMPANY["name"])
    fields["companyUrl"] = accept(*COMPANY["hosts"])
    fields["companyDescription"] = accept()
    return {"id": job["id"], "title": title, "salaryIn": salary_in, "text": text, "fields": fields}


def build_key(api: dict, pages: dict[str, dict] | None = None,
              only: list[str] | None = None) -> dict:
    pages = pages or {}
    jobs = [j for j in api.get("jobs") or [] if j.get("isListed", True)]
    if only:
        jobs = [j for j in jobs if j["id"] in only]
    return {
        "board": BOARD,
        "capturedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "careersPageUrl": CAREERS_PAGES,
        "roles": [build_role(j, pages.get(j["id"])) for j in jobs],
    }


# Scoring ----------------------------------------------------------------------


def _correct(field: str, value, expected: dict, role: dict) -> bool:
    accept = expected.get("accept") or []
    text = role["text"]
    if field == "title":
        return any(norm(value) == norm(a) for a in accept)
    if field == "description":
        return (grounded_share(str(value), text) >= 0.9
                and len(tokens(str(value)) & tokens(text)) >= 0.8 * len(tokens(text)))
    if field == "sourceUrl":
        return role["id"] in str(value) and not host_path(str(value))[1].endswith("/application")
    if field == "applyUrl":
        return role["id"] in str(value) and host_path(str(value))[1].endswith("/application")
    if field in ("location", "department"):
        return any(norm(a) in norm(value) or norm(value) in norm(a) for a in accept)
    if field == "seniority":
        return any(re.search(rf"\b{re.escape(a)}\b", norm(value)) for a in accept)
    if field in ("postedAt", "expiresAt"):
        return parse_date(value) in accept
    if field == "skills":
        items = _items(value)
        return sum(item_grounded(str(i), f"{role['title']}\n{text}") for i in items) >= 0.8 * len(items)
    if field in ("locationType", "compensationType", "ir35Status", "payPeriod"):
        return str(value).upper() in accept
    if field in ("salaryMin", "salaryMax"):
        number = parse_number(value)
        return number is not None and any(abs(number - a) <= 0.005 * a for a in accept)
    if field == "salaryCurrency":
        return currency(value) in accept
    if field == "visaSponsorship":
        return visa_meaning(value) in accept
    if field == "companyName":
        return any(a in norm(value) for a in accept)
    if field == "companyUrl":
        return host_path(str(value))[0] in accept
    if field == "companyDescription":
        return grounded_share(str(value), text) >= 0.8
    return False


def _grounded_when_not_shown(field: str, value, role: dict) -> bool:
    source = f"{role['title']}\n{role['text']}"
    if field in ("salaryMin", "salaryMax"):
        twin = role["fields"].get("salaryMax" if field == "salaryMin" else "salaryMin")
        return bool(twin) and _correct(field, value, twin, role)
    if field == "skills":
        items = _items(value)
        return all(item_grounded(str(i), source) for i in items)
    if field in ("seniority", "location", "department", "description", "companyDescription"):
        return item_grounded(str(value), source) or grounded_share(str(value), source) >= 0.9
    return False


def _items(value) -> list:
    return value if isinstance(value, list) else [v for v in re.split(r"[,;\n]", str(value)) if v.strip()]


def record_id(record: dict) -> str | None:
    for key in ("sourceUrl", "applyUrl"):
        match = UUID_RE.search(str(record.get(key) or "").casefold())
        if match:
            return match.group(0)
    return None


def _output(run: dict) -> dict:
    out = run.get("output")
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            out = None
    return out if isinstance(out, dict) else {}


def _run_spec(run: dict, spec: dict | None) -> tuple[dict, str]:
    if run.get("_spec"):
        return run["_spec"], run.get("_variant") or "?"
    if spec is None:
        raise ValueError(f"run {run.get('id')} has no _spec; pass --spec")
    return spec, spec.get("_variant", "?")


def score_run(run: dict, key: dict, spec: dict | None = None) -> dict:
    if not key["roles"]:
        raise ValueError("the answer key has no roles")
    spec, variant = _run_spec(run, spec)
    named = named_fields(spec)
    fields = [f for f in job_properties(spec["outputSchema"]) if f in key["roles"][0]["fields"]]
    output = _output(run)
    records = [r for r in output.get("jobs") or [] if isinstance(r, dict)]
    roles = {r["id"]: r for r in key["roles"]}
    by_title = {norm(r["title"]): r["id"] for r in key["roles"]}

    tally = {f: dict(hit=0, miss=0, wrong=0, invented=0, grounded=0, asked=0, proactive=0)
             for f in fields}
    issues: list[dict] = []
    buckets = {"asked": [0, 0], "proactive": [0, 0]}
    invented = duplicates = duplicate_cells = unlisted = 0
    seen: set[str] = set()

    def note(kind, field, value=None, role=None):
        issues.append({"kind": kind, "field": field, "role": role, "value": value})

    for record in records:
        rid = record_id(record) or by_title.get(norm(record.get("title") or ""))
        filled = [f for f in fields if not is_empty(record.get(f))]
        if rid not in roles:
            unlisted += 1
            invented += len(filled)
            note("unlisted record", None, record.get("title"))
            continue
        if rid in seen:
            duplicates += 1
            duplicate_cells += len(named & set(filled))
            note("duplicate record", None, record.get("title"), rid)
            continue
        seen.add(rid)
        role = roles[rid]
        for f in fields:
            expected = role["fields"].get(f)
            value = record.get(f)
            asked = f in named and not (f in SALARY_FIELDS and role["salaryIn"] == "description")
            bucket = "asked" if asked else "proactive"
            if expected is None:
                if is_empty(value):
                    continue
                if _grounded_when_not_shown(f, value, role):
                    tally[f]["grounded"] += 1
                    continue
                tally[f]["invented"] += 1
                invented += 1
                note("invented", f, value, role["title"])
                continue
            tally[f][bucket] += 1
            buckets[bucket][1] += 1
            if is_empty(value):
                tally[f]["miss"] += 1
                note("missing", f, None, role["title"])
            elif _correct(f, value, expected, role):
                tally[f]["hit"] += 1
                buckets[bucket][0] += 1
            else:
                tally[f]["wrong"] += 1
                note("wrong", f, value, role["title"])

    for rid, role in roles.items():
        if rid in seen:
            continue
        note("missing record", None, None, role["title"])
        for f in fields:
            if role["fields"].get(f) is None:
                continue
            asked = f in named and not (f in SALARY_FIELDS and role["salaryIn"] == "description")
            buckets["asked" if asked else "proactive"][1] += 1
            tally[f]["miss"] += 1

    careers = output.get("careersPageUrl")
    careers_bucket = buckets["asked" if "careersPageUrl" in named else "proactive"]
    careers_bucket[1] += 1
    careers_ok = not is_empty(careers) and "".join(host_path(str(careers))) in key["careersPageUrl"]
    if careers_ok:
        careers_bucket[0] += 1
    else:
        note("wrong" if careers else "missing", "careersPageUrl", careers)

    asked_hit, asked_total = buckets["asked"]
    pro_hit, pro_total = buckets["proactive"]
    denominator = asked_total + invented + duplicate_cells
    return {
        "run": run.get("id"),
        "model": run.get("model"),
        "effort": run.get("reasoningEffort"),
        "variant": variant,
        "status": run.get("status"),
        "records": f"{len(seen)}/{len(roles)}",
        "accuracy": round(100 * asked_hit / denominator, 1) if denominator else None,
        "proactive": round(100 * pro_hit / pro_total, 1) if pro_total else None,
        "invented": invented,
        "duplicates": duplicates,
        "unlisted": unlisted,
        "faithful": invented == 0 and unlisted == 0,
        "careersPageUrl": careers_ok,
        "steps": run.get("stepCount"),
        "seconds": run.get("_wall_seconds") or _seconds(run),
        "tokens": (run.get("totalInputTokens") or 0) + (run.get("totalOutputTokens") or 0),
        "llmCostUsd": float(run.get("llmCostUsd") or 0),
        "fields": tally,
        "issues": issues,
    }


def _seconds(run: dict) -> int | None:
    try:
        start = datetime.fromisoformat(run["createdAt"])
        end = datetime.fromisoformat(run["updatedAt"])
    except (KeyError, TypeError, ValueError):
        return None
    return round((end - start).total_seconds())


def summarise(scores: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for s in scores:
        groups.setdefault((s["model"], s["effort"], s["variant"]), []).append(s)

    def mean(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 1) if values else None

    return [
        {
            "model": model, "effort": effort, "variant": variant, "runs": len(runs),
            "accuracy": mean(r["accuracy"] for r in runs),
            "proactive": mean(r["proactive"] for r in runs),
            "records": [r["records"] for r in runs],
            "faithful": all(r["faithful"] for r in runs),
            "seconds": mean(r["seconds"] for r in runs),
            "tokens": mean(r["tokens"] for r in runs),
            "llmCostUsd": mean(r["llmCostUsd"] for r in runs),
        }
        for (model, effort, variant), runs in groups.items()
    ]


# Commands ---------------------------------------------------------------------


def cmd_variant(args) -> None:
    out = make_variant(load_spec(), args.unnamed)
    text = json.dumps(out, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)


def cmd_key(args) -> None:
    headers = {"User-Agent": "Mozilla/5.0 (openbrowse benchmark key)"}
    with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as client:
        api = client.get(ASHBY_API.format(board=BOARD)).raise_for_status().json()
        pages = {}
        for job in api.get("jobs") or []:
            if args.only and job["id"] not in args.only:
                continue
            response = client.get(ASHBY_PAGE.format(board=BOARD, id=job["id"]))
            if response.status_code == 200:
                pages[job["id"]] = page_data(response.text)
    key = build_key(api, pages, args.only)
    Path(args.output).write_text(json.dumps(key, indent=2, ensure_ascii=False) + "\n")
    for role in key["roles"]:
        visa = role["fields"]["visaSponsorship"]
        print(f"{role['id'][:8]}  {role['title']:<50} visa={visa and visa['accept']} "
              f"pay={role['salaryIn']}")
    print(f"{len(key['roles'])} roles -> {args.output}")


def _api_key(explicit: str | None) -> str:
    if explicit or os.environ.get("OPENBROWSE_API_KEY"):
        return explicit or os.environ["OPENBROWSE_API_KEY"]
    from openbrowse.config import settings

    return settings.api_key


def cmd_run(args) -> None:
    from openbrowse.agent.runner import step_timeout

    spec = variant_spec(args.variant)
    base = args.base_url.rstrip("/") + "/v3/sessions"
    headers = {"X-Browser-Use-API-Key": _api_key(args.api_key)}
    with httpx.Client(timeout=60, headers=headers) as client:
        listed = client.get(base, params={"page_size": 100}).raise_for_status().json()
        busy = [s["id"] for s in listed["sessions"] if s["status"] in ("created", "running")]
        if busy:
            sys.exit(f"refusing to start: {len(busy)} session(s) already running")
        body = {"task": spec["task"], "outputSchema": spec["outputSchema"],
                "maxCostUsd": spec["maxCostUsd"], "model": args.model,
                "reasoningEffort": args.effort}
        if args.profile_id:
            body["profileId"] = args.profile_id
        session = client.post(base, json=body).raise_for_status().json()
        sid, started = session["id"], time.time()
        print(json.dumps({"session": sid, "model": args.model, "effort": args.effort,
                          "variant": args.variant}), flush=True)

        # @nonobvious(forced-by): the server already ends a step at its step timeout, so
        # only a run that outlasts that without a new step has stopped moving.
        stall_after = step_timeout(args.effort) + 120
        timeline, last_steps, last_change, stalled = [], -1, time.time(), False
        while True:
            time.sleep(5)
            session = client.get(f"{base}/{sid}").raise_for_status().json()
            steps, now = session.get("stepCount") or 0, time.time()
            if steps != last_steps:
                timeline.append([round(now - started), steps])
                print(json.dumps({"session": sid, "step": steps, "at": round(now - started)}),
                      flush=True)
                last_steps, last_change = steps, now
            if session.get("status") not in ("created", "running"):
                break
            if now - last_change > stall_after:
                stalled = True
                client.post(f"{base}/{sid}/stop", json={})
                for _ in range(24):
                    time.sleep(5)
                    session = client.get(f"{base}/{sid}").raise_for_status().json()
                    if session.get("status") not in ("created", "running"):
                        break
                break

    session.update({
        "_variant": args.variant,
        "_spec": {"task": spec["task"], "outputSchema": spec["outputSchema"]},
        "_timeline": timeline,
        "_stalled": stalled,
        "_wall_seconds": round(time.time() - started),
    })
    Path(args.output).write_text(json.dumps(session, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({"session": sid, "status": session.get("status"), "stalled": stalled,
                      "seconds": session["_wall_seconds"], "steps": session.get("stepCount"),
                      "llmCostUsd": session.get("llmCostUsd")}), flush=True)


def cmd_score(args) -> None:
    key = json.loads(Path(args.key).read_text())
    spec = json.loads(Path(args.spec).read_text()) if args.spec else None
    if spec is not None and args.spec_variant:
        spec["_variant"] = args.spec_variant
    scores = []
    for path in args.runs:
        result = score_run(json.loads(Path(path).read_text()), key, spec)
        result["file"] = Path(path).name
        scores.append(result)
        print(f"{result['file']}: {result['records']} records, accuracy "
              f"{result['accuracy']}%, proactive {result['proactive']}%, "
              f"{'faithful' if result['faithful'] else 'INVENTED VALUES'}")
        for issue in result["issues"]:
            value = json.dumps(issue["value"], ensure_ascii=False)
            print(f"    {issue['kind']:<16} {issue['field'] or '':<18} "
                  f"{(issue['role'] or '')[:40]:<40} {value[:90]}")
    report = {"key": Path(args.key).name, "scoredAt": date.today().isoformat(),
              "summary": summarise(scores), "runs": scores}
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.benchmark")
    commands = parser.add_subparsers(dest="command", required=True)

    variant = commands.add_parser("variant", help="write the spec with some fields unnamed")
    variant.add_argument("--unnamed", nargs="*", default=[])
    variant.add_argument("-o", "--output")
    variant.set_defaults(handler=cmd_variant)

    key = commands.add_parser("key", help="capture the live answer key from Ashby")
    key.add_argument("-o", "--output", required=True)
    key.add_argument("--only", nargs="*", help="keep only these Ashby job ids")
    key.set_defaults(handler=cmd_key)

    run = commands.add_parser("run", help="run the benchmark once against an OpenBrowse server")
    run.add_argument("--model", required=True)
    run.add_argument("--effort", required=True)
    run.add_argument("--variant", choices=sorted(VARIANTS), default="A")
    run.add_argument("--profile-id")
    run.add_argument("--base-url", default="http://127.0.0.1:8420")
    run.add_argument("--api-key")
    run.add_argument("-o", "--output", required=True)
    run.set_defaults(handler=cmd_run)

    score = commands.add_parser("score", help="score saved runs against an answer key")
    score.add_argument("runs", nargs="+")
    score.add_argument("--key", required=True)
    score.add_argument("--spec", help="the spec runs used, for runs saved without one")
    score.add_argument("--spec-variant", help="the label for runs scored against --spec")
    score.add_argument("-o", "--output")
    score.set_defaults(handler=cmd_score)

    args = parser.parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main()
