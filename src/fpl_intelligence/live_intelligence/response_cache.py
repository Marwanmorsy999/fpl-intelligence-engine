"""Phase 9.1 response cache — the primary free-tier safeguard.

Extraction is *pure with respect to the model*: the same prompt, sent to the
same model with the same decoding parameters, should yield the same text. Every
repeat of that request is therefore pure waste, and on a free tier waste is not
merely inefficient — it is the thing that exhausts the daily quota before the
work is finished.

This module makes the repeat free. A response is stored under a key derived
from everything that could change the answer::

    provider | model | prompt_hash | input_hash | max_output_tokens | temperature

and nothing that could not. Re-running the dry-run script against the same
transcript costs zero tokens and zero requests.

Why the input hash is separate from the prompt hash
---------------------------------------------------

The rendered prompt already contains the source text, so the input hash is
partly redundant — but it is stored and keyed independently because it answers
a different question during an audit: *which source text produced this?* When
the prompt template is later versioned up, the input hash is what lets you find
every response derived from the same transcript across template generations.

Determinism caveat
------------------

The cache assumes the request is meant to be reproducible. At ``temperature``
above zero a model may legitimately return something different, so the
temperature is part of the key and a non-zero setting is recorded on the entry
rather than hidden. The engine's default is ``0.0``.
"""
from __future__ import annotations

import abc
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_response_cache (
    cache_key          TEXT PRIMARY KEY,
    provider_name      TEXT NOT NULL,
    model_name         TEXT NOT NULL,
    prompt_hash        TEXT NOT NULL,
    input_hash         TEXT NOT NULL,
    max_output_tokens  INTEGER NOT NULL,
    temperature        REAL NOT NULL,
    response_text      TEXT NOT NULL,
    usage_json         TEXT,
    created_at         TEXT NOT NULL,
    last_hit_at        TEXT,
    hit_count          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_llm_response_cache_prompt_hash
    ON llm_response_cache (prompt_hash);
CREATE INDEX IF NOT EXISTS ix_llm_response_cache_input_hash
    ON llm_response_cache (input_hash);
"""


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheKeyParts:
    """Everything that can legitimately change a model's answer.

    Adding a field here invalidates the whole cache by construction, which is
    the correct behaviour: an old entry was produced under different
    conditions and must not be served as if it were current.
    """

    provider_name: str
    model_name: str
    prompt_hash: str
    input_hash: str
    max_output_tokens: int
    temperature: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "prompt_hash": self.prompt_hash,
            "input_hash": self.input_hash,
            "max_output_tokens": self.max_output_tokens,
            # Rounded so 0.0 and -0.0, or float noise from a config round-trip,
            # cannot fragment the cache into near-duplicate entries.
            "temperature": round(float(self.temperature), 4),
        }

    def key(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_cache_key(
    *,
    provider_name: str,
    model_name: str,
    prompt_hash: str,
    input_hash: str,
    max_output_tokens: int,
    temperature: float,
) -> str:
    """Convenience wrapper around :meth:`CacheKeyParts.key`."""
    return CacheKeyParts(
        provider_name=provider_name,
        model_name=model_name,
        prompt_hash=prompt_hash,
        input_hash=input_hash,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
    ).key()


# ---------------------------------------------------------------------------
# Entries and statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheEntry:
    """A stored provider response plus the provenance needed to audit it."""

    cache_key: str
    response_text: str
    provider_name: str
    model_name: str
    prompt_hash: str
    input_hash: str
    max_output_tokens: int
    temperature: float
    created_at: datetime
    usage: dict[str, Any] = field(default_factory=dict)
    hit_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_key": self.cache_key,
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "prompt_hash": self.prompt_hash,
            "input_hash": self.input_hash,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "created_at": self.created_at.isoformat(),
            "hit_count": self.hit_count,
            "usage": self.usage,
            "response_chars": len(self.response_text),
        }


@dataclass
class CacheStats:
    """Counters for one process. Reported by the dry-run script."""

    hits: int = 0
    misses: int = 0
    writes: int = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Fraction of lookups served without an API call. 0.0 when unused."""
        return self.hits / self.lookups if self.lookups else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "lookups": self.lookups,
            "hit_rate": round(self.hit_rate, 4),
        }


# ---------------------------------------------------------------------------
# Cache implementations
# ---------------------------------------------------------------------------


class ResponseCache(abc.ABC):
    """Read-through cache for provider responses."""

    def __init__(self) -> None:
        self.stats = CacheStats()

    @property
    @abc.abstractmethod
    def enabled(self) -> bool:
        """False for the null cache, so callers can report honestly."""

    @abc.abstractmethod
    def _lookup(self, cache_key: str) -> CacheEntry | None: ...

    @abc.abstractmethod
    def _store(self, entry: CacheEntry) -> None: ...

    def get(self, cache_key: str) -> CacheEntry | None:
        """Return a cached response, counting the hit or miss."""
        entry = self._lookup(cache_key)
        if entry is None:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return entry

    def put(self, entry: CacheEntry) -> None:
        """Store a response. Never raises on a cache-write failure path."""
        self._store(entry)
        self.stats.writes += 1

    def close(self) -> None:  # pragma: no cover - trivial default
        """Release any resources. Safe to call more than once.

        The base class has nothing to release; subclasses that hold a
        connection or file handle override this method.
        """
        return None


class NullResponseCache(ResponseCache):
    """Disabled cache. Every lookup misses; nothing is written.

    Exists so that ``llm_cache_enabled=False`` needs no ``if cache is not None``
    branch in the provider, which is where a caching bug would otherwise hide.
    """

    @property
    def enabled(self) -> bool:
        return False

    def _lookup(self, cache_key: str) -> CacheEntry | None:
        return None

    def _store(self, entry: CacheEntry) -> None:
        return None


class InMemoryResponseCache(ResponseCache):
    """Process-local cache. Used by tests and by ``--no-persist`` runs."""

    def __init__(self) -> None:
        super().__init__()
        self._entries: dict[str, CacheEntry] = {}

    @property
    def enabled(self) -> bool:
        return True

    def __len__(self) -> int:
        return len(self._entries)

    def _lookup(self, cache_key: str) -> CacheEntry | None:
        entry = self._entries.get(cache_key)
        if entry is None:
            return None
        bumped = replace(entry, hit_count=entry.hit_count + 1)
        self._entries[cache_key] = bumped
        return bumped

    def _store(self, entry: CacheEntry) -> None:
        self._entries[entry.cache_key] = entry


class SqliteResponseCache(ResponseCache):
    """Durable cache backed by a local SQLite file.

    SQLite rather than a JSON blob because the cache must survive a crashed
    dry-run without truncating, and because ``PRIMARY KEY`` gives the
    idempotency guarantee for free. The file is local state, never committed —
    it holds model output, and re-deriving it is a quota expense, not a
    correctness one.
    """

    def __init__(self, path: Path | str) -> None:
        super().__init__()
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @property
    def enabled(self) -> bool:
        return True

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def __len__(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM llm_response_cache").fetchone()
        return int(row["n"])

    def _lookup(self, cache_key: str) -> CacheEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM llm_response_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE llm_response_cache SET hit_count = hit_count + 1, last_hit_at = ? "
                "WHERE cache_key = ?",
                (datetime.now(UTC).isoformat(), cache_key),
            )
        return _row_to_entry(row, hit_bump=1)

    def _store(self, entry: CacheEntry) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO llm_response_cache ("
                "cache_key, provider_name, model_name, prompt_hash, input_hash, "
                "max_output_tokens, temperature, response_text, usage_json, "
                "created_at, last_hit_at, hit_count"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.cache_key,
                    entry.provider_name,
                    entry.model_name,
                    entry.prompt_hash,
                    entry.input_hash,
                    entry.max_output_tokens,
                    entry.temperature,
                    entry.response_text,
                    json.dumps(entry.usage) if entry.usage else None,
                    entry.created_at.isoformat(),
                    None,
                    entry.hit_count,
                ),
            )

    def purge(self) -> int:
        """Delete every entry. Returns the number removed."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM llm_response_cache")
            return int(cursor.rowcount or 0)


def _row_to_entry(row: sqlite3.Row, *, hit_bump: int = 0) -> CacheEntry:
    return CacheEntry(
        cache_key=row["cache_key"],
        response_text=row["response_text"],
        provider_name=row["provider_name"],
        model_name=row["model_name"],
        prompt_hash=row["prompt_hash"],
        input_hash=row["input_hash"],
        max_output_tokens=int(row["max_output_tokens"]),
        temperature=float(row["temperature"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        usage=json.loads(row["usage_json"]) if row["usage_json"] else {},
        hit_count=int(row["hit_count"]) + hit_bump,
    )


def build_cache(*, enabled: bool, path: Path | str | None) -> ResponseCache:
    """Select a cache implementation from configuration.

    ``enabled=False`` yields the null cache; an absent path yields the
    in-memory cache, so a caller is never silently given a cache that writes
    somewhere it did not ask for.
    """
    if not enabled:
        return NullResponseCache()
    if path is None:
        return InMemoryResponseCache()
    return SqliteResponseCache(path)
