# -*- coding: utf-8 -*-
"""QGIS checks on what a saved download carries and how it draws, offline.

Run under a QGIS Python (python-qgis-ltr.bat / python-qgis.bat):
    python -m zero2agent_osm_downloader.tests.qgis_persist_style

Every check reads the result back the way a later user would: a layer opened
again from the GeoPackage path, and the file's own tables through sqlite3
(never by iterating a QGIS layer, which locks the file on Windows).

- The ODbL attribution and the OSM data date are stored in the GeoPackage:
  metadata in ``gpkg_metadata``, attribution and date in
  ``gpkg_contents.description``.
- The stored style embeds the crossing icon instead of naming a folder on the
  author's disk, and the reopened layer still draws it, pixel for pixel.
- A folder with ``#``, spaces or Turkish letters takes a second and third save
  into the same file; repeated tables get ``_2`` and ``_3``.
- The polygon expressions classify real OSM rows exactly as the form that
  evaluated the building class twice did.
- OSM As-Is point and polygon labels are scale-gated in the stored style;
  street names and place names are not.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsProviderRegistry,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QSize

STAMP = "2026-10-26T08:15:02Z"
LABEL = "OSM As-Is — Konak"

#: Real elements from a live Overpass answer for central Konak, Izmir
#: (2026-11-02, ``out body geom``), trimmed to the tags the checks read:
#: ``(type, id, tags, (lon, lat) vertices)``.
#: © OpenStreetMap contributors, ODbL 1.0.
KONAK_ELEMENTS = (
    ("node", 27779129, {"amenity": "ferry_terminal", "name": "Konak Vapur İskelesi",
                        "public_transport": "station"}, ((27.1251343, 38.4187814),)),
    ("node", 6364569406, {"amenity": "cafe", "name": "Kahve Deryası"},
     ((27.1306511, 38.4211758),)),
    ("node", 9455492883, {"highway": "crossing"}, ((27.129274, 38.4216687),)),
    ("node", 11436603480, {"amenity": "pharmacy", "healthcare": "pharmacy",
                           "name": "Gözdem Eczanesi"}, ((27.1315937, 38.4223396),)),
    ("node", 14174028348, {"crossing": "traffic_signals", "highway": "crossing"},
     ((27.1318971, 38.4224051),)),
    ("way", 49409049, {"highway": "residential", "name": "861. Sokak"},
     ((27.1318139, 38.420799), (27.1321841, 38.4210936), (27.1325521, 38.4213831))),
    ("way", 1391606555, {"highway": "residential", "name": "853. Sokak"},
     ((27.1307455, 38.4213003), (27.1308284, 38.4207846), (27.1309358, 38.420542),
      (27.1311777, 38.4199057))),
    ("way", 865373167, {"building": "yes", "name": "Paykoç İş Hanı"},
     ((27.1303266, 38.4204489), (27.1301957, 38.4204354), (27.1301374, 38.4200903),
      (27.130261, 38.4200663), (27.1303266, 38.4204489))),
    ("way", 867516375, {"amenity": "place_of_worship", "building": "mosque",
                        "name": "Katipzade Mescidi"},
     ((27.1312062, 38.4204789), (27.1311703, 38.4205692), (27.1313034, 38.4206013),
      (27.1313335, 38.4205155), (27.1312062, 38.4204789))),
)

#: A path written into a style names somebody's disk: ``C:/...`` or ``D:\...``.
#: The look-behind keeps ``http://`` and ``https://`` out of it.
_DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")


def _schema_layer(geometry: str, name: str) -> QgsVectorLayer:
    """An empty memory layer with the download's attribute schema."""
    from zero2agent_osm_downloader.core.schema import FIELD_NAMES

    fields = "&".join(f"field={column}:string" for column in FIELD_NAMES)
    layer = QgsVectorLayer(f"{geometry}?crs=EPSG:4326&{fields}", name, "memory")
    if not layer.isValid():
        raise RuntimeError(f"Could not build a {geometry} test layer.")
    return layer


def _konak_layers() -> dict:
    """The real Konak elements as point, line and polygon result layers."""
    from zero2agent_osm_downloader.core.schema import TAG_COLUMNS

    layers = {
        "points": _schema_layer("Point", f"{LABEL} — Points"),
        "lines": _schema_layer("LineString", f"{LABEL} — Lines"),
        "polygons": _schema_layer("Polygon", f"{LABEL} — Polygons"),
    }
    features = {kind: [] for kind in layers}
    for osm_type, osm_id, tags, vertices in KONAK_ELEMENTS:
        points = [QgsPointXY(x, y) for x, y in vertices]
        if osm_type == "node":
            kind, geometry = "points", QgsGeometry.fromPointXY(points[0])
        elif vertices[0] == vertices[-1]:
            kind, geometry = "polygons", QgsGeometry.fromPolygonXY([points])
        else:
            kind, geometry = "lines", QgsGeometry.fromPolylineXY(points)
        feature = QgsFeature(layers[kind].fields())
        feature.setAttribute("osm_id", str(osm_id))
        feature.setAttribute("osm_type", osm_type)
        feature.setAttribute("name", tags.get("name", ""))
        for column, tag in TAG_COLUMNS:
            feature.setAttribute(column, tags.get(tag, ""))
        feature.setGeometry(geometry)
        features[kind].append(feature)
    for kind, layer in layers.items():
        if not layer.dataProvider().addFeatures(features[kind]):
            raise RuntimeError(f"Could not seed the Konak {kind}.")
        layer.updateExtents()
    return layers


def _table_of(layer: QgsVectorLayer) -> str:
    return QgsProviderRegistry.instance().decodeUri("ogr", layer.source())["layerName"]


def _reopened(path: str, table: str) -> QgsVectorLayer:
    """A new layer on ``table``, opened from the path, not the object just written."""
    layer = QgsVectorLayer(f"{path}|layername={table}", f"reopened {table}", "ogr")
    if not layer.isValid():
        raise RuntimeError(f"The saved table {table!r} does not open from {path}.")
    return layer


def _sqlite(path: str, statement: str, parameters: tuple = ()) -> list:
    """Rows read straight from the file, read-only (TRAPS 4.8)."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as connection:
        return connection.execute(statement, parameters).fetchall()


def _pixels(symbol) -> tuple:
    image = symbol.asImage(QSize(48, 48))
    return tuple(
        image.pixel(x, y) for y in range(image.height()) for x in range(image.width())
    )


def _crossing_drawing(layer: QgsVectorLayer, svg_file: str) -> tuple:
    """``(icon path, pixels as stored, pixels with the icon file instead)``.

    Everything is copied out of the renderer before it is used: a symbol
    read from a temporary category list dies with the list (TRAPS 4.9).
    """
    stored = None
    for category in layer.renderer().categories():
        if str(category.value()) == "crossing":
            stored = category.symbol().clone()
    if stored is None:
        raise RuntimeError(f"{layer.name()} has no 'crossing' category.")
    icon = stored.symbolLayer(0)
    if type(icon).__name__ != "QgsSvgMarkerSymbolLayer":
        raise RuntimeError(f"Crossings are stored as {type(icon).__name__}, not as the icon.")
    path = str(icon.path())
    from_file = stored.clone()
    replacement = from_file.symbolLayer(0).clone()
    # setPath() resets the colours to the SVG's own defaults; put them back
    # so that the icon source is the only difference.
    replacement.setPath(svg_file)
    replacement.setFillColor(icon.fillColor())
    replacement.setStrokeColor(icon.strokeColor())
    replacement.setStrokeWidth(icon.strokeWidth())
    from_file.changeSymbolLayer(0, replacement)
    return path, _pixels(stored), _pixels(from_file)


def _label_gate(layer: QgsVectorLayer) -> tuple:
    """``(labels on, scale-gated, most zoomed-out scale)`` as plain values."""
    labeling = layer.labeling()
    if labeling is None or not layer.labelsEnabled():
        return False, False, 0.0
    settings = labeling.settings()
    return True, bool(settings.scaleVisibility), float(settings.minimumScale)


def _check_attribution(work: str) -> tuple:
    from zero2agent_osm_downloader.core.map_styling import (
        apply_map_theme,
        carto_ground_layer,
    )
    from zero2agent_osm_downloader.core.persist import (
        ATTRIBUTION,
        TIMESTAMP_PROPERTY,
        apply_provenance,
        write_geopackage,
    )

    # What the dock does with a finished download: style, provenance, save.
    styled = []
    for suffix, layer in _konak_layers().items():
        apply_map_theme(layer, "osm_as_is")
        apply_provenance(layer, STAMP, LABEL)
        styled.append((layer, suffix))
    ground = carto_ground_layer(
        QgsRectangle(27.125, 38.418, 27.132, 38.4225),
        QgsCoordinateReferenceSystem("EPSG:4326"),
    )
    styled.append((ground, "ground"))
    gpkg = os.path.join(work, "konak.gpkg")
    tables = [_table_of(layer) for layer in write_geopackage(styled, gpkg, LABEL)]
    osm_tables = tables[:3]
    when = f"OSM data as of {STAMP} (UTC)"
    for table in osm_tables:
        reopened = _reopened(gpkg, table)
        metadata = reopened.metadata()
        if ATTRIBUTION not in metadata.rights():
            raise RuntimeError(f"{table} reopened without the attribution: {metadata.rights()}")
        if "ODbL-1.0" not in metadata.licenses():
            raise RuntimeError(f"{table} reopened without the licence: {metadata.licenses()}")
        if LABEL not in metadata.abstract() or STAMP not in metadata.abstract():
            raise RuntimeError(f"{table} reopened without its request: {metadata.abstract()!r}")
        if when not in metadata.history():
            raise RuntimeError(f"{table} reopened without the data date: {metadata.history()}")
        if reopened.customProperty(TIMESTAMP_PROPERTY) != STAMP:
            raise RuntimeError(f"{table} reopened without its timestamp property.")
        stored = _sqlite(
            gpkg,
            "SELECT m.md_standard_uri, m.metadata FROM gpkg_metadata AS m"
            " JOIN gpkg_metadata_reference AS r ON r.md_file_id = m.id"
            " WHERE r.table_name = ?",
            (table,),
        )
        documents = [text for standard, text in stored if standard == "http://mrcc.com/qgis.dtd"]
        if len(documents) != 1 or "OpenStreetMap" not in documents[0] or STAMP not in documents[0]:
            raise RuntimeError(f"gpkg_metadata holds no QGIS attribution for {table}: {stored}")
        # GDAL files a copy of that document in its own format for every table
        # but the last one saved (measured: once, it does not grow). A reader of
        # either copy must still find the attribution.
        if len(stored) > 2 or not all("OpenStreetMap" in text for _standard, text in stored):
            raise RuntimeError(f"gpkg_metadata rows of {table} disagree: {stored}")
        description = _sqlite(
            gpkg, "SELECT description FROM gpkg_contents WHERE table_name = ?", (table,)
        )
        if (description != [(metadata.abstract(),)] or ATTRIBUTION not in description[0][0]
                or when not in description[0][0]):
            raise RuntimeError(f"gpkg_contents.description of {table} is {description}")
    if _sqlite(gpkg, "SELECT description FROM gpkg_contents WHERE table_name = ?",
               (tables[3],)) != [("",)]:
        raise RuntimeError("The ground polygon, which is not OSM data, was attributed to OSM.")
    return gpkg, tables


def _check_style(gpkg: str, tables: list) -> str:
    from zero2agent_osm_downloader.core.map_styling import (
        CARTO_LABEL_MIN_SCALE,
        PLUGIN_SVG_DIR,
        apply_map_theme,
        plugin_svg,
    )

    styles = dict(_sqlite(gpkg, "SELECT f_table_name, styleQML FROM layer_styles"))
    if sorted(styles) != sorted(tables):
        raise RuntimeError(f"Stored styles {sorted(styles)} do not match the tables {tables}.")
    home = os.path.expanduser("~")
    personal = {text.lower() for text in (
        home, home.replace("\\", "/"), PLUGIN_SVG_DIR, PLUGIN_SVG_DIR.replace("\\", "/"),
    )}
    for table, qml in styles.items():
        found = _DRIVE_PATH.search(qml)
        if found:
            raise RuntimeError(f"The style of {table} names a local path: "
                               f"{qml[found.start():found.start() + 80]!r}")
        if any(text in qml.lower() for text in personal) or "crossing.svg" in qml:
            raise RuntimeError(f"The style of {table} points into the user's profile.")
    points_table = tables[0]
    if "base64:" not in styles[points_table]:
        raise RuntimeError("The points style does not embed the crossing icon.")
    reopened = _reopened(gpkg, points_table)
    path, stored, from_file = _crossing_drawing(reopened, plugin_svg("crossing.svg"))
    if not path.startswith("base64:"):
        raise RuntimeError(f"The reopened crossing icon reads from {path[:60]!r}.")
    if stored != from_file:
        raise RuntimeError("The embedded crossing icon draws differently from the icon file.")
    if sum(1 for pixel in stored if pixel >> 24) < 50:
        raise RuntimeError("The reopened crossing icon draws nothing.")

    # Labels: points and polygons gated, streets not -- read from the file.
    expected = {tables[0]: True, tables[1]: False, tables[2]: True}
    for table, gated in expected.items():
        enabled, scale_gated, scale = _label_gate(_reopened(gpkg, table))
        if not enabled:
            raise RuntimeError(f"{table} reopened with its labels off.")
        if scale_gated != gated or (gated and scale != CARTO_LABEL_MIN_SCALE):
            raise RuntimeError(
                f"{table} labels: gated={scale_gated} at 1:{scale:,.0f}; "
                f"expected gated={gated} at 1:{CARTO_LABEL_MIN_SCALE:,.0f}."
            )
    places = _schema_layer("Point", "places")
    places.setCustomProperty("zero2agent/style_scope", "places")
    apply_map_theme(places, "osm_as_is")
    if _label_gate(places) != (True, False, 0.0):
        raise RuntimeError(f"Place names were scale-gated: {_label_gate(places)}")
    return "the stored style embeds the crossing icon, draws it, and gates POI and area labels"


def _check_awkward_folders(work: str) -> str:
    from zero2agent_osm_downloader.core.persist import (
        ATTRIBUTION,
        apply_provenance,
        write_geopackage,
    )

    points = _konak_layers()["points"]
    apply_provenance(points, STAMP, LABEL)
    for folder in ("Maps #2", "Şehir İçi ğüşöç", "with  two spaces"):
        path = os.path.join(work, folder, "konak.gpkg")
        names = []
        for _save in range(3):
            saved = write_geopackage([(points, "points")], path, LABEL)
            names.extend(_table_of(layer) for layer in saved)
        base = names[0]
        if names != [base, f"{base}_2", f"{base}_3"]:
            raise RuntimeError(f"Saves into {folder!r} wrote {names}.")
        stored = sorted(row[0] for row in _sqlite(path, "SELECT table_name FROM gpkg_contents"))
        if stored != sorted(names + ["layer_styles"]):
            raise RuntimeError(f"{folder!r} holds {stored}, expected {names}.")
        if ATTRIBUTION not in _reopened(path, names[-1]).metadata().rights():
            raise RuntimeError(f"The third save into {folder!r} lost the attribution.")
    return "folders with '#', spaces and Turkish letters take repeated saves (_2, _3)"


def _classify(expression_text: str, rows: list, empty) -> list:
    from zero2agent_osm_downloader.core.map_styling_expressions import STYLING_COLUMNS

    layer = _schema_layer("Polygon", "rows")
    features = []
    for row in rows:
        feature = QgsFeature(layer.fields())
        for column in STYLING_COLUMNS:
            feature.setAttribute(column, row.get(column, empty))
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    expression = QgsExpression(expression_text)
    if expression.hasParserError():
        raise RuntimeError(f"Expression does not parse: {expression.parserErrorString()}")
    context = QgsExpressionContext()
    context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))
    expression.prepare(context)
    classes = []
    for feature in layer.getFeatures():
        context.setFeature(feature)
        classes.append(expression.evaluate(context))
        if expression.hasEvalError():
            raise RuntimeError(f"Expression failed: {expression.evalErrorString()}")
    return classes


def _check_classification() -> str:
    from zero2agent_osm_downloader.core.map_styling_expressions import (
        BUILDING_EXPRESSION,
        BUILDING_GUARD,
        CARTO_POLYGON_EXPRESSION,
        POLYGON_EXPRESSION,
        expression_values,
    )

    rows = list(REAL_ROWS) + [{"building": value} for value in REAL_BUILDING_VALUES]
    guarded = f" WHEN {BUILDING_GUARD} THEN ({BUILDING_EXPRESSION})"
    twice = f" WHEN ({BUILDING_EXPRESSION}) <> '' THEN ({BUILDING_EXPRESSION})"
    pinned = (
        ({"building": "Commercial"}, "building_commercial", "building_commercial"),
        ({"building": "no"}, "building_other", "building_other"),
        ({"amenity": "place_of_worship", "building": "mosque"}, "building_worship",
         "building_worship"),
        ({"landuse": "grass"}, "green", "green"),
        ({}, "other", "other"),
        ({"admin_level": "6", "boundary": "administrative", "natural": "coastline"},
         "boundary", "admin_6"),
    )
    for index, (name, shipped) in enumerate((
        ("CARTO_POLYGON_EXPRESSION", CARTO_POLYGON_EXPRESSION),
        ("POLYGON_EXPRESSION", POLYGON_EXPRESSION),
    )):
        if shipped.count(guarded) != 1 or shipped.count(BUILDING_EXPRESSION) != 1:
            raise RuntimeError(f"{name} does not evaluate the building class once.")
        reference = shipped.replace(guarded, twice)
        for empty in ("", None):  # absent tags as written today, and as NULL
            got = _classify(shipped, rows, empty)
            want = _classify(reference, rows, empty)
            differ = [(row, a, b) for row, a, b in zip(rows, got, want) if a != b]
            if differ:
                raise RuntimeError(f"{name} reclassified {len(differ)} real rows: {differ[:3]}")
            produced = set(got)
            if len(produced) < 15 or not produced <= expression_values(shipped):
                raise RuntimeError(f"{name} produced {sorted(map(str, produced))}.")
        got = _classify(shipped, [row for row, _carto, _analytic in pinned], "")
        if got != [expected[1 + index] for expected in pinned]:
            raise RuntimeError(f"{name} pinned rows classify as {got}.")
    return (f"{len(rows)} real rows classify identically to the double evaluation, "
            "absent tags as '' and as NULL")


def run_checks() -> str:
    work = tempfile.mkdtemp(prefix="z2a-persist-")
    try:
        gpkg, tables = _check_attribution(work)
        notes = ["attribution, licence and data date reopen from the GeoPackage and sit "
                 "in gpkg_metadata and gpkg_contents.description"]
        notes.append(_check_style(gpkg, tables))
        notes.append(_check_awkward_folders(work))
        notes.append(_check_classification())
        return "; ".join(notes) + "."
    finally:
        # Best effort: Windows keeps a file QGIS still holds.
        shutil.rmtree(work, ignore_errors=True)


def main(exit_process: bool = False) -> bool:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    plugin_dir = Path(__file__).resolve().parent.parent
    if str(plugin_dir.parent) not in sys.path:
        sys.path.insert(0, str(plugin_dir.parent))
    app = QgsApplication.instance()
    owns_app = app is None
    profile = None
    if owns_app:
        profile = tempfile.TemporaryDirectory(prefix="zero2agent-persist-")
        app = QgsApplication([], True, profile.name, "external")
        app.initQgis()
    from zero2agent_osm_downloader.tests._offline import isolate_network

    # Off every public host before anything runs; the QGIS-wide guard only
    # in a QGIS this run started.
    isolate_network(guard_qgis=owns_app)
    print("=" * 62)
    print(" 02Agent OSM Downloader - saved file and styling checks")
    print("=" * 62)
    ok = False
    try:
        print(f"  [PASS] {run_checks()}")
        ok = True
    except Exception as error:  # a failed check is a failed test, not a crash
        import traceback
        traceback.print_exc()
        print(f"  [FAIL] {type(error).__name__}: {error}")
    finally:
        print("-" * 62)
        print(f"  {1 if ok else 0}/1 passed")
        QgsProject.instance().clear()
        if owns_app:
            sys.stdout.flush()
            sys.stderr.flush()
            if exit_process:
                os._exit(0 if ok else 1)
    return ok


#: Real OSM attribute values, read from turkey-latest.osm.pbf (2026-10-26):
#: every distinct `building` value on the 9.6 million tagged ways and
#: relations of Turkiye, and every distinct combination of the polygon
#: expression's columns that occurs 1,000 times or more (or 100 times with
#: three or more columns), plus every row of the live Konak answer above.
#: © OpenStreetMap contributors, ODbL 1.0.
REAL_BUILDING_VALUES = (
    "5858. Sokak", ":part=yes", "<i>", "A", "B", "Bina", "Civil_service",
    "Commercial", "Community_Services", "Dorado Restaurant", "EV",
    "Ege eveleri", "Factory", "Government_Opera_and_Theathe", "Kent Sokak",
    "Kilise", "Muhtarlık", "NGO", "Netsel Marina", "Okul", "Otel", "R",
    "Romney", "Soyak_Sitesi",
    "Union_of_Chambers_of_Turkish_Engineers_and_Architects", "abandoned",
    "administrative", "agricultural", "airport", "airport_terminal", "alley",
    "allotment_house", "alumni_association", "amenity=training",
    "amphitheatre", "annexe", "apartments", "apartments;yes", "arcade", "atm",
    "b", "bakehouse", "bank", "barn", "barracks", "basilica", "bath",
    "beach_hut", "bina", "bleachers", "block", "boathouse", "border_control",
    "bridge", "bungalow", "bunker", "bus_station", "cabin", "cafe", "camii",
    "cami̇", "canopy", "car_repair", "caravanseray", "carport", "castle",
    "cathedral", "cemevi", "central_office", "ceya_konakları", "chalet",
    "chapel", "church", "cinema", "civic", "clinic", "coach", "collapsed",
    "college", "commerc", "commercial", "communication_tower",
    "community_center", "community_centre", "con", "conference_centre",
    "construction", "container", "control tower", "courthouse", "cowshed",
    "cultural", "dam", "damaged", "data_center", "demolished", "depot",
    "destroyed", "detached", "digester", "disused", "ditch", "dormitory",
    "eczane", "education", "education center", "educational", "electric",
    "electricity", "elevator", "eme", "emergency", "event", "factory", "farm",
    "farm_auxiliary", "farms", "fire_station", "food", "foof", "fort",
    "fortress", "foundations", "frontier_post", "fuel", "garage", "garage;yes",
    "garages", "garden_house", "gas_holder", "gasometer", "gate", "gatehous",
    "gatehouse", "gateway", "gazebo", "gecekondu", "ger", "goverment_agency",
    "government", "government_office", "grandstand", "gre", "green",
    "greenhouse", "ground", "ground_station", "guardhouse", "guest_house",
    "güvenlik", "hamam", "han", "hangar", "hastane", "havuz", "healthcare",
    "heating_station", "heritage", "high school", "historic", "historical",
    "hospital", "hospital;yes;roof", "hotel", "house", "house 2", "house 3",
    "house 4", "house 7", "house 9", "hut", "ind", "industrial", "inn", "kafe",
    "kiln", "kindergarten", "kiosk", "lib", "library", "lighthouse",
    "livestock", "lodging", "madrasa", "mall", "man_made=oven", "manor",
    "mansion", "manufacture", "market", "marketplace", "mausoleum", "medrese",
    "metasuit", "military", "minaret", "mobilya", "monastery", "monument",
    "moo", "mosque", "mosque;yes", "museum", "müstakil_villa", "müze", "no",
    "not:building=yes", "observation", "observatory", "office", "official",
    "orduevi", "otel_binası", "outbuilding", "palace", "parking", "pavilion",
    "phone_box", "plaza", "police", "policlinic", "pool", "post_office",
    "preserved", "prison", "pub", "public", "public_building",
    "pumping_station", "radar", "railway", "razed", "religious", "research",
    "residence", "residential", "restaurant", "retail", "retailq",
    "riding_hall", "roof", "roof;yes", "ruin", "ruins", "saracoglu_evi",
    "school", "security", "security_booth", "semidetached_house", "service",
    "services", "shed", "sheepfold", "shelter", "ship", "shipya", "shop",
    "shopping_centre", "shrine", "silo", "spiritual", "sport_centre",
    "sports_centre", "sports_hall", "stable", "stadium", "stage",
    "state agency", "static_caravan", "storage_tank", "store",
    "student_accomodation", "substation", "supermarket", "support_tower",
    "synagogue", "tank", "tarihi_bina", "taxi", "telephone_exchange", "temple",
    "tent", "terrace", "terrace_lounge", "the_alsancak_apartmanı", "theatre",
    "tiny_house", "toilets", "tomb", "toplu_konut", "tower", "townhall",
    "trafo", "train_station", "transformer", "transformer_tower",
    "transportation", "tree_house", "tribune", "triumphal_arch",
    "tunnel_entrance", "turkish_bath", "underground", "university",
    "unspecified_building_typeg", "uğur", "ventilation_shaft", "villa",
    "wagon", "warehouse", "water_tower", "windmill", "wine_press_house", "wo",
    "workshop", "yes", "yes;apartments", "yes;roof", "çi̇ğköfteci̇_sai̇t",
)

REAL_ROWS = (
    {},
    {"amenity": "atm"},
    {"amenity": "bank"},
    {"amenity": "cafe"},
    {"amenity": "charging_station"},
    {"amenity": "community_centre"},
    {"amenity": "fast_food"},
    {"amenity": "ferry_terminal"},
    {"amenity": "fountain"},
    {"amenity": "parking"},
    {"amenity": "parking_space"},
    {"amenity": "payment_centre"},
    {"amenity": "police"},
    {"amenity": "post_office"},
    {"amenity": "pub"},
    {"amenity": "restaurant"},
    {"amenity": "school"},
    {"amenity": "taxi"},
    {"amenity": "telephone"},
    {"amenity": "toilets"},
    {"amenity": "university"},
    {"boundary": "administrative"},
    {"boundary": "political"},
    {"boundary": "postal_code"},
    {"building": "apartments"},
    {"building": "commercial"},
    {"building": "construction"},
    {"building": "cowshed"},
    {"building": "detached"},
    {"building": "dormitory"},
    {"building": "government"},
    {"building": "greenhouse"},
    {"building": "hospital"},
    {"building": "hotel"},
    {"building": "house"},
    {"building": "industrial"},
    {"building": "public"},
    {"building": "residential"},
    {"building": "retail"},
    {"building": "roof"},
    {"building": "ruins"},
    {"building": "school"},
    {"building": "semidetached_house"},
    {"building": "service"},
    {"building": "shed"},
    {"building": "storage_tank"},
    {"building": "train_station"},
    {"building": "university"},
    {"building": "warehouse"},
    {"building": "yes"},
    {"highway": "bus_stop"},
    {"highway": "construction"},
    {"highway": "crossing"},
    {"highway": "cycleway"},
    {"highway": "footway"},
    {"highway": "living_street"},
    {"highway": "motorway"},
    {"highway": "motorway_link"},
    {"highway": "path"},
    {"highway": "pedestrian"},
    {"highway": "primary"},
    {"highway": "primary_link"},
    {"highway": "proposed"},
    {"highway": "residential"},
    {"highway": "secondary"},
    {"highway": "secondary_link"},
    {"highway": "service"},
    {"highway": "steps"},
    {"highway": "tertiary"},
    {"highway": "tertiary_link"},
    {"highway": "track"},
    {"highway": "trunk"},
    {"highway": "trunk_link"},
    {"highway": "unclassified"},
    {"landuse": "brownfield"},
    {"landuse": "cemetery"},
    {"landuse": "commercial"},
    {"landuse": "construction"},
    {"landuse": "farmland"},
    {"landuse": "farmyard"},
    {"landuse": "forest"},
    {"landuse": "grass"},
    {"landuse": "greenhouse_horticulture"},
    {"landuse": "industrial"},
    {"landuse": "landfill"},
    {"landuse": "meadow"},
    {"landuse": "military"},
    {"landuse": "orchard"},
    {"landuse": "quarry"},
    {"landuse": "residential"},
    {"landuse": "vineyard"},
    {"leisure": "garden"},
    {"leisure": "park"},
    {"leisure": "pitch"},
    {"leisure": "playground"},
    {"leisure": "swimming_pool"},
    {"natural": "bare_rock"},
    {"natural": "bay"},
    {"natural": "beach"},
    {"natural": "cliff"},
    {"natural": "coastline"},
    {"natural": "grassland"},
    {"natural": "heath"},
    {"natural": "ridge"},
    {"natural": "scree"},
    {"natural": "scrub"},
    {"natural": "shingle"},
    {"natural": "tree"},
    {"natural": "tree_row"},
    {"natural": "water"},
    {"natural": "wetland"},
    {"natural": "wood"},
    {"waterway": "canal"},
    {"waterway": "dam"},
    {"waterway": "ditch"},
    {"waterway": "drain"},
    {"waterway": "river"},
    {"waterway": "stream"},
    {"admin_level": "4", "boundary": "administrative"},
    {"admin_level": "6", "boundary": "administrative"},
    {"admin_level": "8", "boundary": "administrative"},
    {"amenity": "cafe", "building": "yes"},
    {"amenity": "library", "building": "ship"},
    {"amenity": "pharmacy", "healthcare": "pharmacy"},
    {"amenity": "place_of_worship", "building": "mosque"},
    {"amenity": "place_of_worship", "building": "yes"},
    {"amenity": "restaurant", "building": "yes"},
    {"amenity": "shelter", "building": "yes"},
    {"amenity": "toilets", "building": "toilets"},
    {"leisure": "pitch", "sport": "basketball"},
    {"leisure": "pitch", "sport": "soccer"},
    {"leisure": "pitch", "sport": "tennis"},
    {"leisure": "swimming_pool", "sport": "swimming"},
    {"natural": "water", "water": "pond"},
    {"natural": "water", "water": "reservoir"},
    {"natural": "water", "water": "river"},
    {"admin_level": "6", "boundary": "administrative", "natural": "coastline"},
    {"amenity": "clinic", "building": "yes", "healthcare": "clinic"},
    {"amenity": "hospital", "building": "hospital", "healthcare": "hospital"},
    {"amenity": "hospital", "emergency": "yes", "healthcare": "hospital"},
    {"landuse": "basin", "natural": "water", "water": "wastewater"},
)


if __name__ == "__main__":
    sys.exit(0 if main(exit_process=True) else 1)
else:
    main(exit_process=True)
