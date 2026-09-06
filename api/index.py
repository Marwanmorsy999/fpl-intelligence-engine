"""Vercel serverless entrypoint for the FPL Intelligence Engine (src layout).

Vercel internally rewrites public URLs to ``/api/index.py`` for Python
functions. Newer Vercel routing behavior exposes that rewritten destination
path to the ASGI application, which breaks FastAPI routes such as ``/dashboard``
and ``/health``. The project routing therefore carries the original public path
in the ``__route`` query parameter; this entrypoint restores it before passing
the request to FastAPI.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode
import sys
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

_THIS_FILE = Path(__file__).resolve()

#: Directories that may act as the deployment root, in priority order.
_CANDIDATE_ROOTS: tuple[Path, ...] = (
    _THIS_FILE.parent,
    _THIS_FILE.parent.parent,
    _THIS_FILE.parent.parent.parent,
    Path.cwd(),
    Path.cwd().parent,
    Path("/var/task"),
)

_PACKAGE_MARKER = Path("fpl_intelligence") / "__init__.py"


def _import_roots() -> list[Path]:
    """Return every existing directory from which ``fpl_intelligence`` imports."""
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate_root in _CANDIDATE_ROOTS:
        try:
            resolved = candidate_root.resolve()
        except OSError:
            continue
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


SYS_PATH_ADDITIONS: list[str] = _bootstrap_sys_path()

from fpl_intelligence.api.main import app as _fastapi_app  # noqa: E402


class _RestoreOriginalPath:
    """Restore the public request path carried by the Vercel rewrite."""

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            raw_query = scope.get("query_string", b"").decode("latin-1")
            pairs = parse_qsl(raw_query, keep_blank_values=True)
            original_path: str | None = None
            filtered: list[tuple[str, str]] = []
            for key, value in pairs:
                if key == "__route":
                    if original_path is None and value.startswith("/"):
                        original_path = value
                else:
                    filtered.append((key, value))

            if original_path:
                scope = dict(scope)
                scope["path"] = original_path
                scope["raw_path"] = original_path.encode("utf-8")
                scope["query_string"] = urlencode(filtered, doseq=True).encode("ascii")

        await self.inner(scope, receive, send)


# Vercel's Python runtime invokes a module-level ASGI callable named ``app``.
app: ASGIApp = _RestoreOriginalPath(_fastapi_app)

__all__ = ["SYS_PATH_ADDITIONS", "app"]
