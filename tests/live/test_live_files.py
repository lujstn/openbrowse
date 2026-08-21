"""Agent filesystem tools, file upload, and page capture."""

import pytest

from tests.live.fixture_site import FILES_FIXED_TEXT, UPLOAD_GREETING
from tests.live.harness import (
    assert_no_doom_loop,
    assert_success,
    assert_used,
)

pytestmark = pytest.mark.live


def _text(trace) -> str:
    return str(trace.output or "")


def test_files_family(run_scenario, fixture_url):
    trace = run_scenario(
        "files_family",
        f"Go to {fixture_url}/files.html. Step 1: save the delivery terms sentence "
        "shown on the page to a file terms.txt with write_file. Step 2: the "
        "sentence contains the typo 'Lodnon' — correct it to 'London' in the file "
        "with replace_file. Step 3: read the file back with read_file and report "
        "its corrected content exactly.",
    )
    assert_success(trace)
    assert_used(trace, "write_file")
    assert_used(trace, "replace_file")
    assert_used(trace, "read_file")
    assert_no_doom_loop(trace)
    assert FILES_FIXED_TEXT in _text(trace), trace.describe()


def test_upload(run_scenario, fixture_url):
    trace = run_scenario(
        "upload",
        "Step 1: create a file greeting.txt containing exactly "
        f"'{UPLOAD_GREETING}' using write_file. Step 2: go to "
        f"{fixture_url}/upload.html, attach greeting.txt to the file input using "
        "the upload_file tool, and click Upload. Step 3: report the confirmation "
        "line the server shows, exactly.",
    )
    assert_success(trace)
    assert_used(trace, "write_file")
    assert_used(trace, "upload_file")
    assert_no_doom_loop(trace)
    assert f"Received greeting.txt: {UPLOAD_GREETING}" in _text(trace), trace.describe()


def test_shots(run_scenario, fixture_url):
    trace = run_scenario(
        "shots",
        f"Go to {fixture_url}/article.html. Take a screenshot saved to a named file "
        "shot.png using the screenshot tool, and also save the page as a PDF with "
        "save_as_pdf. Report both file paths.",
    )
    assert_success(trace)
    assert_used(trace, "screenshot")
    assert_used(trace, "save_as_pdf")
    assert_no_doom_loop(trace)
