"""Preset and custom-tag OSM Processing algorithms."""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QByteArray, QUrl, QUrlQuery, QVariant
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
    QgsProject,
    QgsWkbTypes,
)

from ..core.catalog import GEOMETRY_KINDS, PRESETS, TagSpec
from ..core.query import (
    MAX_RESPONSE_BYTES,
    OVERPASS_ENDPOINTS,
    OVERPASS_TIMEOUT_SECONDS,
    QueryError,
    build_query,
    compact_tags,
    matching_specs,
    normalize_tag,
    normalized_specs,
    validate_payload,
)

USER_AGENT = (
    "02Agent-OSM-Downloader-QGIS/0.1 "
    "(https://github.com/YusufEminoglu/02Agent-OSM-Downloader)"
)
_CACHE: Dict[str, Tuple[float, Dict]] = {}
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
    cached = _CACHE.get(query)
    if cached is not None and time.monotonic() - cached[0] <= _CACHE_TTL_SECONDS:
        feedback.pushInfo("Using the in-session OSM cache.")
        return cached[1]
    _CACHE.pop(query, None)

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
        code = client.post(request, body, False, feedback)
        if code != QgsBlockingNetworkRequest.NoError:
            failures.append("network request failed")
            continue
        reply = client.reply()
        status = reply.attribute(_status_attribute())
        try:
            status_code = int(status)
        except (TypeError, ValueError):
            status_code = 0
        if status_code and not 200 <= status_code < 300:
            failures.append(f"HTTP {status_code}")
            continue
        content = bytes(reply.content())
        if len(content) > MAX_RESPONSE_BYTES:
            raise QgsProcessingException(
                "The OSM response exceeded 64 MB; zoom in and retry."
            )
        try:
            payload = validate_payload(json.loads(content.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, QueryError) as exc:
            failures.append(str(exc))
            continue
        if len(_CACHE) >= _CACHE_LIMIT:
            oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
            _CACHE.pop(oldest, None)
        _CACHE[query] = (time.monotonic(), payload)
        return payload
    detail = failures[-1] if failures else "no server answered"
    raise QgsProcessingException(
        f"All OSM servers failed ({detail}). Zoom in or retry shortly."
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


def _relation_polygon(element: Dict) -> Optional[QgsGeometry]:
    members = element.get("members")
    if not isinstance(members, list):
        return None
    outers: List[QgsGeometry] = []
    inners: List[QgsGeometry] = []
    for member in members:
        if not isinstance(member, dict) or member.get("type") != "way":
            continue
        ring = _closed_ring(member.get("geometry"))
        if not ring:
            continue
        polygon = QgsGeometry.fromPolygonXY([ring])
        if polygon.isEmpty():
            continue
        if member.get("role") == "inner":
            inners.append(polygon)
        else:
            outers.append(polygon)
    if not outers:
        return None
    geometry = QgsGeometry.unaryUnion(outers)
    if inners and not geometry.isEmpty():
        geometry = geometry.difference(QgsGeometry.unaryUnion(inners))
    if geometry.isEmpty():
        return None
    geometry.convertToMultiType()
    return geometry


def _kind_for_element(
    element: Dict, specs: Tuple[TagSpec, ...]
) -> Tuple[str, Tuple[TagSpec, ...]]:
    element_type = element.get("type")
    tags = element.get("tags")
    if element_type == "node":
        matches = matching_specs(tags, specs, "point")
        return ("point", matches) if matches else ("", ())
    if element_type == "relation":
        matches = matching_specs(tags, specs, "polygon")
        return ("polygon", matches) if matches else ("", ())
    if element_type != "way":
        return "", ()
    line_matches = matching_specs(tags, specs, "line")
    polygon_matches = matching_specs(tags, specs, "polygon")
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
        "tourism", "sport", "height", "building_levels", "tags_json",
    ):
        fields.append(QgsField(name, QVariant.String))
    return fields


def _attributes(
    element: Dict,
    preset_id: str,
    theme: str,
    match: TagSpec,
) -> List[str]:
    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
    return [
        str(element.get("id", "")), str(element.get("type", "")),
        str(tags.get("name", "")), preset_id, theme, match.key,
        str(tags.get(match.key, match.value)), str(tags.get("building", "")),
        str(tags.get("highway", "")), str(tags.get("amenity", "")),
        str(tags.get("landuse", "")), str(tags.get("leisure", "")),
        str(tags.get("natural", "")), str(tags.get("railway", "")),
        str(tags.get("public_transport", "")), str(tags.get("tourism", "")),
        str(tags.get("sport", "")), str(tags.get("height", "")),
        str(tags.get("building:levels", "")), compact_tags(tags),
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
    ) -> Tuple[str, str, Tuple[TagSpec, ...]]:
        raise NotImplementedError

    def processAlgorithm(self, parameters, context, feedback):
        preset_id, theme, specs = self._request(parameters, context)
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
            query = build_query(specs, bbox)
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
            kind, matches = _kind_for_element(element, specs)
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
                _attributes(element, preset_id, theme, matches[0])
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
                defaultValue=0,
            )
        )
        self._add_common_parameters()

    def _request(self, parameters, context):
        index = self.parameterAsEnum(parameters, self.PRESET, context)
        if index < 0 or index >= len(PRESETS):
            raise QgsProcessingException("The selected preset is not valid.")
        preset = PRESETS[index]
        return preset.preset_id, preset.group_title, preset.tags


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
        return "custom", "Custom tag", normalized_specs((spec,))
