from __future__ import annotations

import calendar
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from zero2agent_osm_downloader.core.cache import (
    DEFAULT_TTL_SECONDS,
    STALE_AFTER_SECONDS,
    TOO_DENSE_TTL_SECONDS,
    UNFINISHED_JOB_TTL_SECONDS,
    QueryCache,
    cache_refusal,
    too_dense_key,
)

# Shapes of real answers (overpass-api.de, 2026-11-02): data date and layout.
FRESH_STAMP = "2026-11-02T06:58:39Z"
FRESH_AT = calendar.timegm(time.strptime(FRESH_STAMP, "%Y-%m-%dT%H:%M:%SZ"))


def _answer(elements, stamp=FRESH_STAMP, **extra):
    payload = {
        "version": 0.6,
        "generator": "Overpass API 0.7.62.11 87bfad18",
        "osm3s": {"timestamp_osm_base": stamp},
        "elements": elements,
    }
    payload.update(extra)
    return payload


BUILDING = {
    "type": "way", "id": 867516394,
    "geometry": [{"lat": 38.4206916, "lon": 27.1318471}],
    "tags": {"building": "retail", "building:levels": "2"},
}


class FakeClock:
    def __init__(self, start: float = FRESH_AT) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now


class QueryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "cache.sqlite3"

    def _cache(self, **kwargs) -> QueryCache:
        cache = QueryCache(self.db_path, **kwargs)
        self.addCleanup(cache.close)
        return cache

    def test_set_then_get_returns_the_stored_payload(self) -> None:
        cache = self._cache(ttl_seconds=60)
        payload = {"elements": [{"type": "node", "id": 1}]}
        cache.set("query-a", payload)
        self.assertEqual(cache.get("query-a"), payload)

    def test_miss_returns_none(self) -> None:
        cache = self._cache(ttl_seconds=60)
        self.assertIsNone(cache.get("missing"))

    def test_non_object_json_is_evicted_as_corrupt(self) -> None:
        cache = self._cache(ttl_seconds=60)
        key = QueryCache._key("bad-shape")
        cache._connection.execute(
            "INSERT INTO query_cache (query_hash, stored_at, payload) VALUES (?, ?, ?)",
            (key, time.time(), json.dumps(["not", "an", "object"])),
        )
        self.assertIsNone(cache.get("bad-shape"))
        row = cache._connection.execute(
            "SELECT COUNT(*) FROM query_cache WHERE query_hash = ?", (key,)
        ).fetchone()
        self.assertEqual(row[0], 0)

    def test_delete_removes_only_the_selected_query(self) -> None:
        cache = self._cache(ttl_seconds=60)
        cache.set("keep", {"elements": []})
        cache.set("drop", {"elements": []})
        cache.delete("drop")
        self.assertEqual(cache.get("keep"), {"elements": []})
        self.assertIsNone(cache.get("drop"))

    def test_entry_survives_a_new_connection_to_the_same_file(self) -> None:
        self._cache(ttl_seconds=60).set("query-a", {"elements": []})
        reopened = self._cache(ttl_seconds=60)
        self.assertEqual(reopened.get("query-a"), {"elements": []})

    def test_expired_entries_are_dropped(self) -> None:
        cache = self._cache(ttl_seconds=60)
        cache.set("query-a", {"elements": []})
        cache._connection.execute(
            "UPDATE query_cache SET stored_at = ? WHERE query_hash = ?",
            (time.time() - 3600, QueryCache._key("query-a")),
        )
        self.assertIsNone(cache.get("query-a"))

    def test_limit_evicts_the_oldest_entry_first(self) -> None:
        cache = self._cache(ttl_seconds=3600, limit=2)
        cache.set("query-a", {"elements": [1]})
        cache.set("query-b", {"elements": [2]})
        cache.set("query-c", {"elements": [3]})
        self.assertIsNone(cache.get("query-a"))
        self.assertEqual(cache.get("query-b"), {"elements": [2]})
        self.assertEqual(cache.get("query-c"), {"elements": [3]})

    def test_purge_expired_removes_only_stale_rows(self) -> None:
        cache = self._cache(ttl_seconds=60)
        cache.set("fresh", {"elements": []})
        cache.set("stale", {"elements": []})
        cache._connection.execute(
            "UPDATE query_cache SET stored_at = ? WHERE query_hash = ?",
            (time.time() - 3600, QueryCache._key("stale")),
        )
        cache.purge_expired()
        self.assertEqual(cache.get("fresh"), {"elements": []})
        row = cache._connection.execute(
            "SELECT COUNT(*) FROM query_cache"
        ).fetchone()
        self.assertEqual(row[0], 1)

    def test_different_queries_hash_to_different_keys(self) -> None:
        cache = self._cache(ttl_seconds=60)
        cache.set("query-a", {"elements": [1]})
        cache.set("query-b", {"elements": [2]})
        self.assertEqual(cache.get("query-a"), {"elements": [1]})
        self.assertEqual(cache.get("query-b"), {"elements": [2]})

    def test_get_and_set_work_from_a_different_thread_than_the_creator(
        self,
    ) -> None:
        """Regression: QGIS runs each download on a different worker thread.

        A `QueryCache` created lazily on one thread and then reused from a
        later download's own thread must not hit sqlite3's default
        same-thread restriction.
        """
        cache = self._cache(ttl_seconds=60)
        errors: list[BaseException] = []
        results: dict = {}

        def worker() -> None:
            try:
                cache.set("from-worker", {"elements": [7]})
                results["value"] = cache.get("from-worker")
            except BaseException as exc:  # noqa: BLE001 - captured for assert
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results.get("value"), {"elements": [7]})
        # Readable back from the original (creator) thread too.
        self.assertEqual(cache.get("from-worker"), {"elements": [7]})

    def test_count_and_clear_remove_all_cached_entries(self) -> None:
        cache = self._cache(ttl_seconds=60)
        self.assertEqual(cache.count(), 0)
        cache.set("query-1", {"elements": [1]})
        cache.set("query-2", {"elements": [2]})
        self.assertEqual(cache.count(), 2)
        removed = cache.clear()
        self.assertEqual(removed, 2)
        self.assertEqual(cache.count(), 0)
        self.assertIsNone(cache.get("query-1"))


class CacheHonestyTests(unittest.TestCase):
    """R7: per-row TTL, purge on first use, byte budget, VACUUM, has()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "cache.sqlite3"
        self.clock = FakeClock()

    def _cache(self, **kwargs) -> QueryCache:
        kwargs.setdefault("clock", self.clock)
        cache = QueryCache(self.db_path, **kwargs)
        self.addCleanup(cache.close)
        return cache

    def test_each_row_keeps_its_own_time_to_live(self) -> None:
        cache = self._cache()
        cache.set("default", _answer([BUILDING]))
        cache.set("short", _answer([BUILDING]), ttl_seconds=10)
        self.clock.now += 11
        self.assertIsNone(cache.get("short"))
        self.assertIsNotNone(cache.get("default"))
        self.clock.now += DEFAULT_TTL_SECONDS
        self.assertIsNone(cache.get("default"))

    def test_parts_of_an_unfinished_download_are_kept_for_a_day(self) -> None:
        cache = self._cache()
        cache.set("part-1", _answer([BUILDING]))
        cache.set("part-2", _answer([BUILDING]))
        self.clock.now += 100
        self.assertEqual(
            cache.keep_until(["part-1", "part-1", "missing"], UNFINISHED_JOB_TTL_SECONDS), 1
        )
        self.clock.now += UNFINISHED_JOB_TTL_SECONDS - 200
        self.assertIsNone(cache.get("part-2"))
        self.assertEqual(cache.get("part-1")["elements"], [BUILDING])
        self.clock.now += 300
        self.assertIsNone(cache.get("part-1"))

    def test_expired_rows_are_purged_when_the_cache_opens(self) -> None:
        first = QueryCache(self.db_path, clock=self.clock)
        first.set("old", _answer([BUILDING]))
        first.set("kept", _answer([BUILDING]), ttl_seconds=UNFINISHED_JOB_TTL_SECONDS)
        first.close()
        self.clock.now += DEFAULT_TTL_SECONDS + 1
        reopened = self._cache()
        rows = reopened._connection.execute("SELECT COUNT(*) FROM query_cache").fetchone()
        self.assertEqual(rows[0], 1)
        self.assertIsNotNone(reopened.get("kept"))

    def test_the_byte_budget_evicts_the_soonest_expiring_rows(self) -> None:
        payload = _answer([dict(BUILDING, id=n) for n in range(40)])
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        cache = self._cache(byte_budget=int(size * 3.5))
        cache.set("a", payload)
        self.clock.now += 1
        cache.set("b", payload, ttl_seconds=UNFINISHED_JOB_TTL_SECONDS)
        self.clock.now += 1
        cache.set("c", payload)
        self.clock.now += 1
        cache.set("d", payload)
        self.assertEqual(cache.count(), 3)
        self.assertIsNone(cache.get("a"))
        for key in ("b", "c", "d"):
            self.assertIsNotNone(cache.get(key))

    def test_clear_gives_the_disk_space_back(self) -> None:
        cache = self._cache()
        big = _answer([dict(BUILDING, id=n, note="x" * 400) for n in range(4000)])
        for index in range(4):
            cache.set(f"q{index}", big)
        before = self.db_path.stat().st_size
        self.assertGreater(before, 4_000_000)
        self.assertEqual(cache.clear(), 4)
        self.assertLess(self.db_path.stat().st_size, before / 10)

    def test_a_too_dense_part_is_marked_for_a_day_apart_from_its_data(self) -> None:
        cache = self._cache()
        part = "[out:json][timeout:40];(node[amenity=cafe](38.2,26.9,38.21,26.91););out body geom;"
        self.assertFalse(cache.is_too_dense(part))
        cache.mark_too_dense(part, source="overpass-api.de")
        self.assertTrue(cache.is_too_dense(part))
        # Stored under its own key: never read back as the part's data, and
        # stored although it holds no elements at all.
        self.assertIsNone(cache.get(part))
        self.assertFalse(cache.has(part))
        self.assertEqual(cache.entry(too_dense_key(part)).source, "overpass-api.de")
        self.assertNotEqual(too_dense_key(part), part)
        # A day, written out: 24 hours.
        self.assertEqual(TOO_DENSE_TTL_SECONDS, 24 * 60 * 60)
        self.clock.now += 24 * 60 * 60 - 1
        self.assertTrue(cache.is_too_dense(part))
        self.clock.now += 2
        self.assertFalse(cache.is_too_dense(part))
        # Clear cache forgets the marks too.
        cache.mark_too_dense(part)
        cache.clear()
        self.assertFalse(cache.is_too_dense(part))

    def test_has_is_true_only_for_a_live_row(self) -> None:
        cache = self._cache()
        self.assertFalse(cache.has("q"))
        cache.set("q", _answer([BUILDING]))
        self.assertTrue(cache.has("q"))
        self.clock.now += DEFAULT_TTL_SECONDS + 1
        self.assertFalse(cache.has("q"))

    def test_the_serving_host_is_remembered(self) -> None:
        cache = self._cache()
        cache.set("q", _answer([BUILDING]), source="overpass.private.coffee")
        entry = cache.entry("q")
        self.assertEqual(entry.source, "overpass.private.coffee")
        self.assertEqual(entry.payload["elements"], [BUILDING])

    def test_a_cache_file_from_version_1_5_is_upgraded_in_place(self) -> None:
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute(
                "CREATE TABLE query_cache (query_hash TEXT PRIMARY KEY, "
                "stored_at REAL NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO query_cache (query_hash, stored_at, payload) VALUES (?, ?, ?)",
                (QueryCache._key("old"), self.clock.now - 60, json.dumps(_answer([BUILDING]))),
            )
            connection.commit()
        finally:
            connection.close()
        cache = self._cache()
        columns = {row[1] for row in cache._connection.execute("PRAGMA table_info(query_cache)")}
        self.assertTrue({"ttl", "size", "source"} <= columns)
        entry = cache.entry("old")
        self.assertEqual((entry.payload["elements"], entry.source), ([BUILDING], ""))
        cache.set("new", _answer([BUILDING]))
        self.assertEqual(cache.count(), 2)


class CacheRefusalTests(unittest.TestCase):
    """What the download never stores: empty, incomplete, or stale answers."""

    def test_a_fresh_answer_with_data_may_be_stored(self) -> None:
        self.assertEqual(cache_refusal(_answer([BUILDING]), now=FRESH_AT + 120), "")
        # No data date at all: nothing proves it stale.
        self.assertEqual(cache_refusal({"elements": [BUILDING]}, now=FRESH_AT), "")

    def test_empty_incomplete_and_stale_answers_are_refused(self) -> None:
        self.assertEqual(cache_refusal(_answer([]), now=FRESH_AT), "it is empty")
        self.assertEqual(
            cache_refusal(
                _answer([BUILDING], remark="runtime error: Query timed out"), now=FRESH_AT
            ),
            "the server marked it as incomplete",
        )
        # The audit found a real cached answer 85.6 days behind its server.
        stale = _answer([BUILDING], stamp="2026-08-08T08:00:00Z")
        self.assertEqual(cache_refusal(stale, now=FRESH_AT), "its data is 85 days old")
        self.assertEqual(cache_refusal([BUILDING]), "it is not an OSM response")

    def test_data_older_than_seven_days_is_never_stored(self) -> None:
        # Seven days, written out here rather than read from the code: a
        # server that stopped updating a week ago is not a cache source.
        week = 7 * 24 * 60 * 60
        self.assertEqual(STALE_AFTER_SECONDS, week)

        def aged(seconds):
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(FRESH_AT - seconds))
            return _answer([BUILDING], stamp=stamp)

        self.assertEqual(cache_refusal(aged(week), now=FRESH_AT), "")
        self.assertEqual(cache_refusal(aged(week + 1), now=FRESH_AT), "its data is 7 days old")
        self.assertEqual(cache_refusal(aged(8 * 86400), now=FRESH_AT), "its data is 8 days old")
        self.assertEqual(cache_refusal(aged(6 * 86400), now=FRESH_AT), "")


if __name__ == "__main__":
    unittest.main()
