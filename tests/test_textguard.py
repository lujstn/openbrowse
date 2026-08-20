"""Tests for the output guard's repeat-suppression hash (app/agent/textguard.py).

Pure — stdlib only, no browser-use or pydantic — so it runs in any environment.
"""

from openbrowse.agent.textguard import guard_key


def test_same_text_same_key():
    assert guard_key("hello world") == guard_key("hello world")


def test_whitespace_insensitive():
    assert guard_key("hello   world") == guard_key("hello world")
    assert guard_key("hello world") == guard_key("  hello\n\tworld  ")
    assert guard_key("[\n  1,\n  2\n]") == guard_key("[ 1, 2 ]")


def test_different_text_different_key():
    assert guard_key("hello world") != guard_key("hello worlds")
    assert guard_key("a b c") != guard_key("a b")


def test_empty_and_none_safe():
    assert guard_key("") == guard_key("   ")
    assert guard_key(None) == guard_key("")
