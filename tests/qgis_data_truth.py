"""Data truth: what lands in the user's layers is true, typed and accounted for.

Run under a QGIS Python (python-qgis-ltr.bat / python-qgis.bat) with
QGIS_CUSTOM_CONFIG_PATH pointing at a throwaway directory:
    python -m zero2agent_osm_downloader.tests.qgis_data_truth

Offline: every answer is seeded into the plugin's cache under the exact key
the download asks for, and the hosts point at a closed local port, so an
unseeded part fails the check instead of reaching a public server.

Real shapes come from tests/fixtures -- OpenStreetMap data, (c) OpenStreetMap
contributors, available under the Open Database License (ODbL) 1.0:
* overpass_konak_wildcard.json: a live overpass-api.de answer of 2026-11-02
  for "All tagged objects" over 0.3 km^2 of Konak, trimmed to 33 elements;
* overpass_building_levels_regex.json: a live answer for
  way["building:levels"~"^[[:digit:]]+$"], verbatim (3 buildings);
* osm_izmir_istanbul_extract.json: relations, open ways and place nodes from
  the Geofabrik Turkiye extract (replication 2026-10-24), with libosmium's
  assembly of each relation as the independent truth;
* postpass_ferry_overselection.json: a live postpass.geofabrik.de answer for
  a Konak box that also returned the Thessaloniki ferry, trimmed.
The adversarial shapes are the A2 audit's fixtures (audit/a2/t01, t03, t14).

Covers D1 open ways, D2 ring nesting and repair, D3 drops by reason and
FEATURES_DROPPED, D4 typed columns and ellipsoidal measures, D5 field types,
D6 wildcard relations, the As-Is exclusion and relation points, D7 POSIX
regex, E1 stable PRESET indexes, E2 the extent drop and E3 transform failures.
"""
from __future__ import annotations

import itertools
import json
import math
import os
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingUtils,
    QgsProject,
    QgsRectangle,
    QgsReferencedRectangle,
    QgsVectorLayer,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: The box of the live Konak answer: (west, south, east, north).
KONAK_LIVE = (27.1250, 38.4180, 27.1320, 38.4225)
#: The box of the live Postpass answers: (west, south, east, north).
KONAK_POSTPASS = (27.125, 38.415, 27.145, 38.430)
#: The A2 fixture box: (west, south, east, north).
A2_BOX = (27.10, 38.40, 27.11, 38.41)

# WGS84, written out from the definition so no expected value is read back
# from the code under test (docs/TRAPS.md 5.8).
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_E = math.sqrt(_E2)
_B = _A * (1 - _F)


def _q(latitude: float) -> float:
    s = math.sin(math.radians(latitude))
    return s / (1 - _E2 * s * s) + math.log((1 + _E * s) / (1 - _E * s)) / (2 * _E)


def quadrangle_m2(west: float, south: float, east: float, north: float) -> float:
    """Exact area of a longitude/latitude rectangle on the WGS84 ellipsoid."""
    return _B * _B / 2 * math.radians(east - west) * (_q(north) - _q(south))


def meridian_m(south: float, north: float, steps: int = 2000) -> float:
    """Arc length along a meridian on the WGS84 ellipsoid (Simpson's rule)."""
    def radius(phi: float) -> float:
        return _A * (1 - _E2) / (1 - _E2 * math.sin(phi) ** 2) ** 1.5

    low, high = math.radians(south), math.radians(north)
    step = (high - low) / steps
    total = radius(low) + radius(high) + sum(
        (4 if index % 2 else 2) * radius(low + index * step) for index in range(1, steps)
    )
    return total * step / 3


class CheckFailed(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def close(actual, expected, tolerance: float, message: str) -> None:
    check(
        actual is not None and abs(actual - expected) <= tolerance * max(abs(expected), 1e-300),
        f"{message}: {actual!r} is not within {tolerance:.2%} of {expected!r}",
    )


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def pairs_to_geometry(pairs):
    return [{"lat": y, "lon": x} for x, y in pairs]


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def member(pairs, role="outer"):
    return {"type": "way", "ref": 0, "role": role, "geometry": pairs_to_geometry(pairs)}


class Feedback(QgsProcessingFeedback):
    def __init__(self) -> None:
        super().__init__()
        self.warnings = []
        self.infos = []

    def pushWarning(self, text):  # noqa: N802 - QGIS naming
        self.warnings.append(text)
        super().pushWarning(text)

    def pushInfo(self, text):  # noqa: N802 - QGIS naming
        self.infos.append(text)
        super().pushInfo(text)


class Rig:
    """Seeds answers and runs the plugin's own algorithms on them."""

    def __init__(self, work: Path) -> None:
        import processing

        from zero2agent_osm_downloader.processing import osm_algorithms

        self.processing = processing
        self.oa = osm_algorithms
        self.work = work
        self.context = QgsProcessingContext()
        self.context.setProject(QgsProject.instance())
        self.files = itertools.count()

    def seed(self, specs, box, payload, match_mode="any", value_mode="exact"):
        """Store `payload` under every cache key the download will ask for."""
        from zero2agent_osm_downloader.core.query import build_queries

        west, south, east, north = box
        queries = build_queries(specs, (south, west, north, east), match_mode, value_mode=value_mode)
        for query in queries:
            self.oa._cache().set(query, payload)
        return len(queries)

    def run(self, algorithm, parameters, box=None, crs="EPSG:4326", files=False, feedback=None):
        params = dict(parameters)
        if box is not None:
            params["EXTENT"] = QgsReferencedRectangle(
                QgsRectangle(*box), QgsCoordinateReferenceSystem(crs)
            )
        paths = {}
        for name in ("OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POLYGONS"):
            if files:
                path = self.work / f"{name.lower()}_{next(self.files)}.gpkg"
                params[name] = str(path)
                paths[name] = path
            else:
                params[name] = "TEMPORARY_OUTPUT"
        feedback = feedback or Feedback()
        results = self.processing.run(
            f"zero2agentosm:{algorithm}", params, context=self.context,
            feedback=feedback, is_child_algorithm=True,
        )
        layers = {}
        for name in ("OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POLYGONS"):
            if files:
                layers[name[7:].lower()] = paths[name]
            else:
                layers[name[7:].lower()] = QgsProcessingUtils.mapLayerFromString(
                    results[name], self.context, True
                )
        return results, layers, feedback

    def preset_index(self, preset_id: str) -> int:
        """The PRESET index the algorithm itself offers for a preset."""
        from zero2agent_osm_downloader.core.catalog import get_preset

        algorithm = QgsApplication.processingRegistry().algorithmById("zero2agentosm:download_preset")
        options = algorithm.parameterDefinition("PRESET").options()
        return options.index(get_preset(preset_id).processing_label)


def rows(path: Path, columns: str) -> list:
    """Read a written GeoPackage with sqlite3: lock-free (docs/TRAPS.md 4.8)."""
    with sqlite3.connect(str(path)) as connection:
        table = connection.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type = 'features'"
        ).fetchone()
        if table is None:
            return []
        return connection.execute(
            "SELECT " + columns + ' FROM "' + table[0] + '" ORDER BY fid'
        ).fetchall()


def declared_types(path: Path) -> list:
    with sqlite3.connect(str(path)) as connection:
        table = connection.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type = 'features'"
        ).fetchone()[0]
        return [
            (row[1], str(row[2]).upper())
            for row in connection.execute('PRAGMA table_info("' + table + '")')
        ]


def by_id(layer) -> dict:
    return {feature["osm_id"]: feature for feature in layer.getFeatures()}


def aggregate(layer, expression: str):
    context = QgsExpressionContext()
    context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    parsed = QgsExpression(expression)
    value = parsed.evaluate(context)
    check(not parsed.hasEvalError(), f"{expression}: {parsed.evalErrorString()}")
    return value


# --------------------------------------------------------------------- E1
def check_preset_indexes_are_stable(rig: Rig) -> str:
    from zero2agent_osm_downloader.core.catalog import PRESETS, get_preset
    # The 43 ids of 1.5.1 written out in the pure suite, not the constant
    # the plugin itself reads.
    from zero2agent_osm_downloader.tests.test_around_history import V151_PRESET_IDS

    registry = QgsApplication.processingRegistry()
    for algorithm_id in ("zero2agentosm:download_preset", "zero2agentosm:download_place"):
        algorithm = registry.createAlgorithmById(algorithm_id)
        options = algorithm.parameterDefinition("PRESET").options()
        for index, preset_id in enumerate(V151_PRESET_IDS):
            check(options[index] == get_preset(preset_id).processing_label,
                  f"{algorithm_id} option {index} is {options[index]!r}, which 1.5.1 called {preset_id}.")
        check(options[43] == get_preset("everyday_amenities").processing_label,
              f"{algorithm_id} option 43 is {options[43]!r}.")
        check(sorted(options) == sorted(preset.processing_label for preset in PRESETS),
              f"{algorithm_id} must offer every catalogue preset exactly once.")
    algorithm = registry.createAlgorithmById("zero2agentosm:download_preset")
    for index, expected in ((0, "urban_context"), (3, "buildings"), (19, "osm_as_is_full_visible_stack"),
                            (42, "traffic_controls"), (43, "everyday_amenities")):
        preset_id = algorithm._request({"PRESET": [index]}, rig.context)[0]
        check(preset_id == expected, f"PRESET {index} ran {preset_id}, not {expected}.")
    order = rig.oa.PRESET_ENUM_ORDER
    catalogue = sorted(preset.preset_id for preset in PRESETS)
    check(sorted(order) == catalogue and len(set(order)) == len(order),
          "Every catalogue preset must appear exactly once in PRESET_ENUM_ORDER.")
    return "every 1.5.1 index keeps its dataset; Everyday amenities is index 43"


# --------------------------------------------------------------------- D1
def check_open_ways_stay_lines(rig: Rig) -> str:
    from zero2agent_osm_downloader.core.catalog import TagSpec

    oa = rig.oa
    wildcard = (TagSpec("*", "", "point"), TagSpec("*", "", "line"), TagSpec("*", "", "polygon"))
    open_way = pairs_to_geometry([(0, 0), (1, 0), (2, 1), (3, 1)])
    closed_way = pairs_to_geometry([(0, 0), (1, 0), (1, 1), (0, 0)])
    two_nodes = pairs_to_geometry([(0, 0), (1, 0)])
    three_aba = pairs_to_geometry([(0, 0), (1, 0), (0, 0)])
    # The A2 table: every one of these came back as an invented polygon.
    for tags in ({"barrier": "wall"}, {"barrier": "hedge"}, {"barrier": "retaining_wall"},
                 {"man_made": "pier"}, {"man_made": "breakwater"}, {"aeroway": "runway"},
                 {"historic": "citywalls"}, {"amenity": "bench"}, {"railway": "platform"},
                 {"highway": "footway", "area": "yes"}, {"building": "yes"}):
        for label, geometry in (("open", open_way), ("two-node", two_nodes), ("A-B-A", three_aba)):
            element = {"type": "way", "id": 1, "tags": tags, "geometry": geometry}
            kinds = [kind for kind, _m in oa._kinds_for_element(element, wildcard)]
            check(kinds == ["line"], f"An {label} {tags} way became {kinds} under the wildcard.")
            built = oa._build(element, "line")
            check(len(built.geometries) == 1 and not built.geometries[0].isEmpty(),
                  f"The {label} {tags} line was not built.")
            polygon = oa._build(element, "polygon")
            check(not polygon.geometries and polygon.reason == oa.DROP_UNCLOSED,
                  f"An {label} {tags} way built a polygon: {polygon}.")
        element = {"type": "way", "id": 1, "tags": tags, "geometry": closed_way}
        kinds = [kind for kind, _m in oa._kinds_for_element(element, wildcard)]
        check(kinds == ["polygon"], f"A closed {tags} way became {kinds}.")
    vertices = oa._build({"type": "way", "geometry": open_way}, "line").geometries[0]
    check(vertices.constGet().numGeometries() == 1 and len(vertices.asMultiPolyline()[0]) == 4,
          "The open way's line lost or gained a vertex.")

    # Real open ways from Istanbul and Izmir: the city wall, piers, a wall, a
    # retaining wall and a platform.  The wildcard reads every one as a line
    # with all its vertices.
    from zero2agent_osm_downloader.core.catalog import get_preset

    extract = load("osm_izmir_istanbul_extract.json")
    open_ways = [e for e in extract["elements"] if e["type"] == "way"]
    check(len(open_ways) == 6, f"{len(open_ways)} real open ways in the fixture.")
    for element in open_ways:
        kinds = [kind for kind, _m in oa._kinds_for_element(element, wildcard)]
        check(kinds == ["line"], f"Real open way {element['id']} {element['tags']} became {kinds}.")
        line = oa._build(element, "line").geometries[0]
        check(len(line.asMultiPolyline()[0]) == len(element["geometry"]),
              f"Real open way {element['id']} lost vertices.")

    # End to end: the Heritage preset asks for historic=* as an area only, so
    # Istanbul's open city wall is dropped and counted, never closed.
    wall = next(e for e in open_ways if e["tags"].get("historic") == "citywalls")
    xs = [point["lon"] for point in wall["geometry"]]
    ys = [point["lat"] for point in wall["geometry"]]
    box = (min(xs) - 0.001, min(ys) - 0.001, max(xs) + 0.001, max(ys) + 0.001)
    rig.seed(get_preset("heritage").tags, box, {"osm3s": extract["osm3s"], "elements": [wall]})
    results, layers, feedback = rig.run("download_preset", {"PRESET": [rig.preset_index("heritage")]}, box)
    check(layers["polygons"].featureCount() == 0, "The open city wall became a polygon.")
    check(results["FEATURES_DROPPED"] == 1, f"FEATURES_DROPPED is {results['FEATURES_DROPPED']}, not 1.")
    check("1 OSM feature was not written: 1 area outline that does not close." in feedback.warnings,
          f"Drop summary: {feedback.warnings}")
    return "open walls, piers, platforms and city walls are lines; an area-only request drops and counts them"


# --------------------------------------------------------------------- D2
def check_rings_nest(rig: Rig) -> str:
    oa = rig.oa
    A, B, C, D = rect(0, 0, 10, 10), rect(2, 2, 8, 8), rect(4, 4, 6, 6), rect(4.5, 4.5, 5.5, 5.5)
    bow = [(0, 0), (4, 4), (4, 0), (0, 4), (0, 0)]
    bow2 = [(0, 0), (10, 10), (10, 0), (0, 10), (0, 0)]
    a, b = [(0, 0), (5, 0), (5, 5)], [(5, 5), (0, 5), (0, 0)]
    open1 = [(0, 0), (5, 0), (5, 5)]
    spike = [(0, 0), (4, 0), (4, 4), (2, 4), (2, 8), (2, 4), (0, 4), (0, 0)]
    eight = [(0, 0), (2, 0), (2, 2), (4, 2), (4, 4), (2, 4), (2, 2), (0, 2), (0, 0)]
    no_role = member(A)
    no_role.pop("role")
    null_role = dict(member(A), role=None)
    # (label, members, relation type, expected area or None, expected repaired)
    table = (
        ("outer + inner", [member(A), member(B, "inner")], "", 64, False),
        ("island in a lake", [member(A), member(B, "inner"), member(C)], "", 68, False),
        ("island with a pond", [member(A), member(B, "inner"), member(C), member(D, "inner")], "", 67, False),
        ("two islands", [member(A), member(B, "inner"), member(rect(3, 3, 4, 4)), member(rect(6, 6, 7, 7))], "", 66, False),
        ("swapped roles", [member(B), member(A, "inner")], "", 64, False),
        ("only inner roles", [member(A, "inner"), member(rect(20, 20, 21, 21), "inner")], "", 101, False),
        ("inner tagged outer", [member(A), member(B)], "", 64, False),
        ("inner with empty role", [member(A), member(B, "")], "", 64, False),
        ("role missing", [no_role], "", 100, False),
        ("role null", [null_role], "", 100, False),
        ("role 'Outer'", [member(A, "Outer")], "", 100, False),
        ("exclave", [member(A), member(rect(20, 20, 21, 21), "exclave")], "", 101, False),
        ("enclave", [member(A), member(B, "enclave")], "", 64, False),
        ("site perimeter", [member(A, "perimeter"), member(B, "")], "site", 100, False),
        ("building outline", [member(A, "outline"), member(B, "part")], "building", 100, False),
        ("building parts only", [member(A, "part")], "building", None, False),
        ("touching outers", [member(rect(0, 0, 2, 2)), member([(2, 2), (4, 2), (4, 4), (2, 4), (2, 2)])], "", 8, False),
        ("fragments, one reversed", [member(a), member(b[::-1])], "", 25, False),
        ("bow-tie outer", [member(bow)], "", 8, True),
        ("bow-tie outer + inner", [member(bow2), member(rect(7, 4, 9, 6), "inner")], "", 46, True),
        ("valid outer + bow-tie inner", [member(A), member([(2, 2), (8, 8), (8, 2), (2, 8), (2, 2)], "inner")], "", 82, True),
        ("outer with a spike", [member(spike)], "", 16, True),
        ("figure eight", [member(eight)], "", 8, True),
        ("same closed way twice", [member(A), member(A)], "", 100, False),
        ("duplicate fragment", [member(a), member(b), member(a)], "", 25, False),
        ("duplicate inner", [member(A), member(B, "inner"), member(B, "inner")], "", 64, False),
        ("inner touches outer at a node", [member(A), member([(0, 5), (3, 3), (3, 7), (0, 5)], "inner")], "", 94, False),
        ("overlapping outers", [member(rect(0, 0, 4, 4)), member(rect(2, 2, 6, 6))], "", 28, False),
        ("'inner' outside every outer", [member(rect(0, 0, 4, 4)), member(rect(10, 10, 12, 12), "inner")], "", 20, False),
        ("repeated consecutive nodes", [member([(0, 0), (0, 0), (4, 0), (4, 4), (4, 4), (0, 4), (0, 0)])], "", 16, False),
    )
    def element(members, relation_type=""):
        tags = {"type": relation_type} if relation_type else {}
        return {"type": "relation", "id": 1, "tags": tags, "members": members}

    for label, members, relation_type, expected, _repaired in table:
        geometry = oa._relation_polygon(element(members, relation_type))
        if expected is None:
            check(geometry is None, f"{label}: built {geometry and geometry.area()}")
            continue
        check(geometry is not None, f"{label}: dropped, truth {expected}")
        check(geometry.isGeosValid(), f"{label}: invalid output")
        check(abs(geometry.area() - expected) < 1e-9, f"{label}: area {geometry.area()}, truth {expected}")
    # Member order must not matter: a figure eight of four open members.
    centre = (2, 2)
    parts = ([centre, (0, 2), (0, 0)], [(0, 0), (2, 0), centre], [centre, (4, 2), (4, 4)], [(4, 4), (2, 4), centre])
    for order in itertools.permutations(parts):
        geometry = oa._relation_polygon(element([member(p) for p in order]))
        check(geometry is not None and geometry.isGeosValid() and abs(geometry.area() - 8) < 1e-9,
              f"figure eight in order {order}: {geometry and geometry.asWkt(3)}")
    # Repairs are reported as repairs, not as drops.
    for label, members, relation_type, expected, repaired in table:
        if expected is not None:
            area = oa._relation_area(element(members, relation_type))
            check(area.repaired == repaired, f"{label}: repaired={area.repaired}")
    for label, members, reason in (
        ("unclosed gap", [member([(0, 0), (5, 0), (5, 5)]), member([(0, 5), (0, 0)])], oa.DROP_UNCLOSED),
        ("same open way twice", [member(open1), member(open1)], oa.DROP_UNCLOSED),
        ("node members only", [{"type": "node", "ref": 1, "role": "", "lat": 1, "lon": 1}], oa.DROP_NO_GEOMETRY),
        ("super-relation", [{"type": "relation", "ref": 1, "role": ""}], oa.DROP_NO_GEOMETRY),
    ):
        area = oa._relation_area({"type": "relation", "members": members})
        check(area.geometry is None and area.reason == reason, f"{label}: {area}")

    # Real relations against libosmium, the ones the role-based builder lost
    # (Rumeli Hisari: roles swapped; Huber Koskus, Fenerli Oda: inner only)
    # or got wrong included.
    extract = load("osm_izmir_istanbul_extract.json")
    truth = extract["libosmium_area_deg2"]
    relations = [e for e in extract["elements"] if e["type"] == "relation"]
    names = []
    for relation in relations:
        geometry = oa._relation_polygon(relation)
        check(geometry is not None, f"relation {relation['id']} {relation['tags'].get('name', '')} dropped")
        close(geometry.area(), truth[str(relation["id"])], 0.005, f"relation {relation['id']} area")
        names.append(relation.get("tags", {}).get("name", str(relation["id"])))
    check(len(names) == 8, f"{len(names)} real relations read.")
    # Square metres on the ellipsoid, cross-checked through UTM 35N.
    utm = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                 QgsCoordinateReferenceSystem("EPSG:32635"), QgsProject.instance())
    measure = oa._ellipsoid_measure(rig.context)
    check(measure.willUseEllipsoid(), "The area measure is not on the ellipsoid.")
    for relation in relations:
        geometry = oa._relation_polygon(relation)
        projected = QgsGeometry(geometry)
        projected.transform(utm)
        close(measure.measureArea(geometry), projected.area(), 0.002,
              f"relation {relation['id']} m² against UTM 35N")
    return f"{len(table)} ring fixtures, 24 member orders and 8 real relations ({', '.join(names[:3])}, ...) match"


# --------------------------------------------------------------------- D3
def t03_elements():
    A = rect(27.100, 38.400, 27.110, 38.410)
    B = rect(27.102, 38.402, 27.108, 38.408)
    C = rect(27.104, 38.404, 27.106, 38.406)
    bow = [(27.102, 38.402), (27.108, 38.408), (27.108, 38.402), (27.102, 38.408), (27.102, 38.402)]
    tags = {"building": "yes"}

    def rel(identifier, members):
        return {"type": "relation", "id": identifier,
                "tags": dict(tags, type="multipolygon", name=f"rel{identifier}"), "members": members}

    return A, B, C, [
        rel(1, [member(A), member(B, "inner")]),
        rel(2, [member(A), member(B, "inner"), member(C)]),
        rel(3, [member(A), member(bow, "inner")]),
        rel(4, [member([(27.10, 38.40), (27.11, 38.40), (27.11, 38.41)]), member([(27.10, 38.41), (27.10, 38.40)])]),
        rel(5, [{"type": "node", "ref": 1, "role": "", "lat": 38.4, "lon": 27.1}]),
        rel(6, [member([(27.10, 38.40), (27.11, 38.40), (27.11, 38.41)]),
                member([(27.11, 38.41), (27.10, 38.41), (27.10, 38.40)]),
                member([(27.10, 38.40), (27.11, 38.40), (27.11, 38.41)])]),
        {"type": "way", "id": 10, "tags": dict(tags, name="open"),
         "geometry": pairs_to_geometry([(27.10, 38.40), (27.105, 38.401), (27.11, 38.40)])},
        {"type": "way", "id": 11, "tags": dict(tags, name="bowtie"), "geometry": pairs_to_geometry(bow)},
        {"type": "way", "id": 12, "tags": dict(tags, name="2nodes"),
         "geometry": pairs_to_geometry([(27.10, 38.40), (27.11, 38.40)])},
    ]


def check_drops_are_counted(rig: Rig) -> str:
    from zero2agent_osm_downloader.core.query import advanced_specs

    A, B, C, elements = t03_elements()
    payload = {"elements": elements, "osm3s": {"timestamp_osm_base": "2026-10-26T08:15:02Z"}}
    specs = advanced_specs((("building", ""),), ("polygon",), "any")
    rig.seed(specs, A2_BOX, payload)
    results, layers, feedback = rig.run(
        "download_advanced", {"MATCH_MODE": 0, "GEOMETRY": 3, "KEY_1": "building"}, A2_BOX
    )
    check(results["FEATURES_DROPPED"] == 4, f"FEATURES_DROPPED is {results['FEATURES_DROPPED']}, not 4.")
    expected = ("4 OSM features were not written: 3 area outlines that do not close, "
                "1 with no coordinates. 2 invalid outlines were repaired and kept.")
    check(expected in feedback.warnings, f"Drop summary is {feedback.warnings}")
    polygons = by_id(layers["polygons"])
    check(sorted(polygons) == ["1", "11", "2", "3", "6"], f"Written: {sorted(polygons)}")
    cell = quadrangle_m2(27.100, 38.400, 27.110, 38.410) / 1e-4   # m² per square degree here
    expected_deg2 = {"1": 0.000064, "2": 0.000068, "3": 0.000082, "6": 0.0001, "11": 0.000018}
    expected_m2 = {
        "1": quadrangle_m2(27.100, 38.400, 27.110, 38.410) - quadrangle_m2(27.102, 38.402, 27.108, 38.408),
        "2": quadrangle_m2(27.100, 38.400, 27.110, 38.410) - quadrangle_m2(27.102, 38.402, 27.108, 38.408)
        + quadrangle_m2(27.104, 38.404, 27.106, 38.406),
        "6": quadrangle_m2(27.100, 38.400, 27.110, 38.410),
        "3": 0.000082 * cell,
        "11": 0.000018 * cell,
    }
    for identifier, feature in polygons.items():
        geometry = feature.geometry()
        check(geometry.isGeosValid(), f"Written polygon {identifier} is invalid.")
        check(abs(geometry.area() - expected_deg2[identifier]) < 1e-12,
              f"polygon {identifier}: {geometry.area()} square degrees, truth {expected_deg2[identifier]}")
        close(feature["area_m2"], expected_m2[identifier], 0.002 if identifier in ("3", "11") else 1e-4,
              f"polygon {identifier} area_m2")
    return "9 elements: 5 written (2 repaired), 3 unclosed outlines and 1 without coordinates counted"


# --------------------------------------------------------------------- D4, D5
def check_typed_columns(rig: Rig) -> str:
    from zero2agent_osm_downloader.core.catalog import get_preset
    from zero2agent_osm_downloader.core.schema import FIELD_NAMES

    oa = rig.oa
    typed = ("height_m", "levels_n", "lanes_n", "maxspd_kmh", "width_m", "pop_n", "length_m", "area_m2")
    konak = load("overpass_konak_wildcard.json")
    rig.seed(get_preset("osm_as_is_full_visible_stack").tags, KONAK_LIVE, konak)
    results, paths, feedback = rig.run(
        "download_preset", {"PRESET": [rig.preset_index("osm_as_is_full_visible_stack")]},
        KONAK_LIVE, files=True,
    )
    declared = dict(declared_types(paths["polygons"]))
    order = [name for name, _type in declared_types(paths["polygons"]) if name not in ("fid", "geom")]
    check(order[-len(typed):] == list(typed), f"The written columns end with {order[-len(typed):]}.")
    check(order == list(FIELD_NAMES), "The written columns are not in FIELD_NAMES order.")
    for name in ("height_m", "levels_n", "maxspd_kmh", "width_m", "length_m", "area_m2"):
        check(declared[name] in ("REAL", "DOUBLE", "FLOAT"), f"{name} is stored as {declared[name]}.")
    for name in ("lanes_n", "pop_n"):
        check("INT" in declared[name], f"{name} is stored as {declared[name]}.")
    check(declared["height"] == "TEXT", f"The text column height is stored as {declared['height']}.")

    heights = {osm_id: (text, value) for osm_id, text, value in rows(paths["polygons"], "osm_id, height, height_m")}
    expected = {"59869337": ("25", 25.0), "401790576": ("0.25", 0.25), "401868624": ("4.50", 4.5),
                "401934708": ("7.50", 7.5), "401967283": ("8 m", 8.0), "401967284": ("12", 12.0),
                "402608751": ("3 m", 3.0), "402923404": ("15 m", 15.0), "402923811": ("18 m", 18.0),
                "404799330": ("17.75", 17.75), "404802189": ("17.50", 17.5)}
    for osm_id, (text, value) in expected.items():
        check(heights.get(osm_id) == (text, value), f"way {osm_id}: {heights.get(osm_id)}, truth {(text, value)}")
    check(heights["867516394"] == ("", None), f"An absent height must be NULL, not {heights['867516394']}.")
    levels = {osm_id: value for osm_id, value in rows(paths["polygons"], "osm_id, levels_n")}
    check([levels[i] for i in ("867516394", "867516395", "867516396")] == [2.0, 2.0, 2.0],
          "building:levels=2 did not become 2.0.")
    roads = {osm_id: (speed, lanes) for osm_id, speed, lanes in rows(paths["lines"], "osm_id, maxspd_kmh, lanes_n")}
    check(roads["4581798"] == (50.0, 4) and roads["23616116"] == (70.0, 3) and roads["25034948"] == (70.0, 3),
          f"Road speeds and lanes: {roads}")

    layer = QgsVectorLayer(str(paths["polygons"]), "konak polygons", "ogr")
    check(layer.isValid(), "The written polygons do not reopen.")
    check(aggregate(layer, 'maximum("height_m")') == 25.0, "maximum(height_m) is not 25.")
    check(aggregate(layer, 'minimum("height_m")') == 0.25, "minimum(height_m) is not 0.25.")
    check(abs(aggregate(layer, 'sum("height_m")') - 128.5) < 1e-9, "sum(height_m) is not 128.5.")
    check(aggregate(layer, 'count("height_m")') == 11, "count(height_m) is not 11.")

    # Ground measures: on the WGS84 geometry, before the output CRS, so a
    # Web Mercator run reports the same square metres, not 1.6 times more.
    utm = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                 QgsCoordinateReferenceSystem("EPSG:32635"), QgsProject.instance())
    measured = dict(rows(paths["polygons"], "osm_id, area_m2"))
    _results, wgs_layers, _fb = rig.run(
        "download_preset", {"PRESET": [rig.preset_index("osm_as_is_full_visible_stack")]}, KONAK_LIVE)
    for feature in wgs_layers["polygons"].getFeatures():
        projected = QgsGeometry(feature.geometry())
        projected.transform(utm)
        close(feature["area_m2"], projected.area(), 0.002, f"{feature['osm_id']} area_m2 against UTM 35N")
        close(measured[feature["osm_id"]], feature["area_m2"], 1e-9, "GeoPackage and memory area_m2")
        check(feature["length_m"] is None or str(feature["length_m"]) == "NULL", "A polygon has a length_m.")
    for feature in wgs_layers["lines"].getFeatures():
        projected = QgsGeometry(feature.geometry())
        projected.transform(utm)
        close(feature["length_m"], projected.length(), 0.0015, f"{feature['osm_id']} length_m against UTM 35N")
    mercator = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                      QgsCoordinateReferenceSystem("EPSG:3857"), QgsProject.instance())
    box = mercator.transformBoundingBox(QgsRectangle(*KONAK_LIVE))
    rig.seed(get_preset("osm_as_is_full_visible_stack").tags,
             _wgs_box_of(box, "EPSG:3857"), konak)
    _results, m_layers, _fb = rig.run(
        "download_preset", {"PRESET": [rig.preset_index("osm_as_is_full_visible_stack")]},
        (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()), crs="EPSG:3857")
    hall = by_id(m_layers["polygons"])["59869337"]
    close(hall["area_m2"], measured["59869337"], 1e-6, "area_m2 in a Web Mercator run")
    check(hall.geometry().area() > 1.5 * hall["area_m2"], "The Mercator geometry should be inflated, the column not.")

    # A meridian line and a longitude/latitude rectangle, against the
    # ellipsoid's own formulas.
    synthetic = {"elements": [
        {"type": "way", "id": 1, "tags": {"highway": "residential", "maxspeed": "50 mph", "width": "40'"},
         "geometry": pairs_to_geometry([(27.105, 38.400), (27.105, 38.410)])},
        {"type": "way", "id": 2, "tags": {"building": "yes", "height": "100"},
         "geometry": pairs_to_geometry(rect(27.100, 38.400, 27.110, 38.410))},
        {"type": "way", "id": 3, "tags": {"building": "yes", "height": "9"},
         "geometry": pairs_to_geometry(rect(27.101, 38.401, 27.102, 38.402))},
        {"type": "way", "id": 4, "tags": {"building": "yes", "height": "12 m"},
         "geometry": pairs_to_geometry(rect(27.103, 38.401, 27.104, 38.402))},
        {"type": "way", "id": 5, "tags": {"building": "yes", "height": "7.5"},
         "geometry": pairs_to_geometry(rect(27.105, 38.401, 27.106, 38.402))},
        {"type": "way", "id": 6, "tags": {"building": "yes", "height": "TR:urban"},
         "geometry": pairs_to_geometry(rect(27.107, 38.401, 27.108, 38.402))},
        {"type": "node", "id": 7, "lat": 38.405, "lon": 27.105,
         "tags": {"place": "town", "population": "766609", "name": "Kucukcekmece"}},
    ]}
    from zero2agent_osm_downloader.core.query import advanced_specs

    adv = advanced_specs((("highway", ""), ("building", ""), ("place", "")), ("point", "line", "polygon"), "any")
    rig.seed(adv, A2_BOX, synthetic)
    _r, s_paths, _fb = rig.run(
        "download_advanced",
        {"MATCH_MODE": 0, "GEOMETRY": 0, "KEY_1": "highway", "KEY_2": "building", "KEY_3": "place"},
        A2_BOX, files=True)
    line = dict((osm_id, (length, speed, width)) for osm_id, length, speed, width
                in rows(s_paths["lines"], "osm_id, length_m, maxspd_kmh, width_m"))
    close(line["1"][0], meridian_m(38.400, 38.410), 1e-5, "length_m of a meridian line")
    close(line["1"][1], 50 * 1.609344, 1e-12, "50 mph")
    close(line["1"][2], 40 * 0.3048, 1e-12, "40'")
    blocks = dict(rows(s_paths["polygons"], "osm_id, area_m2"))
    close(blocks["2"], quadrangle_m2(27.100, 38.400, 27.110, 38.410), 1e-4, "area_m2 of a 0.01° cell")
    reopened = QgsVectorLayer(str(s_paths["polygons"]), "synthetic", "ogr")
    check(aggregate(reopened, 'maximum("height_m")') == 100, "maximum(height_m) is not 100.")
    check(aggregate(reopened, 'count_missing("height_m")') == 1, "TR:urban must be NULL.")
    town = rows(s_paths["points"], "osm_id, pop_n")
    check(town == [("7", 766609)], f"population: {town}")

    # D5: the QMetaType probe and the QVariant fallback build the same fields
    # (the fallback is the path QGIS 3.28-3.36 takes; never a raw integer).
    types = oa._field_types()
    probe = oa._fields(types)
    fallback = oa._fields(oa._variant_field_types())
    for name in FIELD_NAMES:
        check(probe.field(name).type() == fallback.field(name).type(),
              f"{name}: the QVariant fallback builds a different type.")
    check(len(probe) == len(FIELD_NAMES) and all(len(name) <= 10 for name in typed),
          "Typed column names must fit a Shapefile.")
    check(all(type(value) is not int for value in types.values()), f"A raw integer field type: {types}")
    return (f"typed columns stored as numbers; maximum(height_m) is 25 on real Konak rows and 100 on "
            f"the A2 set; area_m2 and length_m match the ellipsoid; {len(types)} field types probed")


def _wgs_box_of(rectangle: QgsRectangle, crs: str):
    transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem(crs),
                                       QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    box = transform.transformBoundingBox(rectangle)
    return (box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum())


# --------------------------------------------------------------------- D6
def check_wildcard_relations(rig: Rig) -> str:
    from zero2agent_osm_downloader.core import postpass
    from zero2agent_osm_downloader.core.catalog import TagSpec, get_preset
    from zero2agent_osm_downloader.core.query import advanced_specs, merge_elements

    konak = load("overpass_konak_wildcard.json")
    wildcard = get_preset("osm_as_is_full_visible_stack").tags
    rig.seed(wildcard, KONAK_LIVE, konak)
    results, layers, feedback = rig.run(
        "download_preset", {"PRESET": [rig.preset_index("osm_as_is_full_visible_stack")]}, KONAK_LIVE)
    counts = {name: layer.featureCount() for name, layer in layers.items()}
    check(counts == {"points": 3, "lines": 8, "polygons": 19}, f"Konak wildcard wrote {counts}")
    check(results["FEATURES_DROPPED"] == 0, f"FEATURES_DROPPED {results['FEATURES_DROPPED']}")
    written = {(f["osm_type"], f["osm_id"]) for layer in layers.values() for f in layer.getFeatures()}
    for relation in ("451604", "18354566", "7981784"):   # ferry route, turn restriction, stop area
        check(("relation", relation) not in written, f"relation {relation} reached a layer")
    polygons = {(f["osm_type"], f["osm_id"]) for f in layers["polygons"].getFeatures()}
    for relation in ("15452081", "19852339", "6040265", "12201542"):
        check(("relation", relation) in polygons, f"area relation {relation} is missing")
    check(not any(kind == "relation" for kind, _id in
                  ((f["osm_type"], f["osm_id"]) for f in layers["points"].getFeatures())),
          "A relation became a point under the wildcard.")
    check(("way", "41873193") in {(f["osm_type"], f["osm_id"]) for f in layers["lines"].getFeatures()},
          "The open breakwater is not a line.")

    # Postpass strips `type`: its multipolygons (rings) count, its routes do not.
    payload = merge_elements((postpass.adapt(load("postpass_buildings.json")),
                              postpass.adapt(load("postpass_routes.json"))))
    rig.seed(wildcard, KONAK_POSTPASS, payload)
    results, layers, _fb = rig.run(
        "download_preset", {"PRESET": [rig.preset_index("osm_as_is_full_visible_stack")]}, KONAK_POSTPASS)
    relations = {(f["osm_id"], name) for name, layer in layers.items()
                 for f in layer.getFeatures() if f["osm_type"] == "relation"}
    check({name for _id, name in relations} == {"polygons"} and len(relations) == 2,
          f"Postpass relations under the wildcard: {relations}")
    hans = [f for f in layers["polygons"].getFeatures() if f["osm_type"] == "relation"]
    check(all(len(f.geometry().asMultiPolygon()[0]) == 2 for f in hans), "A han lost its courtyard.")

    # The As-Is exclusion belongs to the wildcard, not to every As-Is preset.
    line = pairs_to_geometry([(27.10, 38.40), (27.101, 38.40), (27.102, 38.401)])
    square = pairs_to_geometry(rect(27.10, 38.40, 27.101, 38.401))
    for preset, element in (
        ("osm_as_is_urban_detail", {"type": "way", "id": 3, "geometry": square,
                                    "tags": {"highway": "pedestrian", "place": "square", "area": "yes"}}),
        ("osm_as_is_overview", {"type": "way", "id": 6, "geometry": square,
                                "tags": {"natural": "coastline", "place": "island", "name": "Karantina Adasi"}}),
        ("osm_as_is_overview", {"type": "way", "id": 7, "geometry": line,
                                "tags": {"waterway": "river", "boundary": "administrative", "admin_level": "6"}}),
    ):
        rig.seed(get_preset(preset).tags, A2_BOX, {"elements": [element]})
        _r, out, _fb = rig.run("download_preset", {"PRESET": [rig.preset_index(preset)]}, A2_BOX)
        written = sum(layer.featureCount() for layer in out.values())
        check(written == 1, f"{preset} wrote {written} for {element['tags']}")

    # Relation points only where an administrative point spec asked.
    ring = [(27.101, 38.401), (27.104, 38.401), (27.104, 38.404), (27.101, 38.401)]
    route = {"type": "relation", "id": 565, "members": [member(ring, "")],
             "tags": {"type": "route", "route": "bus", "name": "565 Evka 4 - Bornova Metro"}}
    rig.seed(get_preset("urban_transit").tags, A2_BOX, {"elements": [route]})
    _r, out, _fb = rig.run("download_preset", {"PRESET": [rig.preset_index("urban_transit")]}, A2_BOX)
    check((out["points"].featureCount(), out["lines"].featureCount()) == (0, 1),
          f"A circular bus route gave {out['points'].featureCount()} points, {out['lines'].featureCount()} lines.")
    park = {"type": "relation", "id": 77, "tags": {"type": "multipolygon", "leisure": "park", "name": "P"},
            "members": [member(rect(27.101, 38.401, 27.109, 38.409)), member(rect(27.103, 38.403, 27.107, 38.407), "inner")]}
    specs = advanced_specs((("leisure", "park"),), ("point", "line", "polygon"), "any")
    rig.seed(specs, A2_BOX, {"elements": [park]})
    _r, out, _fb = rig.run("download_advanced", {"MATCH_MODE": 0, "GEOMETRY": 0, "KEY_1": "leisure",
                                                  "VALUE_1": "park"}, A2_BOX)
    check(out["points"].featureCount() == 0 and out["polygons"].featureCount() == 1,
          f"A park relation in 'All geometries' gave {out['points'].featureCount()} points.")
    admin = {"type": "relation", "id": 92, "members": [
        member(rect(27.101, 38.401, 27.109, 38.409)),
        {"type": "node", "ref": 1, "role": "label", "lat": 38.405, "lon": 27.105}],
        "tags": {"type": "boundary", "boundary": "administrative", "admin_level": "8", "name": "Mahalle"}}
    rig.seed(get_preset("admin_level_8").tags, A2_BOX, {"elements": [admin]})
    _r, out, _fb = rig.run("download_preset", {"PRESET": [rig.preset_index("admin_level_8")]}, A2_BOX)
    points = [f.geometry().asPoint() for f in out["points"].getFeatures()]
    check(points == [QgsPointXY(27.105, 38.405)], f"The admin label point is {points}.")
    check(TagSpec("*", "", "line") in wildcard, "")
    return "routes, restrictions and stop areas leave the wildcard; Postpass rings stay; As-Is keeps squares and coastlines; relation points only for admin boundaries"


# --------------------------------------------------------------------- D7
def check_posix_regex(rig: Rig) -> str:
    from zero2agent_osm_downloader.core.query import advanced_specs

    answer = load("overpass_building_levels_regex.json")
    box = (27.1315155, 38.4204691, 27.1320471, 38.4209691)
    for value, label in (("^[[:digit:]]+$", "a POSIX class"), ("*", "any value")):
        specs = advanced_specs((("building:levels", value),), ("polygon",), "any", "regex")
        rig.seed(specs, box, answer, value_mode="regex")
        _r, out, _fb = rig.run("download_advanced", {"MATCH_MODE": 0, "GEOMETRY": 3, "VALUE_MODE": True,
                                                      "KEY_1": "building:levels", "VALUE_1": value}, box)
        levels = sorted(f["levels_n"] for f in out["polygons"].getFeatures())
        check(levels == [2.0, 2.0, 2.0], f"The server sent 3 buildings for {label}; written {levels}.")
    for pattern, words in (("(?i)park", "(?"), ("^([a-zA-Z]+ ?)*$", "repeats a group"), ("a{b", "'{'")):
        feedback = Feedback()
        try:
            rig.run("download_advanced", {"MATCH_MODE": 0, "GEOMETRY": 1, "VALUE_MODE": True,
                                          "KEY_1": "name", "VALUE_1": pattern}, A2_BOX, feedback=feedback)
        except QgsProcessingException as error:
            said = [str(error)] + feedback.infos
            check(any(words in text for text in said), f"{pattern}: {said}")
        else:
            raise CheckFailed(f"{pattern} was accepted.")
    return "the live [[:digit:]] answer writes its 3 buildings (was 0); (?i), nested repeats and a stray { are refused by name"


# --------------------------------------------------------------------- E2
def _segment_hits_box(p, q, box) -> bool:
    """Liang-Barsky: does the segment p-q touch the (west, south, east, north) box?"""
    (x0, y0), (x1, y1) = p, q
    t0, t1 = 0.0, 1.0
    dx, dy = x1 - x0, y1 - y0
    for edge_p, edge_q in ((-dx, x0 - box[0]), (dx, box[2] - x0), (-dy, y0 - box[1]), (dy, box[3] - y0)):
        if edge_p == 0:
            if edge_q < 0:
                return False
            continue
        ratio = edge_q / edge_p
        if edge_p < 0:
            t0 = max(t0, ratio)
        else:
            t1 = min(t1, ratio)
        if t0 > t1:
            return False
    return True


def check_extent_drop(rig: Rig) -> str:
    from zero2agent_osm_downloader.core import postpass
    from zero2agent_osm_downloader.core.query import advanced_specs

    answer = load("postpass_ferry_overselection.json")
    ferry = next(f for f in answer["features"] if f["properties"]["osm_id"] == 452429)
    paths = ferry["geometry"]["coordinates"]
    check(not any(_segment_hits_box(a, b, KONAK_POSTPASS) for path in paths for a, b in zip(path, path[1:])),
          "The fixture's ferry was expected to miss the Konak box.")
    payload = postpass.adapt(answer)
    specs = advanced_specs((("route", "^(tram|ferry)$"),), ("line",), "any", "regex")
    rig.seed(specs, KONAK_POSTPASS, payload, value_mode="regex")
    results, out, feedback = rig.run("download_advanced", {"MATCH_MODE": 0, "GEOMETRY": 2, "VALUE_MODE": True,
                                                            "KEY_1": "route", "VALUE_1": "^(tram|ferry)$"},
                                     KONAK_POSTPASS)
    written = sorted(f["osm_id"] for f in out["lines"].getFeatures())
    check("452429" not in written, "The Thessaloniki ferry was written for a Konak box.")
    check(written == ["1026394536", "451604", "451612"], f"Written ferries: {written}")
    # Outside the chosen area is the request working: counted apart from
    # the features that could not be built or written, and not a warning.
    check(results["FEATURES_OUTSIDE"] == 1 and results["FEATURES_DROPPED"] == 0,
          f"FEATURES_OUTSIDE {results['FEATURES_OUTSIDE']}, FEATURES_DROPPED {results['FEATURES_DROPPED']}")
    check("1 matched OSM feature was left out because it lies outside the requested extent."
          in feedback.infos, f"Outside summary: {feedback.infos[-3:]}")
    check(not any("not written" in text for text in feedback.warnings), f"Warnings: {feedback.warnings}")
    return ("the Thessaloniki ferry Postpass returned for a Konak box is left out and counted as "
            "outside; the 3 local ferries stay")


# --------------------------------------------------------------------- C1
#: Three roads; the second carries a lanes value no 32-bit column holds.
ROADS = {"osm3s": {"timestamp_osm_base": "2026-11-02T06:58:39Z"}, "elements": [
    {"type": "way", "id": 1, "tags": {"highway": "primary", "lanes": "2"},
     "geometry": pairs_to_geometry([(27.101, 38.401), (27.102, 38.402)])},
    {"type": "way", "id": 2, "tags": {"highway": "primary", "lanes": "99999999999"},
     "geometry": pairs_to_geometry([(27.1011, 38.4011), (27.1021, 38.4021)])},
    {"type": "way", "id": 3, "tags": {"highway": "residential", "lanes": "1"},
     "geometry": pairs_to_geometry([(27.1012, 38.4012), (27.1022, 38.4022)])},
]}
ROAD_PARAMS = {"KEY": "highway", "VALUE": "", "GEOMETRY": 1}


def check_unwritable_values(rig: Rig) -> str:
    from zero2agent_osm_downloader.core.catalog import TagSpec
    from zero2agent_osm_downloader.core.query import normalized_specs
    from zero2agent_osm_downloader.core.schema import FIELD_NAMES

    oa = rig.oa
    specs = normalized_specs((TagSpec("highway", "", "line"),))
    rig.seed(specs, A2_BOX, ROADS)
    # An overflowing value reads as NULL: every road is written, in memory
    # and in a GeoPackage reopened from disk (1.6.0 failed the download).
    results, out, _fb = rig.run("download_custom_tag", ROAD_PARAMS, A2_BOX)
    lanes = {f["osm_id"]: f["lanes_n"] for f in out["lines"].getFeatures()}
    check(set(lanes) == {"1", "2", "3"} and lanes["1"] == 2 and lanes["3"] == 1
          and (lanes["2"] is None or str(lanes["2"]) == "NULL"), f"memory lanes_n: {lanes}")
    check(results["FEATURES_DROPPED"] == 0, f"FEATURES_DROPPED {results['FEATURES_DROPPED']}")
    _r, paths, _fb = rig.run("download_custom_tag", ROAD_PARAMS, A2_BOX, files=True)
    stored = dict(rows(paths["lines"], "osm_id, lanes_n"))
    check(stored == {"1": 2, "2": None, "3": 1}, f"GeoPackage lanes_n: {stored}")

    # A value the output still refuses costs that feature only: counted as
    # "could not be written", the other two are written.
    original = oa.feature_attributes
    lanes_at = FIELD_NAMES.index("lanes_n")

    def overflowing(only):
        def attributes(element, *args, **kwargs):
            row = original(element, *args, **kwargs)
            if only is None or element.get("id") == only:
                row[lanes_at] = 99_999_999_999
            return row
        return attributes

    try:
        for files in (False, True):
            oa.feature_attributes = overflowing(2)
            results, out, feedback = rig.run("download_custom_tag", ROAD_PARAMS, A2_BOX, files=files)
            written = (sorted(osm_id for osm_id, in rows(out["lines"], "osm_id")) if files
                       else sorted(f["osm_id"] for f in out["lines"].getFeatures()))
            check(written == ["1", "3"], f"written with one refused road (files={files}): {written}")
            check(results["FEATURES_DROPPED"] == 1,
                  f"FEATURES_DROPPED {results['FEATURES_DROPPED']} (files={files})")
            check("1 OSM feature was not written: 1 that QGIS could not write to the output."
                  in feedback.warnings, f"Drop summary (files={files}): {feedback.warnings}")
        # An output that refuses every feature is broken: the run fails and
        # says so instead of handing back an empty layer.
        oa.feature_attributes = overflowing(None)
        try:
            rig.run("download_custom_tag", ROAD_PARAMS, A2_BOX)
        except QgsProcessingException as error:
            message = str(error)
        else:
            message = ""
        check(message.startswith("QGIS could not write any of the 3 lines to the output")
              and "Check that the output file can be written" in message,
              f"Refusing output: {message!r}")
    finally:
        oa.feature_attributes = original
    return ("lanes=99999999999 is NULL and all 3 roads are written; one refused feature is "
            "counted, an output refusing all 3 fails clearly")


# --------------------------------------------------------------------- E3
def check_transform_failures(rig: Rig) -> str:
    from zero2agent_osm_downloader.core.around import circle_bbox
    from zero2agent_osm_downloader.core.catalog import get_preset

    oa = rig.oa
    # An orthographic view of the far side of the Earth: Konak cannot be shown.
    ortho = QgsCoordinateReferenceSystem.fromProj(
        "+proj=ortho +lat_0=-38.42 +lon_0=-152.87 +datum=WGS84 +units=m +no_defs")
    check(ortho.isValid(), "The orthographic CRS did not build.")
    lon, lat, radius = 27.1300, 38.4200, 300.0
    south, west, north, east = circle_bbox(lon, lat, radius)
    payload = {"elements": [
        {"type": "node", "id": 1, "lat": lat, "lon": lon, "tags": {"place": "neighbourhood", "name": "A"}},
        {"type": "node", "id": 2, "lat": lat + 0.0005, "lon": lon, "tags": {"place": "neighbourhood", "name": "B"}},
        {"type": "way", "id": 3, "tags": {"barrier": "wall"},
         "geometry": pairs_to_geometry([(lon, lat), (lon + 0.001, lat)])},
    ]}
    rig.seed(get_preset("urban_form").tags, (west, south, east, north), payload)
    params = {"PRESET": [rig.preset_index("urban_form")],
              "AROUND_X": lon, "AROUND_Y": lat, "AROUND_METERS": radius,
              "EXTENT": QgsReferencedRectangle(QgsRectangle(-1000, -1000, 1000, 1000), ortho)}
    results, out, feedback = rig.run("download_preset", params)
    check(results["FEATURES_DROPPED"] == 3, f"FEATURES_DROPPED {results['FEATURES_DROPPED']}")
    check(sum(layer.featureCount() for layer in out.values()) == 0, "A feature behind the globe was written.")
    check(any("3 that could not be projected to the output CRS" in text for text in feedback.warnings),
          f"Drop summary: {feedback.warnings}")
    behind = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), ortho, QgsProject.instance())
    check(not oa._project(QgsGeometry.fromPointXY(QgsPointXY(27.13, 38.42)), behind),
          "A point behind the globe was projected.")
    line = QgsGeometry.fromPolylineXY([QgsPointXY(27.13, 38.42), QgsPointXY(-152.0, -38.0)])
    check(not oa._project(line, behind), "A line through the far side was projected.")
    check(oa._project(QgsGeometry.fromPointXY(QgsPointXY(-152.87, -38.42)), behind),
          "The projection centre did not project.")
    return "points and lines an orthographic output CRS cannot hold are counted, not a crash"


CHECKS = (
    ("E1 stable PRESET indexes", check_preset_indexes_are_stable),
    ("D1 open ways", check_open_ways_stay_lines),
    ("D2 ring nesting", check_rings_nest),
    ("D3 drops by reason", check_drops_are_counted),
    ("D4/D5 typed columns", check_typed_columns),
    ("D6 wildcard relations", check_wildcard_relations),
    ("D7 POSIX regex", check_posix_regex),
    ("E2 extent drop", check_extent_drop),
    ("E3 transform failures", check_transform_failures),
    ("C1 unwritable values", check_unwritable_values),
)


def run_checks(work: Path) -> int:
    from zero2agent_osm_downloader.processing import osm_algorithms

    # Network is forbidden: every part comes from the seeded cache, and an
    # unseeded one fails against a closed local port.
    osm_algorithms.OVERPASS_ENDPOINTS = ("http://127.0.0.1:9/api/interpreter",)
    rig = Rig(work)
    passed = 0
    for label, function in CHECKS:
        try:
            note = function(rig)
        except Exception as error:  # a failed check is a failed test, not a crash
            traceback.print_exc()
            print(f"  [FAIL] {label}: {type(error).__name__}: {error}")
        else:
            passed += 1
            print(f"  [PASS] {label}: {note}")
        sys.stdout.flush()
    return passed


def main(exit_process: bool = False) -> bool:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    plugin_dir = Path(__file__).resolve().parent.parent
    if str(plugin_dir.parent) not in sys.path:
        sys.path.insert(0, str(plugin_dir.parent))
    app = QgsApplication.instance()
    owns_app = app is None
    profile = None
    if owns_app:
        profile = tempfile.TemporaryDirectory(prefix="zero2agent-truth-")
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

    # Keep a Python reference: a provider object that only the registry holds
    # is garbage-collected, and every algorithm then fails in createInstance().
    provider = AgentOsmProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    from zero2agent_osm_downloader.tests._offline import isolate_network

    # Off every public host before anything runs; the QGIS-wide guard only
    # in a QGIS this run started.
    isolate_network(guard_qgis=owns_app)
    print("=" * 62)
    print(" 02Agent OSM Downloader - data truth")
    print("=" * 62)
    work = tempfile.TemporaryDirectory(prefix="zero2agent-truth-out-")
    passed = 0
    try:
        passed = run_checks(Path(work.name))
    finally:
        print("-" * 62)
        print(f"  {passed}/{len(CHECKS)} passed")
        QgsProject.instance().clear()
        if owns_app:
            sys.stdout.flush()
            sys.stderr.flush()
            if exit_process:
                os._exit(0 if passed == len(CHECKS) else 1)
    return passed == len(CHECKS)


if __name__ == "__main__":
    sys.exit(0 if main(exit_process=True) else 1)
else:
    main(exit_process=True)
