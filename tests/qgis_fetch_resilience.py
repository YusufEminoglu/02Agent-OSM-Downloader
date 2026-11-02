"""QGIS suite: the network client against a fake Overpass server on 127.0.0.1.

Run under a QGIS Python, from the folder that contains the plugin, with
QGIS_CUSTOM_CONFIG_PATH pointing at a throwaway directory (TRAPS 4.6):

    python-qgis-ltr.bat -m zero2agent_osm_downloader.tests.qgis_fetch_resilience
    python-qgis.bat     -m zero2agent_osm_downloader.tests.qgis_fetch_resilience

Everything goes through the real QgsBlockingNetworkRequest and the real
Processing algorithms; only the servers are fake.  QGIS's network timeout is
set to 4 s, so the transfer timeout is T = 4 - 2 = 2 s and every request
claims [timeout:10] (the R5 formula, not a patched constant).  Server
behaviour follows the audit's fake server, whose error bodies are copied
from the Overpass source and a live capture.  No public host is contacted.
"""
from __future__ import annotations

import contextlib
import ctypes
import gc
import json
import math
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsNetworkAccessManager,
    QgsProcessingAlgRunnerTask,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingUtils,
    QgsProject,
    QgsRectangle,
    QgsReferencedRectangle,
    QgsTask,
)
from qgis.PyQt.QtCore import QCoreApplication

QGIS_TIMEOUT_MS = 4000
BOX1 = (38.400, 27.100, 38.430, 27.140)        # one tile
BOX2 = (38.400, 27.100, 38.489, 27.320)        # two ~95 km² tiles
BOX9 = (38.200, 26.900, 38.460, 27.230)        # 3 x 3 tiles
BOX25 = (38.200, 26.900, 38.600, 27.400)       # 5 x 5 tiles
BOX_DENSE = (38.400, 27.100, 38.490, 27.210)   # one ~96 km² tile
BOX_MANY = (38.400, 27.100, 38.580, 27.330)    # four tiles, for the memory check
FRESH = "2026-11-02T06:58:39Z"
DENSE_KM2 = 30.0
FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: Konak district (relation 1269255) as an Overpass area id; the fake
#: Nominatim answers its lookup with the outline saved on 2026-11-02.
KONAK_AREA = 3_601_269_255


# ------------------------------------------------------------------ server
def _overpass_page(text: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
        "\n<head>\n  <title>OSM3S Response</title>\n</head>\n<body>\n"
        f'<p><strong style="color:#FF0000">Error</strong>: {text} </p>\n</body>\n</html>\n'
    ).encode("utf-8")


PAGE_429 = _overpass_page(
    "runtime error: open64: 0 Success /osm3s_osm_base "
    "Dispatcher_Client::request_read_and_idx::rate_limited. "
    "Please check /api/status for the quota of your IP address."
)
PAGE_400 = _overpass_page("line 2: parse error: Unknown type &quot;&quot;")
#: What the servers' POSIX engine (glibc regcomp) makes of "(?:...)".
PAGE_400_REGEX = _overpass_page(
    "line 3: static error: Invalid regular expression: Invalid preceding "
    "regular expression"
)
PAGE_404 = b"<html><head><title>404 Not Found</title></head><body><h1>Not Found</h1></body></html>"
BODY_502 = (
    b"# 502 Gateway Timeout\n\nThis Overpass API server is currently at capacity. "
    b"All query workers are busy\nand the request queue is full.\n"
)
_BOX_RE = re.compile(r"\((-?\d+\.\d+),(-?\d+\.\d+),(-?\d+\.\d+),(-?\d+\.\d+)\)")


def _doc(elements, remark=None) -> bytes:
    """Output_JSON's byte layout: header, elements, optional remark."""
    text = (
        '{\n  "version": 0.6,\n  "generator": "Overpass API 0.7.62.11 fake",\n'
        f'  "osm3s": {{\n    "timestamp_osm_base": "{FRESH}",\n'
        '    "copyright": "The data included in this document is from '
        'www.openstreetmap.org. The data is made available under ODbL."\n  },\n'
        '  "elements": [\n\n' + ",\n".join(json.dumps(item) for item in elements) + "\n\n  ]"
    )
    if remark:
        text += ',\n"remark": ' + json.dumps(remark)
    return (text + "\n}\n").encode("utf-8")


def _area_km2(box) -> float:
    south, west, north, east = box
    return (north - south) * 111.32 * (east - west) * 111.32 * math.cos(math.radians(abs(south)))


class FakeServers:
    """Two Overpass hosts and a Nominatim host, each on its own loopback port."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.hits = []
        self.modes = {}
        self.ports = {}
        self.box_ids = {}
        self.cache = {}
        for name in ("a", "b", "n"):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(64)
            self.ports[name] = listener.getsockname()[1]
            threading.Thread(target=self._serve, args=(name, listener), daemon=True).start()
            self.set(name, "ok")

    def url(self, name: str, path: str = "/api/interpreter") -> str:
        return f"http://127.0.0.1:{self.ports[name]}{path}"

    def host(self, name: str) -> str:
        return f"127.0.0.1:{self.ports[name]}"

    def set(self, name: str, mode: str, **options) -> None:
        with self.lock:
            self.modes[name] = {"mode": mode, "options": options, "since": time.monotonic(), "seen": 0}

    def take_hits(self):
        with self.lock:
            hits, self.hits = self.hits, []
        return hits

    # ------------------------------------------------------------ plumbing
    def _serve(self, name, listener) -> None:
        while True:
            connection, _address = listener.accept()
            threading.Thread(target=self._handle, args=(name, connection), daemon=True).start()

    @staticmethod
    def _read(connection):
        data = b""
        connection.settimeout(30)
        while b"\r\n\r\n" not in data:
            chunk = connection.recv(65536)
            if not chunk:
                return None
            data += chunk
        head, _sep, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            key, _colon, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        need = int(headers.get("content-length", "0") or 0)
        while len(body) < need:
            chunk = connection.recv(65536)
            if not chunk:
                break
            body += chunk
        method, path = (lines[0].split(" ") + ["", ""])[:2]
        return method, path, headers, body

    @staticmethod
    def _send(connection, status, reason, body, ctype="application/json", extra=()) -> None:
        head = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {ctype}",
                f"Content-Length: {len(body)}", "Connection: close", *extra]
        connection.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)

    @staticmethod
    def _hold(connection, limit=60.0) -> float:
        start = time.monotonic()
        connection.settimeout(0.2)
        while time.monotonic() - start < limit:
            try:
                if connection.recv(4096) == b"":
                    break
            except socket.timeout:
                continue
            except OSError:
                break
        return time.monotonic() - start

    def _handle(self, name, connection) -> None:
        with contextlib.closing(connection):
            try:
                request = self._read(connection)
                if request is None:
                    return
                method, path, headers, body = request
                query = urllib.parse.parse_qs(body.decode("latin-1")).get("data", [""])[0]
                with self.lock:
                    state = self.modes[name]
                    state["seen"] += 1
                    mode, options = state["mode"], state["options"]
                    since, seen = state["since"], state["seen"]
                    record = {
                        "host": name, "method": method, "path": path, "query": query,
                        "referer": headers.get("referer", ""), "ua": headers.get("user-agent", ""),
                        "t": time.monotonic(), "mode": mode,
                    }
                    self.hits.append(record)
                if "seq" in options:
                    steps = options["seq"].split(",")
                    mode = steps[min(seen - 1, len(steps) - 1)]
                self._answer(connection, mode, options, since, query, path, record)
            except OSError:
                return

    def _ids_for(self, box) -> int:
        with self.lock:
            return self.box_ids.setdefault(box, 1000 + len(self.box_ids))

    def _elements(self, query, per_box=1):
        boxes = list(dict.fromkeys(_BOX_RE.findall(query)))
        elements = [{"type": "node", "id": 1, "lat": 38.41, "lon": 27.11,
                     "tags": {"amenity": "cafe", "name": "on every seam"}}]
        for raw in boxes:
            south, west, north, east = (float(value) for value in raw)
            base = self._ids_for(raw) * 1_000_000
            for index in range(per_box):
                share = (index + 1) / (per_box + 1)
                elements.append({
                    "type": "node", "id": base + index,
                    "lat": round(south + (north - south) * share, 7),
                    "lon": round(west + (east - west) * share, 7),
                    "tags": {"amenity": "cafe"},
                })
        return elements

    def _many(self, query, count) -> bytes:
        raw = _BOX_RE.findall(query)[0]
        key = (raw, count)
        with self.lock:
            cached = self.cache.get(key)
        if cached is None:
            south, west = float(raw[0]), float(raw[1])
            base = self._ids_for(raw) * 1_000_000
            parts = [
                '{"type":"node","id":%d,"lat":%.7f,"lon":%.7f,"tags":{"amenity":"cafe"}}'
                % (base + index, south + 0.001, west + 0.001)
                for index in range(count)
            ]
            cached = ('{"version":0.6,"osm3s":{"timestamp_osm_base":"%s"},"elements":[%s]}'
                      % (FRESH, ",".join(parts))).encode("ascii")
            with self.lock:
                self.cache[key] = cached
        return cached

    def _answer(self, connection, mode, options, since, query, path, record) -> None:
        if path.startswith("/search"):
            return self._nominatim(connection, mode, path)
        if path.startswith("/lookup"):
            body = (FIXTURES / "nominatim_lookup_konak.json").read_bytes()
            return self._send(connection, 200, "OK", body, extra=("Cache-Control: no-store",))
        if "(?" in query:
            # POSIX has no "(?": the real servers refuse such a query whole.
            return self._send(connection, 400, "Bad Request", PAGE_400_REGEX, "text/html")
        if "boundary" in query and '"name"~' in query and mode == "ok":
            place = {"type": "relation", "id": 223474,
                     "tags": {"boundary": "administrative", "admin_level": "6", "name": "Konak"},
                     "bounds": {"minlat": 38.38, "minlon": 27.05, "maxlat": 38.46, "maxlon": 27.20}}
            return self._send(connection, 200, "OK", _doc([place]))
        boxes = _BOX_RE.findall(query)
        area = _area_km2(tuple(float(v) for v in boxes[0])) if boxes else 0.0
        if mode in ("dense", "dense_big", "oom"):
            # Heavy above a size, or, with a "spot", wherever the box covers
            # that point (a dense block in a light district), at every size.
            spot = options.get("spot")
            if spot is not None:
                south, west, north, east = (float(v) for v in boxes[0]) if boxes else (0, 0, 0, 0)
                heavy = south <= spot[0] <= north and west <= spot[1] <= east
            else:
                heavy = area > float(options.get("km2", DENSE_KM2))
            if not heavy:
                mode = "ok"
        if mode == "rst_for":
            mode = "rst" if time.monotonic() - since < float(options.get("seconds", 2)) else "ok"
        if mode == "ok":
            self._send(connection, 200, "OK", _doc(self._elements(query)))
        elif mode == "empty":
            self._send(connection, 200, "OK", _doc([]))
        elif mode == "many":
            self._send(connection, 200, "OK", self._many(query, int(options.get("count", 140_000))))
        elif mode == "hang":
            record["held"] = round(self._hold(connection), 2)
        elif mode == "rst":
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        elif mode == "h429":
            self._send(connection, 429, "Too Many Requests", PAGE_429, "text/html")
        elif mode == "h502":
            self._send(connection, 502, "Bad Gateway", BODY_502, "text/plain")
        elif mode == "h400":
            self._send(connection, 400, "Bad Request", PAGE_400, "text/html")
        elif mode == "h404":
            self._send(connection, 404, "Not Found", PAGE_404, "text/html")
        elif mode == "dense":
            self._send(connection, 200, "OK", _doc([], remark=(
                'runtime error: Query timed out in "query" at line 3 after 41 seconds.')))
        elif mode == "oom":
            self._send(connection, 200, "OK", _doc([], remark=(
                'runtime error: Query ran out of memory in "query" at line 3. '
                "It would need at least 513 MB of RAM to continue.")))
        elif mode == "dense_big":
            total = 80 * 1024 * 1024
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(total).encode() + b"\r\nConnection: close\r\n\r\n"
            )
            # Paced, so socket buffers cannot swallow the whole answer before
            # the client hangs up: what was sent shows where it stopped.
            chunk, sent = b" " * (1024 * 1024), 0
            with contextlib.suppress(OSError):
                while sent < total:
                    connection.sendall(chunk)
                    sent += len(chunk)
                    time.sleep(0.02)
            record["sent"] = sent
        else:
            self._send(connection, 500, "Fake", b"unknown mode", "text/plain")

    def _nominatim(self, connection, mode, path) -> None:
        name = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("q", [""])[0]
        no_store = ("Cache-Control: no-store",)
        if mode == "nominatim_empty":
            self._send(connection, 200, "OK", b"[]", extra=no_store)
        elif mode == "nominatim_503":
            self._send(connection, 503, "Service Unavailable", b"<html>busy</html>", "text/html", no_store)
        else:
            entry = [{"place_id": 1, "osm_type": "relation", "osm_id": 223474,
                      "category": "boundary", "type": "administrative", "addresstype": "town",
                      "name": name, "display_name": f"{name}, Izmir, Turkiye", "importance": 0.6,
                      "boundingbox": ["38.38", "38.46", "27.05", "27.20"],
                      "address": {"town": name}, "extratags": {"admin_level": "6"}}]
            self._send(connection, 200, "OK", json.dumps(entry).encode(), extra=no_store)


# --------------------------------------------------------------- harness
class Recorder(QgsProcessingFeedback):
    """Feedback with the dock's live-event hook; records log lines too."""

    def __init__(self) -> None:
        super().__init__(False)
        self.events = []
        self.lines = []

    def zero2agent_event(self, event) -> None:  # noqa: N802 - interface name
        self.events.append(dict(event))

    def pushInfo(self, info) -> None:  # noqa: N802 - QGIS naming
        self.lines.append(("info", info))
        super().pushInfo(info)

    def pushWarning(self, warning) -> None:  # noqa: N802 - QGIS naming
        self.lines.append(("warning", warning))
        super().pushWarning(warning)

    def phases(self):
        return [event["phase"] for event in self.events]


def _extent(box):
    south, west, north, east = box
    return QgsReferencedRectangle(
        QgsRectangle(west, south, east, north), QgsCoordinateReferenceSystem("EPSG:4326")
    )


def _parameters(box):
    return {
        "KEY": "amenity", "VALUE": "cafe", "GEOMETRY": 0, "EXTENT": _extent(box),
        "OUTPUT_POINTS": "TEMPORARY_OUTPUT", "OUTPUT_LINES": "TEMPORARY_OUTPUT",
        "OUTPUT_POLYGONS": "TEMPORARY_OUTPUT",
        # Overpass alone: the Postpass fallback is tested in qgis_fallback_boundary.
        "ALLOW_FALLBACK": False,
    }


class Check:
    def __init__(self) -> None:
        self.failures = []
        self.timings = []

    def that(self, condition, message) -> None:
        if not condition:
            self.failures.append(message)
            print(f"    CHECK FAILED: {message}", flush=True)


def _download(box, servers, *, expect_failure=False):
    """Run the custom-tag download in this thread; return what happened."""
    import processing

    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = Recorder()
    servers.take_hits()
    started = time.monotonic()
    results, error = None, ""
    try:
        results = processing.run(
            "zero2agentosm:download_custom_tag", _parameters(box),
            context=context, feedback=feedback, is_child_algorithm=True,
        )
    except QgsProcessingException as exc:
        error = str(exc)
    elapsed = time.monotonic() - started
    hits = servers.take_hits()
    points = None
    if results is not None:
        layer = QgsProcessingUtils.mapLayerFromString(results["OUTPUT_POINTS"], context, True)
        points = sorted(int(feature["osm_id"]) for feature in layer.getFeatures())
    if expect_failure != bool(error):
        print(f"    unexpected outcome: error={error!r}", flush=True)
    return {
        "results": results, "error": error, "elapsed": elapsed, "hits": hits,
        "feedback": feedback, "points": points,
        "requests": feedback.phases().count("request"),
    }


def _fresh(servers, **modes):
    from zero2agent_osm_downloader.core import mirrors
    from zero2agent_osm_downloader.processing import osm_algorithms

    mirrors.BOARD.reset()
    osm_algorithms.clear_cache()
    for name, mode in modes.items():
        if isinstance(mode, tuple):
            servers.set(name, mode[0], **mode[1])
        else:
            servers.set(name, mode)


def _timing(check, name, run, rt) -> None:
    wire = len([hit for hit in run["hits"] if hit["path"].startswith("/api")])
    check.timings.append((name, round(run["elapsed"], 2), run["requests"], wire))
    print(f"  [TIME] {rt} {name}: {run['elapsed']:.2f} s, "
          f"{run['requests']} requests ({wire} on the wire)", flush=True)


# --------------------------------------------------------------- checks
def check_outages(check, servers, rt) -> None:
    from zero2agent_osm_downloader.core import mirrors

    host_a, host_b = servers.host("a"), servers.host("b")
    _fresh(servers, a="hang", b="hang")
    run = _download(BOX1, servers, expect_failure=True)
    _timing(check, "1 tile, all hosts hang", run, rt)
    check.that(run["requests"] == 2, f"one hung tile used {run['requests']} requests, not 2")
    check.that(run["elapsed"] < 8, f"one hung tile took {run['elapsed']:.1f} s")
    # The advice first, the hosts last (a narrow dock shows the start).
    check.that(
        run["error"] == (
            "The OpenStreetMap servers are busy. Nothing was downloaded. Try again in "
            f"about 2 minutes. ({host_a}, {host_b}: did not answer within 2 seconds)"
        ),
        f"hang message: {run['error']}",
    )
    check.that("mirror" not in run["error"].casefold() and "pinned" not in run["error"].casefold(),
               "the failure message uses jargon")
    failed = [event for event in run["feedback"].events if event["phase"] == "failed"]
    check.that(
        len(failed) == 1 and failed[0]["category"] == "busy"
        and failed[0]["kind"] == "client_timeout" and host_a in failed[0]["detail"]
        and failed[0]["message"] == run["error"],
        f"failed event: {failed}",
    )
    check.that(mirrors.BOARD.remaining(host_a) > 100, "a hung host was not rested")

    _fresh(servers, a="hang", b="hang")
    run = _download(BOX25, servers, expect_failure=True)
    _timing(check, "25 tiles, all hosts hang", run, rt)
    check.that(run["requests"] == 2, f"25 hung tiles used {run['requests']} requests, not 2")
    check.that(run["elapsed"] < 8, f"25 hung tiles took {run['elapsed']:.1f} s")

    _fresh(servers, a="h429", b="h429")
    run = _download(BOX25, servers, expect_failure=True)
    _timing(check, "25 tiles, every host answers 429", run, rt)
    check.that(len(run["hits"]) == 2, f"429 everywhere sent {len(run['hits'])} requests, not 2")
    check.that("asked this computer to slow down" in run["error"]
               and "Wait about a minute" in run["error"], f"429 message: {run['error']}")
    check.that(mirrors.BOARD.remaining(host_a) >= 59, "a 429 host is not rested for 60 s")
    again = _download(BOX1, servers, expect_failure=True)
    check.that(len(again["hits"]) == 0, "a host was asked again inside its 429 rest")
    check.that(f"{host_a}: not asked, because it asked this computer to slow down" in again["error"],
               f"resting message: {again['error']}")
    # When Continue could work: both Overpass hosts rest about a minute;
    # Postpass, when allowed, may be asked now, until it rests too.
    from zero2agent_osm_downloader.core import transport
    from zero2agent_osm_downloader.processing import osm_algorithms

    overpass_only = osm_algorithms.seconds_until_a_server_is_ready(False)
    check.that(55 <= overpass_only <= 60, f"Overpass ready in {overpass_only:.1f} s")
    check.that(osm_algorithms.seconds_until_a_server_is_ready(True) == 0.0,
               "Postpass is not resting, yet a download is said to wait")
    postpass_host = osm_algorithms._host_name(osm_algorithms.POSTPASS_ENDPOINT)
    mirrors.BOARD.strike(postpass_host, transport.BUSY)
    with_postpass = osm_algorithms.seconds_until_a_server_is_ready(True)
    check.that(44 <= with_postpass <= 45, f"with Postpass resting too: {with_postpass:.1f} s")
    mirrors.BOARD.reset()
    check.that(osm_algorithms.seconds_until_a_server_is_ready(False) == 0.0, "rested hosts still wait")

    _fresh(servers, a="h400", b="ok")
    run = _download(BOX1, servers, expect_failure=True)
    check.that(len(run["hits"]) == 1, f"an invalid request was sent {len(run['hits'])} times")
    check.that(run["error"].startswith("The OpenStreetMap server rejected the request as invalid.")
               and f"({host_a}: rejected the request as invalid (HTTP 400); {host_b}: not asked)"
               in run["error"], f"400 message: {run['error']}")


def check_first_host_hangs(check, servers, rt) -> None:
    from zero2agent_osm_downloader.processing.osm_algorithms import REFERER

    host_a, host_b = servers.host("a"), servers.host("b")
    _fresh(servers, a="hang", b="ok")
    run = _download(BOX25, servers)
    _timing(check, "25 tiles, first host hangs", run, rt)
    results = run["results"] or {}
    per_host = {name: len([h for h in run["hits"] if h["host"] == name]) for name in ("a", "b")}
    check.that(per_host == {"a": 1, "b": 25}, f"requests per host: {per_host}")
    check.that(results.get("TILES_TOTAL") == 25 and results.get("TILES_MISSING") == 0,
               f"tiles: {results.get('TILES_TOTAL')} / {results.get('TILES_MISSING')}")
    check.that(results.get("DATA_SOURCE") == host_b, f"DATA_SOURCE {results.get('DATA_SOURCE')!r}")
    check.that(results.get("OSM_TIMESTAMP") == FRESH, "the data date was lost")
    # 25 tile nodes plus one node every tile returns, kept once.
    check.that(run["points"] is not None and len(run["points"]) == 26 and run["points"].count(1) == 1,
               f"merged points: {None if run['points'] is None else len(run['points'])}")
    times = [hit["t"] for hit in run["hits"] if hit["host"] == "b"]
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    check.that(gaps and min(gaps) >= 0.95, f"requests to one host closer than 1 s: {min(gaps or [0]):.2f}")
    phases = run["feedback"].phases()
    check.that(phases.count("part_done") == 25 and phases.count("switch") == 1
               and phases[-1] == "done", f"phases: {sorted(set(phases))}")
    # Identity and the claim on every request.
    overpass = [hit for hit in run["hits"] if hit["path"].startswith("/api")]
    check.that(all(hit["referer"] == REFERER for hit in overpass), "a request lacked the Referer")
    check.that(all(hit["query"].startswith("[out:json][timeout:10][maxsize:134217728];")
                   for hit in overpass), f"header on the wire: {overpass[0]['query'][:60]}")
    suffix = any("02Agent-OSM-Downloader/" in hit["ua"] for hit in overpass)
    if rt.startswith("4"):
        check.that(suffix, f"QGIS 4.2 did not carry the User-Agent suffix: {overpass[0]['ua']}")
    else:
        check.that(not suffix, "QGIS 3.x reports a suffix it cannot set")
    check.that(bool(overpass[0]["ua"]), "no User-Agent at all")
    host = host_a  # keeps both names visible in the log
    print(f"    hosts: {host} hung, {host_b} delivered; UA on the wire: {overpass[0]['ua']}", flush=True)


def _sizes(run):
    """(host, km² of the box asked) of every Overpass request of a run."""
    return [
        (hit["host"], round(_area_km2(tuple(float(v) for v in _BOX_RE.findall(hit["query"])[0]))))
        for hit in run["hits"] if hit["path"].startswith("/api")
    ]


def check_dense_tile(check, servers, rt) -> None:
    host_a, host_b = servers.host("a"), servers.host("b")
    # Both hosts run out of time on the 96 km² part: only then is it split.
    # Each rests 30 s after its timeout, so the first quarter waits for a.
    _fresh(servers, a="dense", b="dense")
    run = _download(BOX_DENSE, servers)
    _timing(check, "1 dense tile, both hosts time out, split into 4", run, rt)
    results = run["results"] or {}
    check.that(_sizes(run) == [("a", 96), ("b", 96)] + [("a", 24)] * 4,
               f"dense tile requests: {_sizes(run)}")
    check.that(results.get("TILES_TOTAL") == 4 and results.get("TILES_MISSING") == 0,
               f"dense tiles: {results.get('TILES_TOTAL')} / {results.get('TILES_MISSING')}")
    check.that(run["points"] is not None and len(run["points"]) == 5, f"dense points: {run['points']}")
    split = [event for event in run["feedback"].events if event["phase"] == "split"]
    check.that(len(split) == 1 and split[0]["detail"] == (
        f"{host_a} and {host_b} ran out of time on this query; part 1 is split into "
        "4 smaller parts."), f"split events: {split}")
    check.that(run["elapsed"] >= 28, f"the quarters did not wait for a resting host: {run['elapsed']:.1f} s")

    _fresh(servers, a="dense_big", b="ok")
    run = _download(BOX_DENSE, servers)
    _timing(check, "1 tile over 64 MB, split into 4", run, rt)
    big = [hit for hit in run["hits"] if hit["mode"] == "dense_big"]
    sent = big[0].get("sent", 0) if big else 0
    check.that(len(run["hits"]) == 5 and run["results"] is not None,
               f"oversize tile: {len(run['hits'])} requests, error {run['error']!r}")
    check.that(64 * 2**20 <= sent < 80 * 2**20,
               f"the transfer was not cut at 64 MB: server sent {sent / 2**20:.1f} MB")
    # An answer too big costs the host no rest: a stays first for the quarters.
    check.that(_sizes(run) == [("a", 96)] + [("a", 24)] * 4,
               f"oversize tile requests: {_sizes(run)}")


def check_heavy_parts(check, servers, rt) -> None:
    """A server timeout is no proof of density; out of memory is."""
    from zero2agent_osm_downloader.core import transport
    from zero2agent_osm_downloader.core.catalog import TagSpec
    from zero2agent_osm_downloader.core.query import plan_requests
    from zero2agent_osm_downloader.processing import osm_algorithms

    host_b = servers.host("b")
    # An overloaded host times out on everything; the healthy host gets the
    # same part at the same size, and nothing is split.  1.6.0 split on the
    # first timeout and started every later tile split: 37 requests for 9.
    _fresh(servers, a=("dense", {"km2": 0}), b="ok")
    run = _download(BOX1, servers)
    _timing(check, "1 tile, first host times out on everything", run, rt)
    results = run["results"] or {}
    check.that(_sizes(run) == [("a", 12), ("b", 12)], f"requests: {_sizes(run)}")
    check.that(results.get("TILES_TOTAL") == 1 and results.get("TILES_MISSING") == 0
               and results.get("DATA_SOURCE") == host_b, f"result: {run['error']!r} {results}")
    check.that("split" not in run["feedback"].phases(), "a part was split on one timeout")
    _fresh(servers, a=("dense", {"km2": 0}), b="ok")
    run = _download(BOX9, servers)
    _timing(check, "9 tiles, first host times out on everything", run, rt)
    results = run["results"] or {}
    per_host = {name: len([h for h in run["hits"] if h["host"] == name]) for name in ("a", "b")}
    check.that(per_host == {"a": 1, "b": 9}, f"overloaded host, 9 tiles: {per_host}")
    check.that(results.get("TILES_TOTAL") == 9 and results.get("TILES_MISSING") == 0,
               f"9 tiles were pre-split or lost: {results}")

    # Proven density: out of memory wherever a block of the box is.  Split at
    # once without asking b, later parts start small, and the block is still
    # too heavy at 6.25 km²: marked for a day, missing, the rest delivered.
    spot = (38.4005, 27.1005)
    block = plan_requests((TagSpec("amenity", "cafe", "point"),), BOX1)[0].split()[0].split()[0]
    block_box = tuple(f"{value:.7f}" for value in block.bbox)
    note = transport.too_dense_note(1)
    _fresh(servers, a=("oom", {"spot": spot}), b="ok")
    run = _download(BOX1, servers)
    _timing(check, "1 tile with a block out of memory at every size", run, rt)
    results = run["results"] or {}
    check.that(len(run["hits"]) == 18 and {hit["host"] for hit in run["hits"]} == {"a"},
               f"out-of-memory block: {len(run['hits'])} requests to {sorted({h['host'] for h in run['hits']})}")
    check.that(results.get("TILES_TOTAL") == 16 and results.get("TILES_MISSING") == 1,
               f"out-of-memory block result: {run['error']!r} {results}")
    warnings = [text for level, text in run["feedback"].lines if level == "warning"]
    check.that(any(text.startswith("Finished with gaps: 1 of 16 parts are missing.") and note in text
                   for text in warnings), f"marker sentence: {warnings}")
    check.that(osm_algorithms._cache().is_too_dense(block.cache_key()), "the block was not marked")
    # Continue: the marked block is missing without a request.
    again = _download(BOX1, servers)
    asked = [tuple(_BOX_RE.findall(hit["query"])[0]) for hit in again["hits"]
             if hit["path"].startswith("/api")]
    results = again["results"] or {}
    check.that(block_box not in asked and results.get("TILES_MISSING") == 1,
               f"Continue asked the marked block again: {len(asked)} requests, {results}")
    skipped = [event["detail"] for event in again["feedback"].events if event["phase"] == "part_lost"]
    check.that(len(skipped) == 1 and skipped[0].startswith("Part 1 of ") and skipped[0].endswith(
        "is missing: it was found too dense even at the smallest size in the last 24 hours, "
        "so it is not asked again."), f"skip event: {skipped}")
    print(f"    Continue after the marked block: {len(asked)} requests", flush=True)


def check_gaps_and_continue(check, servers, rt) -> None:
    from zero2agent_osm_downloader.core import mirrors
    from zero2agent_osm_downloader.core.query import plan_requests
    from zero2agent_osm_downloader.core.catalog import TagSpec
    from zero2agent_osm_downloader.processing import osm_algorithms

    _fresh(servers, a=("ok", {"seq": "ok,ok,ok,h502"}), b="h502")
    run = _download(BOX9, servers)
    results = run["results"] or {}
    _timing(check, "9 tiles, servers fail after 3", run, rt)
    check.that(results.get("TILES_TOTAL") == 9 and results.get("TILES_MISSING") == 6,
               f"gaps: {results.get('TILES_TOTAL')} / {results.get('TILES_MISSING')}")
    check.that(len(run["hits"]) == 5, f"a lost part was asked for again: {len(run['hits'])} requests")
    warnings = [text for level, text in run["feedback"].lines if level == "warning"]
    check.that(any(text.startswith("Finished with gaps: 6 of 9 parts are missing.") for text in warnings),
               f"no gap warning: {warnings}")
    done = [event for event in run["feedback"].events if event["phase"] == "done"]
    check.that(done and done[-1]["missing"] == 6 and done[-1]["category"] == "gaps",
               f"done event: {done}")
    check.that(run["feedback"].phases().count("part_lost") == 2, "lost parts were not announced")
    # The three parts that arrived are kept for a day, for Continue.
    plan = plan_requests((TagSpec("amenity", "cafe", "point"),), BOX9)
    rows = osm_algorithms._cache()._connection.execute(
        "SELECT stored_at + ttl - ? FROM query_cache WHERE ttl IS NOT NULL", (time.time(),)
    ).fetchall()
    check.that(len(rows) == 3 and all(row[0] > 23 * 3600 for row in rows),
               f"kept parts: {rows}")
    check.that(sum(osm_algorithms._cache().has(r.cache_key()) for r in plan) == 3,
               "the delivered parts are not in the cache")

    mirrors.BOARD.reset()   # the servers' rest has passed
    servers.set("a", "ok")
    servers.set("b", "ok")
    again = _download(BOX9, servers)
    results = again["results"] or {}
    _timing(check, "9 tiles, run again after the gaps", again, rt)
    check.that(len(again["hits"]) == 6, f"run again sent {len(again['hits'])} requests, not 6")
    check.that(results.get("TILES_MISSING") == 0 and again["points"] is not None
               and len(again["points"]) == 10, f"run again result: {results}")


def check_overload_by_timeouts(check, servers, rt) -> None:
    """Every host times out at every size: overload, not density."""
    from zero2agent_osm_downloader.processing import osm_algorithms

    host_a, host_b = servers.host("a"), servers.host("b")
    _fresh(servers, a=("dense", {"km2": 0}), b=("dense", {"km2": 0}))
    run = _download(BOX9, servers, expect_failure=True)
    _timing(check, "9 tiles, every host times out at every size", run, rt)
    # The first tile on both hosts, then two of its quarters once a rest is
    # over: nothing arrived, so the download stops; nothing is marked.
    check.that(_sizes(run) == [("a", 93), ("b", 93), ("a", 23), ("b", 23)],
               f"overload requests: {_sizes(run)}")
    check.that(run["error"] == (
        "The OpenStreetMap servers are busy. Nothing was downloaded. Try again in about a "
        f"minute. ({host_a}, {host_b}: ran out of time on this query)"),
        f"overload sentence: {run['error']!r}")
    check.that(osm_algorithms._cache().count() == 0, "an overloaded download left markers")
    failed = [event for event in run["feedback"].events if event["phase"] == "failed"]
    check.that(failed and failed[-1]["category"] == "busy", f"failed event: {failed}")


def check_host_errors(check, servers, rt) -> None:
    """C5: a 404 is that host's failure; the healthy host gets the part."""
    from zero2agent_osm_downloader.core import mirrors

    host_a, host_b = servers.host("a"), servers.host("b")
    for name, box, tiles in (("1 tile", BOX1, 1), ("9 tiles", BOX9, 9)):
        _fresh(servers, a="h404", b="ok")
        run = _download(box, servers)
        _timing(check, f"{name}, first host answers 404", run, rt)
        results = run["results"] or {}
        per_host = {key: len([h for h in run["hits"] if h["host"] == key]) for key in ("a", "b")}
        check.that(per_host == {"a": 1, "b": tiles}, f"{name}: requests per host {per_host}")
        check.that(results.get("TILES_MISSING") == 0 and results.get("DATA_SOURCE") == host_b,
                   f"{name}: {run['error']!r} {results}")
        # A 120 s rest from the moment it answered.
        kind, _detail, age = mirrors.BOARD.last_answer(host_a)
        rest = mirrors.BOARD.remaining(host_a)
        check.that(kind == "host_error" and abs(rest + age - 120) < 1.5,
                   f"{name}: the 404 host ({kind}) rests {rest:.0f} s, {age:.0f} s after it answered")
        switch = [event for event in run["feedback"].events if event["phase"] == "switch"]
        check.that(switch and switch[0]["detail"] == (
            f"{host_a} answered with HTTP 404 instead of data; trying another server."),
            f"{name}: switch {switch[:1]}")
    # The next download leaves the resting host alone.
    again = _download(BOX1, servers)
    check.that([hit["host"] for hit in again["hits"]] == ["b"], f"again: {again['hits']}")
    # Both hosts 404: a server error, not an invalid request.
    _fresh(servers, a="h404", b="h404")
    run = _download(BOX1, servers, expect_failure=True)
    check.that(run["error"] == (
        "The OpenStreetMap servers answered with an error instead of data. Nothing was "
        f"downloaded. Try again in about 2 minutes. ({host_a}, {host_b}: answered with "
        "HTTP 404 instead of data)"), f"two 404s: {run['error']!r}")


def check_continue_after_split(check, servers, rt) -> None:
    """C2: a re-run takes the saved quarters of a split part, never the part."""
    from zero2agent_osm_downloader.core.catalog import TagSpec
    from zero2agent_osm_downloader.core.query import plan_requests
    from zero2agent_osm_downloader.processing import osm_algorithms

    def seed(request, count):
        """Save the first `count` quarters of a part, as a run that split it."""
        quarters = request.split()
        for index, quarter in enumerate(quarters[:count]):
            south, west, north, east = quarter.bbox
            node = {"type": "node", "id": 7_000_000 + index, "tags": {"amenity": "cafe"},
                    "lat": (south + north) / 2, "lon": (west + east) / 2}
            osm_algorithms._cache().set(
                quarter.cache_key(), {"osm3s": {"timestamp_osm_base": FRESH}, "elements": [node]},
                source=servers.host("a"))
        return quarters

    specs = (TagSpec("amenity", "cafe", "point"),)
    dense = plan_requests(specs, BOX_DENSE)
    # Continue while the servers are still busy: the two saved quarters are
    # the result, the 96 km² part is never asked for (1.6.0 asked both hosts
    # for it and said "Nothing was downloaded").
    _fresh(servers, a="h502", b="h502")
    seed(dense[0], 2)
    run = _download(BOX_DENSE, servers)
    _timing(check, "Continue a split part, servers busy", run, rt)
    results = run["results"] or {}
    check.that(results.get("TILES_TOTAL") == 4 and results.get("TILES_MISSING") == 2
               and run["points"] == [7_000_000, 7_000_001], f"busy Continue: {run['error']!r} {results}")
    check.that(all(size == 24 for _host, size in _sizes(run)) and len(run["hits"]) == 2,
               f"busy Continue asked {_sizes(run)}")
    # Continue with healthy servers: only the two missing quarters are sent.
    _fresh(servers, a=("dense", {"km2": DENSE_KM2}), b="ok")
    seed(dense[0], 2)
    run = _download(BOX_DENSE, servers)
    _timing(check, "Continue a split part, servers healthy", run, rt)
    results = run["results"] or {}
    check.that(_sizes(run) == [("a", 24), ("a", 24)], f"healthy Continue asked {_sizes(run)}")
    check.that(results.get("TILES_TOTAL") == 4 and results.get("TILES_MISSING") == 0
               and run["points"] is not None and len(run["points"]) == 5,
               f"healthy Continue: {results} {run['points']}")
    # After a stop, a later part's saved quarters are still collected.
    two = plan_requests(specs, BOX2)
    check.that(len(two) == 2, f"the two-part box plans {len(two)} parts")
    _fresh(servers, a="h502", b="h502")
    seed(two[1], 2)
    run = _download(BOX2, servers)
    results = run["results"] or {}
    check.that(len(run["hits"]) == 2 and results.get("TILES_TOTAL") == 5
               and results.get("TILES_MISSING") == 3
               and run["points"] == [7_000_000, 7_000_001],
               f"stopped job: {len(run['hits'])} requests, {run['error']!r} {results}")


def check_cache_honesty(check, servers, rt) -> None:
    """An empty answer is used once and never replayed from the cache."""
    from zero2agent_osm_downloader.core.catalog import TagSpec
    from zero2agent_osm_downloader.core.query import plan_requests
    from zero2agent_osm_downloader.processing import osm_algorithms

    key = plan_requests((TagSpec("amenity", "cafe", "point"),), BOX1)[0].cache_key()
    _fresh(servers, a="empty", b="ok")
    run = _download(BOX1, servers)
    check.that(run["results"] is not None and run["points"] == [], f"empty answer: {run['error']!r}")
    check.that(not osm_algorithms._cache().has(key), "an empty answer was stored")
    again = _download(BOX1, servers)
    check.that([hit["host"] for hit in again["hits"]] == ["a"],
               f"the empty answer was replayed: {again['hits']}")


def check_nominatim_spacing(check, servers, rt) -> None:
    """Nominatim's one request per second holds for the place search and the
    boundary lookup together, across two downloads running at once on the
    task manager's worker threads, as the dock's downloads do."""
    from zero2agent_osm_downloader.core import geocoding
    from zero2agent_osm_downloader.processing import osm_algorithms

    fake = f"http://127.0.0.1:{servers.ports['n']}"
    saved = (osm_algorithms.build_search_url, osm_algorithms.build_lookup_url)
    osm_algorithms.build_search_url = lambda place, *args, **kwargs: geocoding.build_search_url(
        place, *args, **kwargs).replace("https://nominatim.openstreetmap.org", fake)
    osm_algorithms.build_lookup_url = lambda area_id: geocoding.build_lookup_url(
        area_id).replace("https://nominatim.openstreetmap.org", fake)
    errors = []
    outlines = []

    def place(_task, name, area_id):
        try:
            feedback = Recorder()
            osm_algorithms._geocode(name, feedback)
            outline, why = osm_algorithms._place_boundary(area_id, feedback)
            outlines.append((area_id, outline is not None, why))
        except Exception as error:  # noqa: BLE001 - reported by the check below
            errors.append(f"{type(error).__name__}: {error}")
        return True

    try:
        _fresh(servers, n="ok")
        servers.take_hits()   # nothing left over from an earlier check
        workers = [QgsTask.fromFunction(f"Nominatim {name}", place, name, area_id)
                   for name, area_id in (("Konak", KONAK_AREA), ("Bornova", KONAK_AREA + 1))]
        for worker in workers:
            QgsApplication.taskManager().addTask(worker)
        deadline = time.monotonic() + 60
        while len(outlines) + len(errors) < 2 and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        hits = [hit for hit in servers.take_hits() if hit["host"] == "n"]
        times = sorted(hit["t"] for hit in hits)
        gaps = [round(later - earlier, 3) for earlier, later in zip(times, times[1:])]
        kinds = sorted(hit["path"].split("?")[0] for hit in hits)
        check.that(not errors, f"Nominatim calls failed: {errors}")
        check.that(kinds == ["/lookup", "/lookup", "/search", "/search"],
                   f"Nominatim requests: {kinds}; lookups returned {outlines}")
        # One second start to start on this side; a few milliseconds of
        # arrival jitter on the loopback.
        check.that(len(gaps) == 3 and min(gaps) >= 0.9, f"Nominatim requests closer than 1 s: {gaps}")
        print(f"    Nominatim: 2 searches and 2 lookups from two tasks, {gaps} s apart", flush=True)
    finally:
        osm_algorithms.build_search_url, osm_algorithms.build_lookup_url = saved


def check_reset_retry(check, servers, rt) -> None:
    _fresh(servers, a=("rst_for", {"seconds": 2}), b="ok")
    run = _download(BOX1, servers)
    _timing(check, "connection reset, one retry", run, rt)
    a_hits = [hit for hit in run["hits"] if hit["host"] == "a"]
    check.that(run["results"] is not None and run["results"].get("DATA_SOURCE") == servers.host("a"),
               f"reset retry: {run['error']!r}")
    check.that(run["requests"] == 2 and run["elapsed"] >= 5.0,
               f"reset retry used {run['requests']} attempts in {run['elapsed']:.1f} s")
    # Qt 5 resends a closed POST by itself; each attempt is still bounded.
    check.that(2 <= len(a_hits) <= 8 and not [h for h in run["hits"] if h["host"] == "b"],
               f"wire requests after a reset: {len(a_hits)}")


def check_place_search(check, servers, rt) -> None:
    from zero2agent_osm_downloader.core import geocoding
    from zero2agent_osm_downloader.processing import osm_algorithms

    real = geocoding.build_search_url
    isolated = osm_algorithms.build_search_url
    fake_base = f"http://127.0.0.1:{servers.ports['n']}"
    osm_algorithms.build_search_url = lambda place, *args, **kwargs: real(
        place, *args, **kwargs).replace("https://nominatim.openstreetmap.org", fake_base)
    try:
        _fresh(servers, a="ok", b="ok", n="nominatim_empty")
        feedback = Recorder()
        found = osm_algorithms.resolve_place("Konakk", feedback)
        hits = servers.take_hits()
        check.that(found == () and [hit["host"] for hit in hits] == ["n"],
                   f"a plain no-match reached Overpass: {[hit['host'] for hit in hits]}")
        check.that(all(hit["referer"] == osm_algorithms.REFERER for hit in hits),
                   "Nominatim request lacked the Referer")

        _fresh(servers, a="ok", b="ok", n="nominatim_503")
        found = osm_algorithms.resolve_place("Konak", Recorder())
        hits = servers.take_hits()
        overpass = [hit for hit in hits if hit["host"] != "n"]
        check.that(len(overpass) == 1 and overpass[0]["query"].startswith(
            "[out:json][timeout:15][maxsize:134217728];"),
            f"fallback after a Nominatim error: {[h['query'][:50] for h in overpass]}")
        # A plain group: the servers' POSIX engine refuses "(?:" (HTTP 400),
        # and this fake refuses it the same way.
        check.that(overpass and "(?" not in overpass[0]["query"]
                   and '["name"~"(^Konak$)",i]' in overpass[0]["query"],
                   f"place pattern on the wire: {overpass[0]['query'][40:160] if overpass else ''}")
        check.that(found and found[0].name == "Konak" and found[0].source == "overpass",
                   f"fallback candidates: {found}")

        _fresh(servers, a="h502", b="h502", n="nominatim_503")
        try:
            osm_algorithms.resolve_place("Karsiyaka", Recorder())
            message = ""
        except QgsProcessingException as error:
            message = str(error)
        hits = [hit for hit in servers.take_hits() if hit["host"] != "n"]
        check.that(len(hits) == 2 and "The place search did not run." in message,
                   f"failed fallback: {len(hits)} requests, {message!r}")
    finally:
        # Back to the closed local port, never to the public geocoder.
        osm_algorithms.build_search_url = isolated


def _run_task(algorithm, parameters, cancel_after=None, limit=120.0):
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = Recorder()
    state = {}
    task = QgsProcessingAlgRunnerTask(algorithm, parameters, context, feedback)
    task.executed.connect(lambda ok, results: state.update(ok=ok))
    QgsApplication.taskManager().addTask(task)
    started = time.monotonic()
    canceled_at = None
    while "ok" not in state and time.monotonic() - started < limit:
        QCoreApplication.processEvents()
        if cancel_after is not None and canceled_at is None and time.monotonic() - started > cancel_after:
            feedback.cancel()
            canceled_at = time.monotonic()
        time.sleep(0.01)
    for _ in range(20):
        QCoreApplication.processEvents()
    ended = time.monotonic()
    return state.get("ok"), feedback, (ended - canceled_at) if canceled_at else None


def _now_stamp() -> str:
    """A data date five minutes ago: parts a check keeps between its runs
    stay storable (the cache never keeps data more than a week old)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 300))


class ScriptedBackend:
    """A backend for `_fetch_all` without a network or a host board.

    A part is too heavy below `heavy_below_depth`, or wherever `heavy_if`
    says, and is answered with `heavy_kind` (a server timeout unless given);
    `lost_if(request)` makes the server busy for that part.  Every other
    part delivers one node of its own.
    """

    hosts = ("scripted",)

    def __init__(self, heavy_below_depth: int = 0, heavy_if=None, lost_if=None,
                 heavy_kind: str = "") -> None:
        self.heavy_below_depth = heavy_below_depth
        self.heavy_if = heavy_if
        self.lost_if = lost_if
        self.heavy_kind = heavy_kind
        self.asked = []
        self.boxes = []

    def fetch(self, request, job, feedback, part, parts):
        from zero2agent_osm_downloader.core import mirrors, transport

        if not job.budget.take():
            return mirrors.HostResult(mirrors.NO_BUDGET)
        self.asked.append(request.depth)
        self.boxes.append(request.bbox)
        if self.lost_if is not None and self.lost_if(request):
            verdict = transport.Verdict(transport.BUSY, "was busy (HTTP 502)")
            job.failures["scripted"] = verdict
            return mirrors.HostResult(mirrors.LOST, answers=(("scripted", verdict),))
        heavy = (
            self.heavy_if(request) if self.heavy_if is not None
            else request.depth < self.heavy_below_depth
        )
        if heavy:
            kind = self.heavy_kind or transport.SERVER_TIMEOUT
            detail = ("ran out of memory on this query" if kind == transport.SERVER_MEMORY
                      else "ran out of time on this query")
            verdict = transport.Verdict(kind, detail)
            job.failures["scripted"] = verdict
            return mirrors.HostResult(verdict.kind, None, "scripted", verdict.detail,
                                      (("scripted", verdict),))
        south, west, north, east = request.bbox
        node = {"type": "node", "id": int(abs(south * 1e6) + abs(west * 1e3)),
                "lat": (south + north) / 2, "lon": (west + east) / 2, "tags": {"amenity": "cafe"}}
        payload = {"osm3s": {"timestamp_osm_base": _now_stamp()}, "elements": [node]}
        return mirrors.HostResult(transport.OK, payload, "scripted", "", ())


def check_job_rules(check, servers, rt) -> None:
    """R4, R6 and the dense-part rules inside `_fetch_all`, through the
    backend seam: no network, the real cache, parts kept between runs."""
    from zero2agent_osm_downloader.core import transport
    from zero2agent_osm_downloader.core.catalog import TagSpec
    from zero2agent_osm_downloader.core.query import MAX_JOB_REQUESTS, TileRequest, plan_requests
    from zero2agent_osm_downloader.processing import osm_algorithms

    specs = (TagSpec("amenity", "cafe", "point"),)
    plan = plan_requests(specs, BOX25)
    plan9 = plan_requests(specs, BOX9)
    original = osm_algorithms._backends

    def run(backend, tiles):
        osm_algorithms._backends = lambda: (backend,)
        feedback = Recorder()
        try:
            return osm_algorithms._fetch_all(tiles, feedback), "", "", feedback
        except QgsProcessingException as error:
            return None, str(error), getattr(error, "category", ""), feedback

    def covering(points):
        """Too heavy wherever a part covers one of these (lat, lon) points."""
        def heavy(request):
            south, west, north, east = request.bbox
            return any(south <= lat <= north and west <= lon <= east for lat, lon in points)
        return heavy

    def delivered(result):
        return None if result is None else (result.total - len(result.missing), result.total)

    try:
        # Out of memory at 100 km² proves the area dense: the first tile
        # teaches it, the rest start small, and the request ceiling holds.
        _fresh(servers)
        backend = ScriptedBackend(heavy_below_depth=1, heavy_kind=transport.SERVER_MEMORY)
        result, _message, _category, feedback = run(backend, plan)
        check.that(backend.asked[:5] == [0, 1, 1, 1, 1] and 0 not in backend.asked[5:],
                   f"the learned depth was not kept: {backend.asked[:12]}")
        check.that(result is not None and result.requests == MAX_JOB_REQUESTS == len(backend.asked),
                   f"the request ceiling: {len(backend.asked)} requests")
        check.that(result is not None and result.total - len(result.missing) == 63
                   and "used all 64 of its requests" in result.gaps,
                   f"budget gaps: {delivered(result)}, {result.gaps if result else ''!r}")
        check.that(feedback.phases().count("split") >= 2, "no split events")

        # A city too heavy for 100 km² parts: the first tile runs out of time
        # and all four of its quarters arrive, which proves it too heavy, so
        # every tile after it starts at 25 km².  One run, one timeout (before,
        # each tile cost a timeout and the sixth stopped at 20 of 27 parts).
        _fresh(servers)
        backend = ScriptedBackend(heavy_below_depth=1)
        result, *_rest = run(backend, plan9)
        check.that(backend.asked == [0] + [1] * 36 and delivered(result) == (36, 36)
                   and result is not None and result.gaps == "",
                   f"a proven timeout split: {backend.asked[:12]} {delivered(result)}")

        # One quarter lost after a timeout split proves nothing: the tiles
        # after it are still asked whole.  (A quarter that runs out of time
        # proves nothing either: N1 B below asks tiles 6-9 whole.)
        first = plan9[0]
        second_quarter = first.split()[1].bbox
        _fresh(servers)
        backend = ScriptedBackend(heavy_if=lambda request: request.bbox == first.bbox,
                                  lost_if=lambda request: request.bbox == second_quarter)
        result, *_rest = run(backend, plan9)
        check.that(backend.asked == [0, 1, 1, 1, 1] + [0] * 8 and delivered(result) == (11, 12),
                   f"a lost quarter taught a depth: {backend.asked} {delivered(result)}")

        # That quarter runs out of time instead: it is split at its first
        # timeout (density is suspected there), which proves nothing even
        # when its four parts arrive, so the tiles after it are asked whole,
        # not pre-split to 6.25 km².
        _fresh(servers)
        backend = ScriptedBackend(
            heavy_if=lambda request: request.bbox in (first.bbox, second_quarter))
        result, *_rest = run(backend, plan9)
        check.that(backend.asked == [0, 1, 1, 2, 2, 2, 2, 1, 1] + [0] * 8
                   and delivered(result) == (15, 15),
                   f"a first-timeout split taught a depth: {backend.asked} {delivered(result)}")

        # N1 A: one block too dense at every size, first in order.  It is
        # marked and skipped, every light part arrives (1.6.0 failed the whole
        # download), and the same download again asks for nothing.
        hot_a = [(38.2005, 26.9005)]
        note = transport.too_dense_note(1)
        _fresh(servers)
        backend = ScriptedBackend(heavy_if=covering(hot_a))
        result, message, *_rest = run(backend, plan9)
        check.that(len(backend.asked) == 17 and delivered(result) == (14, 15)
                   and result is not None and note in result.gaps,
                   f"one dense block: {len(backend.asked)} requests, {delivered(result)} {message!r}")
        backend = ScriptedBackend(heavy_if=covering(hot_a))
        result, message, *_rest = run(backend, plan9)
        check.that(backend.asked == [] and delivered(result) == (14, 15),
                   f"one dense block again: {backend.asked} {delivered(result)} {message!r}")

        # N1 B: two dense blocks in tile 5.  Tiles 6-9 arrive in the first run
        # (1.6.0 stopped at 4 of 15), and Continue asks for nothing.
        hot_b = [(38.2900, 27.0150), (38.2900, 27.0500)]
        _fresh(servers)
        backend = ScriptedBackend(heavy_if=covering(hot_b))
        result, message, *_rest = run(backend, plan9)
        check.that(len(backend.asked) == 17 and delivered(result) == (13, 15)
                   and result is not None and transport.too_dense_note(2) in result.gaps,
                   f"two dense blocks: {len(backend.asked)} requests, {delivered(result)} {message!r}")
        backend = ScriptedBackend(heavy_if=covering(hot_b))
        result, *_rest = run(backend, plan9)
        check.that(backend.asked == [] and delivered(result) == (13, 15),
                   f"two dense blocks, Continue: {backend.asked} {delivered(result)}")

        # Continue skips the marked blocks and fetches the light parts still
        # missing; the saved parts it collects count as data, so one busy part
        # does not stop it as if nothing had arrived.
        busy_6_7 = {plan9[5].bbox, plan9[6].bbox}
        _fresh(servers)
        backend = ScriptedBackend(heavy_if=covering(hot_b),
                                  lost_if=lambda request: request.bbox in busy_6_7)
        result, *_rest = run(backend, plan9)
        check.that(delivered(result) == (9, 15) and len(backend.asked) == 15,
                   f"first run, busy after tile 5: {delivered(result)} {len(backend.asked)}")
        backend = ScriptedBackend(heavy_if=covering(hot_b),
                                  lost_if=lambda request: request.bbox == plan9[5].bbox)
        result, *_rest = run(backend, plan9)
        check.that(backend.boxes == [request.bbox for request in plan9[5:]]
                   and delivered(result) == (12, 15),
                   f"Continue asked {len(backend.boxes)} parts: {delivered(result)}")

        # Timeouts everywhere are overload, not density: the tile, then two
        # of its quarters before anything arrived, and the download stops with
        # the busy sentence; nothing is marked (1.6.0 round 2 sent 21, or 64).
        for tiles in (plan9[:1], plan9):
            _fresh(servers)
            backend = ScriptedBackend(heavy_below_depth=3)
            result, message, category, _feedback = run(backend, tiles)
            check.that(backend.asked == [0, 1, 1] and result is None and category == "busy"
                       and message.startswith("The OpenStreetMap servers are busy. ")
                       and osm_algorithms._cache().count() == 0,
                       f"overload over {len(tiles)} tiles: {backend.asked} {category} {message!r}")

        # At most 6 timeouts a download: three dense spots after data has
        # arrived; the second spot's last part reaches the cap, it is still
        # marked, nothing more is asked, and the third spot is never touched.
        def corner_of(tile):
            south, west, north, east = tile.bbox
            return (south + (north - south) / 64, west + (east - west) / 64)

        spots = [corner_of(plan9[index]) for index in (1, 3, 5)]
        _fresh(servers)
        backend = ScriptedBackend(heavy_if=covering(spots))
        result, message, *_rest = run(backend, plan9)
        heavy_asked = [box for box in backend.boxes if covering(spots)(TileRequest(specs, box, depth=0))]
        third = [box for box in backend.boxes if covering(spots[2:])(TileRequest(specs, box, depth=0))]
        check.that(len(backend.asked) == 14 and len(heavy_asked) == 6 and not third
                   and delivered(result) == (8, 21),
                   f"timeout cap: {len(backend.asked)} requests, {len(heavy_asked)} timeouts, "
                   f"third spot asked {len(third)}, {delivered(result)} {message!r}")
        check.that(result is not None and transport.timeouts_note(6) in result.gaps
                   and transport.too_dense_note(2) in result.gaps,
                   f"cap sentence: {result.gaps if result else ''!r}")
        # Continue: the marked spots are skipped, the third is met with a
        # fresh allowance and marked, every light part arrives.
        backend = ScriptedBackend(heavy_if=covering(spots))
        result, *_rest = run(backend, plan9)
        check.that(delivered(result) == (24, 27),
                   f"Continue after the cap: {delivered(result)}, {len(backend.asked)} requests")
    finally:
        osm_algorithms._backends = original


def check_cancel(check, servers, rt) -> None:
    registry = QgsApplication.processingRegistry()
    _fresh(servers, a="hang", b="hang")
    ok, feedback, after = _run_task(
        registry.createAlgorithmById("zero2agentosm:download_custom_tag"),
        _parameters(BOX1), cancel_after=0.6,
    )
    failed = [event for event in feedback.events if event["phase"] == "failed"]
    check.that(ok is False and after is not None and after < 1.5,
               f"cancel took {after} s (ok={ok})")
    check.that(failed and failed[-1]["category"] == "canceled", f"cancel event: {failed}")
    servers.take_hits()


def _private_mb() -> float:
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    process = ctypes.windll.kernel32.GetCurrentProcess
    process.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi")
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    psapi.GetProcessMemoryInfo(process(), ctypes.byref(counters), counters.cb)
    return counters.PagefileUsage / 1e6


def check_memory_after_failures(check, servers, rt) -> None:
    """R9, measured as the audit did: private bytes over three failed runs."""
    if sys.platform != "win32":
        print("  [SKIP] private-bytes measurement needs Windows", flush=True)
        return
    from zero2agent_osm_downloader.processing.osm_algorithms import DownloadCustomTagAlgorithm

    class DirectRaise(DownloadCustomTagAlgorithm):
        """The pre-1.6 shape: re-raised where it was caught, as 1.5's
        `raise QgsProcessingException(str(exc)) from exc` at the object cap
        did.  The chained exception keeps every frame below it alive; a
        Python subclass escaping, or a fresh exception raised after the
        `except` block, does not (measured on 3.44 and 4.2)."""

        def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
            try:
                return self._run(parameters, context, feedback)
            except QgsProcessingException as exc:
                raise QgsProcessingException(str(exc)) from exc

    registry = QgsApplication.processingRegistry()

    def failed_run(algorithm, thin=True):
        _fresh(servers, a=("many", {"count": 140_000}), b="ok")
        ok, feedback, _after = _run_task(algorithm, _parameters(BOX_MANY))
        check.that(ok is False, "a memory run did not fail")
        if thin:
            failed = [e for e in feedback.events if e["phase"] == "failed"]
            check.that(failed and failed[-1]["category"] == "too_many",
                       f"memory run did not fail on the object cap: {failed}")
        # Nine tiles of 140,000: the cap is crossed by the third, and no
        # fourth request is sent.
        hits = servers.take_hits()
        check.that(len(hits) == 3, f"the object cap let {len(hits)} requests through")

    # Each run holds 420,000 objects when it fails.  The warm-up sets the
    # high-water mark; a run that freed its memory reuses it, a leaking run
    # cannot, so growth over the next runs is the retained memory.
    failed_run(registry.createAlgorithmById("zero2agentosm:download_custom_tag"))
    gc.collect()
    before = _private_mb()
    for _ in range(3):
        failed_run(registry.createAlgorithmById("zero2agentosm:download_custom_tag"))
    gc.collect()
    growth = _private_mb() - before
    print(f"  [MEM] {rt} three failed runs through the thin frame: +{growth:.1f} MB", flush=True)
    check.that(growth < 50, f"three failed runs kept {growth:.0f} MB")
    # Control, last because it leaks: the same failure raised from the body.
    # The first leaking run can still hide in the freed memory above, so two.
    control_before = _private_mb()
    for _ in range(2):
        failed_run(DirectRaise(), thin=False)
    gc.collect()
    control = _private_mb() - control_before
    print(f"  [MEM] {rt} two failed runs raising from the body (control): +{control:.1f} MB",
          flush=True)
    check.that(control > 150, f"the measurement cannot see the leak it guards against (+{control:.0f} MB)")


# ------------------------------------------------------------------ main
def run_checks() -> Check:
    from qgis.core import Qgis
    from zero2agent_osm_downloader.processing import osm_algorithms
    from zero2agent_osm_downloader.tests._offline import isolate_network

    # Every host at a closed local port before anything runs; the fake
    # servers below are on 127.0.0.1 too.
    isolate_network()
    check = Check()
    rt = Qgis.version().split("-")[0]
    servers = FakeServers()
    QgsNetworkAccessManager.setTimeout(QGIS_TIMEOUT_MS)
    original = osm_algorithms.OVERPASS_ENDPOINTS
    osm_algorithms.OVERPASS_ENDPOINTS = (servers.url("a"), servers.url("b"))
    try:
        for step in (check_outages, check_first_host_hangs, check_dense_tile,
                     check_heavy_parts, check_overload_by_timeouts, check_host_errors,
                     check_gaps_and_continue,
                     check_continue_after_split, check_cache_honesty, check_job_rules,
                     check_reset_retry, check_place_search, check_nominatim_spacing,
                     check_cancel, check_memory_after_failures):
            started = time.monotonic()
            try:
                step(check, servers, rt)
            except Exception as error:  # noqa: BLE001 - a crashed step is a failed step
                traceback.print_exc()
                check.failures.append(f"{step.__name__} crashed: {type(error).__name__}: {error}")
            print(f"  ({step.__name__}: {time.monotonic() - started:.1f} s)", flush=True)
    finally:
        osm_algorithms.OVERPASS_ENDPOINTS = original
    return check


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    plugin_dir = Path(__file__).resolve().parent.parent
    if str(plugin_dir.parent) not in sys.path:
        sys.path.insert(0, str(plugin_dir.parent))
    profile = tempfile.TemporaryDirectory(prefix="zero2agent-fetch-")
    app = QgsApplication([], False, profile.name, "external")
    app.initQgis()
    plugins_path = os.path.join(QgsApplication.prefixPath(), "python", "plugins")
    if plugins_path not in sys.path:
        sys.path.append(plugins_path)
    from processing.core.Processing import Processing
    from qgis.analysis import QgsNativeAlgorithms

    QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())
    Processing.initialize()
    from zero2agent_osm_downloader.processing.provider import AgentOsmProvider

    provider = AgentOsmProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    print("=" * 62)
    print(" 02Agent OSM Downloader - network client against a fake server")
    print("=" * 62, flush=True)
    started = time.monotonic()
    ok = False
    try:
        check = run_checks()
        ok = not check.failures
        for failure in check.failures:
            print(f"  [FAIL] {failure}")
        if ok:
            print(f"  [PASS] outages, host errors, splitting and heavy parts, gaps and "
                  f"resume, Continue after a split, cache honesty, identity, place search, "
                  f"Nominatim spacing, cancel and memory checked in "
                  f"{time.monotonic() - started:.0f} s")
    except Exception as error:  # noqa: BLE001 - a crash is a failed suite
        traceback.print_exc()
        print(f"  [FAIL] {type(error).__name__}: {error}")
    print("-" * 62)
    print(f"  {1 if ok else 0}/1 passed")
    sys.stdout.flush()
    sys.stderr.flush()
    # TRAPS 4.6: exit from the frame that holds the application.
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
