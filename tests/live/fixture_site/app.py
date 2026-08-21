"""Deterministic fixture site the live suite points the agent at.

Two instances run per test session: the main site, and a second instance on another
port whose pages are embedded as genuinely cross-origin iframes. Every page is
rendered from the ground-truth constants in __init__.py.
"""

from __future__ import annotations

import html

from fastapi import FastAPI, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from tests.live.fixture_site import (
    ARTICLE,
    COLOPHON_SENTENCE,
    CORRECTIONS_PAGE,
    DATA_JSON,
    DELAY_SECONDS,
    DELAYED_CODE,
    DROPDOWN_OPTIONS,
    EXPEDITION,
    FILES_TYPO_TEXT,
    FRAME_SENTENCE,
    IFRAMED_DETAILS,
    NAV_CODE,
    NUMBERS,
    RATES,
    SEARCH_PAGE_SENTENCE,
    SOCIAL_LINKS,
    STAFF,
    TWO_ITEMS,
)

FILLER_PARAGRAPH = (
    "The glasshouse tradition rewards patience: putty cures slowly, cast iron rusts "
    "slowly, and ferns unfurl on no schedule but their own. Restoration is therefore "
    "less a trade than a long correspondence with the building. "
)


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><title>{html.escape(title)}</title></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )


def build_app(frame_base: str = "") -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/api/data.json")
    async def data_json() -> JSONResponse:
        return JSONResponse(DATA_JSON)

    @app.get("/article.html")
    async def article() -> HTMLResponse:
        body = (
            f"<p>By <span id='author'>{ARTICLE['author']}</span></p>"
            f"<p>{FILLER_PARAGRAPH}</p>"
            f"<script>window.__secret = '{ARTICLE['secret']}';</script>"
        )
        return _page(ARTICLE["title"], body)

    @app.get("/listing.html")
    async def listing() -> HTMLResponse:
        rows = "".join(
            f"<li><a href='/detail/{i}.html'>{html.escape(s['name'])}</a></li>"
            for i, s in enumerate(STAFF, start=1)
        )
        return _page("Wardian &amp; Frost — Staff", f"<ul>{rows}</ul>")

    @app.get("/listing_small.html")
    async def listing_small() -> HTMLResponse:
        rows = "".join(
            f"<li><a href='/detail/{i}.html'>{html.escape(s['name'])}</a></li>"
            for i, s in enumerate(STAFF[:4], start=1)
        )
        return _page("Wardian &amp; Frost — Senior Staff", f"<ul>{rows}</ul>")

    def _detail_facts(n: int) -> str:
        s = STAFF[n - 1]
        return (
            f"<p>Name: {html.escape(s['name'])}</p>"
            f"<p>Role: {html.escape(s['role'])}</p>"
            f"<p>Daily rate: £{s['dailyRateGbp']}</p>"
            f"<p>City: {html.escape(s['city'])}</p>"
        )

    @app.get("/detail/{n}.html")
    async def detail(n: int) -> HTMLResponse:
        if not 1 <= n <= len(STAFF):
            return HTMLResponse("not found", status_code=404)
        if n in IFRAMED_DETAILS and frame_base:
            body = (
                "<p>Profile below is embedded from our records system.</p>"
                f"<iframe src='{frame_base}/frame/detail/{n}.html' "
                "width='600' height='400'></iframe>"
            )
        else:
            body = _detail_facts(n)
        return _page(f"Staff profile {n}", body)

    @app.get("/frame/detail/{n}.html")
    async def frame_detail(n: int) -> HTMLResponse:
        if not 1 <= n <= len(STAFF):
            return HTMLResponse("not found", status_code=404)
        return _page(f"Records card {n}", _detail_facts(n))

    @app.get("/frame/content.html")
    async def frame_content() -> HTMLResponse:
        return _page("Records extract", f"<p>{FRAME_SENTENCE}</p>")

    @app.get("/iframe_host.html")
    async def iframe_host() -> HTMLResponse:
        body = (
            "<p>The extract below comes from the records system.</p>"
            f"<iframe src='{frame_base}/frame/content.html' width='600' height='300'></iframe>"
        )
        return _page("Embedded records", body)

    @app.get("/two_items.html")
    async def two_items() -> HTMLResponse:
        rows = "".join(
            f"<p>{html.escape(i['name'])} — £{i['priceGbp']}</p>" for i in TWO_ITEMS
        )
        return _page("Cases for sale", rows)

    @app.get("/corrections.html")
    async def corrections() -> HTMLResponse:
        rows = "".join(
            f"<tr><td>{html.escape(r['name'])}</td><td>£{r['dailyRateGbp']}</td></tr>"
            for r in CORRECTIONS_PAGE["rows"]
        )
        note = (
            f"Correction: {CORRECTIONS_PAGE['corrected_name']}'s daily rate is "
            f"£{CORRECTIONS_PAGE['corrected_rate']}, not "
            f"£{next(r['dailyRateGbp'] for r in CORRECTIONS_PAGE['rows'] if r['name'] == CORRECTIONS_PAGE['corrected_name'])}."
        )
        return _page(
            "Day rates",
            f"<table>{rows}</table><p><em>{html.escape(note)}</em></p>",
        )

    @app.get("/rates.html")
    async def rates() -> HTMLResponse:
        rows = "".join(
            f"<tr><td>{html.escape(r['name'])}</td><td>£{r['rateGbp']}</td></tr>"
            for r in RATES
        )
        return _page("Job rates", f"<table>{rows}</table>")

    @app.get("/social.html")
    async def social() -> HTMLResponse:
        links = "".join(
            f"<li><a href='{url}'>{html.escape(url.split('/')[2])} profile</a></li>"
            for url in SOCIAL_LINKS
        )
        return _page("Find us elsewhere", f"<ul>{links}</ul>")

    @app.get("/long.html")
    async def long_page() -> HTMLResponse:
        filler = "".join(f"<p>{FILLER_PARAGRAPH}</p>" for _ in range(60))
        mid = f"<p>{SEARCH_PAGE_SENTENCE}</p>"
        colophon = f"<h2>Colophon</h2><p>{COLOPHON_SENTENCE}</p>"
        return _page("A history of the nursery", filler + mid + filler + colophon)

    @app.get("/form.html")
    async def form() -> HTMLResponse:
        body = (
            "<form action='/form_result' method='get'>"
            "<label>Search the catalogue: <input type='text' name='q'></label>"
            "<button type='submit'>Search</button></form>"
        )
        return _page("Catalogue search", body)

    @app.get("/enter_form.html")
    async def enter_form() -> HTMLResponse:
        body = (
            "<form action='/form_result' method='get'>"
            "<label>Search the catalogue (press Enter to submit): "
            "<input type='text' name='q'></label></form>"
        )
        return _page("Catalogue quick search", body)

    @app.get("/form_result")
    async def form_result(q: str = "") -> HTMLResponse:
        return _page("Search result", f"<p id='result'>You searched for: {html.escape(q)}</p>")

    @app.get("/dropdown.html")
    async def dropdown() -> HTMLResponse:
        options = "".join(f"<option value='{o}'>{o}</option>" for o in DROPDOWN_OPTIONS)
        body = (
            "<label>Vegetable of the month: "
            f"<select id='veg' onchange=\"document.getElementById('chosen').textContent="
            "'You chose: ' + this.value\">"
            f"<option value=''>Pick one</option>{options}</select></label>"
            "<p id='chosen'>Nothing chosen yet.</p>"
        )
        return _page("Kitchen garden poll", body)

    @app.get("/tabs.html")
    async def tabs() -> HTMLResponse:
        body = (
            "<p>Our featured article opens in a new tab:</p>"
            "<a href='/article.html' target='_blank'>Read the featured article</a>"
        )
        return _page("Reading room", body)

    @app.get("/numbers.html")
    async def numbers() -> HTMLResponse:
        cells = "".join(f"<li>{n}</li>" for n in NUMBERS)
        return _page("Seed counts by tray", f"<ol>{cells}</ol>")

    @app.get("/delayed.html")
    async def delayed() -> HTMLResponse:
        body = (
            f"<p>The daily code appears roughly {DELAY_SECONDS} seconds after the page loads.</p>"
            "<p id='code'>Waiting for the code…</p>"
            f"<script>setTimeout(function() {{"
            f"document.getElementById('code').textContent = 'Daily code: {DELAYED_CODE}';"
            f"}}, {DELAY_SECONDS * 1000});</script>"
        )
        return _page("Daily code board", body)

    @app.get("/nav_a.html")
    async def nav_a() -> HTMLResponse:
        body = (
            "<p>This is the front desk. The store room holds today's code.</p>"
            "<a href='/nav_b.html'>Enter the store room</a>"
        )
        return _page("Front desk", body)

    @app.get("/nav_b.html")
    async def nav_b() -> HTMLResponse:
        return _page("Store room", f"<p>Store room code: {NAV_CODE}</p>")

    @app.get("/files.html")
    async def files_page() -> HTMLResponse:
        return _page("Delivery terms", f"<p id='terms'>{FILES_TYPO_TEXT}</p>")

    @app.get("/expedition.html")
    async def expedition() -> HTMLResponse:
        members = "".join(f"<li>{html.escape(m)}</li>" for m in EXPEDITION["members"])
        body = (
            f"<p>Curator: {EXPEDITION['curator']}</p>"
            f"<p>Founded: {EXPEDITION['foundedYear']}</p>"
            "<p>Note: the census office publishes no telephone number.</p>"
            f"<h2>Members</h2><ul>{members}</ul>"
        )
        return _page(EXPEDITION["title"], body)

    @app.get("/upload.html")
    async def upload_page() -> HTMLResponse:
        body = (
            "<form action='/upload' method='post' enctype='multipart/form-data'>"
            "<label>Choose a file: <input type='file' name='file'></label>"
            "<button type='submit'>Upload</button></form>"
        )
        return _page("Document drop", body)

    @app.post("/upload")
    async def upload(file: UploadFile) -> HTMLResponse:
        content = (await file.read()).decode(errors="replace")
        return _page(
            "Upload received",
            f"<p id='echo'>Received {html.escape(file.filename or '')}: "
            f"{html.escape(content.strip())}</p>",
        )

    return app
