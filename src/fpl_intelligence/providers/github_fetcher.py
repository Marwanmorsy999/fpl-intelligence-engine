"""Disk-caching HTTP fetcher for real historical data.

Fetches raw CSV/JSON files from public sources (e.g. the public
``vaastav/Fantasy-Premier-League`` mirror of FPL historical data), storing the
raw payloads under ``data/raw/<provider>/<season>/<dataset>/`` so imports are:

* reproducible: the retrieval timestamp, source URL and payload hash are
  captured;
* idempotent: re-fetching the same source does not duplicate raw files;
* audit-able: the raw evidence is always traceable back to its source.

This fetcher uses a single download-on-demand + local-cache strategy. Raw files
are NOT committed to Git by default (see ``data/raw/.gitignore``).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import httpx

logger = logging.getLogger(__name__)

DEFAULT_RAW_ROOT = Path(__file__).resolve().parents[3] / "data" / "raw"

# Polite low request rate to avoid hammering the source (no fragile scraping).
DEFAULT_USER_AGENT = (
    "fpl-intelligence-engine/0.1 (reproducible historical-data import; " "educational/research use)"
)
DEFAULT_DELAY_SECONDS = 0.35


class FetchError(RuntimeError):
    """Raised when a raw source cannot be retrieved."""


class DiskCachingFetcher:
    """Fetch public CSV/JSON sources, caching raw payloads on disk."""

    def __init__(
        self,
        raw_root: Path = DEFAULT_RAW_ROOT,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        timeout_seconds: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
        offline: bool = False,
    ) -> None:
        self.raw_root = raw_root
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        # ``offline`` allows replaying imports solely from cached raw files.
        self.offline = offline
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": user_agent, "Accept": "*/*"},
            follow_redirects=True,
        )

    # -- raw path helpers ---------------------------------------------------
    def raw_path(self, provider: str, season: str, dataset: str, filename: str) -> Path:
        return self.raw_root / provider / season / dataset / filename

    # -- fetching -----------------------------------------------------------
    def _fetch_text(self, url: str) -> str:
        if self.offline:
            raise FetchError(f"offline mode enabled; cannot fetch {url}")
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:  # noqa: BLE001
            raise FetchError(f"failed to fetch {url}: {exc}") from exc
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        return resp.text

    def ensure_cached(
        self, provider: str, season: str, dataset: str, filename: str, url: str
    ) -> Path:
        """Ensure the raw file exists on disk, fetching it on demand if needed.

        Returns the path to the cached raw file. Idempotent: if the file already
        exists it is not re-downloaded.
        """
        target = self.raw_path(provider, season, dataset, filename)
        if target.exists() and target.stat().st_size > 0:
            return target
        if dataset == "manifest":
            # manifest carries the retrieval metadata and is written below
            body = self._fetch_text(url)
        else:
            body = self._fetch_text(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return target

    def fetch_csv_rows(self, url: str) -> list[dict[str, Any]]:
        """Fetch a remote CSV and return its rows as dicts keyed by header."""
        text = self._fetch_text(url)
        reader = csv.DictReader(io.StringIO(text))
        return [row for row in reader]

    # -- raw manifest / provenance ------------------------------------------
    def write_manifest(
        self,
        provider: str,
        season: str,
        dataset: str,
        source_url: str,
        fields_provided: Sequence[str],
        license_notes: str,
    ) -> dict[str, Any]:
        """Write a small JSON manifest recording provenance for a raw dataset."""
        manifest = {
            "provider": provider,
            "season": season,
            "dataset": dataset,
            "source_url": source_url,
            "access_method": "public HTTP (raw.githubusercontent.com)",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "license_notes": license_notes,
            "fields_provided": list(fields_provided),
            "payload_hash": None,
        }
        target = self.raw_path(provider, season, dataset, "manifest.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest

    def sha256_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def close(self) -> None:
        self._client.close()
