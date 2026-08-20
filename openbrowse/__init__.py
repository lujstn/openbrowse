"""OpenBrowse: self-hosted AI browser automation with a live dashboard."""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path


def _read_version() -> str:
    # @nonobvious(must-hold): a git checkout's pyproject.toml outranks installed
    # metadata, which goes stale between a version bump and the next `uv sync`.
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        match = re.search(r'^version = "([^"]+)"$', pyproject.read_text(), re.MULTILINE)
        if match:
            return match.group(1)
    except OSError:
        pass
    try:
        return metadata.version("openbrowse")
    except metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = _read_version()
