"""OpenBrowse: self-hosted AI browser automation with a live dashboard."""

from __future__ import annotations

import re
from importlib import metadata

from openbrowse.paths import checkout_root


def _read_version() -> str:
    # @nonobvious(must-hold): a git checkout's pyproject.toml outranks installed
    # metadata, which goes stale between a version bump and the next `uv sync`.
    checkout = checkout_root()
    if checkout is not None:
        try:
            text = (checkout / "pyproject.toml").read_text()
            match = re.search(r'^version = "([^"]+)"$', text, re.MULTILINE)
            if match:
                return match.group(1)
        except OSError:
            pass
    try:
        return metadata.version("openbrowse")
    except metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = _read_version()
