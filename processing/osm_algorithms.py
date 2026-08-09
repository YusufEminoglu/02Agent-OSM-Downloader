"""Preset and custom-tag OSM Processing algorithms."""
from __future__ import annotations

import json
import threading
import time
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QByteArray, QMetaType, QUrl, QUrlQuery
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import (
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
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterString,
    QgsReferencedRectangle,
    QgsRectangle,
    QgsProject,
    QgsWkbTypes,
)

from ..core.catalog import (
    GEOMETRY_KINDS,
    PRESETS,
    TagSpec,
)
from ..core.places import build_place_query, parse_place_candidates
from ..core.query import (
    MAX_ADVANCED_FILTERS,
    MAX_RESPONSE_BYTES,
    OVERPASS_ENDPOINTS,
    OVERPASS_TIMEOUT_SECONDS,
    QueryError,
    advanced_specs,
    build_query,
    compact_tags,
    matching_specs,
    normalize_tag,
    normalized_specs,
    validate_payload,
)

USER_AGENT = (
    "02Agent-OSM-Downloader-QGIS/0.4.0 "
    "(https://github.com/YusufEminoglu/02Agent-OSM-Downloader)"
)
_CACHE: Dict[str, Tuple[float, Dict]] = {}
_CACHE_LOCK = threading.RLock()
_CACHE_TTL_SECONDS = 15 * 60
_CACHE_LIMIT = 8


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


def _fetch_json(query: str, feedback) -> Dict:
    with _CACHE_LOCK:
        cached = _CACHE.get(query)
        if cached is not None and (
            time.monotonic() - cached[0] > _CACHE_TTL_SECONDS
        ):
            _CACHE.pop(query, None)
            cached = None
    if cached is not None:
        feedback.pushInfo("Using the in-session OSM cache.")
        return cached[1]

    encoded = QUrlQuery()
    encoded.addQueryItem("data", query)
    body = QByteArray(
        encoded.query(
            QUrl.ComponentFormattingOption.FullyEncoded
        ).encode("ascii")
    )
    failures: List[str] = []
    for index, endpoint in enumerate(OVERPASS_ENDPOINTS):
        if feedback.isCanceled():
            raise QgsProcessingException("The OSM download was canceled.")
        host = QUrl(endpoint).host()
        feedback.pushInfo(
            f"Querying {host} ..." if index == 0 else f"Trying mirror {host} ..."
        )
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
            detail = str(error).strip() or "request exception"
            failures.append(f"{host}: {detail}")
            feedback.pushInfo(
                f"{host} failed ({detail}); trying the next OSM mirror."
            )
            continue
        if code != QgsBlockingNetworkRequest.NoError:
            detail = "network request failed"
            error_message = getattr(client, "errorMessage", None)
            if callable(error_message):
                detail = str(error_message()).strip() or detail
            failures.append(f"{host}: {detail}")
            feedback.pushInfo(
                f"{host} failed ({detail}); trying the next OSM mirror."
            )
            continue
        reply = client.reply()
        status = reply.attribute(_status_attribute())
        try:
            status_code = int(status)
        except (TypeError, ValueError):
            status_code = 0
        if status_code and not 200 <= status_code < 300:
            detail = f"HTTP {status_code}"
            failures.append(f"{host}: {detail}")
            feedback.pushInfo(
                f"{host} returned {detail}; trying the next OSM mirror."
            )
            continue
        content = bytes(reply.content())
        if len(content) > MAX_RESPONSE_BYTES:
            raise QgsProcessingException(
                "The OSM response exceeded 64 MB; zoom in and retry."
            )
        try:
            payload = validate_payload(json.loads(content.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, QueryError) as exc:
            detail = str(exc).strip() or "invalid OSM response"
            failures.append(f"{host}: {detail}")
            feedback.pushInfo(
                f"{host} returned an unusable response ({detail}); "
                "trying the next OSM mirror."
            )
            continue
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_LIMIT:
                oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
                _CACHE.pop(oldest, None)
            _CACHE[query] = (time.monotonic(), payload)
        return payload
    detail = "; ".join(failures[-3:]) if failures else "no server answered"
    raise QgsProcessingException(
        "All pinned OSM mirrors failed. Reduce the map extent or retry shortly. "
        f"Details: {detail}"
    )


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
) -> Tuple[str, Tuple[TagSpec, ...]]:
    element_type = element.get("type")
    tags = element.get("tags")
    if element_type == "node":
        matches = matching_specs(tags, specs, "point", match_mode)
        return ("point", matches) if matches else ("", ())
    if element_type == "relation":
        line_matches = matching_specs(tags, specs, "line", match_mode)
        if line_matches:
            return "line", line_matches
        matches = matching_specs(tags, specs, "polygon", match_mode)
        return ("polygon", matches) if matches else ("", ())
    if element_type != "way":
        return "", ()
    line_matches = matching_specs(tags, specs, "line", match_mode)
    polygon_matches = matching_specs(tags, specs, "polygon", match_mode)
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
        return QgsGeometry.fromPolylineXY(points) if len(points) >= 2 else None
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
    for name in (
        "osm_id", "osm_type", "name", "preset_id", "theme",
        "query_key", "query_value", "building", "highway", "amenity",
        "landuse", "leisure", "natural", "railway", "public_transport",
        "route", "tourism", "sport", "height", "building_levels", "tags_json",
        "matched_tags",
    ):
        fields.append(QgsField(name, QMetaType.Type.QString))
    return fields


def _attributes(
    element: Dict,
    preset_id: str,
    theme: str,
    matches: Tuple[TagSpec, ...],
) -> List[str]:
    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
    first_match = matches[0]
    matched_tags = {
        match.key: str(tags.get(match.key, match.value))
        for match in matches
    }
    return [
        str(element.get("id", "")), str(element.get("type", "")),
        str(tags.get("name", "")), preset_id, theme, first_match.key,
        str(tags.get(first_match.key, first_match.value)), str(tags.get("building", "")),
        str(tags.get("highway", "")), str(tags.get("amenity", "")),
        str(tags.get("landuse", "")), str(tags.get("leisure", "")),
        str(tags.get("natural", "")), str(tags.get("railway", "")),
        str(tags.get("public_transport", "")), str(tags.get("tourism", "")),
        str(tags.get("route", "")), str(tags.get("sport", "")),
        str(tags.get("height", "")),
        str(tags.get("building:levels", "")), compact_tags(tags),
        json.dumps(matched_tags, ensure_ascii=False, sort_keys=True),
    ]


class _BaseDownloadAlgorithm(QgsProcessingAlgorithm):
    EXTENT = "EXTENT"
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
            "mirrors. The extent is limited to 100 km². No raw query, URL, "
            "path, API key, or external dependency is accepted."
        )

    def _add_common_parameters(self) -> None:
        self.addParameter(
            QgsProcessingParameterExtent(self.EXTENT, "Download extent")
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

    def processAlgorithm(self, parameters, context, feedback):
        preset_id, theme, specs, match_mode = self._request(parameters, context)
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
            query = build_query(specs, bbox, match_mode)
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc

        fields = _fields()
        sink_specs = {
            "point": (
                self.OUTPUT_POINTS, QgsWkbTypes.Type.Point,
            ),
            "line": (
                self.OUTPUT_LINES, QgsWkbTypes.Type.LineString,
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
        payload = _fetch_json(query, feedback)
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
            kind, matches = _kind_for_element(element, specs, match_mode)
            if not kind or not matches:
                continue
            geometry = _geometry(element, kind)
            if geometry is None or geometry.isEmpty():
                continue
            if to_target is not None and geometry.transform(to_target) != 0:
                continue
            feature = QgsFeature(fields)
            feature.setGeometry(geometry)
            feature.setAttributes(
                _attributes(element, preset_id, theme, matches)
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
    ALGORITHM_NAME = "download_place"
    DISPLAY_NAME = "Download curated OSM datasets for a named place"

    def initAlgorithm(self, _configuration=None) -> None:
        self.addParameter(
            QgsProcessingParameterString(
                self.PLACE,
                "Place or administrative name",
            )
        )
        super().initAlgorithm(_configuration)

    def processAlgorithm(self, parameters, context, feedback):
        place = self.parameterAsString(parameters, self.PLACE, context).strip()
        if not place:
            raise QgsProcessingException("Enter a place or administrative name.")
        try:
            payload = _fetch_json(build_place_query(place), feedback)
            candidates = parse_place_candidates(payload, place)
        except (QueryError, ValueError) as exc:
            raise QgsProcessingException(str(exc)) from exc
        if not candidates:
            raise QgsProcessingException(
                f"No administrative place matched '{place}'. Try a fuller name."
            )
        candidate = candidates[0]
        south, west, north, east = candidate.bbox
        feedback.pushInfo(
            f"Resolved place: {candidate.label}"
            + (f" (admin level {candidate.admin_level})" if candidate.admin_level else "")
        )
        working = dict(parameters)
        working[self.EXTENT] = QgsReferencedRectangle(
            QgsRectangle(west, south, east, north),
            QgsCoordinateReferenceSystem("EPSG:4326"),
        )
        return super().processAlgorithm(working, context, feedback)


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

    def _request(self, parameters, context):
        mode_index = self.parameterAsEnum(parameters, self.MATCH_MODE, context)
        if mode_index not in (0, 1):
            raise QgsProcessingException("The tag match mode is not valid.")
        match_mode = ("any", "all")[mode_index]

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
            )
        except QueryError as exc:
            raise QgsProcessingException(str(exc)) from exc
        return "advanced", "Advanced query", specs, match_mode
