"""A small on-disk cache for bounded Overpass responses.

Framework-free by design: the QGIS glue layer resolves a real profile path
at run time, while every test here supplies its own throwaway path, so the
cache is fully exercised without ever touching a live QGIS settings
directory (see docs/TRAPS.md 4.6 for why that distinction matters).

Honesty rules (what the download stores is decided by `cache_refusal`):
an empty answer, an answer the server marked with a `remark`, and data more
than seven days old are never kept, so a retry after the server recovers
asks again instead of replaying the bad answer for an hour.

Apart from answers it keeps one kind of note: a part too dense for the
servers even at the smallest size is marked for a day (`mark_too_dense`), so
a re-run skips it instead of spending the servers' time on it again.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, NamedTuple, Optional

DEFAULT_TTL_SECONDS = 60 * 60
#: Parts of a download that ended with gaps are kept this long, so running
#: the same download again ("Continue") fetches only the missing parts.
UNFINISHED_JOB_TTL_SECONDS = 24 * 60 * 60
DEFAULT_ENTRY_LIMIT = 300
#: Total payload size kept on disk.  The largest download the object cap
#: allows stores about 300 MB, so one such job always fits.
DEFAULT_BYTE_BUDGET = 512 * 1024 * 1024
#: Data older than this was served by a server that stopped updating.
STALE_AFTER_SECONDS = 7 * 24 * 60 * 60
#: A part found too dense even at the smallest size is skipped this long.
TOO_DENSE_TTL_SECONDS = 24 * 60 * 60
#: Marker keys start with text no Overpass request starts with, so a marker
#: can never be read back as a part's data.
_TOO_DENSE_PREFIX = "too dense even at the smallest size: "

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def too_dense_key(query: str) -> str:
    """The key a part's too-dense marker is stored under, apart from its data."""
    return _TOO_DENSE_PREFIX + str(query or "")


def _data_age_seconds(payload: Dict[str, Any], now: float) -> Optional[float]:
    meta = payload.get("osm3s")
    stamp = meta.get("timestamp_osm_base") if isinstance(meta, dict) else None
    if not isinstance(stamp, str):
        return None
    try:
        moment = calendar.timegm(time.strptime(stamp, _STAMP_FORMAT))
    except ValueError:
        return None
    return now - moment


def cache_refusal(payload: Any, now: Optional[float] = None) -> str:
    """Why a response must not be stored, or "" when it may be.

    `now` is seconds since the epoch (time.time()).
    """
    if not isinstance(payload, dict):
        return "it is not an OSM response"
    if str(payload.get("remark") or "").strip():
        return "the server marked it as incomplete"
    elements = payload.get("elements")
    if not isinstance(elements, list) or not elements:
        return "it is empty"
    age = _data_age_seconds(payload, time.time() if now is None else now)
    if age is not None and age > STALE_AFTER_SECONDS:
        return f"its data is {int(age // 86400)} days old"
    return ""


class CacheEntry(NamedTuple):
    payload: Dict[str, Any]
    #: The host that delivered the response, "" for entries from before 1.6.
    source: str


class QueryCache:
    """A bounded, TTL'd key/value store for validated Overpass JSON payloads.

    One instance is shared process-wide (see `osm_algorithms._cache()`), but
    each QGIS download runs on whichever worker thread the task manager
    happens to hand it — a different thread from the one that lazily created
    this instance, and often a different thread from the previous download
    too. sqlite3 connections refuse cross-thread use by default, so the
    connection is opened with `check_same_thread=False` and every access is
    serialized through one lock; a single small on-disk cache has no need for
    real concurrent reads.

    Every row carries its own time to live (`ttl`, seconds after
    `stored_at`); a row without one uses the instance default.  Expired rows
    are purged when the cache is opened, and the oldest-expiring rows are
    evicted past the row limit or the byte budget.
    """

    def __init__(
        self,
        db_path: Any,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        limit: int = DEFAULT_ENTRY_LIMIT,
        byte_budget: int = DEFAULT_BYTE_BUDGET,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(db_path)
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._limit = max(1, int(limit))
        self._byte_budget = max(1, int(byte_budget))
        self._clock = clock
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self._path), isolation_level=None, check_same_thread=False
        )
        with self._lock:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS query_cache ("
                "query_hash TEXT PRIMARY KEY, "
                "stored_at REAL NOT NULL, "
                "payload TEXT NOT NULL"
                ")"
            )
            # Columns added in 1.6; a 1.5 cache file gains them in place.
            present = {
                row[1]
                for row in self._connection.execute(
                    "PRAGMA table_info(query_cache)"
                )
            }
            for column, statement in (
                ("ttl", "ALTER TABLE query_cache ADD COLUMN ttl REAL"),
                ("size", "ALTER TABLE query_cache ADD COLUMN size INTEGER"),
                ("source", "ALTER TABLE query_cache ADD COLUMN source TEXT"),
            ):
                if column not in present:
                    self._connection.execute(statement)
            self.purge_expired()

    @staticmethod
    def _key(query: str) -> str:
        return hashlib.sha256(str(query or "").encode("utf-8")).hexdigest()

    def has(self, query: str) -> bool:
        """True when a live entry exists, without reading the payload."""
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM query_cache "
                "WHERE query_hash = ? AND stored_at + COALESCE(ttl, ?) >= ?",
                (self._key(query), self._ttl_seconds, self._clock()),
            ).fetchone()
            return row is not None

    def entry(self, query: str) -> Optional[CacheEntry]:
        """The stored payload and the host it came from, or None."""
        key = self._key(query)
        with self._lock:
            row = self._connection.execute(
                "SELECT stored_at, COALESCE(ttl, ?), payload, source "
                "FROM query_cache WHERE query_hash = ?",
                (self._ttl_seconds, key),
            ).fetchone()
            if row is None:
                return None
            stored_at, ttl, payload_text, source = row
            if self._clock() - stored_at > ttl:
                self._connection.execute(
                    "DELETE FROM query_cache WHERE query_hash = ?", (key,)
                )
                return None
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                payload = None
            if not isinstance(payload, dict):
                self._connection.execute(
                    "DELETE FROM query_cache WHERE query_hash = ?", (key,)
                )
                return None
            return CacheEntry(payload, str(source or ""))

    def get(self, query: str) -> Optional[Dict[str, Any]]:
        found = self.entry(query)
        return found.payload if found is not None else None

    def delete(self, query: str) -> None:
        """Remove one cached query, including a structurally invalid entry."""
        key = self._key(query)
        with self._lock:
            self._connection.execute(
                "DELETE FROM query_cache WHERE query_hash = ?", (key,)
            )

    def set(
        self,
        query: str,
        payload: Dict[str, Any],
        ttl_seconds: Optional[float] = None,
        source: str = "",
    ) -> None:
        """Store a payload as given; what deserves storing is the caller's call."""
        key = self._key(query)
        text = json.dumps(payload, ensure_ascii=False)
        ttl = float(ttl_seconds) if ttl_seconds is not None else None
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO query_cache "
                "(query_hash, stored_at, payload, ttl, size, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, self._clock(), text, ttl, len(text.encode("utf-8")), str(source or "")),
            )
            self._enforce_bounds(key)

    def mark_too_dense(self, query: str, source: str = "") -> None:
        """Remember for TOO_DENSE_TTL_SECONDS that a part is too dense.

        Stored under `too_dense_key(query)`, never under the part's own key,
        and without `cache_refusal`: the marker holds no OSM data at all.
        """
        self.set(
            too_dense_key(query), {"too_dense": True},
            ttl_seconds=TOO_DENSE_TTL_SECONDS, source=source,
        )

    def is_too_dense(self, query: str) -> bool:
        """True while a part carries a live too-dense marker."""
        return self.has(too_dense_key(query))

    def keep_until(self, queries: Iterable[str], ttl_seconds: float) -> int:
        """Keep existing entries at least `ttl_seconds` from now; return how many."""
        now = self._clock()
        kept = 0
        with self._lock:
            for query in dict.fromkeys(queries):
                cursor = self._connection.execute(
                    "UPDATE query_cache SET ttl = MAX(COALESCE(ttl, ?), ? - stored_at) "
                    "WHERE query_hash = ?",
                    (self._ttl_seconds, now + float(ttl_seconds), self._key(query)),
                )
                kept += cursor.rowcount
        return kept

    def _enforce_bounds(self, newest_key: str) -> None:
        rows = self._connection.execute(
            "SELECT query_hash, COALESCE(size, length(payload)) FROM query_cache "
            "ORDER BY stored_at + COALESCE(ttl, ?) ASC, stored_at ASC",
            (self._ttl_seconds,),
        ).fetchall()
        count = len(rows)
        total = sum(int(size or 0) for _key, size in rows)
        doomed = []
        for key, size in rows:
            if count <= self._limit and total <= self._byte_budget:
                break
            if key == newest_key:
                continue
            doomed.append(key)
            count -= 1
            total -= int(size or 0)
        for key in doomed:
            self._connection.execute(
                "DELETE FROM query_cache WHERE query_hash = ?", (key,)
            )

    def count(self) -> int:
        """Return the number of entries currently in the cache."""
        with self._lock:
            return int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM query_cache"
                ).fetchone()[0]
            )

    def clear(self) -> int:
        """Remove all cached entries, give the space back, return how many."""
        with self._lock:
            removed = self.count()
            self._connection.execute("DELETE FROM query_cache")
            # DELETE only marks pages free; without VACUUM the file keeps
            # its size and Clear cache appears to free nothing.
            self._connection.execute("VACUUM")
            return removed

    def purge_expired(self) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM query_cache WHERE stored_at + COALESCE(ttl, ?) < ?",
                (self._ttl_seconds, self._clock()),
            )
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._connection.close()
