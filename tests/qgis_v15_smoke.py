"""QGIS smoke for the v1.5.0 features, offline (every response is seeded).

Run under a QGIS Python (python-qgis-ltr.bat / python-qgis.bat):
    python -m zero2agent_osm_downloader.tests.qgis_v15_smoke

Covers: the around search (a feature outside the circle is dropped even
though Overpass returned it), the OSM data date returned as OSM_TIMESTAMP and
written into layer metadata, the three new area modes, the heavy-wildcard
confirmation, recent downloads and "Run again", and GeoPackage output with
the style stored inside the file.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProcessingUtils,
    QgsProject,
    QgsRectangle,
    QgsReferencedRectangle,
    QgsVectorLayer,
)

STAMP = "2026-10-26T08:15:02Z"


class _Iface:
    """The two iface calls the dock makes for area modes."""

    def __init__(self, canvas, layer=None):
        self._canvas = canvas
        self._layer = layer

    def mapCanvas(self):  # noqa: N802 - QGIS naming
        return self._canvas

    def activeLayer(self):  # noqa: N802 - QGIS naming
        return self._layer


def _points_layer(points, crs="EPSG:4326"):
    layer = QgsVectorLayer(f"Point?crs={crs}&field=name:string", "pts", "memory")
    feats = []
    for index, (x, y) in enumerate(points):
        feature = QgsFeature(layer.fields())
        feature.setAttributes([f"p{index}"])
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        feats.append(feature)
    layer.dataProvider().addFeatures(feats)
    layer.updateExtents()
    return layer


def run_checks() -> str:
    import processing
    from qgis.gui import QgsMapCanvas

    from zero2agent_osm_downloader.core.around import circle_bbox
    from zero2agent_osm_downloader.core.catalog import PRESETS, get_preset
    from zero2agent_osm_downloader.core.persist import (
        TIMESTAMP_PROPERTY,
        apply_provenance,
        write_geopackage,
    )
    from zero2agent_osm_downloader.core.query import build_queries
    from zero2agent_osm_downloader.dialogs.dock import AgentOsmDock
    from zero2agent_osm_downloader.processing import osm_algorithms

    notes = []

    # Network is forbidden: every response this test needs is seeded.
    def _cache_only(query, _feedback):
        payload = osm_algorithms._cached(query)
        if payload is None:
            raise RuntimeError("A v1.5 smoke query was not seeded:\n" + query)
        return payload

    osm_algorithms._fetch_json = _cache_only

    # ---------------------------------------------------------- around search
    lon, lat, radius = 27.1300, 38.4200, 300.0
    bbox = circle_bbox(lon, lat, radius)
    specs = get_preset("urban_form").tags
    queries = build_queries(specs, bbox, "any")
    south, west, north, east = bbox
    payload = {
        "osm3s": {"timestamp_osm_base": STAMP},
        "elements": [
            {"type": "node", "id": 11, "lat": lat, "lon": lon,
             "tags": {"place": "neighbourhood", "name": "Inside"}},
            # Inside the request rectangle, outside the circle: the corner.
            {"type": "node", "id": 12, "lat": north - 1e-5, "lon": east - 1e-5,
             "tags": {"place": "neighbourhood", "name": "Corner"}},
        ],
    }
    for query in queries:
        osm_algorithms._cache().set(query, payload)
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    preset_index = next(i for i, p in enumerate(PRESETS) if p.preset_id == "urban_form")
    results = processing.run(
        "zero2agentosm:download_preset",
        {
            "PRESET": preset_index,
            "EXTENT": QgsReferencedRectangle(
                QgsRectangle(26.0, 37.0, 26.1, 37.1),  # ignored: the circle rules
                QgsCoordinateReferenceSystem("EPSG:4326"),
            ),
            "AROUND_X": lon, "AROUND_Y": lat, "AROUND_METERS": radius,
            "OUTPUT_POINTS": "TEMPORARY_OUTPUT",
            "OUTPUT_LINES": "TEMPORARY_OUTPUT",
            "OUTPUT_POLYGONS": "TEMPORARY_OUTPUT",
        },
        context=context, feedback=feedback, is_child_algorithm=True,
    )
    points = QgsProcessingUtils.mapLayerFromString(results["OUTPUT_POINTS"], context, True)
    names = sorted(f["name"] for f in points.getFeatures())
    if names != ["Inside"]:
        raise RuntimeError(f"Around search kept {names}; only 'Inside' touches the circle.")
    if results.get("OSM_TIMESTAMP") != STAMP:
        raise RuntimeError(f"OSM_TIMESTAMP is {results.get('OSM_TIMESTAMP')!r}, not {STAMP}.")
    try:
        processing.run("zero2agentosm:download_preset", {
            "PRESET": preset_index,
            "EXTENT": QgsReferencedRectangle(QgsRectangle(26.0, 37.0, 26.1, 37.1),
                                             QgsCoordinateReferenceSystem("EPSG:4326")),
            "AROUND_X": lon, "AROUND_Y": lat, "AROUND_METERS": 9000.0,
            "OUTPUT_POINTS": "TEMPORARY_OUTPUT", "OUTPUT_LINES": "TEMPORARY_OUTPUT",
            "OUTPUT_POLYGONS": "TEMPORARY_OUTPUT",
        }, context=context, feedback=QgsProcessingFeedback(), is_child_algorithm=True)
        raise RuntimeError("A 9 km radius was accepted; the limit is 5 km.")
    except Exception as error:  # noqa: BLE001 - Processing wraps the refusal
        if "9 km radius was accepted" in str(error):
            raise
    notes.append("around search drops the circle's corner and reports the data date")

    # ---------------------------------------------------------- area modes
    canvas = QgsMapCanvas()
    canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    canvas.resize(400, 400)
    canvas.setExtent(QgsRectangle(27.10, 38.40, 27.14, 38.43))
    selected = _points_layer([(27.11, 38.41), (27.12, 38.42)])
    selected.selectAll()
    dock = AgentOsmDock(_Iface(canvas, selected))
    modes = {dock.extent_combo.itemData(i) for i in range(dock.extent_combo.count())}
    if not {"selection", "drawn", "around"} <= modes:
        raise RuntimeError(f"Area modes missing: {modes}")
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("selection"))
    box = dock._extent()
    if box is None or abs(box.xMinimum() - 27.11) > 1e-9 or abs(box.yMaximum() - 38.42) > 1e-9:
        raise RuntimeError(f"Selection extent is wrong: {box}")
    single = _points_layer([(27.11, 38.41)])
    single.selectAll()
    dock.iface._layer = single
    if dock._extent() is not None or "Around a point" not in dock._missing_extent_message():
        raise RuntimeError("A one-point selection must be refused with a pointer to Around.")
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("drawn"))
    if dock._extent() is not None:
        raise RuntimeError("Draw mode returned an extent before anything was drawn.")
    dock._extent_drawn(QgsRectangle(27.101, 38.401, 27.105, 38.404))
    drawn = dock._extent()
    if drawn is None or abs(drawn.xMaximum() - 27.105) > 1e-9:
        raise RuntimeError(f"The drawn rectangle was not kept: {drawn}")
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("around"))
    dock.around_centre_combo.setCurrentIndex(dock.around_centre_combo.findData("map"))
    dock.around_radius.setValue(1000)
    around_box = dock._extent()
    if around_box is None or not (0.017 < around_box.height() < 0.019):
        raise RuntimeError(f"Around extent is not a 2 km box: {around_box}")
    dock.around_centre_combo.setCurrentIndex(dock.around_centre_combo.findData("place"))
    if dock._extent() is not None:
        raise RuntimeError("Around a searched place worked with no place searched.")
    notes.append("selection, drawn and around areas resolve and refuse correctly")

    # ---------------------------------------------------------- heavy warning
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("map"))
    canvas.setExtent(QgsRectangle(27.10, 38.40, 27.14, 38.43))  # ~10 km²
    asked, submitted = [], []
    dock._confirm_heavy_request = lambda area: asked.append(area) or False
    dock._submit = lambda algorithm_id, parameters, extent: submitted.append(
        (algorithm_id, dict(parameters), extent))
    dock.tabs.setCurrentWidget(dock.download_tab)
    dock.custom_check.setChecked(False)
    dock._select_preset("osm_as_is_full_visible_stack")
    dock._download()
    if len(asked) != 1 or submitted:
        raise RuntimeError(f"Wildcard over ~10 km² must ask first and stop on No: {asked}, {submitted}")
    dock._select_preset("buildings")
    dock._download()
    if len(asked) != 1 or len(submitted) != 1:
        raise RuntimeError("A filtered preset must not trigger the wildcard warning.")
    notes.append("an unfiltered pull over 2 km² asks first; a filtered one does not")

    # ---------------------------------------------------------- history
    del dock._submit
    rerun = []
    dock._submit = lambda algorithm_id, parameters, extent: rerun.append(
        (algorithm_id, dict(parameters), QgsRectangle(extent)))
    extent = QgsReferencedRectangle(QgsRectangle(27.11, 38.41, 27.12, 38.42),
                                    QgsCoordinateReferenceSystem("EPSG:4326"))
    dock._pending_request = ("zero2agentosm:download_preset", {"PRESET": [3]}, extent)
    dock._current_label = "Smoke history"
    dock._current_style_scope = ""
    dock._remember_download("Points: 1")
    if dock.recent_combo.currentData() is None or "Smoke history" not in dock.recent_combo.currentText():
        raise RuntimeError("The finished download was not listed under Recent.")
    dock._rerun_recent()
    if not rerun or rerun[0][1] != {"PRESET": [3]} or rerun[0][2] != QgsRectangle(extent):
        raise RuntimeError(f"Run again did not repeat the request: {rerun}")
    notes.append("Recent remembers a download and Run again repeats it exactly")

    # ---------------------------------------------------------- GeoPackage
    work = tempfile.mkdtemp(prefix="z2a-gpkg-")
    gpkg = str(Path(work) / "downloads.gpkg")
    pts = _points_layer([(27.11, 38.41), (27.12, 38.42)])
    pts.setName("Parks — Points")
    dock._style_layer(pts, dock.current_map_theme(), dock.current_road_width_mode())
    apply_provenance(pts, STAMP, "Parks — Konak")
    written = write_geopackage([(pts, "points")], gpkg, "Parks — Konak")
    again = write_geopackage([(pts, "points")], gpkg, "Parks — Konak")
    for layer in written + again:
        if layer.providerType() != "ogr" or layer.featureCount() != 2:
            raise RuntimeError(f"A saved table did not reopen with its features: {layer.source()}")
        if layer.renderer().type() != pts.renderer().type():
            raise RuntimeError("The saved layer lost the downloaded style.")
        if layer.customProperty(TIMESTAMP_PROPERTY) != STAMP:
            raise RuntimeError("The saved layer lost its OSM data date.")
        if not any("OpenStreetMap" in right for right in layer.metadata().rights()):
            raise RuntimeError("The saved layer lost the OSM attribution.")
    tables = sorted(layer.source().split("layername=")[1] for layer in written + again)
    if tables != ["parks_konak_points", "parks_konak_points_2"]:
        raise RuntimeError(f"Second save must add a table, not overwrite: {tables}")
    with sqlite3.connect(gpkg) as connection:
        styles = connection.execute(
            "SELECT f_table_name, useAsDefault FROM layer_styles ORDER BY f_table_name"
        ).fetchall()
    if [row[0] for row in styles] != tables or not all(row[1] for row in styles):
        raise RuntimeError(f"Styles were not stored as defaults in the GeoPackage: {styles}")
    notes.append("GeoPackage keeps data, default style, date and attribution; a second save adds a table")

    # ---------------------------------------------------------- dock end to end
    store_context = QgsProcessingContext()
    store_context.setProject(QgsProject.instance())
    memory = _points_layer([(27.11, 38.41)])
    store_context.temporaryLayerStore().addMapLayer(memory)
    dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("gpkg"))
    dock.gpkg_widget.setFilePath(str(Path(work) / "dock.gpkg"))
    dock._current_label = "Dock flow"
    dock._current_extent = None
    added = dock._add_result_layers(
        {"OUTPUT_POINTS": memory.id(), "OSM_TIMESTAMP": STAMP}, store_context)
    if not any(item.startswith("saved to") for item in added):
        raise RuntimeError(f"The dock did not save to the GeoPackage: {added}")
    loaded = [layer for layer in QgsProject.instance().mapLayers().values()
              if layer.name() == "Dock flow — Points"]
    if len(loaded) != 1 or loaded[0].providerType() != "ogr":
        raise RuntimeError("The project received a temporary layer instead of the saved one.")
    if loaded[0].customProperty(TIMESTAMP_PROPERTY) != STAMP:
        raise RuntimeError("The dock flow dropped the OSM data date.")
    notes.append("the dock adds the file-backed layer, not the temporary one")
    return "; ".join(notes) + "."


def main(exit_process: bool = False) -> bool:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    plugin_dir = Path(__file__).resolve().parent.parent
    if str(plugin_dir.parent) not in sys.path:
        sys.path.insert(0, str(plugin_dir.parent))
    app = QgsApplication.instance()
    owns_app = app is None
    profile = None
    if owns_app:
        profile = tempfile.TemporaryDirectory(prefix="zero2agent-v15-")
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

    # Keep a Python reference: a provider object that only the registry holds
    # is garbage-collected, and every algorithm then fails in createInstance().
    provider = AgentOsmProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    print("=" * 62)
    print(" 02Agent OSM Downloader - v1.5 feature smoke")
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


if __name__ == "__main__":
    sys.exit(0 if main(exit_process=True) else 1)
else:
    main(exit_process=True)
