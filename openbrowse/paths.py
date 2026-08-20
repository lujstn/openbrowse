"""Where this copy of OpenBrowse lives on disk.

Deliberately free of imports from the rest of the package: ``openbrowse``
itself reads its version through this module, before configuration exists.
"""

from __future__ import annotations

from pathlib import Path


def checkout_root() -> Path | None:
    """The source checkout this package runs from, or None for an installed copy.

    Answers "where is the code", never "where is the data"; ``OPENBROWSE_HOME``
    moves the latter and must not be able to move the former.
    """
    root = Path(__file__).resolve().parent.parent
    return root if (root / "pyproject.toml").exists() else None
