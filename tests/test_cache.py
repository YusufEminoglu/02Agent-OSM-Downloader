from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from zero2agent_osm_downloader.core.cache import QueryCache


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


if __name__ == "__main__":
    unittest.main()
