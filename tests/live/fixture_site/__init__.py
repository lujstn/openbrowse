"""Ground truth for the live fixture site.

Every value the agent is asked to extract lives here, and the pages in app.py are
rendered from these constants, so a scenario's expected output and the page it reads
can never drift apart. All content is fictional.
"""

STAFF = [
    {"name": "Marianne Frost", "role": "Principal Restorer", "dailyRateGbp": 420, "city": "London"},
    {"name": "Basil Finch", "role": "Glazier", "dailyRateGbp": 210, "city": "Norwich"},
    {"name": "Iris Callow", "role": "Horticulturist", "dailyRateGbp": 260, "city": "Leeds"},
    {"name": "Edmund Vane", "role": "Ironwork Specialist", "dailyRateGbp": 305, "city": "Sheffield"},
    {"name": "Prudence Ash", "role": "Archivist", "dailyRateGbp": 190, "city": "Oxford"},
    {"name": "Casper Reed", "role": "Surveyor", "dailyRateGbp": 275, "city": "Bristol"},
    {"name": "Odette Marsh", "role": "Conservator", "dailyRateGbp": 340, "city": "Cambridge"},
    {"name": "Hugh Bracken", "role": "Carpenter", "dailyRateGbp": 225, "city": "York"},
    {"name": "Sylvia Thorne", "role": "Botanical Illustrator", "dailyRateGbp": 180, "city": "Bath"},
    {"name": "Ambrose Quill", "role": "Heating Engineer", "dailyRateGbp": 290, "city": "Manchester"},
    {"name": "Verity Lockwood", "role": "Project Manager", "dailyRateGbp": 380, "city": "Edinburgh"},
    {"name": "Felix Harrow", "role": "Apprentice Glazier", "dailyRateGbp": 120, "city": "Durham"},
]

# Detail pages for these 1-based indices render their facts inside a cross-origin
# iframe served from the second fixture port, so the bulk-read path has to cope
# with embedded frames to score full marks.
IFRAMED_DETAILS = {11, 12}

ARTICLE = {
    "title": "The Quiet Art of Glasshouse Restoration",
    "author": "Marianne Frost",
    "secret": "FERN-0451",
}

SOCIAL_PLATFORMS = [
    "twitter.com", "x.com", "facebook.com", "linkedin.com", "instagram.com",
    "youtube.com", "github.com", "mastodon.social", "bsky.app", "threads.net",
]
SOCIAL_HANDLES = ["wardianfrost", "glasshouseguild", "fernhousefans", "palmstove"]
SOCIAL_LINKS = [
    f"https://{platform}/{handle}"
    for platform in SOCIAL_PLATFORMS
    for handle in SOCIAL_HANDLES
]

COLOPHON_SENTENCE = (
    "This catalogue was set in Caslon and printed on paper made from recycled seed envelopes."
)

DELAYED_CODE = "MOTH-7421"
DELAY_SECONDS = 8

NAV_CODE = "LANTERN-88"

NUMBERS = [(i * 37 + 11) % 97 for i in range(50)]
NUMBERS_SUM = sum(NUMBERS)

DATA_JSON = {"service": "wardian-frost-fixture", "launchCode": "ZX-2949", "records": 3}

RATES = [
    {"name": "Fern house survey", "rateGbp": 800},
    {"name": "Palm stove reglazing", "rateGbp": 4200},
    {"name": "Vinery gutter repair", "rateGbp": 650},
    {"name": "Orchid case rebuild", "rateGbp": 1500},
    {"name": "Boiler flue relining", "rateGbp": 980},
]
RATE_DISCOUNT = 0.10

CORRECTIONS_PAGE = {
    "rows": [
        {"name": "Basil Finch", "dailyRateGbp": 210},
        {"name": "Iris Callow", "dailyRateGbp": 260},
        {"name": "Hugh Bracken", "dailyRateGbp": 225},
    ],
    "corrected_name": "Basil Finch",
    "corrected_rate": 240,
}

TWO_ITEMS = [
    {"name": "Wardian case, mahogany", "priceGbp": 340},
    {"name": "Wardian case, teak", "priceGbp": 415},
]

EXPEDITION = {
    "title": "The Fernhouse Census",
    "curator": "Prudence Ash",
    "foundedYear": 1897,
    # members deliberately includes one duplicate row for the remove_items repair path
    "members": ["Iris Callow", "Casper Reed", "Sylvia Thorne", "Casper Reed"],
}

DROPDOWN_OPTIONS = ["Artichoke", "Aubergine", "Fennel", "Samphire"]
DROPDOWN_TARGET = "Aubergine"

FRAME_SENTENCE = "The palm stove's original 1862 boiler is still in working order."

SEARCH_PAGE_NEEDLE = "zeppelin"
SEARCH_PAGE_SENTENCE = (
    "Deliveries once arrived by zeppelin mooring at the old airfield beside the nursery."
)

FILES_TYPO_TEXT = "Prices quoted include delivery within Lodnon and the home counties."
FILES_FIXED_TEXT = "Prices quoted include delivery within London and the home counties."

UPLOAD_GREETING = "hello from openbrowse"
