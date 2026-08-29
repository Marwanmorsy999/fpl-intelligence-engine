from __future__ import annotations

import json
import lzma
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fpl_intelligence.availability.historical.materialize_pit import DeadlineCutoff, download_snapshot, latest_remote_before, local_snapshot_path


def test_deadline_cutoff_requires_timezone() -> None:
    with pytest.raises(ValueError):
        DeadlineCutoff("2024-25", 1, datetime(2024, 8, 16, 16))


def test_local_snapshot_path_is_canonical(tmp_path: Path) -> None:
    path = local_snapshot_path(tmp_path, datetime(2025, 8, 15, 18, 0, tzinfo=UTC))
    assert path == tmp_path / "2025" / "8" / "15" / "1800.json.xz"


def test_download_snapshot_validates_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"elements": [{"id": 1}]}).encode()
    compressed = lzma.compress(payload)

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def read(self):
            return compressed

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    dest = tmp_path / "nested" / "snapshot.json.xz"
    download_snapshot("https://example.invalid/snapshot", dest)
    assert dest.exists()
    assert lzma.decompress(dest.read_bytes()) == payload
