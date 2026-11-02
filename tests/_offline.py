"""Keep a QGIS test suite off every public host.

Every QGIS suite calls :func:`isolate_network` before it runs anything.  It
points each host the plugin asks at a closed port on this computer -- the
Overpass hosts and Postpass, which ``processing.osm_algorithms._backends()``
reads, and the Nominatim search and lookup -- and then guards QGIS's network
access itself: a request for any host other than this computer is turned into
a request for that closed port, recorded, and reported with a ``[FAIL]`` line
(pf verify fails a suite on such a line, docs/TRAPS.md 4.4).  A host the
plugin gains later therefore cannot reach the internet from a test either.  A
suite then points the hosts it exercises at its own fake servers on
127.0.0.1.

Added after a verify run sent real requests to postpass.geofabrik.de: the
suites redirected the Overpass hosts only, and the Postpass fallback (on by
default) asked the real service for every part they let fail.
"""
from __future__ import annotations

import threading
from typing import List
from urllib.parse import urlsplit

#: Nothing listens on the discard port: a request there is refused locally.
CLOSED = "http://127.0.0.1:9"
#: The moment the suites' recorded OSM data is judged against. Their fake
#: servers and fixtures carry data dates from 2026-10-26 to 2026-11-02, and a
#: download refuses to cache data more than 7 days old; judged by the wall
#: clock, every check that relies on the cache would start failing a week
#: after the data was recorded. The 7-day rule itself is tested with an
#: explicit clock in tests/test_cache.py.
DATA_NOW = "2026-11-02T08:00:00Z"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_GUARDED_SCHEMES = frozenset({"http", "https", "ftp", "ws", "wss"})
_LOCK = threading.Lock()
_REFUSED: List[str] = []
_GUARD: List[str] = []


def refused() -> List[str]:
    """Every public URL the guard has refused in this process, in order."""
    with _LOCK:
        return list(_REFUSED)


def _closed(url: str) -> str:
    """The same path and query, on the closed local port."""
    parts = urlsplit(url)
    return CLOSED + parts.path + (f"?{parts.query}" if parts.query else "")


def _guard(request) -> None:
    """QGIS request preprocessor: no request leaves this computer."""
    from qgis.PyQt.QtCore import QUrl

    url = request.url()
    host = str(url.host()).lower()
    if str(url.scheme()).lower() not in _GUARDED_SCHEMES or host in _LOCAL_HOSTS:
        return
    text = str(url.toString())
    with _LOCK:
        _REFUSED.append(text)
    request.setUrl(QUrl(f"{CLOSED}/refused-by-the-offline-guard"))
    print(f"  [FAIL] offline guard refused a request for {host}: {text[:160]}", flush=True)


def pin_data_age() -> None:
    """Judge the age of downloaded data as of DATA_NOW, not the wall clock.

    Only the age test moves: the download still refuses empty, incomplete
    and old answers through the real ``core.cache.cache_refusal``.
    """
    import calendar
    import time

    from zero2agent_osm_downloader.core import cache
    from zero2agent_osm_downloader.processing import osm_algorithms

    pinned = calendar.timegm(time.strptime(DATA_NOW, "%Y-%m-%dT%H:%M:%SZ"))

    def refusal(payload, now=None):
        return cache.cache_refusal(payload, now=pinned if now is None else now)

    osm_algorithms.cache_refusal = refusal


def isolate_network(guard_qgis: bool = True) -> None:
    """Point every plugin host at a closed local port and guard the rest.

    `guard_qgis` installs the request guard, which covers every request this
    QGIS makes; a suite passes False when it runs inside a QGIS session it
    did not start, whose own network must keep working.  Calling it again
    is harmless: the guard is installed once.  It also pins the date the
    suites' recorded data is judged against (:func:`pin_data_age`), which
    every suite that uses this module needs as well.
    """
    from qgis.core import QgsNetworkAccessManager

    from zero2agent_osm_downloader.core import geocoding
    from zero2agent_osm_downloader.processing import osm_algorithms

    pin_data_age()

    # The same closed host for Overpass and Postpass: after Overpass is
    # refused the host rests, so the fallback adds no request and no host to
    # a suite's messages.
    osm_algorithms.OVERPASS_ENDPOINTS = (f"{CLOSED}/api/interpreter",)
    osm_algorithms.POSTPASS_ENDPOINT = f"{CLOSED}/api/interpreter"
    osm_algorithms.build_search_url = lambda place, *args, **kwargs: _closed(
        geocoding.build_search_url(place, *args, **kwargs)
    )
    osm_algorithms.build_lookup_url = lambda area_id: _closed(
        geocoding.build_lookup_url(area_id)
    )
    if not guard_qgis:
        return
    with _LOCK:
        if not _GUARD:
            _GUARD.append(QgsNetworkAccessManager.setRequestPreprocessor(_guard))
