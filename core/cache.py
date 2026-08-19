"""A small on-disk cache for bounded Overpass responses.

Framework-free by design: the QGIS glue layer resolves a real profile path
at run time, while every test here supplies its own throwaway path, so the
cache is fully exercised without ever touching a live QGIS settings
directory (see docs/TRAPS.md 4.6 for why that distinction matters).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_TTL_SECONDS = 60 * 60
DEFAULT_ENTRY_LIMIT = 300


class QueryCache:
    """A bounded, TTL'd key/value store for validated Overpass JSON payloads."""

    def __init__(
        self,
        db_path: Any,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        limit: int = DEFAULT_ENTRY_LIMIT,
    ) -> None:
        self._path = Path(db_path)
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._limit = max(1, int(limit))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self._path), isolation_level=None)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS query_cache ("
            "query_hash TEXT PRIMARY KEY, "
            "stored_at REAL NOT NULL, "
            "payload TEXT NOT NULL"
            ")"
        )

    @staticmethod
    def _key(query: str) -> str:
        return hashlib.sha256(str(query or "").encode("utf-8")).hexdigest()

    def get(self, query: str) -> Optional[Dict[str, Any]]:
        row = self._connection.execute(
            "SELECT stored_at, payload FROM query_cache WHERE query_hash = ?",
            (self._key(query),),
        ).fetchone()
        if row is None:
            return None
        stored_at, payload_text = row
        if time.time() - stored_at > self._ttl_seconds:
            self._connection.execute(
                "DELETE FROM query_cache WHERE query_hash = ?", (self._key(query),)
            )
            return None
        try:
            return json.loads(payload_text)
        except json.JSONDecodeError:
            self._connection.execute(
                "DELETE FROM query_cache WHERE query_hash = ?", (self._key(query),)
            )
            return None

    def set(self, query: str, payload: Dict[str, Any]) -> None:
        key = self._key(query)
        self._connection.execute(
            "INSERT OR REPLACE INTO query_cache (query_hash, stored_at, payload) "
            "VALUES (?, ?, ?)",
            (key, time.time(), json.dumps(payload, ensure_ascii=False)),
        )
        count = self._connection.execute(
            "SELECT COUNT(*) FROM query_cache"
        ).fetchone()[0]
        if count > self._limit:
            self._connection.execute(
                "DELETE FROM query_cache WHERE query_hash IN ("
                "SELECT query_hash FROM query_cache "
                "ORDER BY stored_at ASC LIMIT ?"
                ")",
                (count - self._limit,),
            )

    def purge_expired(self) -> None:
        cutoff = time.time() - self._ttl_seconds
        self._connection.execute(
            "DELETE FROM query_cache WHERE stored_at < ?", (cutoff,)
        )

    def close(self) -> None:
        self._connection.close()
