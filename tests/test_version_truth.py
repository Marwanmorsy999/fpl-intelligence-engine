"""Version truth (v2.7.4-prod-heal).

The /health endpoint once served 2.5.5 while the repo was on v2.7.x tags —
the version string must never be allowed to drift behind (or diverge from)
the release history again.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from fpl_intelligence import __version__

_REPO_ROOT = Path(__file__).resolve().parents[1]

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _latest_git_tag() -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        tag = out.stdout.strip()
        return tag or None
    except Exception:  # noqa: BLE001 — git missing / not a repo → skip
        return None


def _semver(value: str) -> tuple[int, int, int]:
    match = _SEMVER.match(value.strip())
    assert match is not None, f"unparseable version/tag: {value!r}"
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def test_version_is_plain_semver() -> None:
    """__version__ must start MAJOR.MINOR.PATCH so /health is comparable."""
    assert re.match(r"^\d+\.\d+\.\d+", __version__), (
        f"__version__ {__version__!r} must start with MAJOR.MINOR.PATCH"
    )


def test_version_never_older_than_latest_tag() -> None:
    """The shipped version can never regress below the newest git tag.

    This is the guard that makes 'health says 2.5.5' impossible to ship
    again: if __version__ parses older than the latest v-prefixed tag, the
    suite fails before any deploy can lie about what's running.
    """
    tag = _latest_git_tag()
    if not tag:
        pytest.skip("git tags unavailable in this environment")
    match = _SEMVER.match(tag)
    if match is None:
        pytest.skip(f"latest tag {tag!r} carries no semver prefix")
    assert _semver(__version__) >= _semver(tag), (
        f"__version__ {__version__!r} is OLDER than latest tag {tag!r} — "
        "bump __version__ (or cut a new tag) so /health tells the truth"
    )
