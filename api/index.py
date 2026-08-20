"""Vercel serverless entrypoint for the FPL Intelligence Engine (src layout).

Why this bootstrap exists
-------------------------
This repository uses a *src layout*: the importable package lives at
``src/fpl_intelligence`` rather than ``fpl_intelligence`` at the repository
root. Vercel's Python runtime imports this file from the deployment root (e.g.
``/var/task/api/index.py``) with the **project root** on ``sys.path`` — the
``src`` directory is never added automatically. If ``fpl_intelligence`` is not
also present in the function's site-packages, the import fails at runtime with::

    ModuleNotFoundError: No module named 'fpl_intelligence'

Relying on the build step (``pip install .``) to place the package in
site-packages is not sufficient on its own: this bundle exceeds Vercel's
standard function size, so the platform runs a dependency-optimization pass
that can drop the locally built distribution. Shipping ``src/**`` in the bundle
(see ``includeFiles`` in ``vercel.json``) plus this explicit ``sys.path``
bootstrap makes the import deterministic.

The probing below is deliberately defensive so the module imports identically
from the repository root, from inside ``api/``, and from ``/var/task`` on
Vercel. Only directories that actually contain the package are added, so a
wrong guess can never shadow a real installation.
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS_FILE = Path(__file__).resolve()

#: Directories that may act as the deployment root, in priority order.
_CANDIDATE_ROOTS: tuple[Path, ...] = (
    _THIS_FILE.parent,  # .../api               (executed from api/)
    _THIS_FILE.parent.parent,  # repo root / /var/task (Vercel, local root)
    _THIS_FILE.parent.parent.parent,  # one level above the repo root
    Path.cwd(),  # Vercel sets cwd to the project base
    Path.cwd().parent,
    Path("/var/task"),  # Vercel serverless task root
)

#: Marker proving a directory really is an import root for the package.
_PACKAGE_MARKER = Path("fpl_intelligence") / "__init__.py"


def _import_roots() -> list[Path]:
    """Return every existing directory from which ``fpl_intelligence`` imports."""
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate_root in _CANDIDATE_ROOTS:
        try:
            resolved = candidate_root.resolve()
        except OSError:  # pragma: no cover - defensive on exotic filesystems
            continue
        # ``src`` first: the canonical location for this project's src layout.
        for candidate in (resolved / "src", resolved):
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if (candidate / _PACKAGE_MARKER).is_file():
                roots.append(candidate)
    return roots


def _bootstrap_sys_path() -> list[str]:
    """Prepend the package import roots to ``sys.path``; return what was added."""
    added: list[str] = []
    position = 0
    for root in _import_roots():
        entry = str(root)
        if entry in sys.path:
            continue
        sys.path.insert(position, entry)
        added.append(entry)
        position += 1
    return added


#: Recorded for diagnostics and for the deployment tests.
SYS_PATH_ADDITIONS: list[str] = _bootstrap_sys_path()

from fpl_intelligence.api.main import app  # noqa: E402  (import needs sys.path above)

# Vercel's Python runtime looks for a module-level ASGI callable named ``app``.
__all__ = ["SYS_PATH_ADDITIONS", "app"]
