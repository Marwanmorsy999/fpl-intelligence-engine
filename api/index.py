"""Vercel serverless entrypoint for the FPL Intelligence Engine (src layout)."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode
import sys
from pathlib import Path

from starlette.types import Receive, Scope, Send

_THIS_FILE = Path(__file__).resolve()
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


class RestoreOriginalPathMiddleware:
    """Restore the public path encoded by the Vercel rewrite query parameter."""

    def __init__(self, app):
        self.app = app

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

        await self.app(scope, receive, send)


# Keep the exact FastAPI app object exported by main.py so existing runtime and
# regression contracts remain intact. Add path recovery as a Starlette middleware
# to that same object instead of wrapping it in a second ASGI object.
_fastapi_app.add_middleware(RestoreOriginalPathMiddleware)
app = _fastapi_app

__all__ = ["SYS_PATH_ADDITIONS", "RestoreOriginalPathMiddleware", "app"]
