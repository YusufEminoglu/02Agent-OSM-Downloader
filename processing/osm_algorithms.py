"""Preset and custom-tag OSM Processing algorithms."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QByteArray, QMetaType, QUrl, QUrlQuery
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import (
    QgsApplication,
    QgsBlockingNetworkRequest,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterString,
    QgsReferencedRectangle,
    QgsRectangle,
    QgsProject,
    QgsWkbTypes,
)

from ..core.cache import QueryCache
from ..core.catalog import (
    GEOMETRY_KINDS,
    PRESETS,
    TagSpec,
)
from ..core.geocoding import (
    NOMINATIM_MIN_INTERVAL_SECONDS,
    GeocodeError,
    build_search_url,
    parse_geocode_results,
)
from ..core.places import (
    PlaceCandidate,
    build_place_query,
    parse_place_candidates,
)
from ..core.schema import FIELD_NAMES, feature_attributes
from ..core.query import (
    MAX_ADVANCED_FILTERS,
    MAX_BBOX_AREA_KM2,
    MAX_RESPONSE_BYTES,
    MAX_TILE_AREA_KM2,
    MAX_TILES,
    OVERPASS_ENDPOINTS,
    OVERPASS_TIMEOUT_SECONDS,
    QueryError,
    advanced_specs,
    bbox_area_km2,
    build_area_query,
    build_queries,
    excludes_tag,
    matching_specs,
    merge_elements,
    normalize_tag,
    normalized_specs,
    validate_area_id,
    validate_payload,
)

USER_AGENT = (
    "02Agent-OSM-Downloader-QGIS/1.2.0 "
    "(https://github.com/YusufEminoglu/02Agent-OSM-Downloader)"
)
_CACHE_TTL_SECONDS = 60 * 60
_CACHE_LIMIT = 300
_QUERY_CACHE: Optional[QueryCache] = None
_QUERY_CACHE_LOCK = threading.RLock()
# Overpass asks clients to leave a gap between consecutive requests.  A tiled
# job is the only path here that issues several in a row, so it pauses between
# tiles instead of hammering one mirror.
_TILE_PAUSE_SECONDS = 1.0
# A transient failure (a dropped connection, a 5xx) gets one retry against the
# same mirror before moving on; a definitive one (4xx, an unusable body) does
# not, since a mirror that already answered clearly will not change its mind.
_MIRROR_RETRY_DELAY_SECONDS = 1.2


def _cache() -> QueryCache:
    """Return the process-wide persistent Overpass response cache.

    Resolved lazily against the real QGIS profile directory rather than at
    import time, so importing this module never touches disk on its own —
    only an actual fetch does (see docs/TRAPS.md 4.6 on `QgsSettings`
    blocking against an uninitialised profile).
    """
    global _QUERY_CACHE
    with _QUERY_CACHE_LOCK:
        if _QUERY_CACHE is None:
            db_path = (
                Path(QgsApplication.qgisSettingsDirPath())
                / "zero2agent_osm_downloader"
                / "overpass_cache.sqlite3"
            )
            _QUERY_CACHE = QueryCache(
                db_path, ttl_seconds=_CACHE_TTL_SECONDS, limit=_CACHE_LIMIT
            )
        return _QUERY_CACHE


def _cached(query: str) -> Optional[Dict]:
    """Return a live cache entry for this exact query, dropping stale ones."""
    return _cache().get(query)


def _known_header(name: str):
    enum = getattr(QNetworkRequest, "KnownHeaders", None)
    return getattr(enum, name) if enum is not None else getattr(
        QNetworkRequest, name
    )


def _status_attribute():
    enum = getattr(QNetworkRequest, "Attribute", None)
    if enum is not None:
        return enum.HttpStatusCodeAttribute
    return QNetworkRequest.HttpStatusCodeAttribute


def _attempt_mirror(
    endpoint: str, body: QByteArray, feedback
) -> Tuple[Optional[Dict], str, bool]:
    """Try one OSM mirror once.

    Returns `(payload, failure_detail, transient)`. `transient` marks a
    failure worth retrying against the same mirror — a network error or a
    5xx — as opposed to a 4xx or an unusable body, which will not change on
    a second try.
    """
    request = QNetworkRequest(QUrl(endpoint))
    if hasattr(request, "setTransferTimeout"):
        request.setTransferTimeout((OVERPASS_TIMEOUT_SECONDS + 5) * 1000)
    request.setHeader(
        _known_header("ContentTypeHeader"),
        "application/x-www-form-urlencoded",
    )
    request.setRawHeader(b"Accept", b"application/json")
    request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
    client = QgsBlockingNetworkRequest()
    try:
        code = client.post(request, body, False, feedback)
    except Exception as error:  # noqa: BLE001 - mirror fallback boundary
        return None, str(error).strip() or "request exception", True
    if code != QgsBlockingNetworkRequest.NoError:
        detail = "network request failed"
        error_message = getattr(client, "errorMessage", None)
        if callable(error_message):
            detail = str(error_message()).strip() or detail
        return None, detail, True
    reply = client.reply()
    status = reply.attribute(_status_attribute())
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 0
    if status_code and not 200 <= status_code < 300:
        return None, f"HTTP {status_code}", 500 <= status_code < 600
    content = bytes(reply.content())
    if len(content) > MAX_RESPONSE_BYTES:
        raise QgsProcessingException(
            "The OSM response exceeded 64 MB; zoom in and retry."
        )
    try:
        payload = validate_payload(json.loads(content.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, QueryError) as exc:
        return None, str(exc).strip() or "invalid OSM response", False
    return payload, "", False


def _fetch_json(query: str, feedback) -> Dict:
    cached = _cached(query)
    if cached is not None:
        feedback.pushInfo("Using the cached OSM response.")
        return cached

    encoded = QUrlQuery()
    encoded.addQueryItem("data", query)
    body = QByteArray(
        encoded.query(
            QUrl.ComponentFormattingOption.FullyEncoded
        ).encode("ascii")
    )
    failures: List[str] = []
    for index, endpoint in enumerate(OVERPASS_ENDPOINTS):
        host = QUrl(endpoint).host()
        for attempt in range(2):
            if feedback.isCanceled():
                raise QgsProcessingException("The OSM download was canceled.")
            if attempt == 0:
                feedback.pushInfo(
                    f"Querying {host} ..." if index == 0
                    else f"Trying mirror {host} ..."
                )
            else:
                feedback.pushInfo(f"{host} had a transient error; retrying once ...")
                time.sleep(_MIRROR_RETRY_DELAY_SECONDS)
            payload, detail, transient = _attempt_mirror(endpoint, body, feedback)
            if payload is not None:
                _cache().set(query, payload)
                return payload
            failures.append(f"{host}: {detail}")
            if not (transient and attempt == 0):
                feedback.pushInfo(
                    f"{host} failed ({detail}); trying the next OSM mirror."
                )
                break
    detail = "; ".join(failures[-3:]) if failures else "no server answered"
    raise QgsProcessingException(
        "All pinned OSM mirrors failed. Reduce the map extent or retry shortly. "
        f"Details: {detail}"
    )


_NOMINATIM_LOCK = threading.RLock()
_NOMINATIM_LAST_REQUEST = 0.0


def _prepared_request(url: str) -> QNetworkRequest:
    request = QNetworkRequest(QUrl(url))
    if hasattr(request, "setTransferTimeout"):
        request.setTransferTimeout((OVERPASS_TIMEOUT_SECONDS + 5) * 1000)
    request.setRawHeader(b"Accept", b"application/json")
    request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
    return request


def _reply_status(reply) -> int:
    try:
        return int(reply.attribute(_status_attribute()))
    except (TypeError, ValueError):
        return 0


def _geocode(place: str, feedback) -> Tuple[PlaceCandidate, ...]:
    """Look a place up with Nominatim, honouring its one-request-per-second rule."""
    global _NOMINATIM_LAST_REQUEST

    url = build_search_url(place)
    with _NOMINATIM_LOCK:
        elapsed = time.monotonic() - _NOMINATIM_LAST_REQUEST
        if elapsed < NOMINATIM_MIN_INTERVAL_SECONDS:
            time.sleep(NOMINATIM_MIN_INTERVAL_SECONDS - elapsed)
        _NOMINATIM_LAST_REQUEST = time.monotonic()

    client = QgsBlockingNetworkRequest()
    code = client.get(_prepared_request(url), False, feedback)
    if code != QgsBlockingNetworkRequest.NoError:
        detail = "network request failed"
        error_message = getattr(client, "errorMessage", None)
        if callable(error_message):
            detail = str(error_message()).strip() or detail
        raise GeocodeError(f"Nominatim did not answer ({detail}).")
    reply = client.reply()
    status = _reply_status(reply)
    if status and not 200 <= status < 300:
        raise GeocodeError(f"Nominatim returned HTTP {status}.")
    content = bytes(reply.content())
    if len(content) > MAX_RESPONSE_BYTES:
        raise GeocodeError("The Nominatim response was unexpectedly large.")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeocodeError("Nominatim returned an unreadable response.") from exc
    return parse_geocode_results(payload, place)


def resolve_place(place: str, feedback) -> Tuple[PlaceCandidate, ...]:
    """Resolve a place name, preferring Nominatim over the Overpass search.

    Nominatim understands free-form text and ranks worldwide results, so it
    is tried first.  The original Overpass administrative search stays as the
    fallback, which keeps place resolution working whenever the geocoder is
    unreachable or rate-limits the client.
    """
    try:
        candidates = _geocode(place, feedback)
        if candidates:
            feedback.pushInfo(
                f"Nominatim matched {len(candidates)} place(s) for '{place}'."
            )
            return candidates
        feedback.pushInfo(
            f"Nominatim found no match for '{place}'; "
            "falling back to the OSM administrative search."
        )
    except (GeocodeError, ValueError) as error:
        feedback.pushInfo(
            f"{error} Falling back to the OSM administrative search."
        )
    payload = _fetch_json(build_place_query(place), feedback)
    return parse_place_candidates(payload, place)


def _fetch_all(
    queries: Tuple[str, ...],
    feedback,
    start_progress: float = 5.0,
    end_progress: float = 40.0,
) -> Dict:
    """Run every tile of a request and merge the responses.

    A single-tile request behaves exactly as before.  A tiled request reports
    per-tile progress, stays cancellable between tiles, and merges the
    responses so an object straddling a seam appears once. A tile that
    exhausts every mirror is skipped rather than aborting the whole job — a
    large extent split into a dozen requests should not throw away eleven
    successful tiles because a public Overpass mirror timed out once; the
    caller is told which tiles were skipped and a retry can pick them up from
    where it left off, since every tile that did succeed is already in the
    persistent cache.
    """
    total = len(queries)
    if total == 1:
        return _fetch_json(queries[0], feedback)

    feedback.pushInfo(
        f"The extent needs {total} bounded OSM requests. "
        "Downloading them in sequence."
    )
    payloads = []
    failed_tiles: List[int] = []
    for index, query in enumerate(queries):
        if feedback.isCanceled():
            raise QgsProcessingException("The OSM download was canceled.")
        feedback.pushInfo(f"Tile {index + 1} of {total} ...")
        try:
            payloads.append(_fetch_json(query, feedback))
        except QgsProcessingException:
            if feedback.isCanceled():
                raise
            failed_tiles.append(index + 1)
            feedback.reportError(
                f"Tile {index + 1} of {total} failed after every mirror and "
                "is being skipped.",
                fatalError=False,
            )
        feedback.setProgress(
            start_progress
            + (end_progress - start_progress) * (index + 1) / total
        )
        # Only pause before a tile that will actually hit the network.
        if index + 1 < total and _cached(queries[index + 1]) is None:
            time.sleep(_TILE_PAUSE_SECONDS)
    if not payloads:
        raise QgsProcessingException(
            f"All {total} tiled OSM requests failed. Reduce the map extent "
            "or retry shortly."
        )
    try:
        merged = merge_elements(payloads)
    except QueryError as exc:
        raise QgsProcessingException(str(exc)) from exc
    if failed_tiles:
        tile_list = ", ".join(str(number) for number in failed_tiles)
        feedback.pushInfo(
            f"{len(failed_tiles)} of {total} tiles failed and were skipped "
            f"(tile {tile_list}); the result may be missing data in that "
            "area. Retry to fill the gap — the tiles that already succeeded "
            "are cached and will not be re-downloaded."
        )
    feedback.pushInfo(
        f"Merged {len(payloads)} of {total} tiles into "
        f"{len(merged['elements']):,} OSM objects."
    )
    return merged


def _points(coordinates: object) -> List[QgsPointXY]:
    if not isinstance(coordinates, list):
        return []
    result: List[QgsPointXY] = []
    for coordinate in coordinates:
        if not isinstance(coordinate, dict):
            return []
        try:
            result.append(
                QgsPointXY(
                    float(coordinate["lon"]),
                    float(coordinate["lat"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            return []
    return result


def _closed_ring(coordinates: object) -> List[QgsPointXY]:
    ring = _points(coordinates)
    if len(ring) < 3:
        return []
    if ring[0] != ring[-1]:
        ring.append(QgsPointXY(ring[0]))
    return ring if len(ring) >= 4 else []


def _member_rings(members: object, role: str) -> List[List[QgsPointXY]]:
    """Join fragmented Overpass relation members into closed rings.

    Multipolygon relations commonly split one boundary across several open
    member ways. Treating every member as a standalone polygon silently drops
    those buildings and land-use areas, so assemble matching endpoints first.
    """
    if not isinstance(members, list):
        return []
    segments = []
    for member in members:
        if (
            not isinstance(member, dict)
            or member.get("type") != "way"
            or str(member.get("role") or "outer") != role
        ):
            continue
        points = _points(member.get("geometry"))
        if len(points) >= 2:
            segments.append(points)

    endpoints: Dict[Tuple[float, float], List[int]] = {}
    for index, segment in enumerate(segments):
        for point in (segment[0], segment[-1]):
            key = (point.x(), point.y())
            endpoints.setdefault(key, []).append(index)
    unused = set(range(len(segments)))

    def take_segment(point: QgsPointXY) -> Optional[int]:
        candidates = endpoints.get((point.x(), point.y()), [])
        while candidates:
            candidate = candidates.pop()
            if candidate in unused:
                unused.remove(candidate)
                return candidate
        return None

    rings: List[List[QgsPointXY]] = []
    while unused:
        first = unused.pop()
        chain = list(segments[first])
        while chain[0] != chain[-1]:
            index = take_segment(chain[-1])
            attach_to_end = index is not None
            if index is None:
                index = take_segment(chain[0])
            if index is None:
                break
            segment = segments[index]
            if attach_to_end and chain[-1] == segment[0]:
                chain.extend(segment[1:])
            elif attach_to_end:
                chain.extend(reversed(segment[:-1]))
            elif chain[0] == segment[-1]:
                chain = segment[:-1] + chain
            else:
                chain = list(reversed(segment[1:])) + chain
        if len(chain) >= 4 and chain[0] == chain[-1]:
            rings.append(chain)
    return rings


def _relation_polygon(element: Dict) -> Optional[QgsGeometry]:
    members = element.get("members")
    if not isinstance(members, list):
        return None
    outers = [
        QgsGeometry.fromPolygonXY([ring])
        for ring in _member_rings(members, "outer")
    ]
    inners = [
        QgsGeometry.fromPolygonXY([ring])
        for ring in _member_rings(members, "inner")
    ]
    outers = [geometry for geometry in outers if not geometry.isEmpty()]
    inners = [geometry for geometry in inners if not geometry.isEmpty()]
    if not outers:
        return None
    geometry = QgsGeometry.unaryUnion(outers)
    if inners and not geometry.isEmpty():
        geometry = geometry.difference(QgsGeometry.unaryUnion(inners))
    if geometry.isEmpty():
        return None
    geometry.convertToMultiType()
    return geometry


def _relation_line(element: Dict) -> Optional[QgsGeometry]:
    """Join route relation member ways into a multi-line geometry."""
    members = element.get("members")
    if not isinstance(members, list):
        return None
    lines = []
    for member in members:
        if not isinstance(member, dict) or member.get("type") != "way":
            continue
        points = _points(member.get("geometry"))
        if len(points) >= 2:
            lines.append(QgsGeometry.fromPolylineXY(points))
    if not lines:
        return None
    geometry = QgsGeometry.unaryUnion(lines)
    if geometry.isEmpty():
        return None
    geometry.convertToMultiType()
    return geometry


def _kind_for_element(
    element: Dict,
    specs: Tuple[TagSpec, ...],
    match_mode: str = "any",
    value_mode: str = "exact",
) -> Tuple[str, Tuple[TagSpec, ...]]:
    element_type = element.get("type")
    tags = element.get("tags")
    if element_type == "node":
        matches = matching_specs(tags, specs, "point", match_mode, value_mode)
        return ("point", matches) if matches else ("", ())
    if element_type == "relation":
        line_matches = matching_specs(tags, specs, "line", match_mode, value_mode)
        if line_matches:
            return "line", line_matches
        matches = matching_specs(tags, specs, "polygon", match_mode, value_mode)
        return ("polygon", matches) if matches else ("", ())
    if element_type != "way":
        return "", ()
    line_matches = matching_specs(tags, specs, "line", match_mode, value_mode)
    polygon_matches = matching_specs(
        tags, specs, "polygon", match_mode, value_mode
    )
    if polygon_matches and not line_matches:
        return "polygon", polygon_matches
    if line_matches and not polygon_matches:
        return "line", line_matches
    if polygon_matches and _closed_ring(element.get("geometry")):
        return "polygon", polygon_matches
    return ("line", line_matches) if line_matches else ("", ())


def _geometry(element: Dict, kind: str) -> Optional[QgsGeometry]:
    if kind == "point":
        try:
            return QgsGeometry.fromPointXY(
                QgsPointXY(float(element["lon"]), float(element["lat"]))
            )
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "line":
        if element.get("type") == "relation":
            return _relation_line(element)
        points = _points(element.get("geometry"))
        if len(points) < 2:
            return None
        geometry = QgsGeometry.fromPolylineXY(points)
        # Route relations join into a multi-part line, so the line sink is
        # MultiLineString.  A single-part way must be promoted to match it or
        # the provider silently refuses the geometry.
        geometry.convertToMultiType()
        return geometry
    if element.get("type") == "relation":
        return _relation_polygon(element)
    ring = _closed_ring(element.get("geometry"))
    if not ring:
        return None
    geometry = QgsGeometry.fromPolygonXY([ring])
    geometry.convertToMultiType()
    return geometry


def _fields() -> QgsFields:
    fields = QgsFields()
    for name in FIELD_NAMES:
        fields.append(QgsField(name, QMetaType.Type.QString))
    return fields


class _BaseDownloadAlgorithm(QgsProcessingAlgorithm):
    EXTENT = "EXTENT"
    EXCLUDE_KEY = "EXCLUDE_KEY"
    EXCLUDE_VALUE = "EXCLUDE_VALUE"
    OUTPUT_POINTS = "OUTPUT_POINTS"
    OUTPUT_LINES = "OUTPUT_LINES"
    OUTPUT_POLYGONS = "OUTPUT_POLYGONS"
    ALGORITHM_NAME = ""
    DISPLAY_NAME = ""

    def name(self) -> str:
        return self.ALGORITHM_NAME

    def displayName(self) -> str:
        return self.DISPLAY_NAME

    def group(self) -> str:
        return "OSM acquisition"

    def groupId(self) -> str:
        return "osm_acquisition"

    def tags(self) -> List[str]:
        return [
            "osm",
            "openstreetmap",
            "overpass",
            "agent",
            "smartmodeler",
            "download",
            "urban analysis",
            "temporary layers",
        ]

    def helpUrl(self) -> str:
        return (
            "https://github.com/YusufEminoglu/02Agent-OSM-Downloader/"
            "blob/main/AGENT_PROTOCOL.md"
        )

    def createInstance(self):
        return type(self)()

    def shortHelpString(self) -> str:
        return (
            "Downloads a bounded OSM request through three pinned Overpass "
            f"mirrors. One request covers {MAX_TILE_AREA_KM2:,.0f} km²; a wider "
            "extent is split into tiled requests and merged, up to "
            f"{MAX_BBOX_AREA_KM2:,.0f} km² and {MAX_TILES} requests per "
            "download. An optional exclude key/value drops matching features "
            "from the result after it is fetched. No raw query, URL, path, "
            "API key, or external dependency is accepted."
        )

    def _add_common_parameters(self) -> None:
        self.addParameter(
            QgsProcessingParameterExtent(self.EXTENT, "Download extent")
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.EXCLUDE_KEY,
                "Exclude OSM tag key (optional)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.EXCLUDE_VALUE,
                "Exclude OSM tag value (blank or * means any value)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POINTS, "OSM points"
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_LINES, "OSM lines"
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POLYGONS, "OSM polygons"
            )
        )

    def _request(
        self, parameters, context
    ) -> Tuple[str, str, Tuple[TagSpec, ...], str]:
        raise NotImplementedError

    def _value_mode(self, parameters, context) -> str:
        """The tag-value matching mode for this run; only Advanced Query offers regex."""
        return "exact"

    def _exclude_tag(self, parameters, context) -> Tuple[str, str]:
        key = self.parameterAsString(parameters, self.EXCLUDE_KEY, context).strip()
        if not key:
            return "", ""
        value = self.parameterAsString(
            parameters, self.EXCLUDE_VALUE, context
        ).strip()
        try:
            return normalize_tag(key, value)
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc

    def _build_queries(
        self,
        specs: Tuple[TagSpec, ...],
        bbox: Tuple[float, float, float, float],
        match_mode: str,
        feedback,
        value_mode: str = "exact",
    ) -> Tuple[str, ...]:
        """Return every bounded request needed to cover the extent."""
        area = bbox_area_km2(*bbox)
        queries = build_queries(specs, bbox, match_mode, value_mode=value_mode)
        feedback.pushInfo(
            f"Request extent: {area:,.1f} km² "
            f"({len(queries)} bounded OSM request"
            f"{'s' if len(queries) != 1 else ''})."
        )
        return queries

    def processAlgorithm(self, parameters, context, feedback):
        preset_id, theme, specs, match_mode = self._request(parameters, context)
        value_mode = self._value_mode(parameters, context)
        exclude_key, exclude_value = self._exclude_tag(parameters, context)
        extent = self.parameterAsExtent(parameters, self.EXTENT, context)
        extent_crs = self.parameterAsExtentCrs(
            parameters, self.EXTENT, context
        )
        project = context.project() or QgsProject.instance()
        if not extent_crs.isValid() and project is not None:
            extent_crs = project.crs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if not extent_crs.isValid():
            extent_crs = wgs84
        try:
            if extent_crs != wgs84:
                transform = QgsCoordinateTransform(
                    extent_crs, wgs84, context.transformContext()
                )
                wgs_extent = transform.transformBoundingBox(extent)
            else:
                wgs_extent = extent
            bbox = (
                wgs_extent.yMinimum(), wgs_extent.xMinimum(),
                wgs_extent.yMaximum(), wgs_extent.xMaximum(),
            )
            queries = self._build_queries(
                specs, bbox, match_mode, feedback, value_mode
            )
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc

        fields = _fields()
        sink_specs = {
            "point": (
                self.OUTPUT_POINTS, QgsWkbTypes.Type.Point,
            ),
            "line": (
                self.OUTPUT_LINES, QgsWkbTypes.Type.MultiLineString,
            ),
            "polygon": (
                self.OUTPUT_POLYGONS, QgsWkbTypes.Type.MultiPolygon,
            ),
        }
        sinks = {}
        destinations = {}
        for kind, (name, wkb_type) in sink_specs.items():
            sink, destination = self.parameterAsSink(
                parameters, name, context, fields, wkb_type, extent_crs
            )
            if sink is None:
                raise QgsProcessingException(
                    f"QGIS could not create the {kind} output."
                )
            sinks[kind] = sink
            destinations[name] = destination

        feedback.setProgress(5)
        payload = _fetch_all(queries, feedback)
        feedback.setProgress(40)
        to_target = (
            QgsCoordinateTransform(
                wgs84, extent_crs, context.transformContext()
            )
            if extent_crs != wgs84 else None
        )
        counts = {"point": 0, "line": 0, "polygon": 0}
        elements = payload.get("elements", [])
        for index, element in enumerate(elements):
            if feedback.isCanceled():
                raise QgsProcessingException("The OSM download was canceled.")
            kind, matches = _kind_for_element(element, specs, match_mode, value_mode)
            if not kind or not matches:
                continue
            if exclude_key and excludes_tag(
                element.get("tags"), exclude_key, exclude_value
            ):
                continue
            geometry = _geometry(element, kind)
            if geometry is None or geometry.isEmpty():
                continue
            if to_target is not None and geometry.transform(to_target) != 0:
                continue
            feature = QgsFeature(fields)
            feature.setGeometry(geometry)
            feature.setAttributes(
                feature_attributes(element, preset_id, theme, matches)
            )
            if not sinks[kind].addFeature(
                feature, QgsFeatureSink.Flag.FastInsert
            ):
                raise QgsProcessingException("QGIS could not write an OSM feature.")
            counts[kind] += 1
            if index % 250 == 0:
                feedback.setProgress(
                    40 + int(55 * (index + 1) / max(1, len(elements)))
                )
        feedback.pushInfo(
            "Created "
            f"{counts['point']:,} points, {counts['line']:,} lines and "
            f"{counts['polygon']:,} polygons."
        )
        if not any(counts.values()):
            # An empty result is a successful request, so say why it can
            # happen rather than handing back three blank layers in silence.
            feedback.pushInfo(
                "The request succeeded but matched nothing. Either this area "
                "has none of these tags mapped, or the boundary used for the "
                "request covers no data."
            )
        feedback.setProgress(100)
        return destinations


class DownloadPresetAlgorithm(_BaseDownloadAlgorithm):
    PRESET = "PRESET"
    ALGORITHM_NAME = "download_preset"
    DISPLAY_NAME = "Download curated OSM thematic preset"

    def initAlgorithm(self, _configuration=None) -> None:
        self.addParameter(
            QgsProcessingParameterEnum(
                self.PRESET,
                "Thematic preset",
                options=[item.processing_label for item in PRESETS],
                allowMultiple=True,
                defaultValue=[0],
            )
        )
        self._add_common_parameters()

    def _request(self, parameters, context):
        raw_indexes = self.parameterAsEnums(parameters, self.PRESET, context)
        indexes = tuple(dict.fromkeys(int(index) for index in raw_indexes))
        if not indexes:
            fallback = self.parameterAsEnum(parameters, self.PRESET, context)
            indexes = (fallback,)
        if any(index < 0 or index >= len(PRESETS) for index in indexes):
            raise QgsProcessingException("The selected preset is not valid.")
        presets = tuple(PRESETS[index] for index in indexes)
        specs = tuple(tag for preset in presets for tag in preset.tags)
        return "+".join(preset.preset_id for preset in presets), presets[0].group_title, specs, "any"


class DownloadPlaceAlgorithm(DownloadPresetAlgorithm):
    PLACE = "PLACE"
    CLIP = "CLIP"
    AREA_ID = "AREA_ID"
    ALGORITHM_NAME = "download_place"
    DISPLAY_NAME = "Download curated OSM datasets for a named place"
    CLIP_OPTIONS = (
        "Exact mapped boundary of the place",
        "Rectangular extent of the place",
    )

    def __init__(self) -> None:
        super().__init__()
        self._area_id = 0

    def initAlgorithm(self, _configuration=None) -> None:
        self.addParameter(
            QgsProcessingParameterString(
                self.PLACE,
                "Place or administrative name",
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.CLIP,
                "Download boundary",
                options=list(self.CLIP_OPTIONS),
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.AREA_ID,
                "Resolved OSM area id (leave blank to geocode the name)",
                defaultValue="",
                optional=True,
            )
        )
        super().initAlgorithm(_configuration)

    def shortHelpString(self) -> str:
        return (
            "Resolves a place name with the OpenStreetMap Nominatim geocoder "
            "and downloads the selected datasets for it. When the place is a "
            "mapped boundary, the request follows that boundary instead of a "
            "rectangle; otherwise the rectangular extent is split into "
            "bounded tiles as needed."
        )

    def _build_queries(
        self, specs, bbox, match_mode, feedback, value_mode: str = "exact"
    ):
        if not self._area_id:
            return super()._build_queries(
                specs, bbox, match_mode, feedback, value_mode
            )
        # An area carries no size of its own, so the request is filtered by the
        # extent as well.  `build_area_query` enforces the job ceiling on that
        # extent, which is what stops a caller pairing a country-sized area id
        # with a tiny extent and issuing an unbounded request.
        feedback.pushInfo(
            "Clipping the request to the mapped boundary of the place "
            f"({bbox_area_km2(*bbox):,.0f} km² extent, one boundary request)."
        )
        return (
            build_area_query(
                specs, self._area_id, bbox, match_mode, value_mode=value_mode
            ),
        )

    def processAlgorithm(self, parameters, context, feedback):
        place = self.parameterAsString(parameters, self.PLACE, context).strip()
        if not place:
            raise QgsProcessingException("Enter a place or administrative name.")
        clip_index = self.parameterAsEnum(parameters, self.CLIP, context)
        if clip_index < 0 or clip_index >= len(self.CLIP_OPTIONS):
            raise QgsProcessingException("The download boundary is not valid.")
        resolved = self.parameterAsString(
            parameters, self.AREA_ID, context
        ).strip()
        working = dict(parameters)

        if resolved:
            # The caller already chose a specific place, so re-geocoding the
            # name could silently pick a different one.
            try:
                self._area_id = (
                    validate_area_id(resolved) if clip_index == 0 else 0
                )
            except QueryError as exc:
                raise QgsProcessingException(str(exc)) from exc
            feedback.pushInfo(f"Using the boundary already resolved for '{place}'.")
        else:
            try:
                candidates = resolve_place(place, feedback)
            except (QueryError, ValueError) as exc:
                raise QgsProcessingException(str(exc)) from exc
            if not candidates:
                raise QgsProcessingException(
                    f"No place matched '{place}'. Try a fuller name, for "
                    "example 'Konak, Izmir, Turkey'."
                )
            candidate = candidates[0]
            south, west, north, east = candidate.bbox
            detail = f"Resolved place: {candidate.label}"
            if candidate.admin_level:
                detail = f"{detail} (admin level {candidate.admin_level})"
            feedback.pushInfo(f"{detail} — via {candidate.source}.")
            self._area_id = candidate.area_id if clip_index == 0 else 0
            if not self._area_id and clip_index == 0:
                feedback.pushInfo(
                    "This place has no mapped boundary; using its rectangular "
                    "extent instead."
                )
            working[self.EXTENT] = QgsReferencedRectangle(
                QgsRectangle(west, south, east, north),
                QgsCoordinateReferenceSystem("EPSG:4326"),
            )

        try:
            return super().processAlgorithm(working, context, feedback)
        finally:
            self._area_id = 0


class DownloadCustomTagAlgorithm(_BaseDownloadAlgorithm):
    KEY = "KEY"
    VALUE = "VALUE"
    GEOMETRY = "GEOMETRY"
    ALGORITHM_NAME = "download_custom_tag"
    DISPLAY_NAME = "Download custom OSM key/value tag"

    def initAlgorithm(self, _configuration=None) -> None:
        self.addParameter(QgsProcessingParameterString(self.KEY, "OSM tag key"))
        self.addParameter(
            QgsProcessingParameterString(
                self.VALUE,
                "OSM tag value (blank or * means any value)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.GEOMETRY,
                "Geometry type",
                options=["Point", "Line", "Polygon"],
                defaultValue=0,
            )
        )
        self._add_common_parameters()

    def _request(self, parameters, context):
        try:
            key, value = normalize_tag(
                self.parameterAsString(parameters, self.KEY, context),
                self.parameterAsString(parameters, self.VALUE, context),
            )
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc
        index = self.parameterAsEnum(parameters, self.GEOMETRY, context)
        if index < 0 or index >= len(GEOMETRY_KINDS):
            raise QgsProcessingException("The geometry type is not valid.")
        spec = TagSpec(key, value, GEOMETRY_KINDS[index])
        return "custom", "Custom tag", normalized_specs((spec,)), "any"


class DownloadAdvancedQueryAlgorithm(_BaseDownloadAlgorithm):
    MATCH_MODE = "MATCH_MODE"
    GEOMETRY = "GEOMETRY"
    VALUE_MODE = "VALUE_MODE"
    ALGORITHM_NAME = "download_advanced"
    DISPLAY_NAME = "Download structured advanced OSM query"
    GEOMETRY_OPTIONS = (
        "All geometries",
        "Points",
        "Lines",
        "Polygons",
    )

    def initAlgorithm(self, _configuration=None) -> None:
        self.addParameter(
            QgsProcessingParameterEnum(
                self.MATCH_MODE,
                "Tag match mode",
                options=["Match any tag (OR)", "Match all tags (AND)"],
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.GEOMETRY,
                "Geometry scope",
                options=list(self.GEOMETRY_OPTIONS),
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.VALUE_MODE,
                "Match values as regex (~) instead of exact equality",
                defaultValue=False,
            )
        )
        for index in range(1, MAX_ADVANCED_FILTERS + 1):
            self.addParameter(
                QgsProcessingParameterString(
                    f"KEY_{index}",
                    f"OSM tag key {index}",
                    optional=index > 1,
                )
            )
            self.addParameter(
                QgsProcessingParameterString(
                    f"VALUE_{index}",
                    f"OSM tag value {index} (blank or * means any value)",
                    defaultValue="",
                    optional=True,
                )
            )
        self._add_common_parameters()

    def _value_mode(self, parameters, context) -> str:
        return (
            "regex"
            if self.parameterAsBoolean(parameters, self.VALUE_MODE, context)
            else "exact"
        )

    def _request(self, parameters, context):
        mode_index = self.parameterAsEnum(parameters, self.MATCH_MODE, context)
        if mode_index not in (0, 1):
            raise QgsProcessingException("The tag match mode is not valid.")
        match_mode = ("any", "all")[mode_index]
        value_mode = self._value_mode(parameters, context)

        geometry_index = self.parameterAsEnum(parameters, self.GEOMETRY, context)
        geometry_options = (
            GEOMETRY_KINDS,
            ("point",),
            ("line",),
            ("polygon",),
        )
        if geometry_index < 0 or geometry_index >= len(geometry_options):
            raise QgsProcessingException("The geometry scope is not valid.")

        filters = []
        for index in range(1, MAX_ADVANCED_FILTERS + 1):
            key = self.parameterAsString(
                parameters, f"KEY_{index}", context
            ).strip()
            value = self.parameterAsString(
                parameters, f"VALUE_{index}", context
            ).strip()
            if not key:
                if value:
                    raise QgsProcessingException(
                        f"OSM tag value {index} has no corresponding key."
                    )
                continue
            filters.append((key, value))
        try:
            specs = advanced_specs(
                filters,
                geometry_options[geometry_index],
                match_mode,
                value_mode,
            )
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc
        return "advanced", "Advanced query", specs, match_mode
