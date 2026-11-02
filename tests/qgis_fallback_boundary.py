"""QGIS suite: the Postpass fallback (P2) and exact boundaries (B2), offline.

Run under a QGIS Python, from the folder that contains the plugin, with
QGIS_CUSTOM_CONFIG_PATH pointing at a throwaway directory (TRAPS 4.6):

    python-qgis-ltr.bat -m zero2agent_osm_downloader.tests.qgis_fallback_boundary
    python-qgis.bat     -m zero2agent_osm_downloader.tests.qgis_fallback_boundary

Two Overpass hosts, Postpass and Nominatim are fake servers on 127.0.0.1;
everything else is real: the Processing algorithms, QgsBlockingNetworkRequest,
the host board, the cache, GeoPackage output and the dock.  Postpass answers
with real answers saved from postpass.geofabrik.de (tests/fixtures/
postpass_*.json, their data date moved to today so the cache keeps them) and
its errors in the shape its handlers.go writes them; the Nominatim lookup
answers with the real outline of Konak (tests/fixtures/
nominatim_lookup_konak.json).  QGIS's network timeout is 4 s, so a request
waits at most T = 2 s.  No public host is contacted.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsNetworkAccessManager,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingUtils,
    QgsProject,
    QgsRectangle,
    QgsReferencedRectangle,
    QgsSettings,
    QgsVectorLayer,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
QGIS_TIMEOUT_MS = 4000
#: The data date the fake servers report: today, so the cache keeps parts.
FRESH = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 300))
#: (south, west, north, east) of the Konak box the saved Postpass answers
#: were fetched for.
KONAK_BOX = (38.415, 27.125, 38.430, 27.145)
#: One ~96 km² part, which a dense request splits into four of ~24 km².
DENSE_BOX = (38.400, 27.100, 38.490, 27.210)
#: Nine parts.
BOX9 = (38.200, 26.900, 38.460, 27.230)
#: Konak district, relation 1269255, as an Overpass area id; its outline was
#: saved from nominatim.openstreetmap.org on 2026-11-02 (one 257-point ring).
KONAK_AREA = 3_601_269_255
FALLBACK_KEY = "zero2agent_osm_downloader/allow_fallback"
#: amenity=cafe as points, lines and polygons (the saved "cafes" answer).
CAFES = {"MATCH_MODE": 0, "GEOMETRY": 0, "VALUE_MODE": False,
         "KEY_1": "amenity", "VALUE_1": "cafe"}
#: The same with the Postpass fallback turned on (it is off by default).
CAFES_POSTPASS = dict(CAFES, ALLOW_FALLBACK=True)
CAFE_POINTS = {11054359805: "Marache Coffee", 9981858582: "Peppo"}
CAFE_POLYGONS = {872239165: "", 1224700905: "İzmirli"}
BUSY_502 = (
    b"# 502 Gateway Timeout\n\nThis Overpass API server is currently at capacity. "
    b"All query workers are busy\nand the request queue is full.\n"
)
#: PostgreSQL's words, as lib/pq hands them to Postpass.
STATEMENT_TIMEOUT = "pq: canceling statement due to statement timeout"
COLUMN_MISSING = 'pq: column "geom" does not exist'
_ENVELOPE_RE = re.compile(
    r"ST_MakeEnvelope\((-?\d+\.\d+), (-?\d+\.\d+), (-?\d+\.\d+), (-?\d+\.\d+), 4326\)"
)


def _sql_error(message: str) -> bytes:
    """A Postpass SQL error: http.Error writes the message and a newline, and
    HandleInterpreter then writes the message again (handlers.go)."""
    return f"{message}\n{message}".encode("utf-8")


def _postpass_answer(name: str) -> bytes:
    """A saved Postpass answer, verbatim except for today's data date."""
    document = json.loads((FIXTURES / f"postpass_{name}.json").read_text(encoding="utf-8"))
    document["postpass_properties"]["timestamp"] = FRESH
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def _overpass_answer(elements) -> bytes:
    return json.dumps({
        "version": 0.6, "generator": "Overpass API fake",
        "osm3s": {"timestamp_osm_base": FRESH}, "elements": list(elements),
    }).encode("utf-8")


def _envelope_km2(sql: str) -> float:
    """The area of the box a Postpass statement selects from."""
    found = _ENVELOPE_RE.search(sql)
    if found is None:
        return 0.0
    west, south, east, north = (float(value) for value in found.groups())
    return (north - south) * 111.32 * (east - west) * 111.32 * math.cos(math.radians(abs(south)))


# ------------------------------------------------------------------ servers
class Servers:
    """Overpass hosts a and b, Postpass p and Nominatim n, on loopback ports."""

    NAMES = ("a", "b", "p", "n")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.hits = []
        self.modes = {}
        self.ports = {}
        for name in self.NAMES:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(32)
            self.ports[name] = listener.getsockname()[1]
            threading.Thread(target=self._serve, args=(name, listener), daemon=True).start()
            self.set(name, "ok")

    def url(self, name: str, path: str) -> str:
        return f"http://127.0.0.1:{self.ports[name]}{path}"

    def host(self, name: str) -> str:
        return f"127.0.0.1:{self.ports[name]}"

    def set(self, name: str, mode: str, **options) -> None:
        with self.lock:
            self.modes[name] = (mode, options)

    def take_hits(self):
        with self.lock:
            hits, self.hits = self.hits, []
        return hits

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
    def _send(connection, status, reason, body, ctype="application/json") -> None:
        head = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {ctype}",
                f"Content-Length: {len(body)}", "Connection: close", "Cache-Control: no-store"]
        connection.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)

    def _handle(self, name, connection) -> None:
        with contextlib.closing(connection):
            with contextlib.suppress(OSError):
                request = self._read(connection)
                if request is None:
                    return
                method, path, headers, body = request
                text = urllib.parse.parse_qs(body.decode("ascii", "replace")).get("data", [""])[0]
                with self.lock:
                    mode, options = self.modes[name]
                    self.hits.append({
                        "host": name, "method": method, "path": path, "query": text,
                        "referer": headers.get("referer", ""), "t": time.monotonic(),
                    })
                if name == "n":
                    self._nominatim(connection, mode)
                elif name == "p":
                    self._postpass(connection, mode, options, text)
                else:
                    self._overpass(connection, mode, options)

    def _overpass(self, connection, mode, options) -> None:
        with self.lock:
            options["seen"] = options.get("seen", 0) + 1
            seen = options["seen"]
        if "seq" in options:
            steps = options["seq"].split(",")
            mode = steps[min(seen - 1, len(steps) - 1)]
        if mode == "h502":
            self._send(connection, 502, "Bad Gateway", BUSY_502, "text/plain")
        elif mode == "dense":
            # Output_JSON's remark for a query that ran out of server time.
            body = json.dumps({
                "version": 0.6, "osm3s": {"timestamp_osm_base": FRESH}, "elements": [],
                "remark": 'runtime error: Query timed out in "query" at line 3 after 41 seconds.',
            }).encode("utf-8")
            self._send(connection, 200, "OK", body)
        else:
            self._send(connection, 200, "OK", _overpass_answer(options.get("elements", ())))

    def _postpass(self, connection, mode, options, sql) -> None:
        dense = mode == "timeout_above" and _envelope_km2(sql) > float(options.get("km2", 30))
        if dense:
            self._send(connection, 400, "Bad Request", _sql_error(STATEMENT_TIMEOUT), "text/plain")
        elif mode == "bad":
            if options.get("rest_over"):
                # Stands in for the minute the Overpass hosts rest after a
                # 502: they may be asked again for the next part.
                from zero2agent_osm_downloader.core import mirrors

                mirrors.BOARD.reset()
            self._send(connection, 400, "Bad Request", _sql_error(COLUMN_MISSING), "text/plain")
        elif mode == "h503":
            self._send(connection, 503, "Service Unavailable", b"<html>busy</html>", "text/html")
        else:
            self._send(connection, 200, "OK", _postpass_answer(options.get("fixture", "cafes")))

    def _nominatim(self, connection, mode) -> None:
        if mode == "h503":
            self._send(connection, 503, "Service Unavailable", b"<html>busy</html>", "text/html")
        else:
            body = (FIXTURES / "nominatim_lookup_konak.json").read_bytes()
            self._send(connection, 200, "OK", body)


# ------------------------------------------------------------------ harness
class Recorder(QgsProcessingFeedback):
    """Feedback with the dock's live-event hook; keeps the log lines too."""

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

    def said(self, text: str) -> bool:
        return any(text in line for _level, line in self.lines)


class Check:
    def __init__(self) -> None:
        self.failures = []

    def that(self, condition, message) -> None:
        if not condition:
            self.failures.append(message)
            print(f"    CHECK FAILED: {message}", flush=True)


def _extent(box):
    south, west, north, east = box
    return QgsReferencedRectangle(
        QgsRectangle(west, south, east, north), QgsCoordinateReferenceSystem("EPSG:4326")
    )


def _fresh(servers, **modes) -> None:
    """No host resting, no cache, and the servers in the given modes."""
    from zero2agent_osm_downloader.core import mirrors
    from zero2agent_osm_downloader.processing import osm_algorithms

    mirrors.BOARD.reset()
    osm_algorithms.clear_cache()
    for name, mode in modes.items():
        if isinstance(mode, tuple):
            servers.set(name, mode[0], **mode[1])
        else:
            servers.set(name, mode)


def _run(algorithm, parameters, box, servers):
    """Run one download in this thread; what came back, and what was sent."""
    import processing

    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = Recorder()
    servers.take_hits()
    params = dict(parameters)
    params["EXTENT"] = _extent(box)
    for name in ("OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POLYGONS"):
        params[name] = "TEMPORARY_OUTPUT"
    results, error = None, ""
    try:
        results = processing.run(
            f"zero2agentosm:{algorithm}", params,
            context=context, feedback=feedback, is_child_algorithm=True,
        )
    except QgsProcessingException as exc:
        error = str(exc)
    hits = servers.take_hits()
    layers = {}
    if results is not None:
        for kind in ("points", "lines", "polygons"):
            layers[kind] = QgsProcessingUtils.mapLayerFromString(
                results[f"OUTPUT_{kind.upper()}"], context, True
            )
    return SimpleNamespace(
        results=results or {}, error=error, hits=hits, feedback=feedback,
        layers=layers, context=context,
        per_host={name: len([hit for hit in hits if hit["host"] == name]) for name in Servers.NAMES},
    )


def _features(layer):
    """{osm id: feature} of a result layer."""
    return {int(feature["osm_id"]): feature for feature in layer.getFeatures()}


def _utm_area(geometry: QgsGeometry) -> float:
    """Planar area in UTM 35N, the reference the ellipsoidal area is held to."""
    projected = QgsGeometry(geometry)
    projected.transform(QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateReferenceSystem("EPSG:32635"),
        QgsProject.instance(),
    ))
    return projected.area()


# ------------------------------------------------------------------ checks
def check_parameter(check, _servers, _work) -> None:
    """ALLOW_FALLBACK on all four algorithms: optional, off, under Advanced."""
    from qgis.core import Qgis, QgsProcessingParameterDefinition

    flags = getattr(Qgis, "ProcessingParameterFlag", None)
    advanced = flags.Advanced if flags is not None else QgsProcessingParameterDefinition.FlagAdvanced
    registry = QgsApplication.processingRegistry()
    for name in ("download_preset", "download_place", "download_custom_tag", "download_advanced"):
        algorithm = registry.algorithmById(f"zero2agentosm:{name}")
        definition = algorithm.parameterDefinition("ALLOW_FALLBACK") if algorithm else None
        check.that(
            definition is not None
            and definition.type() == "boolean"
            and definition.defaultValue() is False
            and bool(definition.flags() & advanced)
            and definition.description()
            == "If the OpenStreetMap servers are busy, use Geofabrik's Postpass",
            f"{name}: ALLOW_FALLBACK is {definition and definition.toVariantMap()}")
        check.that(definition is not None and bool(
            definition.flags() & (flags.Optional if flags is not None
                                  else QgsProcessingParameterDefinition.FlagOptional)),
            f"{name}: ALLOW_FALLBACK is required, so older models would stop validating")


def check_postpass_delivers(check, servers, work) -> None:
    """(1) Every Overpass host busy: the part comes from Postpass, unchanged."""
    from zero2agent_osm_downloader.core.query import advanced_specs, plan_requests
    from zero2agent_osm_downloader.core.schema import FIELD_NAMES
    from zero2agent_osm_downloader.processing import osm_algorithms

    host_a, host_b, host_p = servers.host("a"), servers.host("b"), servers.host("p")
    _fresh(servers, a="h502", b="h502", p="ok")
    # The fallback is optional and off by default: turned on here.
    run = _run("download_advanced", CAFES_POSTPASS, KONAK_BOX, servers)
    results = run.results
    check.that(run.error == "", f"the fallback run failed: {run.error}")
    check.that(results.get("DATA_SOURCE") == host_p, f"DATA_SOURCE {results.get('DATA_SOURCE')!r}")
    check.that(results.get("TILES_TOTAL") == 1 and results.get("TILES_MISSING") == 0,
               f"parts {results.get('TILES_TOTAL')} / {results.get('TILES_MISSING')}")
    check.that(results.get("OSM_TIMESTAMP") == FRESH, f"data date {results.get('OSM_TIMESTAMP')!r}")
    check.that(run.per_host == {"a": 1, "b": 1, "p": 1, "n": 0}, f"requests per host {run.per_host}")
    sent = [hit for hit in run.hits if hit["host"] == "p"]
    sql = sent[0]["query"] if sent else ""
    check.that(sql.startswith("SELECT p.osm_type, p.osm_id, p.tags, p.geom FROM postpass_point")
               and "ST_MakeEnvelope(27.1250000, 38.4150000, 27.1450000, 38.4300000, 4326)" in sql,
               f"the Postpass statement: {sql[:160]!r}")
    check.that(sent and sent[0]["path"] == "/api/0.2/interpreter" and sent[0]["method"] == "POST",
               f"Postpass request line: {sent[:1]}")
    check.that(all(hit["referer"] == osm_algorithms.REFERER for hit in run.hits),
               "a request did not name the plugin")
    events = run.feedback.events
    switch = [event for event in events if event["phase"] == "switch" and event["host"] == host_p]
    check.that(
        len(switch) == 1 and switch[0]["detail"] == (
            "The OpenStreetMap servers are busy; asking Geofabrik's Postpass "
            "service for the OSM data."),
        f"the switch to Postpass: {switch}")
    order = [(event["phase"], event["host"]) for event in events if event["phase"] in ("switch", "request")]
    check.that(order[-2:] == [("switch", host_p), ("request", host_p)],
               f"Postpass was not asked after the Overpass hosts: {order}")
    done = [event for event in events if event["phase"] == "part_done"]
    check.that(len(done) == 1 and done[0]["host"] == host_p and not done[0]["cached"],
               f"part_done: {done}")
    # The normal schema, with the real values of the saved answer.
    for kind in ("points", "lines", "polygons"):
        names = [field.name() for field in run.layers[kind].fields()]
        check.that(names == list(FIELD_NAMES), f"{kind} schema: {names}")
    points = _features(run.layers["points"])
    polygons = _features(run.layers["polygons"])
    check.that({key: feature["name"] for key, feature in points.items()} == CAFE_POINTS,
               f"points: {sorted(points)}")
    check.that(sorted(polygons) == sorted(CAFE_POLYGONS) and run.layers["lines"].featureCount() == 0,
               f"polygons {sorted(polygons)}, lines {run.layers['lines'].featureCount()}")
    for key, feature in polygons.items():
        reference = _utm_area(feature.geometry())
        check.that(feature["amenity"] == "cafe" and feature["building"] == "yes"
                   and abs(feature["area_m2"] - reference) <= 0.005 * reference,
                   f"polygon {key}: area_m2 {feature['area_m2']} against UTM {reference:.1f}")
    check.that(points[9981858582]["osm_type"] == "node" and polygons[872239165]["osm_type"] == "way",
               "osm_type was not carried over")

    # Cached under the Overpass key, with Postpass as the source.
    request = plan_requests(advanced_specs((("amenity", "cafe"),), ("point", "line", "polygon"), "any"),
                            KONAK_BOX)[0]
    entry = osm_algorithms._cache().entry(request.cache_key())
    check.that(entry is not None and entry.source == host_p,
               f"cache entry: {None if entry is None else entry.source}")
    # The same download again, now with a healthy Overpass: the cache answers.
    servers.set("a", "ok")
    again = _run("download_advanced", CAFES_POSTPASS, KONAK_BOX, servers)
    check.that(again.per_host == {"a": 0, "b": 0, "p": 0, "n": 0}, f"run again asked {again.per_host}")
    check.that(again.results.get("DATA_SOURCE") == host_p
               and _features(again.layers["points"]).keys() == CAFE_POINTS.keys(),
               f"run again: {again.results.get('DATA_SOURCE')!r}")

    # The data source reaches the layer metadata and the GeoPackage file.
    from zero2agent_osm_downloader.core.persist import (
        SOURCE_HISTORY_PREFIX,
        apply_provenance,
        write_geopackage,
    )

    layer = run.layers["points"]
    apply_provenance(layer, results["OSM_TIMESTAMP"], "Cafes", results["DATA_SOURCE"])
    gpkg = str(Path(work) / "postpass.gpkg")
    written = write_geopackage([(layer, "points")], gpkg, "Cafes")
    table = written[0].source().split("layername=")[1]
    reopened = QgsVectorLayer(f"{gpkg}|layername={table}", "reopened", "ogr")
    metadata = reopened.metadata()
    check.that(f"Downloaded from {host_p} with 02Agent OSM Downloader." in metadata.abstract(),
               f"reopened abstract: {metadata.abstract()!r}")
    check.that(f"{SOURCE_HISTORY_PREFIX}{host_p}" in metadata.history()
               and f"OSM data as of {FRESH} (UTC)" in metadata.history(),
               f"reopened history: {metadata.history()}")
    uri = Path(gpkg).resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as connection:
        description = connection.execute(
            "SELECT description FROM gpkg_contents WHERE table_name = ?", (table,)
        ).fetchone()
    check.that(description and host_p in description[0], f"gpkg_contents.description: {description}")
    print(f"    {host_a} and {host_b} busy; {host_p} delivered {len(points)} points and "
          f"{len(polygons)} polygons", flush=True)


def check_fallback_off(check, servers, _work) -> None:
    """(2) ALLOW_FALLBACK left out (off by default) or false: today's busy
    sentence, Postpass never asked."""
    host_a, host_b = servers.host("a"), servers.host("b")
    expected = ("The OpenStreetMap servers are busy. Nothing was downloaded. Try again in "
                f"about a minute. ({host_a}, {host_b}: was at capacity (HTTP 502))")
    for label, parameters in (("left out", CAFES), ("false", dict(CAFES, ALLOW_FALLBACK=False))):
        _fresh(servers, a="h502", b="h502", p="ok")
        run = _run("download_advanced", parameters, KONAK_BOX, servers)
        check.that(run.per_host == {"a": 1, "b": 1, "p": 0, "n": 0},
                   f"ALLOW_FALLBACK {label}: requests per host {run.per_host}")
        check.that(run.error == expected, f"ALLOW_FALLBACK {label}: busy sentence: {run.error!r}")
        check.that(not any(event["phase"] == "switch" and "Postpass" in event["detail"]
                           for event in run.feedback.events),
                   f"ALLOW_FALLBACK {label}: Postpass was announced")
    # The same with the fallback on: the sentence lists Postpass like any host.
    _fresh(servers, a="h502", b="h502", p="h503")
    run = _run("download_advanced", CAFES_POSTPASS, KONAK_BOX, servers)
    check.that(run.per_host == {"a": 1, "b": 1, "p": 1, "n": 0}, f"requests with Postpass busy {run.per_host}")
    check.that(run.error.startswith("The OpenStreetMap servers are busy. ")
               and f"; {servers.host('p')}: was busy (HTTP 503))" in run.error,
               f"sentence with Postpass busy: {run.error!r}")


def check_postpass_split(check, servers, _work) -> None:
    """(3) Postpass runs out of time on a dense part: split when it was the
    only server asked, lost when the others failed in other ways."""
    from zero2agent_osm_downloader.core import mirrors, transport

    host_a, host_b, host_p = servers.host("a"), servers.host("b"), servers.host("p")
    # The Overpass hosts answer 502 to this part, and Postpass runs out of
    # time: one timeout among refusals proves nothing, so the part is lost.
    _fresh(servers, a="h502", b="h502", p=("timeout_above", {"km2": 30}))
    run = _run("download_advanced", CAFES_POSTPASS, DENSE_BOX, servers)
    check.that(run.per_host == {"a": 1, "b": 1, "p": 1, "n": 0}
               and run.error.startswith("The OpenStreetMap servers are busy. "),
               f"a timeout among refusals: {run.per_host} {run.error!r}")
    # The Overpass hosts rest from an earlier download and are not asked:
    # Postpass's timeout is every answer there is, so the part is split.
    _fresh(servers, a="h502", b="h502", p=("timeout_above", {"km2": 30}))
    mirrors.BOARD.strike(host_a, transport.BUSY, "was busy (HTTP 502)")
    mirrors.BOARD.strike(host_b, transport.BUSY, "was busy (HTTP 502)")
    run = _run("download_advanced", CAFES_POSTPASS, DENSE_BOX, servers)
    results = run.results
    check.that(run.error == "" and results.get("TILES_TOTAL") == 4 and results.get("TILES_MISSING") == 0,
               f"dense part: {run.error!r} {results.get('TILES_TOTAL')} / {results.get('TILES_MISSING')}")
    # Postpass rests 30 s after its statement timeout, like any host after a
    # server timeout, and the first quarter waits that out.  By the second
    # quarter the Overpass hosts' 45 s rest is down to 15 s, short enough to
    # wait for, so each is asked once and fails (now resting 90 s); the
    # quarters all come from Postpass.
    check.that(run.per_host == {"a": 1, "b": 1, "p": 5, "n": 0}, f"requests per host {run.per_host}")
    order = [hit["host"] for hit in run.hits]
    check.that(order == ["p", "p", "a", "b", "p", "p", "p"], f"request order {order}")
    sizes = [round(_envelope_km2(hit["query"])) for hit in run.hits if hit["host"] == "p"]
    check.that(sizes[0] > 90 and all(20 < size < 30 for size in sizes[1:]), f"part sizes {sizes}")
    split = [event for event in run.feedback.events if event["phase"] == "split"]
    check.that(len(split) == 1 and split[0]["detail"] == (
        f"{host_p} ran out of time on this query; part 1 is split into 4 smaller parts.")
        and split[0]["kind"] == "server_timeout", f"split event: {split}")
    check.that(not [event for event in run.feedback.events if event["phase"] == "part_lost"],
               "a part was lost")
    check.that(results.get("DATA_SOURCE") == host_p
               and _features(run.layers["points"]).keys() == CAFE_POINTS.keys(),
               f"split result: {results.get('DATA_SOURCE')!r}")


def check_overload_with_postpass(check, servers, _work) -> None:
    """(3b) Every server, Postpass too, times out at every size: overload.

    With the fallback turned on, Postpass is asked as well: the first tile
    on all three, then two of its quarters once a rest is over, and the
    download stops with the busy sentence, marking nothing.
    """
    from zero2agent_osm_downloader.processing import osm_algorithms

    host_a, host_b, host_p = servers.host("a"), servers.host("b"), servers.host("p")
    _fresh(servers, a="dense", b="dense", p=("timeout_above", {"km2": 0}))
    run = _run("download_advanced", CAFES_POSTPASS, BOX9, servers)
    order = [hit["host"] for hit in run.hits]
    check.that(order == ["a", "b", "p", "a", "b"], f"overload with Postpass asked {order}")
    check.that(run.error == (
        "The OpenStreetMap servers are busy. Nothing was downloaded. Try again in about a "
        f"minute. ({host_a}, {host_b}, {host_p}: ran out of time on this query)"),
        f"overload sentence: {run.error!r}")
    check.that(osm_algorithms._cache().count() == 0, "an overloaded download left markers")


def check_postpass_bad_query(check, servers, _work) -> None:
    """(4) Postpass refuses its statement: that part is lost, Postpass is not
    asked again, and the Overpass hosts go on with the other parts."""
    host_a, host_b, host_p = servers.host("a"), servers.host("b"), servers.host("p")
    # The first part, before any data: lost like any part every server
    # failed, which ends the job; the statement is not sent again.
    _fresh(servers, a="h502", b="h502", p="bad")
    run = _run("download_advanced", CAFES_POSTPASS, BOX9, servers)
    check.that(run.per_host == {"a": 1, "b": 1, "p": 1, "n": 0},
               f"a refused statement was asked again or other parts went on: {run.per_host}")
    check.that(run.error.startswith("The OpenStreetMap server rejected the request as invalid. ")
               and run.error.endswith(f"({host_a}, {host_b}: was at capacity (HTTP 502); "
                                      f"{host_p}: rejected the request as invalid (HTTP 400))"),
               f"bad query sentence: {run.error!r}")
    failed = [event for event in run.feedback.events if event["phase"] == "failed"]
    check.that(failed and failed[-1]["category"] == "bad_query", f"failed event: {failed}")
    check.that(run.feedback.said("No more requests are sent for this download"),
               "the stop was not said")

    # Data flowing: part 2 fails on both hosts and Postpass refuses it; the
    # hosts are back for part 3, and when they fail again on part 4 Postpass
    # is no longer asked.  1.6.0 stopped the whole job at the refusal.
    elements = [{"type": "node", "id": 5, "lat": 38.3, "lon": 27.0,
                 "tags": {"amenity": "cafe", "name": "Part one"}}]
    _fresh(servers, a=("ok", {"seq": "ok,h502,ok,h502", "elements": elements}),
           b="h502", p=("bad", {"rest_over": True}))
    run = _run("download_advanced", CAFES_POSTPASS, BOX9, servers)
    results = run.results
    check.that(run.error == "" and results.get("TILES_TOTAL") == 9
               and results.get("TILES_MISSING") == 7 and results.get("DATA_SOURCE") == host_a,
               f"after a refusal: {run.error!r} {results}")
    order = [hit["host"] for hit in run.hits]
    check.that(order == ["a", "a", "b", "p", "a", "a", "b"],
               f"requests after a Postpass refusal: {order}")
    lost = [event["detail"] for event in run.feedback.events if event["phase"] == "part_lost"]
    check.that(lost[:1] == [f"Part 2 of 9 is missing: {host_p} rejected the request as invalid "
                            "(HTTP 400), so it is not asked again during this download."],
               f"lost parts: {lost}")


#: Test features around the real Konak outline, inside CUT_BOX: (type, id,
#: name, [(lon, lat), ...], inside the outline?).  A closed list is a polygon.
CUT_BOX = (38.400, 27.100, 38.460, 27.200)
_RING_IN = [(27.1305, 38.4195), (27.1315, 38.4195), (27.1315, 38.4205),
            (27.1305, 38.4205), (27.1305, 38.4195)]
_RING_OUT = [(27.184, 38.424), (27.186, 38.424), (27.186, 38.426),
             (27.184, 38.426), (27.184, 38.424)]
BOUNDARY_FEATURES = (
    ("node", 101, "Kemeralti", [(27.1310, 38.4200)], True),
    ("node", 102, "Alsancak", [(27.1429, 38.4386)], True),
    ("node", 103, "Bayrakli side", [(27.1850, 38.4480)], False),
    ("node", 104, "Karabaglar side", [(27.1850, 38.4050)], False),
    ("way", 201, "across the edge", [(27.1429, 38.4386), (27.1850, 38.4480)], True),
    ("way", 202, "beyond the edge", _RING_OUT, False),
    ("way", 203, "in Kemeralti", _RING_IN, True),
)


def _boundary_elements():
    elements = []
    for kind, identifier, name, points, _inside in BOUNDARY_FEATURES:
        tags = {"amenity": "cafe", "name": name}
        if kind == "node":
            (lon, lat), = points
            elements.append({"type": "node", "id": identifier, "lat": lat, "lon": lon, "tags": tags})
        else:
            if points[0] == points[-1]:
                tags["building"] = "yes"
            elements.append({"type": "way", "id": identifier, "tags": tags,
                             "geometry": [{"lat": lat, "lon": lon} for lon, lat in points]})
    return elements


def _konak_outline() -> QgsGeometry:
    """The saved Konak outline, read here without the plugin's own parser."""
    entry, = json.loads((FIXTURES / "nominatim_lookup_konak.json").read_text(encoding="utf-8"))
    rings = entry["geojson"]["coordinates"]
    return QgsGeometry.fromPolygonXY(
        [[QgsPointXY(lon, lat) for lon, lat in ring] for ring in rings]
    )


def _shape(kind, points) -> QgsGeometry:
    if kind == "node":
        return QgsGeometry.fromPointXY(QgsPointXY(*points[0]))
    return QgsGeometry.fromPolylineXY([QgsPointXY(*point) for point in points])


def check_boundary_outline(check, servers, _work) -> None:
    """(5) The outline from Nominatim: box requested, features kept against it."""
    from zero2agent_osm_downloader.processing import osm_algorithms

    outline = _konak_outline()
    south, west, north, east = CUT_BOX
    request_box = QgsRectangle(west, south, east, north)
    # The fixture itself: every test feature lies in the requested box, and
    # the real outline puts each where the table says.
    for kind, identifier, _name, points, inside in BOUNDARY_FEATURES:
        geometry = _shape(kind, points)
        check.that(request_box.contains(geometry.boundingBox())
                   and outline.intersects(geometry) == inside,
                   f"feature {identifier} is not where the test says")

    _fresh(servers, a=("ok", {"elements": _boundary_elements()}), b="ok", p="ok", n="ok")
    run = _run("download_advanced", dict(CAFES, AREA_ID=str(KONAK_AREA)), CUT_BOX, servers)
    results = run.results
    check.that(run.error == "", f"outline run failed: {run.error}")
    check.that(run.per_host == {"a": 1, "b": 0, "p": 0, "n": 1}, f"requests per host {run.per_host}")
    lookups = [hit for hit in run.hits if hit["host"] == "n"]
    check.that(lookups and lookups[0]["method"] == "GET" and lookups[0]["path"] == (
        "/lookup?osm_ids=R1269255&format=jsonv2&polygon_geojson=1&polygon_threshold=0.0001")
        and lookups[0]["referer"] == osm_algorithms.REFERER, f"lookup request: {lookups[:1]}")
    query = next((hit["query"] for hit in run.hits if hit["host"] == "a"), "")
    # The outline's own box (38.3930064-38.4508181, 27.0688039-27.1896253)
    # cut by the extent; no area filter for the server to resolve.
    check.that("area(" not in query and "searchArea" not in query
               and "(38.4000000,27.1000000,38.4508181,27.1896253)" in query,
               f"the request: {query[:220]!r}")
    kept = {
        kind: sorted(_features(run.layers[kind])) for kind in ("points", "lines", "polygons")
    }
    check.that(kept == {"points": [101, 102], "lines": [201], "polygons": [203]}, f"kept {kept}")
    # Left out for lying outside the place is the clip working, not a
    # failure: FEATURES_OUTSIDE, an information line, nothing dropped.
    check.that(results.get("FEATURES_OUTSIDE") == 3 and results.get("FEATURES_DROPPED") == 0
               and results.get("NOTICE") == "",
               f"outputs {results.get('FEATURES_OUTSIDE')} / {results.get('FEATURES_DROPPED')} / "
               f"{results.get('NOTICE')!r}")
    said = [(level, line) for level, line in run.feedback.lines if "left out" in line]
    check.that(said == [("info", "3 matched OSM features were left out because they lie outside "
                                 "the boundary of the place.")], f"outside summary: {said}")
    check.that(not any("not written" in line for _level, line in run.feedback.lines),
               "the clip was reported as features not written")
    check.that(run.feedback.said("Keeping only what touches the mapped boundary of the place "
                                 "(44 km² around it, 1 bounded OSM request)."),
               f"planning line: {run.feedback.lines[:3]}")
    done = [event for event in run.feedback.events if event["phase"] == "done"]
    check.that(done and (done[-1]["dropped"], done[-1]["outside"], done[-1]["notice"]) == (0, 3, ""),
               f"done event: {done}")
    # Again: the outline and the part come from the cache, nothing is sent.
    again = _run("download_advanced", dict(CAFES, AREA_ID=str(KONAK_AREA)), CUT_BOX, servers)
    check.that(again.per_host == {"a": 0, "b": 0, "p": 0, "n": 0}, f"second run asked {again.per_host}")
    check.that(again.feedback.said("Using the saved outline of the place.")
               and sorted(_features(again.layers["points"])) == [101, 102],
               "the second run did not reuse the outline")
    # The outline is tested through a prepared engine, with the same answers.
    area = osm_algorithms._RequestedArea(outline, osm_algorithms.DROP_OUTSIDE_EXTENT, False)
    check.that(area._engine is not None, "the outline is not tested through a prepared engine")
    check.that(all(area.touches(_shape(kind, points)) == inside
                   for kind, _id, _name, points, inside in BOUNDARY_FEATURES),
               "the prepared outline answers differently")


def check_boundary_fallback(check, servers, _work) -> None:
    """(6) No outline from Nominatim: the server's area filter, said once."""
    from zero2agent_osm_downloader.processing import osm_algorithms

    _fresh(servers, a=("ok", {"elements": []}), b="ok", p="ok", n="h503")
    box = (38.400, 27.100, 38.460, 27.200)
    run = _run("download_advanced", dict(CAFES, AREA_ID=str(KONAK_AREA)), box, servers)
    check.that(run.error == "", f"area filter run failed: {run.error}")
    check.that(run.per_host == {"a": 1, "b": 0, "p": 0, "n": 1}, f"requests per host {run.per_host}")
    lookups = [hit for hit in run.hits if hit["host"] == "n"]
    check.that(lookups and lookups[0]["path"].startswith("/lookup?osm_ids=R1269255&format=jsonv2"
                                                        "&polygon_geojson=1&polygon_threshold=0.0001"),
               f"lookup request: {lookups[:1]}")
    query = next((hit["query"] for hit in run.hits if hit["host"] == "a"), "")
    check.that("area(3601269255)->.searchArea;" in query
               and "(area.searchArea)(38.4000000,27.1000000,38.4600000,27.2000000)" in query,
               f"the request lost its area filter: {query[:200]!r}")
    told = [line for _level, line in run.feedback.lines if line.startswith("Could not fetch the outline")]
    check.that(told == [
        "Could not fetch the outline of the place: Nominatim answered with HTTP 503. The "
        "OpenStreetMap server clips the request to its own copy of the boundary instead."],
        f"the fallback line: {told}")
    # Nothing came back from the server's own copy of the boundary: the
    # dock's sentence, as NOTICE, on the done event and in the log.
    empty = ("Nothing came back from inside the boundary of this place; the server may not "
             "hold it. Choose Rectangular extent and press Download again.")
    warning = [line for level, line in run.feedback.lines if level == "warning" and "boundary" in line]
    check.that(warning == [empty], f"empty area result: {warning}")
    done = [event for event in run.feedback.events if event["phase"] == "done"]
    check.that(run.results.get("NOTICE") == empty and done and done[-1]["notice"] == empty,
               f"NOTICE {run.results.get('NOTICE')!r}")
    check.that(not osm_algorithms._cache().has(osm_algorithms.build_lookup_url(KONAK_AREA)),
               "a failed lookup was cached")

    # Data from the server's clip: the notice says the outline was not used,
    # with the place's name when the algorithm has one.
    elements = [{"type": "node", "id": 101, "lat": 38.42, "lon": 27.131,
                 "tags": {"amenity": "cafe", "name": "Kemeralti"}}]
    _fresh(servers, a=("ok", {"elements": elements}), b="ok", p="ok", n="h503")
    run = _run("download_advanced", dict(CAFES, AREA_ID=str(KONAK_AREA)), box, servers)
    check.that(run.error == "" and run.results.get("NOTICE") == (
        "The outline of this place could not be fetched, so the server clipped the request "
        "to its own copy of the boundary."), f"outline NOTICE: {run.results.get('NOTICE')!r}")
    _fresh(servers, a=("ok", {"elements": elements}), b="ok", p="ok", n="h503")
    run = _run("download_place", {"PLACE": "Konak, Izmir", "CLIP": 0, "AREA_ID": str(KONAK_AREA),
                                  "PRESET": [_preset_index("healthcare")]}, box, servers)
    check.that(run.error == "" and run.results.get("NOTICE") == (
        "The outline of Konak, Izmir could not be fetched, so the server clipped the request "
        "to its own copy of the boundary."), f"named NOTICE: {run.error!r} {run.results.get('NOTICE')!r}")


def _preset_index(preset_id: str) -> int:
    from zero2agent_osm_downloader.processing.osm_algorithms import PRESET_ENUM_ORDER

    return PRESET_ENUM_ORDER.index(preset_id)


def check_extent_misses_outline(check, servers, _work) -> None:
    """(7) An extent that misses the place's outline: empty layers and a
    warning, as 1.5.1 gave, not a failed run in a batch over many extents."""
    _fresh(servers, a="ok", b="ok", p="ok", n="ok")
    # North-east of Konak's outline (38.393-38.451 N, 27.069-27.190 E).
    away = (38.470, 27.210, 38.490, 27.240)
    run = _run("download_advanced", dict(CAFES, AREA_ID=str(KONAK_AREA)), away, servers)
    results = run.results
    notice = ("The download extent does not overlap the boundary of this place, so nothing "
              "was downloaded.")
    check.that(run.error == "", f"the run failed: {run.error!r}")
    check.that(run.per_host == {"a": 0, "b": 0, "p": 0, "n": 1}, f"requests per host {run.per_host}")
    check.that(all(layer.featureCount() == 0 for layer in run.layers.values()),
               "an extent outside the outline returned features")
    check.that((results.get("TILES_TOTAL"), results.get("TILES_MISSING"), results.get("NOTICE"),
                results.get("FEATURES_DROPPED"), results.get("FEATURES_OUTSIDE"))
               == (0, 0, notice, 0, 0), f"outputs {results}")
    check.that(("warning", notice) in run.feedback.lines, f"warning: {run.feedback.lines[-3:]}")


def check_dock(check, servers, work) -> None:
    """The dock's Postpass checkbox: off by default, kept, passed, and named."""
    from qgis.gui import QgsMapCanvas

    from zero2agent_osm_downloader.dialogs import dock as dockmod
    from zero2agent_osm_downloader.dialogs.dock import AgentOsmDock

    class Iface:
        def __init__(self, canvas):
            self._canvas = canvas

        def mapCanvas(self):  # noqa: N802 - QGIS naming
            return self._canvas

        def activeLayer(self):  # noqa: N802
            return None

    QgsSettings().remove(FALLBACK_KEY)
    canvas = QgsMapCanvas()
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    dock = AgentOsmDock(Iface(canvas))
    box = dock.fallback_check
    check.that(not box.isChecked(), "the Postpass option is on in a new profile")
    check.that(" ".join(box.text().split())
               == "If the OpenStreetMap servers are busy, use Geofabrik's Postpass",
               f"checkbox text: {box.text()!r}")
    check.that(dock.run_group.isAncestorOf(box), "the option is not with the download settings")
    # Turned on: kept in the profile, and a stored on keeps a new dock on.
    box.setChecked(True)
    check.that(dockmod._setting_on(QgsSettings().value(FALLBACK_KEY, False), default=False),
               f"turning the option on was not kept: {QgsSettings().value(FALLBACK_KEY)!r}")
    check.that(AgentOsmDock(Iface(canvas)).fallback_check.isChecked(),
               "a new dock did not keep the stored choice to use Postpass")

    def drive(parameters, label):
        dock._current_label = label
        dock._current_style_scope = ""
        servers.take_hits()
        dock._submit("zero2agentosm:download_custom_tag", parameters, _extent(KONAK_BOX))
        end = time.monotonic() + 60
        while dock._task is not None and time.monotonic() < end:
            QgsApplication.processEvents()
            time.sleep(0.01)
        for _ in range(20):
            QgsApplication.processEvents()
        return dock.status.text(), servers.take_hits()

    host_p = servers.host("p")
    dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("gpkg"))
    gpkg = str(Path(work) / "dock.gpkg")
    dock.gpkg_widget.setFilePath(gpkg)
    _fresh(servers, a="h502", b="h502", p="ok")
    status, hits = drive({"KEY": "amenity", "VALUE": "cafe", "GEOMETRY": 0}, "Dock cafes")
    check.that([hit["host"] for hit in hits] == ["a", "b", "p"], f"dock requests {[h['host'] for h in hits]}")
    check.that(status.startswith("Finished — Points: 2") and status.endswith(f"from {host_p}."),
               f"dock status: {status!r}")
    saved = [layer for layer in QgsProject.instance().mapLayers().values()
             if layer.name() == "Dock cafes — Points"]
    check.that(len(saved) == 1 and saved[0].providerType() == "ogr", f"saved layers {saved}")
    if saved:
        table = saved[0].source().split("layername=")[1]
        reopened = QgsVectorLayer(f"{gpkg}|layername={table}", "reopened", "ogr")
        check.that(f"Downloaded from {host_p} with" in reopened.metadata().abstract(),
                   f"dock metadata: {reopened.metadata().abstract()!r}")

    # Off: kept in the profile, sent as ALLOW_FALLBACK=False, busy sentence.
    box.setChecked(False)
    check.that(not dockmod._setting_on(QgsSettings().value(FALLBACK_KEY, True)),
               f"the choice was not kept: {QgsSettings().value(FALLBACK_KEY)!r}")
    check.that(not AgentOsmDock(Iface(canvas)).fallback_check.isChecked(),
               "a new dock did not read the kept choice")
    dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("temporary"))
    _fresh(servers, a="h502", b="h502", p="ok")
    status, hits = drive({"KEY": "amenity", "VALUE": "cafe", "GEOMETRY": 0}, "Dock cafes off")
    check.that([hit["host"] for hit in hits] == ["a", "b"], f"dock requests with the option off {hits}")
    check.that(status.startswith("The OpenStreetMap servers are busy. ") and host_p not in status,
               f"dock status with the option off: {status!r}")
    QgsSettings().remove(FALLBACK_KEY)
    _check_named_place(check, servers, dock, dockmod)


def _check_named_place(check, servers, dock, dockmod) -> None:
    """The named-place path sends AREA_ID, and the extent note describes the
    request the algorithm then makes: the outline's box, in as many parts."""
    from qgis.PyQt.QtWidgets import QMessageBox

    from zero2agent_osm_downloader.core.geocoding import parse_geocode_results

    konak = parse_geocode_results(
        json.loads((FIXTURES / "nominatim_konak.json").read_text(encoding="utf-8")), "Konak"
    )[0]

    class Done:  # a finished place search, as the dock's task hands it over
        candidates, error_text, error_detail, place = (konak,), "", "", "Konak"

    messages, submitted = [], []

    class Box(QMessageBox):  # a modal box would block this run
        @staticmethod
        def warning(_parent, _title, text, *_args):
            messages.append(str(text))
            return QMessageBox.StandardButton.Ok

        information = critical = question = warning

    original_box = dockmod.QMessageBox
    dockmod.QMessageBox = Box
    try:
        dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("place"))
        task = Done()
        dock._place_task = task
        dock._place_resolved(task, True)
        note = dock.extent_note.text()
        check.that("exact boundary of Konak inside a 68 km² rectangle" in note
                   and note.endswith("1 request"), f"extent note: {note!r}")
        dock._submit = lambda algorithm_id, parameters, extent: submitted.append(
            (algorithm_id, dict(parameters)))
        dock.tabs.setCurrentWidget(dock.download_tab)
        dock.custom_check.setChecked(False)
        dock._select_preset("healthcare")
        dock._download()
    finally:
        dockmod.QMessageBox = original_box
        dock.__dict__.pop("_submit", None)
    check.that(not messages and submitted and submitted[0][0] == "zero2agentosm:download_place"
               and submitted[0][1].get("AREA_ID") == str(KONAK_AREA)
               and submitted[0][1].get("CLIP") == 0,
               f"named place request: {submitted} {messages}")
    if not submitted:
        return
    _fresh(servers, a=("ok", {"elements": []}), b="ok", p="ok", n="ok")
    run = _run("download_place", submitted[0][1], konak.bbox, servers)
    query = next((hit["query"] for hit in run.hits if hit["host"] == "a"), "")
    check.that(run.error == "" and run.per_host == {"a": 1, "b": 0, "p": 0, "n": 1}
               and run.feedback.said("Keeping only what touches the mapped boundary of the "
                                     "place (68 km² around it, 1 bounded OSM request).")
               and "(38.3930064,27.0688039,38.4508181,27.1896253)" in query,
               f"the named-place run: {run.error!r} {run.per_host} {query[:160]!r}")


# ------------------------------------------------------------------ main
def run_checks(work: str) -> Check:
    from zero2agent_osm_downloader.core import geocoding
    from zero2agent_osm_downloader.processing import osm_algorithms
    from zero2agent_osm_downloader.tests._offline import isolate_network

    # Every host at a closed local port and QGIS guarded before anything
    # runs; the hosts exercised here then point at the fakes on 127.0.0.1.
    isolate_network()
    check = Check()
    servers = Servers()
    QgsNetworkAccessManager.setTimeout(QGIS_TIMEOUT_MS)
    redirected = ("OVERPASS_ENDPOINTS", "POSTPASS_ENDPOINT", "build_lookup_url")
    saved = {name: getattr(osm_algorithms, name, None) for name in redirected}
    fake_nominatim = f"http://127.0.0.1:{servers.ports['n']}"
    osm_algorithms.OVERPASS_ENDPOINTS = (
        servers.url("a", "/api/interpreter"), servers.url("b", "/api/interpreter"))
    osm_algorithms.POSTPASS_ENDPOINT = servers.url("p", "/api/0.2/interpreter")
    osm_algorithms.build_lookup_url = lambda area_id: geocoding.build_lookup_url(area_id).replace(
        "https://nominatim.openstreetmap.org", fake_nominatim)
    try:
        for step in (check_parameter, check_postpass_delivers, check_fallback_off,
                     check_postpass_split, check_overload_with_postpass,
                     check_postpass_bad_query, check_boundary_outline,
                     check_boundary_fallback, check_extent_misses_outline, check_dock):
            started = time.monotonic()
            try:
                step(check, servers, work)
            except Exception as error:  # noqa: BLE001 - a crashed step is a failed step
                traceback.print_exc()
                check.failures.append(f"{step.__name__} crashed: {type(error).__name__}: {error}")
            print(f"  ({step.__name__}: {time.monotonic() - started:.1f} s)", flush=True)
    finally:
        for name, value in saved.items():
            setattr(osm_algorithms, name, value)
    return check


def main() -> int:
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    plugin_dir = Path(__file__).resolve().parent.parent
    if str(plugin_dir.parent) not in sys.path:
        sys.path.insert(0, str(plugin_dir.parent))
    profile = tempfile.TemporaryDirectory(prefix="zero2agent-fallback-")
    work = tempfile.TemporaryDirectory(prefix="zero2agent-fallback-work-")
    app = QgsApplication([], True, profile.name, "external")
    app.initQgis()
    plugins_path = os.path.join(QgsApplication.prefixPath(), "python", "plugins")
    if plugins_path not in sys.path:
        sys.path.append(plugins_path)
    from processing.core.Processing import Processing
    from qgis.analysis import QgsNativeAlgorithms

    QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())
    Processing.initialize()
    from zero2agent_osm_downloader.processing.provider import AgentOsmProvider

    # A provider only the registry holds is garbage-collected (qgis_v15_smoke).
    provider = AgentOsmProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    print("=" * 62)
    print(" 02Agent OSM Downloader - Postpass fallback and exact boundaries")
    print("=" * 62, flush=True)
    started = time.monotonic()
    ok = False
    try:
        check = run_checks(work.name)
        ok = not check.failures
        for failure in check.failures:
            print(f"  [FAIL] {failure}")
        if ok:
            print(f"  [PASS] Postpass fallback (off by default), split, refusal, outline clip, "
                  f"area-filter fallback and its notices, an extent off the outline and "
                  f"dock checked in {time.monotonic() - started:.0f} s")
    except Exception as error:  # noqa: BLE001 - a crash is a failed suite
        traceback.print_exc()
        print(f"  [FAIL] {type(error).__name__}: {error}")
    print("-" * 62)
    print(f"  {1 if ok else 0}/1 passed")
    QgsProject.instance().clear()
    sys.stdout.flush()
    sys.stderr.flush()
    # TRAPS 4.6: exit from the frame that holds the application.
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
