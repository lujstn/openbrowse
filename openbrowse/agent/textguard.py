"""Whitespace-normalised hashing for the output guard's repeat-suppression.

Kept free of browser-use and pydantic so it is unit-testable in any environment.
"""

import hashlib


def guard_key(text: str) -> str:
    """A hash that treats two chunks differing only in whitespace as identical, so the
    output guard can spot a large block it has already shown the model.
    """
    return hashlib.sha1(" ".join((text or "").split()).encode()).hexdigest()
