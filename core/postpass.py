"""Postpass, Geofabrik's public PostGIS service, as a second source for a tile.

Pure Python by design, like ``core.query``: no QGIS import, so every rule here
is tested without QGIS.  Three parts:

* :func:`render_sql` renders the selection the Overpass path sends for one
  tile -- the same node, way and relation selectors over the same box,
  optionally clipped to the same mapped boundary -- as one PostGIS ``SELECT``
  over the Postpass tables.
* :func:`adapt` turns the GeoJSON Postpass answers into the element shape
  Overpass ``out body geom`` gives, so matching, the layer schema and the
  styling never need to know which server answered.
* :func:`classify` says what one attempt came to, in the classes of
  ``core.transport``, so the host board, the splitting of heavy parts and
  the closing messages treat Postpass like any Overpass host.

Every key, value and pattern enters the statement hex-encoded, as
``convert_from(decode('<hex>', 'hex'), 'UTF8')``, so no text from a preset, a
typed command or the Advanced Query fields can reach SQL syntax; the only
other values are validated floats and integers.

Server contract, read in woodpeck/postpass (``handlers.go``, ``worker.go``)
and woodpeck/postpass-ops (``postpass.lua``, ``SCHEMA.md``) and checked live
on 2026-11-02:

* The statement is POSTed as form field ``data`` and run as
  ``SELECT ST_AsGeoJSON(t.*) FROM (<statement>) AS t``, so it is one query
  with no semicolon.  An SQL error is HTTP 400 with PostgreSQL's message as
  plain text.
* ``postpass_point`` holds tagged nodes (``osm_type`` 'N'); ``postpass_line``
  and ``postpass_polygon`` hold ways ('W') and relations ('R'); ``tags`` is
  jsonb and ``geom`` is EPSG:4326.
* A way is put in ``postpass_polygon`` when it is closed and carries one of
  Postpass's own polygon keys, otherwise in ``postpass_line``.  That split is
  not the plugin's (``core.osm_areas``): an island's closed
  ``natural=coastline`` sits in the polygon table, a closed ``healthcare=*``
  outline in the line table.  So every way or relation selector reads both
  tables, as an Overpass ``way[...]`` reads every way, and the plugin still
  decides line or polygon afterwards.
* Only route, multipolygon and boundary relations are imported, and a
  boundary is stored twice, as rings and as lines.  The import consumes the
  ``type`` tag itself (``grab_tag``, postpass-ops#42), so relation tags arrive
  without it.  The wildcard's relation selector, limited to multipolygon and
  boundary relations, therefore reads ``postpass_polygon`` alone: that is
  where those relations are stored as rings, and routes never are.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from . import transport
from .catalog import GEOMETRY_KINDS, TagSpec
from .query import QueryError, normalized_specs, validate_area_id

#: The one Postpass instance.  Geofabrik runs it and there is no mirror.
POSTPASS_HOST = "postpass.geofabrik.de"
#: The versioned interpreter: the server reports itself as "Postpass API 0.2".
POSTPASS_ENDPOINT = "https://postpass.geofabrik.de/api/0.2/interpreter"

# Rules copied from core.query.  This module imports only that module's
# validation API while it is being changed; tests/test_postpass.py pins every
# copy to core.query, so a change there fails a test here instead of letting
# the two servers answer different questions.
_MATCH_MODES = ("any", "all")
_VALUE_MODES = ("exact", "regex")
_WILDCARD_EXCLUDED_KEYS: Tuple[Tuple[str, str], ...] = (
    ("boundary", "administrative"),
    ("place", ""),
)
_MAX_TILE_AREA_KM2 = 100.0
_MAX_FEATURES = 150_000
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_KM_PER_DEGREE = 111.32
# Overpass derives an area id from an OSM id: relation + 3600000000, way +
# 2400000000 (core.query.validate_area_id accepts only those two ranges).
_RELATION_AREA_OFFSET = 3_600_000_000
_WAY_AREA_OFFSET = 2_400_000_000

_NODE, _WAY, _RELATION = "N", "W", "R"
#: A wildcard relation selector: core.query asks Overpass for multipolygon
#: and boundary relations only (`["type"~"^(multipolygon|boundary)$"]`).
#: The import strips `type`, but it stores exactly those relations as rings
#: in `postpass_polygon` and routes only in `postpass_line`, so reading the
#: polygon table alone is the same filter.  It is a relation in the table.
_AREA_RELATION = "A"
_TABLES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("postpass_point", (_NODE,)),
    ("postpass_line", (_WAY, _RELATION)),
    ("postpass_polygon", (_WAY, _RELATION, _AREA_RELATION)),
)
_STORED_TYPE = {
    _NODE: _NODE, _WAY: _WAY, _RELATION: _RELATION, _AREA_RELATION: _RELATION,
}
_ELEMENT_TYPES = {_NODE: "node", _WAY: "way", _RELATION: "relation"}

# The statement is joined from these constant fragments and the hex literals
# below -- never formatted, concatenated with `+` or interpolated -- which is
# also what keeps Bandit B608 quiet (docs/TRAPS.md 1.8).
_COLUMNS = "SELECT p.osm_type, p.osm_id, p.tags, p.geom FROM "
#: Overpass `[~"."~"."]`, "carries any tag".  The one object it misses is a
#: relation tagged with nothing but `type`, which the import strips.
_ANY_TAG = "(p.tags <> '{}'::jsonb)"
_CLIP_TEST = (
    "\nAND EXISTS (SELECT 1 FROM clip WHERE ST_Intersects(p.geom, clip.geom))"
)


class PostpassError(QueryError):
    """Postpass sent an answer this plugin cannot use."""


class PostpassTooLarge(PostpassError):
    """The answer passed the size or object ceiling: split the tile."""


def _checked_mode(value: object, allowed: Tuple[str, ...], label: str) -> str:
    mode = str(value or "").strip().casefold()
    if mode not in allowed:
        raise QueryError("".join(("The ", label, " is not valid.")))
    return mode


def _box_area_km2(south: float, west: float, north: float, east: float) -> float:
    """core.query.bbox_area_km2: the worst-case planar area of the box."""
    if south <= 0 <= north:
        scale = 1.0
    else:
        scale = max(0.01, math.cos(math.radians(min(abs(south), abs(north)))))
    return (
        (north - south) * _KM_PER_DEGREE * (east - west) * _KM_PER_DEGREE * scale
    )


def _checked_bbox(bbox: object) -> Tuple[float, float, float, float]:
    """A finite WGS84 (south, west, north, east) box that fits one request.

    A tile the planner made is exactly at the ceiling at most, give or take
    rounding, so only a box clearly past it is refused.
    """
    try:
        south, west, north, east = (float(item) for item in bbox)
    except (TypeError, ValueError) as exc:
        raise QueryError("The selected extent is not valid.") from exc
    values = (south, west, north, east)
    if not all(math.isfinite(item) for item in values):
        raise QueryError("The selected extent is not finite.")
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise QueryError("The selected extent is outside WGS84 bounds.")
    area = _box_area_km2(*values)
    if area > _MAX_TILE_AREA_KM2 * (1 + 1e-9):
        raise QueryError(
            f"The selected extent is {area:,.1f} km²; a single OSM request is "
            f"limited to {_MAX_TILE_AREA_KM2:,.0f} km²."
        )
    return values


def _number(value: float) -> str:
    """A validated coordinate, at Overpass's own precision (1e-7 degree)."""
    return format(float(value), ".7f")


def _text(value: str) -> str:
    """A text literal no input can break out of: its UTF-8 bytes, in hex."""
    return "".join((
        "convert_from(decode('",
        str(value).encode("utf-8").hex(),
        "', 'hex'), 'UTF8')",
    ))


def _all_of(parts: Sequence[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return "".join(("(", " AND ".join(parts), ")"))


def _any_of(parts: Sequence[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return "".join(("(", " OR ".join(parts), ")"))


def _exact(key: str, value: str) -> str:
    """Overpass `["key"="value"]`, or `["key"]` for a blank value.

    jsonb containment and key existence are what the tags index serves.
    """
    if value:
        return "".join((
            "(p.tags @> jsonb_build_object(", _text(key), ", ", _text(value), "))",
        ))
    return "".join(("(p.tags ? ", _text(key), ")"))


def _matches(key: str, pattern: str) -> str:
    """Overpass `["key"~"pattern"]`: a case-sensitive POSIX regex on the value."""
    return "".join(("((p.tags ->> ", _text(key), ") ~ ", _text(pattern), ")"))


def _spec_condition(spec: TagSpec, regex: bool) -> str:
    """One spec's tag filter, as `core.query._render_query` writes it."""
    if spec.key == "*":
        parts = [_ANY_TAG]
        parts.extend(
            "".join(("(NOT ", _exact(key, value), ")"))
            for key, value in _WILDCARD_EXCLUDED_KEYS
        )
    elif spec.value and regex:
        parts = [_matches(spec.key, spec.value)]
    else:
        parts = [_exact(spec.key, spec.value)]
    # Qualifiers are always exact, in both value modes.
    parts.extend(_exact(key, value) for key, value in spec.qualifiers)
    return _all_of(parts)


def _is_admin_boundary(spec: TagSpec) -> bool:
    """core.query.is_admin_boundary_spec: admin areas are read as relations."""
    return (
        spec.key == "boundary"
        and spec.value == "administrative"
        and any(key == "admin_level" and value for key, value in spec.qualifiers)
    )


def _selectors(
    safe_specs: Tuple[TagSpec, ...], match_mode: str
) -> Tuple[Tuple[str, Tuple[TagSpec, ...]], ...]:
    """The (OSM type, specs that must all match) pairs Overpass is sent.

    Match any: one group per spec.  Match all: one group per geometry, every
    spec of that geometry at once.  A point group reads nodes, a line or
    polygon group ways and relations, and an administrative boundary is read
    as a relation only -- the selectors `_render_query` writes.  A group with
    the wildcard reads area relations only (`_AREA_RELATION`).
    """
    if match_mode == "any":
        groups = [(spec.geometry, (spec,)) for spec in safe_specs]
    else:
        groups = []
        for geometry in GEOMETRY_KINDS:
            members = tuple(
                spec for spec in safe_specs if spec.geometry == geometry
            )
            if members:
                groups.append((geometry, members))
    selectors = []
    for geometry, members in groups:
        admin_only = all(_is_admin_boundary(spec) for spec in members)
        relation = (
            _AREA_RELATION if any(spec.key == "*" for spec in members)
            else _RELATION
        )
        if geometry == "point":
            osm_types = (relation,) if admin_only else (_NODE,)
        elif geometry == "line":
            osm_types = (_WAY, relation)
        else:
            osm_types = (relation,) if admin_only else (_WAY, relation)
        selectors.extend((osm_type, members) for osm_type in osm_types)
    return tuple(dict.fromkeys(selectors))


def _table_condition(
    selectors: Tuple[Tuple[str, Tuple[TagSpec, ...]], ...],
    osm_types: Tuple[str, ...],
    regex: bool,
) -> str:
    """The filter for one table: every selector whose OSM type it holds.

    A filter asked for every type the table holds needs no type test; one
    asked for relations only (an administrative boundary) is gated on
    `osm_type`.  Empty when the table can hold none of the selectors.
    """
    wanted: Dict[str, List[str]] = {}
    for osm_type, members in selectors:
        if osm_type in osm_types:
            condition = _all_of(
                [_spec_condition(spec, regex) for spec in members]
            )
            stored = _STORED_TYPE[osm_type]
            if stored not in wanted.setdefault(condition, []):
                wanted[condition].append(stored)
    held = {_STORED_TYPE[item] for item in osm_types}
    parts = []
    for condition, types in wanted.items():
        if held <= set(types):
            parts.append(condition)
        else:
            gate = _any_of(
                ["".join(("(p.osm_type = '", item, "')")) for item in types]
            )
            parts.append(_all_of([gate, condition]))
    return _any_of(parts) if parts else ""


def _clip_cte(area_id: int) -> str:
    """The mapped boundary an Overpass `area(<id>)` stands for, read once.

    Only a boundary Postpass assembled as a polygon can clip; for any other
    id the request returns nothing rather than an unclipped box.
    """
    if area_id >= _RELATION_AREA_OFFSET:
        osm_type, osm_id = _RELATION, area_id - _RELATION_AREA_OFFSET
    else:
        osm_type, osm_id = _WAY, area_id - _WAY_AREA_OFFSET
    return "".join((
        "WITH clip AS MATERIALIZED (SELECT geom FROM postpass_polygon",
        " WHERE osm_type = '", osm_type, "' AND osm_id = ", str(int(osm_id)),
        ")\n",
    ))


def render_sql(
    specs: Iterable[TagSpec],
    bbox: Tuple[object, object, object, object],
    match_mode: str = "any",
    value_mode: str = "exact",
    area_id: object = None,
) -> str:
    """The Postpass statement for one tile of a request.

    `bbox` is (south, west, north, east) in WGS84, as everywhere in
    ``core.query``; `area_id` is an Overpass area id (a falsy value means
    none).  The statement selects `osm_type`, `osm_id`, `tags` and `geom`.
    A relation that is both rings and lines comes back twice, which
    :func:`adapt` folds into one element.

    The box test is `&&`, a bounding-box overlap the spatial index answers
    cheaply.  A long line or relation whose box spans the tile comes back even
    where its geometry misses the tile, which Overpass would not return, so a
    caller drops features that do not intersect the requested extent.
    (`ST_Intersects` here would be exact, but PostGIS costs it so high that
    Postpass would queue a dense tile with its slowest requests.)
    """
    mode = _checked_mode(match_mode, _MATCH_MODES, "query match mode")
    values_mode = _checked_mode(value_mode, _VALUE_MODES, "tag value mode")
    safe_specs = normalized_specs(specs, values_mode)
    south, west, north, east = _checked_bbox(bbox)
    clip_id = validate_area_id(area_id) if area_id else 0
    selectors = _selectors(safe_specs, mode)
    regex = values_mode == "regex"
    box = "".join((
        "ST_MakeEnvelope(", _number(west), ", ", _number(south), ", ",
        _number(east), ", ", _number(north), ", 4326)",
    ))
    branches = []
    for table, osm_types in _TABLES:
        condition = _table_condition(selectors, osm_types, regex)
        if not condition:
            continue
        parts = [_COLUMNS, table, " AS p\nWHERE p.geom && ", box, "\nAND ", condition]
        if clip_id:
            parts.append(_CLIP_TEST)
        branches.append("".join(parts))
    statement = "\nUNION ALL\n".join(branches)
    if clip_id:
        statement = "".join((_clip_cte(clip_id), statement))
    return statement


# ---------------------------------------------------------------- answers
def _excerpt(body: bytes) -> str:
    text = " ".join(body[:400].decode("utf-8", "replace").split())
    if not text:
        return "an empty answer"
    return text if len(text) <= 120 else "".join((text[:119], "…"))


def _document(payload: object) -> object:
    """The parsed answer, refusing a body past the 64 MB ceiling first."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return payload
    body = bytes(payload)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise PostpassTooLarge("The OSM response exceeded 64 MB; zoom in and retry.")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PostpassError(
            "".join(("Postpass did not send OSM data (", _excerpt(body), ")."))
        ) from exc


def _position(value: object) -> Dict[str, float]:
    """A GeoJSON [lon, lat] position as an Overpass {lat, lon} point."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise PostpassError("A Postpass feature has an invalid coordinate.")
    lon, lat = value[0], value[1]
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in (lon, lat)
    ):
        raise PostpassError("A Postpass feature has an invalid coordinate.")
    lon_f, lat_f = float(lon), float(lat)
    if not (
        math.isfinite(lon_f) and math.isfinite(lat_f)
        and -180.0 <= lon_f <= 180.0 and -90.0 <= lat_f <= 90.0
    ):
        raise PostpassError("A Postpass feature has a coordinate outside WGS84.")
    return {"lat": lat_f, "lon": lon_f}


def _paths(value: object, depth: int) -> list:
    """Nested coordinate lists, `depth` levels above the positions."""
    if not isinstance(value, (list, tuple)):
        raise PostpassError("A Postpass feature has an invalid geometry.")
    if depth == 0:
        return [_position(item) for item in value]
    return [_paths(item, depth - 1) for item in value]


def _geometry(geometry: object) -> Tuple[str, Any]:
    """("point", position), ("lines", [path]), ("areas", [[ring]]) or ("", None).

    Single geometries are read as one-part multi geometries.  No geometry --
    null, empty, or the `{"type": null}` PostGIS writes for an empty one
    (postpass#46) -- is ("", None).
    """
    if geometry is None:
        return "", None
    if not isinstance(geometry, dict):
        raise PostpassError("A Postpass feature has an invalid geometry.")
    shape = geometry.get("type")
    if shape is None:
        return "", None
    if shape == "GeometryCollection":
        if geometry.get("geometries") == []:
            return "", None
        raise PostpassError("A Postpass feature has a geometry collection.")
    coordinates = geometry.get("coordinates")
    if isinstance(coordinates, (list, tuple)) and not coordinates:
        return "", None
    if shape == "Point":
        return "point", _position(coordinates)
    if shape == "LineString":
        return "lines", [_paths(coordinates, 0)]
    if shape == "MultiLineString":
        return "lines", _paths(coordinates, 1)
    if shape == "Polygon":
        return "areas", [_paths(coordinates, 1)]
    if shape == "MultiPolygon":
        return "areas", _paths(coordinates, 2)
    raise PostpassError("A Postpass feature has an unsupported geometry type.")


def _feature(feature: object) -> Tuple[str, int, Dict[str, str], str, Any]:
    """(element type, id, tags, geometry kind, geometry) of one feature."""
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise PostpassError("The Postpass answer contains an invalid feature.")
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        raise PostpassError("A Postpass feature has no properties.")
    osm_type = properties.get("osm_type")
    element_type = (
        _ELEMENT_TYPES.get(osm_type) if isinstance(osm_type, str) else None
    )
    if element_type is None:
        raise PostpassError("A Postpass feature has no valid OSM type.")
    osm_id = properties.get("osm_id")
    if isinstance(osm_id, bool) or not isinstance(osm_id, int) or osm_id <= 0:
        raise PostpassError("A Postpass feature has no valid OSM id.")
    tags = properties.get("tags")
    if tags is None:
        tags = {}
    if not isinstance(tags, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in tags.items()
    ):
        raise PostpassError("A Postpass feature has tags that are not OSM tags.")
    kind, value = _geometry(feature.get("geometry"))
    return element_type, osm_id, dict(tags), kind, value


def _node(osm_id: int, tags: Dict[str, str], kind: str, value: Any) -> Dict[str, Any]:
    if kind not in ("", "point"):
        raise PostpassError("A Postpass node has a geometry that is not a point.")
    element: Dict[str, Any] = {"type": "node", "id": osm_id}
    if kind:
        element.update(value)
    if tags:
        element["tags"] = tags
    return element


def _way(osm_id: int, tags: Dict[str, str], kind: str, value: Any) -> Dict[str, Any]:
    """A way's own line, or its closed outline when Postpass made it a polygon."""
    if kind == "lines":
        if len(value) != 1:
            raise PostpassError("A Postpass way has more than one line.")
        geometry = value[0]
    elif kind == "areas":
        if len(value) != 1 or len(value[0]) != 1:
            raise PostpassError("A Postpass way has more than one ring.")
        geometry = value[0][0]
    elif not kind:
        geometry = []
    else:
        raise PostpassError("A Postpass way has a point geometry.")
    element: Dict[str, Any] = {"type": "way", "id": osm_id, "geometry": geometry}
    if tags:
        element["tags"] = tags
    return element


def _relation_parts(found: Dict[str, list], kind: str, value: Any) -> None:
    """Collect a relation's rings ("outer", then its holes "inner") or lines."""
    if kind == "areas":
        for polygon in value:
            for index, ring in enumerate(polygon):
                found["rings"].append({
                    "type": "way",
                    "role": "outer" if index == 0 else "inner",
                    "geometry": ring,
                })
    elif kind == "lines":
        found["lines"].extend(
            {"type": "way", "role": "", "geometry": path} for path in value
        )
    elif kind:
        raise PostpassError("A Postpass relation has a point geometry.")


def _data_date(document: Dict[str, Any]) -> str:
    """Postpass's replication timestamp as `YYYY-MM-DDTHH:MM:SSZ`, or ""."""
    properties = document.get("postpass_properties")
    stamp = properties.get("timestamp") if isinstance(properties, dict) else None
    if not isinstance(stamp, str):
        return ""
    try:
        moment = datetime.strptime(stamp.strip(), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return ""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def adapt(payload: object) -> Dict[str, Any]:
    """The Overpass `out body geom` payload for one Postpass answer.

    `payload` is the reply body (bytes or text) or its parsed JSON.  Nodes
    become `{type, id, lat, lon, tags}`; ways `{type, id, geometry, tags}`,
    the geometry being the way's line or its closed outline; relations
    `{type, id, members, tags}`, whose way members are the rings -- each
    "outer" followed by its holes as "inner" -- or, for a route, the line
    parts with an empty role.  A boundary comes back as rings and as lines and
    becomes one relation built from its rings.  An element with no geometry
    is kept without coordinates, so the layer builder drops it like any
    element it cannot draw.  Empty tags are left out, as Overpass does.

    Raises PostpassTooLarge past the 64 MB or object ceiling, and
    PostpassError for anything that is not Postpass GeoJSON.
    """
    document = _document(payload)
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise PostpassError(
            "Postpass did not answer with a GeoJSON feature collection."
        )
    features = document.get("features")
    if not isinstance(features, list):
        raise PostpassError("The Postpass answer has no features list.")
    if len(features) > _MAX_FEATURES:
        raise PostpassTooLarge("The OSM response is too large; zoom in and retry.")

    elements: Dict[Tuple[str, int], Dict[str, Any]] = {}
    relation_parts: Dict[int, Dict[str, list]] = {}
    for feature in features:
        element_type, osm_id, tags, kind, value = _feature(feature)
        key = (element_type, osm_id)
        if element_type == "relation":
            if key not in elements:
                element: Dict[str, Any] = {
                    "type": "relation", "id": osm_id, "members": [],
                }
                if tags:
                    element["tags"] = tags
                elements[key] = element
                relation_parts[osm_id] = {"rings": [], "lines": []}
            _relation_parts(relation_parts[osm_id], kind, value)
        elif key not in elements:
            # Validation still runs on a repeat; the first copy is kept, as
            # core.query.merge_elements keeps it across tiles.
            builder = _node if element_type == "node" else _way
            elements[key] = builder(osm_id, tags, kind, value)
    for osm_id, found in relation_parts.items():
        elements[("relation", osm_id)]["members"] = found["rings"] or found["lines"]

    result: Dict[str, Any] = {}
    properties = document.get("postpass_properties")
    generator = properties.get("generator") if isinstance(properties, dict) else None
    if isinstance(generator, str) and generator.strip():
        result["generator"] = generator.strip()
    stamp = _data_date(document)
    if stamp:
        result["osm3s"] = {"timestamp_osm_base": stamp}
    result["elements"] = list(elements.values())
    return result


# --------------------------------------------------------------- verdicts
#: PostgreSQL's words for a statement that ran past the server's time limit
#: or out of memory.  Postpass sends them, like every SQL error, as HTTP 400.
_STATEMENT_TIMEOUT = "canceling statement due to statement timeout"
_OUT_OF_MEMORY = "out of memory"


def classify(
    status: int = 0,
    body: bytes = b"",
    error_code: int = 0,
    error_text: str = "",
    oversize: bool = False,
    transfer_s: float = transport.MAX_TRANSFER_SECONDS,
) -> transport.Verdict:
    """Classify one finished Postpass attempt, in core.transport's classes.

    Takes the facts :func:`core.transport.classify` takes.  Postpass answers
    every SQL error with HTTP 400 and PostgreSQL's message (``handlers.go``
    writes it twice), so a 400 is read: a statement timeout or an
    out-of-memory error means the part is too heavy and is split, like an
    Overpass ``remark``; any other 400 is a request the server refuses, which
    asking again cannot change.  A 2xx answer is adapted into Overpass
    elements (the verdict's payload); one past the object ceiling is too
    large, and one that is not Postpass GeoJSON counts against the host like
    a busy answer.  Every other answer -- 429, 403/406, 5xx, no reply, a
    dropped connection -- means what it means for Overpass.
    """
    data = bytes(body or b"")
    try:
        code = int(status or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 400 and not oversize and len(data) <= _MAX_RESPONSE_BYTES:
        said = data[:4096].decode("utf-8", "replace").casefold()
        if _STATEMENT_TIMEOUT in said:
            return transport.Verdict(
                transport.SERVER_TIMEOUT, "ran out of time on this query"
            )
        if _OUT_OF_MEMORY in said:
            return transport.Verdict(
                transport.SERVER_MEMORY, "ran out of memory on this query"
            )
        return transport.Verdict(
            transport.BAD_QUERY, "rejected the request as invalid (HTTP 400)"
        )
    if (
        oversize
        or len(data) > _MAX_RESPONSE_BYTES
        or not data
        or error_code
        or str(error_text or "").strip()
        or (code and not 200 <= code <= 299)
    ):
        return transport.classify(
            code, data, error_code, error_text, oversize, transfer_s
        )
    try:
        payload = adapt(data)
    except PostpassTooLarge:
        return transport.Verdict(
            transport.TOO_LARGE, f"sent more than {_MAX_FEATURES:,} objects"
        )
    except PostpassError:
        return transport.Verdict(
            transport.BUSY, "sent an answer that is not OSM data"
        )
    return transport.Verdict(transport.OK, "delivered the data", payload)
