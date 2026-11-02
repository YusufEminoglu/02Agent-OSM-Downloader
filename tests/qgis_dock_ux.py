"""The real dock in real QGIS: the first-five-minutes fixes of v1.6.

Run under a QGIS Python (python-qgis-ltr.bat / python-qgis.bat), from the
directory that contains the plugin folder:

    python -m zero2agent_osm_downloader.tests.qgis_dock_ux

Offline: no request leaves this process. The place search is answered by a
stand-in for ``resolve_place`` (the parsing of saved real Nominatim answers is
covered by tests/test_geocoding.py), downloads stop at ``_submit``, and result
layers are memory layers handed to ``_add_result_layers``.

Pixel checks need a real font engine (TRAPS 4.3), so on Windows the module
uses the "windows" platform with every window marked WA_DontShowOnScreen:
nothing appears on screen.
"""
from __future__ import annotations

import configparser
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")

from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsPointXY,
    QgsProcessingContext,
    QgsProject,
    QgsRectangle,
    QgsReferencedRectangle,
    QgsSettings,
    QgsVectorLayer,
)

PLUGIN_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
STAMP = "2026-11-02T05:40:12Z"
SETTINGS = (
    "zero2agent_osm_downloader/map_theme",
    "zero2agent_osm_downloader/road_width_mode",
    "zero2agent_osm_downloader/save_mode",
    "zero2agent_osm_downloader/geopackage_path",
    "zero2agent_osm_downloader/recent_downloads",
)
#: Konak, İzmir, as the canvas shows it in EPSG:4326.
KONAK_VIEW = QgsRectangle(27.10, 38.40, 27.14, 38.43)


class CheckFailed(AssertionError):
    pass


def check(condition, message):
    if not condition:
        raise CheckFailed(message)


def pump(seconds=0.05):
    app = QgsApplication.instance()
    end = time.monotonic() + seconds
    while True:
        app.processEvents()
        if time.monotonic() >= end:
            return
        time.sleep(0.005)


def wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        pump(0.02)
    return predicate()


class _Iface:
    """The iface calls the plugin makes, on a real main window and canvas."""

    def __init__(self, window, canvas):
        self._window = window
        self._canvas = canvas
        self.active = None

    def mainWindow(self):  # noqa: N802 - QGIS naming
        return self._window

    def mapCanvas(self):  # noqa: N802
        return self._canvas

    def activeLayer(self):  # noqa: N802
        return self.active

    def addDockWidget(self, area, dock):  # noqa: N802
        self._window.addDockWidget(area, dock)

    def removeDockWidget(self, dock):  # noqa: N802
        self._window.removeDockWidget(dock)

    def addPluginToMenu(self, _name, _action):  # noqa: N802
        return None

    def removePluginMenu(self, _name, _action):  # noqa: N802
        return None

    def addToolBarIcon(self, _action):  # noqa: N802
        return None

    def removeToolBarIcon(self, _action):  # noqa: N802
        return None


class Rig:
    """A QGIS-shaped window: Browser and Layers on the left, a real canvas.

    The canvas is not bridged to the project, so nothing is ever rendered
    and no basemap tile is fetched.
    """

    def __init__(self):
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtWidgets import QDockWidget, QLabel, QMainWindow
        from qgis.gui import QgsMapCanvas

        from zero2agent_osm_downloader.main_plugin import AgentOsmDownloaderPlugin

        self.window = QMainWindow()
        self.window.setObjectName("QgisApp")
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self.window.setCentralWidget(self.canvas)
        self.browser = QDockWidget("Browser", self.window)
        self.browser.setObjectName("Browser")
        self.browser.setWidget(QLabel("Browser panel"))
        layers = QDockWidget("Layers", self.window)
        layers.setObjectName("Layers")
        layers.setWidget(QLabel("Layers panel"))
        area = Qt.DockWidgetArea.LeftDockWidgetArea
        self.window.addDockWidget(area, self.browser)
        self.window.addDockWidget(area, layers)
        self.window.resize(1366, 728)
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.window.show()
        pump(0.1)
        self.iface = _Iface(self.window, self.canvas)
        self.plugin = AgentOsmDownloaderPlugin(self.iface)
        self.plugin.initGui()
        self.plugin.action.trigger()
        pump(0.3)
        self.dock = self.plugin.dock


class _Recorder:
    """Stands in for modal message boxes and for the task that downloads."""

    def __init__(self, dockmod):
        from qgis.PyQt.QtWidgets import QMessageBox

        self.messages = []
        self.titles = []
        self.submitted = []
        recorder = self

        class _Box(QMessageBox):
            @staticmethod
            def warning(_parent, title, text, *_args):
                recorder.titles.append(str(title))
                recorder.messages.append(str(text))
                return QMessageBox.StandardButton.Ok

            information = critical = warning

            @staticmethod
            def question(_parent, title, text, *_args):
                recorder.titles.append(str(title))
                recorder.messages.append(str(text))
                return QMessageBox.StandardButton.No

        dockmod.QMessageBox = _Box

    def attach(self, dock):
        dock._submit = lambda algorithm_id, parameters, extent: self.submitted.append(
            (algorithm_id, dict(parameters), extent)
        )

    def clear(self):
        self.messages.clear()
        self.titles.clear()
        self.submitted.clear()


def _metadata_version():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(PLUGIN_DIR / "metadata.txt", encoding="utf-8")
    return parser["general"]["version"].strip()


def _points_layer(points, name="pts"):
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", name, "memory")
    features = []
    for index, (x, y) in enumerate(points):
        feature = QgsFeature(layer.fields())
        feature.setAttributes([f"p{index}"])
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def _real_candidates(name):
    from zero2agent_osm_downloader.core.geocoding import parse_geocode_results

    payload = json.loads((FIXTURES / f"nominatim_{name}.json").read_text(encoding="utf-8"))
    return parse_geocode_results(payload, name)


def _inside(dock, widget):
    from qgis.PyQt.QtCore import QPoint

    corner = widget.mapTo(dock, QPoint(0, 0))
    return (
        widget.isVisible()
        and corner.x() >= 0 and corner.y() >= 0
        and corner.x() + widget.width() <= dock.width()
        and corner.y() + widget.height() <= dock.height()
    )


def _horizontal_overflow(dock):
    """Largest horizontal scroll range of the dock's scroll areas, per tab."""
    from qgis.PyQt.QtWidgets import QScrollArea

    worst = {}
    outer = dock.findChild(QScrollArea, "agentOsmRootScroll")
    for index in range(dock.tabs.count()):
        dock.tabs.setCurrentIndex(index)
        pump(0.1)
        ranges = [outer.horizontalScrollBar().maximum()]
        page = dock.tabs.widget(index)
        if isinstance(page, QScrollArea):
            ranges.append(page.horizontalScrollBar().maximum())
        worst[dock.tabs.tabText(index)] = max(ranges)
    dock.tabs.setCurrentWidget(dock.download_tab)
    pump(0.1)
    return worst


# --------------------------------------------------------------------- checks


def check_first_open_on_an_empty_map(rig, recorder):
    from zero2agent_osm_downloader.dialogs import dock as dockmod

    dock = rig.dock
    check(rig.canvas.extent().isEmpty(), "The rig canvas should start with no extent.")
    check(dock.extent_combo.currentData() == "place",
          "First open over an empty map did not preselect Named place.")
    pump(0.1)
    check(rig.window.focusWidget() is dock.place_edit,
          "First open over an empty map did not focus the place box.")
    if QgsApplication.platformName() == "windows":
        from qgis.PyQt.QtCore import QPoint

        viewport = dock._root_scroll.viewport()
        top = dock.place_edit.mapTo(viewport, QPoint(0, 0)).y()
        check(0 <= top and top + dock.place_edit.height() <= viewport.height(),
              f"The focused place box is out of view (y={top}, view {viewport.height()} px).")
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("map"))
    check(dock._extent() is None, "An empty canvas extent was returned as an area.")
    message = dock._missing_extent_message()
    check(message == dockmod._EMPTY_MAP_MESSAGE, f"Empty-map copy changed: {message!r}")
    check("WGS84" not in dock.extent_note.text(), "The empty-map note still says WGS84.")
    check(dock.extent_note.text() == message, f"Empty-map note: {dock.extent_note.text()!r}")
    recorder.clear()
    dock.tabs.setCurrentWidget(dock.download_tab)
    dock._download()
    check(recorder.messages == [message] and not recorder.submitted,
          f"Download on an empty map: {recorder.messages} {recorder.submitted}")
    rig.canvas.setExtent(KONAK_VIEW)
    pump(0.05)
    check(dock._extent() is not None, "A real canvas extent was refused.")
    note = dock.extent_note.text()
    check("km²" in note and "1 request" in note and "bounded" not in note,
          f"Extent note with a real map: {note!r}")
    return "an empty map preselects Named place, focuses the box and explains itself"


def check_footer_and_width(rig):
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QComboBox, QLabel, QScrollArea

    dock = rig.dock
    window = rig.window
    check(window.dockWidgetArea(dock) == Qt.DockWidgetArea.LeftDockWidgetArea,
          "The dock no longer opens in the left dock area.")
    check(rig.browser in window.tabifiedDockWidgets(dock),
          "The dock no longer opens tabbed with Browser.")
    content = dock.findChild(QScrollArea, "agentOsmRootScroll").widget()
    footer_widgets = ("download_button", "cancel_button", "clear_cache_button",
                      "progress", "status")
    for name in footer_widgets:
        widget = getattr(dock, name)
        check(not content.isAncestorOf(widget), f"{name} scrolls with the content.")
        check(dock.footer.isAncestorOf(widget), f"{name} is not in the footer.")
    check(dock.findChild(QLabel, "heroBadge") is None, "The OSM READY badge is back.")
    check(not any("READY" in label.text() for label in dock.findChildren(QLabel)),
          "A label still says READY.")
    check(dock.tabs.cornerWidget(Qt.Corner.TopRightCorner) is None,
          "The tab bar still carries a corner button.")
    for combo in dock.findChildren(QComboBox):
        check(combo.sizeAdjustPolicy()
              == QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
              and combo.minimumContentsLength() == 8,
              f"A combo sizes to its longest item: {combo.accessibleName() or combo.toolTip()[:40]}")
    if QgsApplication.platformName() != "windows":
        print("    [SKIP] pixel geometry needs the windows platform (TRAPS 4.3)")
        return "footer widgets sit outside the scroll area (geometry skipped)"
    window.resizeDocks([dock], [350], Qt.Orientation.Horizontal)
    pump(0.3)
    sizes = []
    # This rig has no menu or toolbars, so 1366x540 gives the dock the height
    # QGIS gives it in a 1366x728 window (about 265 px, measured).
    for width, height in ((1366, 728), (1366, 540), (1920, 1040)):
        window.resize(width, height)
        pump(0.3)
        check(dock.width() >= 350, f"The dock is {dock.width()} px wide.")
        for name in ("download_button", "cancel_button", "clear_cache_button", "status"):
            check(_inside(dock, getattr(dock, name)),
                  f"{name} is outside the {dock.width()}x{dock.height()} dock "
                  f"in a {width}x{height} window.")
        sizes.append(f"{width}x{height}: dock {dock.width()}x{dock.height()}")
    window.resize(1366, 728)
    pump(0.2)
    overflow = _horizontal_overflow(dock)
    check(not any(overflow.values()), f"Horizontal scrolling at 350 px: {overflow}")
    # A long Recent entry and a long place label used to widen the dock.
    from zero2agent_osm_downloader.core import history

    long_entry = history.make_entry(
        "Street, built form & nature, Public transport context, Everyday "
        "amenities (15-minute city) — Kordon Boyu Caddesi, Salimbey Mahallesi",
        "zero2agentosm:download_preset", {"PRESET": [0]},
        {"xmin": 27.1, "ymin": 38.4, "xmax": 27.2, "ymax": 38.5, "crs": "EPSG:4326"},
        "2026-11-02T09:12:00", presets=["urban_context"])
    QgsSettings().setValue("zero2agent_osm_downloader/recent_downloads",
                           history.dumps([long_entry]))
    dock._populate_recent()
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("place"))
    _show_candidates(dock, _real_candidates("noarea"))
    pump(0.2)
    overflow = _horizontal_overflow(dock)
    check(not any(overflow.values()),
          f"Horizontal scrolling with long labels at 350 px: {overflow}")
    return "Download, Cancel, Clear cache and status stay on screen; no sideways scroll (" + "; ".join(sizes) + ")"


def _show_candidates(dock, candidates):
    """Hand the dock a finished place search, as the task would."""

    class _Done:
        def __init__(self):
            self.candidates = tuple(candidates)
            self.error_text = ""
            self.error_detail = ""
            self.place = candidates[0].name if candidates else ""

    task = _Done()
    dock._place_task = task
    dock._place_resolved(task, bool(candidates))


def check_basemap_goes_under_the_data():
    from zero2agent_osm_downloader.core.basemap import OSM_BASEMAP_NAME, add_osm_basemap

    project = QgsProject()
    layer = _points_layer([(27.12, 38.41)], "Buildings — Polygons")
    project.addMapLayer(layer, False)
    group = project.layerTreeRoot().insertGroup(0, "02Agent — Buildings")
    group.addLayer(layer)
    basemap, added = add_osm_basemap(project)
    children = project.layerTreeRoot().children()
    check(added and isinstance(children[-1], QgsLayerTreeLayer)
          and children[-1].layerId() == basemap.id(),
          f"The basemap is not the last root child: {[node.name() for node in children]}")
    check(children[0].name() == "02Agent — Buildings" and basemap.name() == OSM_BASEMAP_NAME,
          "The downloaded group did not stay on top.")
    return "the basemap is inserted under the downloaded group"


def check_topic_never_changes_the_map_style(rig, recorder):
    dock = rig.dock
    settings_key = "zero2agent_osm_downloader/map_theme"
    dock.map_theme_combo.setCurrentIndex(dock.map_theme_combo.findData("atlas"))
    check(QgsSettings().value(settings_key) == "atlas", "Choosing a map style did not save it.")
    earlier = _points_layer([(27.12, 38.41)], "Earlier — Points")
    dock._style_layer(earlier, "atlas")
    QgsProject.instance().addMapLayer(earlier)
    dock.custom_check.setChecked(False)
    dock.tabs.setCurrentWidget(dock.download_tab)
    dock.group_combo.setCurrentIndex(dock.group_combo.findData("osm_as_is"))
    dock._select_preset("osm_as_is_overview")
    pump(0.05)
    check(dock.map_theme_combo.currentData() == "atlas",
          "Opening OSM As-Is switched the Map style combo.")
    check(QgsSettings().value(settings_key) == "atlas", "Opening OSM As-Is saved a new map style.")
    check(earlier.customProperty("zero2agent/map_theme") == "atlas",
          "Opening OSM As-Is restyled an earlier layer.")
    # The as-is run itself still arrives in the as-is style.
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("map"))
    rig.canvas.setExtent(KONAK_VIEW)
    dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("temporary"))
    recorder.clear()
    dock._download()
    check(len(recorder.submitted) == 1, f"The as-is download did not start: {recorder.messages}")
    check(dock.current_map_theme() == "osm_as_is" and dock._chosen_map_theme() == "atlas",
          "The as-is run did not get the as-is style for itself only.")
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    result = _points_layer([(27.121, 38.411)], "result")
    context.temporaryLayerStore().addMapLayer(result)
    dock._current_label = "OSM as-is: Overview"
    added = dock._add_result_layers(
        {"OUTPUT_POINTS": result.id(), "OSM_TIMESTAMP": STAMP}, context)
    check(added, "The as-is result was not added.")
    styled = [layer for layer in QgsProject.instance().mapLayers().values()
              if layer.name() == "OSM as-is: Overview — Points"]
    check(styled and styled[0].customProperty("zero2agent/map_theme") == "osm_as_is",
          "The as-is result was not styled as-is.")
    check(earlier.customProperty("zero2agent/map_theme") == "atlas",
          "The as-is result restyled an earlier layer.")
    return "OSM As-Is styles only its own run; the Map style setting and earlier layers stay"


def check_query_tab_defaults(rig, recorder):
    dock = rig.dock
    dock.tabs.setCurrentWidget(dock.query_tab)
    check((dock.query_match_combo.currentData(), dock.query_geometry_combo.currentData())
          == ("any", "all") and dock._query_filters() == [],
          "The Query tab does not start empty on Match any + All geometries.")
    dock.query_example_combo.setCurrentIndex(0)
    dock._load_query_example()
    check(dock.query_match_combo.currentData() == "all", "The first example is no longer Match all.")
    dock.query_exclude_key_edit.setText("access")
    dock._clear_query_filters()
    check((dock.query_match_combo.currentData(), dock.query_geometry_combo.currentData())
          == ("any", "all"), "Clear filters did not restore Match any + All geometries.")
    check(dock._query_filters() == [] and not dock.query_exclude_key_edit.text()
          and not dock.query_regex_check.isChecked(), "Clear filters left a filter behind.")
    for row, value in enumerate(("cafe", "restaurant")):
        dock.query_key_edits[row].setText("amenity")
        dock.query_value_edits[row].setText(value)
    recorder.clear()
    dock._download()
    check(len(recorder.submitted) == 1 and recorder.submitted[0][1]["MATCH_MODE"] == 0,
          f"cafe OR restaurant did not run: {recorder.messages}")
    dock.query_match_combo.setCurrentIndex(dock.query_match_combo.findData("all"))
    recorder.clear()
    dock._download()
    check(not recorder.submitted and recorder.messages
          and "Switch Match to 'Match any tag (OR)'" in recorder.messages[0],
          f"The same-key Match all error gives no way out: {recorder.messages}")
    dock._clear_query_filters()
    return "the Query tab starts and clears to Match any + All; same-key AND points to Match any"


def check_shipped_commands_route_in_the_dock(rig):
    from zero2agent_osm_downloader.core.catalog import COMMAND_EXAMPLES, interpret_prompt

    dock = rig.dock
    check([dock.command_example_combo.itemText(index)
           for index in range(dock.command_example_combo.count())] == list(COMMAND_EXAMPLES),
          "The dock does not ship exactly the tested command examples.")
    places = []
    original = dock._resolve_place_extent
    dock._resolve_place_extent = places.append
    try:
        for text in COMMAND_EXAMPLES:
            intent = interpret_prompt(text)
            places.clear()
            dock.tabs.setCurrentWidget(dock.command_tab)
            dock.prompt.setPlainText(text)
            dock._interpret()
            if intent.extra_tags:
                wanted = {(spec.key, spec.value) for spec in dock._intent_tags(intent)}
                got = set(dock._query_filters())
                got = {(key, "" if value == "*" else value) for key, value in got}
                check(dock.tabs.currentWidget() is dock.query_tab and got == wanted
                      and dock.query_match_combo.currentData() == "any",
                      f"{text!r} did not reach the Query tab as {wanted}: {got}")
            elif intent.mode == "custom":
                check(dock.custom_check.isChecked()
                      and dock.key_edit.text() == intent.key
                      and dock.value_edit.text() == (intent.value or "*"),
                      f"{text!r} did not fill the custom tag.")
            else:
                wanted = {intent.preset_id, *intent.extra_preset_ids}
                check(dock.tabs.currentWidget() is dock.download_tab
                      and set(dock._selected_preset_ids()) == wanted,
                      f"{text!r} ticked {dock._selected_preset_ids()}, not {wanted}.")
            check(places == ([intent.place_name] if intent.place_name else []),
                  f"{text!r} searched {places}, not {intent.place_name!r}.")
        # A place with nothing recognisable to download says so.
        places.clear()
        dock.prompt.setPlainText("sidewalks in Seattle")
        dock._interpret()
        expected = ("Found the place “Seattle”, but not what to download. Tick a "
                    "dataset, or type a tag such as amenity=restaurant.")
        check(dock.status.text() == expected and dock._selected_preset_ids() == ()
              and places == ["Seattle"], f"Place-only command: {dock.status.text()!r}")
        _show_candidates(dock, _real_candidates("konak"))
        check(dock.status.text() == expected,
              f"Resolving the place lost the 'not what to download' message: {dock.status.text()!r}")
    finally:
        dock._resolve_place_extent = original
    return f"all {len(COMMAND_EXAMPLES)} shipped examples fill the dock as their words say"


def check_place_search(rig, recorder):
    from zero2agent_osm_downloader.dialogs import dock as dockmod
    from zero2agent_osm_downloader.processing import osm_algorithms

    dock = rig.dock
    source = Path(dockmod.__file__).read_text(encoding="utf-8")
    for promise in ("will retry", "retry the lookup", "download-time lookup"):
        check(promise not in source, f"The dock still promises: {promise!r}")
    original = osm_algorithms.resolve_place
    started = threading.Event()
    observed = threading.Event()
    calls = []

    def answer(place, feedback):
        calls.append(place)
        if place == "Konakk":
            return ()
        if place == "Fail":
            raise RuntimeError("Nominatim did not answer (Host nominatim not found)")
        if place == "Hang":
            started.set()
            end = time.monotonic() + 20
            while time.monotonic() < end and not feedback.isCanceled():
                time.sleep(0.02)
            if feedback.isCanceled():
                observed.set()
            raise RuntimeError("The OSM download was canceled.")
        return _real_candidates("konak")

    osm_algorithms.resolve_place = answer
    try:
        dock.tabs.setCurrentWidget(dock.download_tab)
        dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("place"))

        def search(place):
            dock.place_edit.setText(place)
            dock._search_place()
            check(wait_for(lambda: dock._place_task is None, 10), f"The {place!r} search never ended.")

        search("Konakk")
        check(dock.status.text() == "No place matched “Konakk”. Try a fuller name, for "
              "example Konak, Izmir, Turkey.", f"No-match copy: {dock.status.text()!r}")
        check(dock.command_place_label.text().endswith("no match"), "No-match label.")
        search("Fail")
        check(dock.status.text() == "Could not look up this place: the place search did "
              "not answer. Check your connection and press Search again.",
              f"Failure copy: {dock.status.text()!r}")
        for text in (dock.status.text(), dock.command_place_label.text()):
            check("retry" not in text, f"A failed search still promises a retry: {text!r}")
        check(dock.place_search_button.isEnabled() and not dock.cancel_button.isEnabled(),
              "A finished search left Search off or Cancel on.")
        # Cancel answers during the search and stops the lookup itself.
        dock.place_edit.setText("Hang")
        dock._search_place()
        check(started.wait(10), "The slow search never started.")
        check(dock.cancel_button.isEnabled() and not dock.place_search_button.isEnabled(),
              "Cancel is not available during a place search.")
        clicked = time.monotonic()
        dock.cancel_button.click()
        pump(0.05)
        check(dock.status.text() == "Place search canceled."
              and dock.place_search_button.isEnabled() and not dock.cancel_button.isEnabled(),
              f"Cancel did not end the search at once: {dock.status.text()!r}")
        check(observed.wait(5) and time.monotonic() - clicked < 5,
              "Canceling did not reach the running lookup.")
        check(wait_for(lambda: QgsApplication.taskManager().countActiveTasks() == 0, 10),
              "The canceled search task did not finish.")
        # A real Nominatim answer (Konak's boundary relation) reaches the request.
        search("Konak")
        check(dock._uses_exact_boundary(), "Konak's boundary relation was not used.")
        note = dock.extent_note.text()
        check("exact boundary of Konak" in note and "1 request" in note, f"Boundary note: {note!r}")
        dock.custom_check.setChecked(False)
        dock.group_combo.setCurrentIndex(dock.group_combo.findData("urban_context"))
        dock._select_preset("buildings")
        dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("temporary"))
        recorder.clear()
        dock._download()
        check(recorder.submitted and recorder.submitted[0][0] == "zero2agentosm:download_place"
              and recorder.submitted[0][1]["AREA_ID"] == "3601269255",
              f"Konak did not go out as its boundary: {recorder.submitted} {recorder.messages}")
    finally:
        osm_algorithms.resolve_place = original
    return "no match and failure say what happened, Cancel stops a search, Konak downloads by boundary"


def check_place_without_a_boundary(rig, recorder):
    """A named place with no mapped boundary (area id 0) says so before the
    download, and the clip choice stops claiming an exact boundary."""
    dock = rig.dock
    combo = dock.place_clip_combo
    dock.tabs.setCurrentWidget(dock.download_tab)
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("place"))
    dock._clear_place()
    konak = _real_candidates("konak")
    _show_candidates(dock, konak)
    check(combo.currentData() == "boundary" and combo.isEnabled()
          and "exact boundary of Konak" in dock.extent_note.text(),
          f"Konak: {combo.currentText()!r} {dock.extent_note.text()!r}")
    noarea = _real_candidates("noarea")
    check(noarea and noarea[0].area_id == 0, "The no-boundary fixture now has a boundary.")
    _show_candidates(dock, noarea)
    note = dock.extent_note.text()
    first, _newline, size = note.partition("\n")
    check(first == "This place has no mapped boundary; its rectangle is used."
          and "km²" in size and size.endswith("1 request"), f"No-boundary note: {note!r}")
    check(combo.currentData() == "rectangle" and not combo.isEnabled()
          and "Exact" not in combo.currentText()
          and combo.toolTip() == "This place has no mapped boundary; its rectangle is used.",
          f"The clip choice still claims a boundary: {combo.currentText()!r}, "
          f"enabled={combo.isEnabled()}")
    check(not dock._uses_exact_boundary(), "A place with no boundary is downloaded by boundary.")
    dock.custom_check.setChecked(False)
    dock._select_preset("road_network")
    dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("temporary"))
    recorder.clear()
    dock._download()
    check(len(recorder.submitted) == 1
          and recorder.submitted[0][0] == "zero2agentosm:download_preset"
          and "AREA_ID" not in recorder.submitted[0][1],
          f"The rectangle did not go out as a rectangle: {recorder.submitted} {recorder.messages}")
    _show_candidates(dock, konak)
    check(combo.currentData() == "boundary" and combo.isEnabled()
          and dock._uses_exact_boundary(), "Konak lost its exact boundary after Kordon.")
    # The user's own choice is only lent to a place without a boundary.
    combo.setCurrentIndex(combo.findData("rectangle"))
    _show_candidates(dock, noarea)
    _show_candidates(dock, konak)
    check(combo.currentData() == "rectangle", "Kordon overwrote the user's Rectangular extent.")
    combo.setCurrentIndex(combo.findData("boundary"))
    # Off the Presets and Query tabs the choice stays locked, as before.
    dock.tabs.setCurrentWidget(dock.command_tab)
    check(not combo.isEnabled(), "The clip choice is enabled on the Command tab.")
    dock.tabs.setCurrentWidget(dock.download_tab)
    check(combo.isEnabled(), "The clip choice stayed locked on the Presets tab.")
    dock._clear_place()
    return "Kordon (area id 0) says its rectangle is used and locks the clip on Rectangular extent; Konak keeps its boundary"


def check_plain_words(rig, recorder):
    """The words REV-UX found too technical, or not matching their buttons."""
    from zero2agent_osm_downloader.core.catalog import PRESETS
    from zero2agent_osm_downloader.dialogs import dock as dockmod

    dock = rig.dock
    source = Path(dockmod.__file__).read_text(encoding="utf-8")
    for old in ("Too many combined dataset selectors", "offline command router",
                "Nominatim geocoder", "XYZ basemap", '"GeoPackage file"',
                "Continue, or choose"):
        check(old not in source, f"The dock still says {old!r}.")
    check(dock.save_mode_combo.itemText(dock.save_mode_combo.findData("gpkg")) == "GeoPackage",
          "The GeoPackage item is not called GeoPackage.")
    dock.tabs.setCurrentWidget(dock.download_tab)
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("place"))
    dock._clear_place()
    note = dock.extent_note.text()
    check(note.endswith("•  Search for a place to see the request size.")
          and "choose an extent" not in note, f"Named place before a search: {note!r}")
    check(dock.place_edit.toolTip() == "Any place name OpenStreetMap's place search understands.",
          f"Place box tooltip: {dock.place_edit.toolTip()!r}")
    check(dock.basemap_button.toolTip()
          == "Add the OpenStreetMap map as a background to the current project.",
          f"Basemap tooltip: {dock.basemap_button.toolTip()!r}")
    dock._refresh_agent_connection()
    check(dock.agent_connection.text().endswith("The Command tab works without it."),
          f"Agent tab without 02SmartModeler: {dock.agent_connection.text()!r}")
    # The heavy-download question is a yes/no question, as its buttons are.
    recorder.clear()
    dock._confirm_heavy_request(12.5)
    question = recorder.messages[-1] if recorder.messages else ""
    check("Download it anyway?" in question and "Continue, or" not in question
          and question.endswith("Choose No to pick a smaller area or a filtered dataset instead."),
          f"Heavy-download question: {question!r}")
    # Too many datasets: the title and text say what to do.
    dock.tabs.setCurrentWidget(dock.download_tab)
    dock.custom_check.setChecked(False)
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("map"))
    rig.canvas.setExtent(KONAK_VIEW)
    dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("temporary"))
    dock._extra_preset_ids = tuple(preset.preset_id for preset in PRESETS)
    recorder.clear()
    try:
        dock._download()
    finally:
        dock._extra_preset_ids = ()
    check(recorder.titles == ["Too many datasets at once"]
          and recorder.messages == ["These datasets need more than 32 tag filters. Tick fewer "
                                    "and press Download again."]
          and not recorder.submitted,
          f"Too many datasets: {recorder.titles} {recorder.messages} {recorder.submitted}")
    return "GeoPackage, place search and background-map tooltips, the Command tab, a yes/no heavy question, Too many datasets"


def check_geopackage(rig, recorder):
    dock = rig.dock
    dock.tabs.setCurrentWidget(dock.download_tab)
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("map"))
    rig.canvas.setExtent(KONAK_VIEW)
    dock.custom_check.setChecked(False)
    dock._select_preset("buildings")
    dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("gpkg"))
    dock.gpkg_widget.setFilePath("")
    recorder.clear()
    dock._download()
    check(recorder.messages == ["Choose a GeoPackage file first."] and not recorder.submitted,
          f"GeoPackage without a file: {recorder.messages} {recorder.submitted}")
    work = Path(tempfile.mkdtemp(prefix="z2a-dock-ux-"))
    dock.gpkg_widget.setFilePath(str(work / "city.gpkg"))
    note = dock.extent_note.text()
    check(note.startswith("Will save to city.gpkg") and not (work / "city.gpkg").exists(),
          f"The note claims a save before anything was saved: {note!r}")
    root = QgsProject.instance().layerTreeRoot()

    def finish_into(label):
        """A download of two points ends with GeoPackage output on."""
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        memory = _points_layer([(27.12, 38.41), (27.13, 38.42)], "memory")
        context.temporaryLayerStore().addMapLayer(memory)
        dock._current_label = label
        dock._current_extent = None
        dock._pending_request = None
        dock._feedback, dock._context, dock._canceling = None, context, False
        dock._finished(True, {"OUTPUT_POINTS": memory.id(), "OSM_TIMESTAMP": STAMP,
                              "TILES_TOTAL": 1, "TILES_MISSING": 0})
        pump(0.05)
        group = root.findGroup(f"02Agent — {label}")
        check(group is not None and len(group.children()) == 1, "The data was lost with the file.")
        kept = group.children()[0].layer()
        check(kept.providerType() == "memory" and kept.featureCount() == 2,
              "The kept layer is not the temporary download.")
        status = dock.status.text()
        check(status == f"Finished — Points: 2. OSM data as of {STAMP.replace('T', ' ').rstrip('Z')} "
              "(UTC).", f"Status after a GeoPackage that could not be written: {status!r}")
        # Paths carry zero-width break points so they wrap in the footer.
        return status, dock.status_notice.text().replace("​", "")

    def no_python_text(*texts):
        for text in texts:
            for raw in ("WinError", "Errno", "Error:", "NOT saved", "Traceback", "'C:"):
                check(raw not in text, f"The closing message shows Python's error text: {text!r}")

    # A folder that cannot be created: a file where the folder should be.
    blocker = work / "not_a_folder"
    blocker.write_text("x", encoding="utf-8")
    target = blocker / "sub" / "city.gpkg"
    dock.gpkg_widget.setFilePath(str(target))
    status, notice = finish_into("Unwritable")
    folder = os.path.dirname(os.path.abspath(dock.gpkg_widget.filePath().strip()))
    check(notice == f"Could not write the GeoPackage: the folder {folder} cannot be created. "
          "The layers are kept as temporary layers; choose another file under Save to.",
          f"Unwritable folder: {notice!r}")
    check(dock.status_notice.isVisibleTo(dock), "The GeoPackage failure is not on screen.")
    no_python_text(status, notice)
    # A file that is there but is no GeoPackage: QGIS cannot write into it.
    not_a_gpkg = work / "notes.gpkg"
    not_a_gpkg.write_text("not a GeoPackage", encoding="utf-8")
    dock.gpkg_widget.setFilePath(str(not_a_gpkg))
    status, notice = finish_into("Not a GeoPackage")
    check(notice == f"Could not write the GeoPackage {dock.gpkg_widget.filePath().strip()}. The "
          "layers are kept as temporary layers; choose another file under Save to. Details: "
          "View > Panels > Log Messages > 02Agent OSM Downloader.",
          f"Unwritable file: {notice!r}")
    no_python_text(status, notice)
    # A long folder name with no spaces wraps in the notice instead of
    # widening the dock (it grew from 350 to 411 px before the break points).
    width_before = dock.width()
    long_blocker = work / ("folder_name_without_any_spaces_to_break_" * 2)
    long_blocker.write_text("x", encoding="utf-8")
    dock.gpkg_widget.setFilePath(str(long_blocker / "sub" / "city.gpkg"))
    status, notice = finish_into("Long path")
    check(str(long_blocker) in notice, f"The long path is not in the notice: {notice!r}")
    check(dock.width() <= width_before,
          f"A long folder path widened the dock from {width_before} to {dock.width()} px.")
    empty = [node.name() for node in root.children()
             if isinstance(node, QgsLayerTreeGroup) and node.name().startswith("02Agent")
             and not node.children()]
    check(not empty, f"An empty group was left behind: {empty}")
    dock._set_status("Ready.")
    check(dock.status_notice.isHidden(), "The GeoPackage failure outlived its closing message.")
    dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("temporary"))
    dock.gpkg_widget.setFilePath("")
    return ("no file: refused before downloading; an uncreatable folder or a file that is no "
            "GeoPackage: temporary layers kept, the reason in words, no empty group")


def check_report_and_about(rig):
    from zero2agent_osm_downloader.dialogs.dock import MANUAL_URL

    dock = rig.dock
    version = _metadata_version()
    _show_candidates(dock, _real_candidates("konak"))
    rig.canvas.setExtent(KONAK_VIEW)

    def fields(url):
        parts = urlsplit(url)
        check(parts.scheme == "https" and parts.netloc == "github.com"
              and parts.path == "/YusufEminoglu/02Agent-OSM-Downloader/issues/new",
              f"Report a problem opens {url}")
        query = parse_qs(parts.query)
        return query["title"][0], query["body"][0]

    dock._last_failure_kind = ""
    url = dock.problem_report_url()
    title, body = fields(url)
    check(version in title, f"The issue title lacks the version: {title!r}")
    for line in (f"Plugin version: {version}", f"QGIS version: {Qgis.version()}",
                 "Operating system: ", "Last failure: none recorded"):
        check(line in body, f"The issue body lacks {line!r}: {body!r}")
    for secret in ("Konak", "İzmir", "27.10", "38.40", "27.0688", "38.393"):
        check(secret not in url and secret not in body,
              f"The issue form carries a place or a coordinate: {secret}")
    dock._last_failure_kind = "rate_limited"
    check("Last failure: rate_limited" in fields(dock.problem_report_url())[1],
          "The last failure class is not in the report.")
    dock._last_failure_kind = "Konak, İzmir"
    check("Last failure: unrecognised" in fields(dock.problem_report_url())[1],
          "A free-text failure reached the public form.")
    dock._last_failure_kind = ""
    check(dock.report_problem_button.text() == "Report a problem"
          and dock.manual_button.text() == "Manual", "The dock links are missing.")
    about = rig.plugin.about_html()
    for piece in (f"Version {version}", MANUAL_URL, "issues/new", "Report a problem",
                  "English or Turkish", "3.28 to 4.x"):
        check(piece in about, f"About lacks {piece!r}.")
    return f"Report a problem carries version {version}, QGIS, OS and the failure class only; About shows them"


def _palette(roles):
    from qgis.PyQt.QtGui import QColor, QPalette
    from qgis.PyQt.QtWidgets import QApplication

    palette = QPalette(QApplication.palette())
    for role, color in roles.items():
        if isinstance(role, tuple):
            palette.setColor(role[0], role[1], QColor(color))
        else:
            palette.setColor(role, QColor(color))
    return palette


def _theme_file_palette(name):
    """A built-in QGIS UI theme's palette.txt applied as QGIS applies it."""
    from qgis.PyQt.QtGui import QPalette

    folder = QgsApplication.uiThemes().get(name)
    if not folder or not (Path(folder) / "palette.txt").exists():
        return None
    roles = {}
    for line in (Path(folder) / "palette.txt").read_text(encoding="utf-8").splitlines():
        role, _sep, color = line.strip().partition(":")
        if role.strip().isdigit() and color.strip():
            roles[QPalette.ColorRole(int(role))] = color.strip()
    return _palette(roles)


def check_theme_contrast(rig):
    from qgis.PyQt.QtGui import QColor, QPalette
    from qgis.PyQt.QtWidgets import QApplication

    from zero2agent_osm_downloader.dialogs.theme import (
        contrast_ratio,
        dock_color_tokens,
        dock_stylesheet,
    )

    role = QPalette.ColorRole
    # The dock's palette in QGIS 3.44 started with UI theme Blend of Gray, as
    # measured by the v1.6 audit: near-black text on a black surface, 1.03:1.
    measured_gray = _palette({
        role.Window: "#000000", role.Base: "#000000", role.Button: "#000000",
        role.WindowText: "#0e0e0e", role.Text: "#0e0e0e", role.ButtonText: "#000000",
        role.Highlight: "#f0f0f0",
        (QPalette.ColorGroup.Disabled, role.WindowText): "#9a9a9a",
    })
    palettes = {"Blend of Gray (measured in QGIS)": measured_gray,
                "QGIS default": QApplication.palette()}
    for name in ("Blend of Gray", "Night Mapping"):
        palette = _theme_file_palette(name)
        check(palette is not None, f"QGIS ships no palette for {name}.")
        palettes[f"{name} (QGIS theme file)"] = palette
    # The accent drawn as text (links, eyebrows, pills, quiet buttons) on
    # every surface it is drawn on as text, and on a plain button's hover.
    ink_on = ("surface", "card", "hero", "status", "tab_active")
    pairs = (("text", "surface"), ("text", "card"), ("text", "hero"), ("text", "status"),
             ("text", "tab_active"), ("input_text", "input_surface"), ("subtle", "surface"),
             ("subtle", "card"), ("subtle", "hero"), ("button_text", "button"),
             ("accent_text", "accent"), ("button_hover_text", "button"),
             *(("accent_ink", background) for background in ink_on),
             *(("accent_hover_ink", background) for background in ink_on))
    worst = {}
    for name, palette in palettes.items():
        tokens = dock_color_tokens(palette)
        for foreground, background in pairs:
            ratio = contrast_ratio(QColor(tokens[foreground]), QColor(tokens[background]))
            check(ratio >= 4.5, f"{name}: {foreground}/{background} is {ratio:.2f}:1.")
            worst[name] = min(worst.get(name, 99.0), ratio)
    raw = contrast_ratio(measured_gray.color(role.WindowText),
                         QColor(dock_color_tokens(measured_gray)["surface"]))
    check(raw < 1.1, f"The measured Blend of Gray palette no longer needs the fallback ({raw:.2f}:1).")
    check("QPushButton#primaryButton:disabled" in dock_stylesheet(QApplication.palette()),
          "The primary button has no disabled style.")
    if QgsApplication.platformName() == "windows":
        dock = rig.dock
        dock.tabs.setCurrentWidget(dock.download_tab)
        pump(0.1)
        button = dock.download_button

        def centre():
            image = button.grab().toImage()
            return image.pixelColor(image.width() // 2, 3).name()

        enabled = centre()
        button.setEnabled(False)
        pump(0.05)
        disabled = centre()
        button.setEnabled(True)
        check(enabled != disabled, f"Disabled Download looks enabled ({enabled}).")
    return "text reads at 4.5:1 or more under " + ", ".join(
        f"{name} (worst {ratio:.1f}:1)" for name, ratio in worst.items()) + "; disabled Download looks disabled"


def _luminance(rgb):
    channels = []
    for value in rgb:
        value /= 255.0
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _ratio(first, second):
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _ink_on_background(image, rect):
    """(background, ink, ratio) in a crop of an RGB32 image.

    The background is the crop's most common colour, the ink the colour
    that contrasts with it most: for text, the core of its strokes.
    """
    from collections import Counter

    from qgis.PyQt.QtCore import QRect

    rect = rect.intersected(QRect(0, 0, image.width(), image.height()))
    counts = Counter()
    for y in range(rect.top(), rect.bottom() + 1):
        for x in range(rect.left(), rect.right() + 1):
            value = image.pixel(x, y)
            counts[((value >> 16) & 255, (value >> 8) & 255, value & 255)] += 1
    if not counts:
        return None
    background = counts.most_common(1)[0][0]
    ink = max(counts, key=lambda colour: _ratio(colour, background))
    return background, ink, _ratio(ink, background)


def _text_rect(widget, dock):
    """The part of a widget that holds its text, in dock coordinates."""
    from qgis.PyQt.QtCore import QPoint, QRect
    from qgis.PyQt.QtWidgets import QCheckBox, QGroupBox, QStyle, QStyleOptionButton

    corner = widget.mapTo(dock, QPoint(0, 0))
    if isinstance(widget, QGroupBox):
        # The title: left 9 px, padding 5 px each side (dialogs/theme.py).
        metrics = widget.fontMetrics()
        width = 9 + 5 + metrics.horizontalAdvance(widget.title()) + 5 + 2
        return QRect(corner.x(), corner.y(), min(width, widget.width()), metrics.height() + 4)
    if isinstance(widget, QCheckBox):
        # The label only: the indicator is drawn by the QGIS theme.
        option = QStyleOptionButton()
        option.initFrom(widget)
        option.text = widget.text()
        inner = widget.style().subElementRect(QStyle.SubElement.SE_CheckBoxContents, option, widget)
        if inner.isValid() and inner.width() > 4:
            return QRect(corner.x() + inner.x(), corner.y(), inner.width(), widget.height())
        return QRect(corner.x() + 24, corner.y(), widget.width() - 24, widget.height())
    return QRect(corner.x(), corner.y(), widget.width(), widget.height())


def _unclipped(widget, dock):
    """True when no scroll area between the widget and the dock cuts it off."""
    from qgis.PyQt.QtCore import QPoint
    from qgis.PyQt.QtWidgets import QAbstractScrollArea

    parent = widget.parentWidget()
    while parent is not None and parent is not dock:
        area = parent.parentWidget()
        if isinstance(area, QAbstractScrollArea) and area.viewport() is parent:
            corner = widget.mapTo(parent, QPoint(0, 0))
            if not (0 <= corner.y() and corner.y() + widget.height() <= parent.height()
                    and 0 <= corner.x() and corner.x() + widget.width() <= parent.width()):
                return False
        parent = parent.parentWidget()
    return True


def _measure_on_screen(dock, results, tab_name):
    """Grab the dock; measure every enabled QLabel, QCheckBox, QGroupBox title
    and QPushButton with text that is on show and not yet in ``results``,
    as (class, object name, text) -> (ratio, background, ink, tab)."""
    from qgis.PyQt.QtCore import QRect
    from qgis.PyQt.QtGui import QImage
    from qgis.PyQt.QtWidgets import QApplication, QCheckBox, QGroupBox, QLabel, QPushButton

    # A focus frame is drawn in the accent: measure text, not it.
    if QApplication.focusWidget() is not None:
        QApplication.focusWidget().clearFocus()
    pixmap = dock.grab()
    scale = pixmap.devicePixelRatio()
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB32)
    for kind in (QLabel, QCheckBox, QGroupBox, QPushButton):
        for widget in dock.widget().findChildren(kind):
            text = widget.title() if isinstance(widget, QGroupBox) else widget.text()
            key = (kind.__name__, widget.objectName(), " ".join(text.split())[:48])
            if (not text or key in results or not widget.isEnabled()
                    or not widget.isVisibleTo(dock) or not _unclipped(widget, dock)):
                continue
            rect = _text_rect(widget, dock)
            rect = QRect(int(rect.x() * scale), int(rect.y() * scale),
                         int(rect.width() * scale), int(rect.height() * scale))
            found = _ink_on_background(image, rect)
            if found is not None:
                background, ink, ratio = found
                results[key] = (ratio, "#%02x%02x%02x" % background, "#%02x%02x%02x" % ink,
                                tab_name)


def _text_contrast_survey(dock):
    """_measure_on_screen on every tab, scrolled through from top to bottom."""
    from qgis.PyQt.QtWidgets import QScrollArea

    results = {}
    outer = dock._root_scroll.verticalScrollBar()
    for tab_name, tab in (("Presets", dock.download_tab), ("Query", dock.query_tab),
                          ("Command", dock.command_tab), ("Agent", dock.connections_tab)):
        dock.tabs.setCurrentWidget(tab)
        pump(0.15)
        inner = tab.verticalScrollBar() if isinstance(tab, QScrollArea) else None
        for position in sorted({0, outer.maximum() // 2, outer.maximum()}):
            for inner_position in (sorted({0, inner.maximum() // 2, inner.maximum()})
                                   if inner is not None else (None,)):
                outer.setValue(position)
                if inner is not None:
                    inner.setValue(inner_position)
                pump(0.1)
                _measure_on_screen(dock, results, tab_name)
    dock.tabs.setCurrentWidget(dock.download_tab)
    outer.setValue(0)
    return results


def check_text_contrast_on_pixels(rig):
    """Every label, checkbox, group title and button reads at 4.5:1 or more
    under QGIS's own themes, measured on the dock's pixels.

    QgsApplication.setUITheme applies a theme's style.qss and palette.txt as
    QGIS does when it starts with that theme. REV-UX measured "Topic" at
    1.42:1 and the group titles near 1.2:1 under Blend of Gray, and accent
    text at 4.02:1 (count pill) and 4.32:1 (links) in the light theme.
    """
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QApplication

    from zero2agent_osm_downloader.dialogs import dock as dockmod
    from zero2agent_osm_downloader.dialogs.dock import AgentOsmDock

    if QgsApplication.platformName() != "windows":
        print("    [SKIP] pixel contrast needs the windows platform (TRAPS 4.3)")
        return "pixel contrast skipped without a font engine"
    def key(kind, name, text):
        return kind, name, " ".join(text.split())[:48]

    # What REV-UX measured below 4.5:1 ...
    flagged = {key(*item) for item in (
        ("QLabel", "", "Topic"), ("QLabel", "", "Datasets"), ("QLabel", "", "Extent"),
        ("QLabel", "", "Save to"), ("QGroupBox", "", "Ready-made datasets"),
        ("QGroupBox", "", "Download options"), ("QLabel", "countPill", "1 of 4 selected"),
        ("QPushButton", "linkButton", "Manual"), ("QPushButton", "linkButton", "Report a problem"),
        ("QCheckBox", "", dockmod._FALLBACK_LABEL),
    )}
    saved_palette = QApplication.palette()
    summary = []
    try:
        for theme in ("default", "Blend of Gray", "Night Mapping"):
            check(theme == "default" or theme in QgsApplication.uiThemes(),
                  f"QGIS ships no {theme} theme.")
            QApplication.setPalette(saved_palette)
            QgsApplication.setUITheme(theme)
            pump(0.2)
            # A dock built under the theme, as when QGIS starts with it.
            dock = AgentOsmDock(rig.iface)
            dock.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            dock.resize(360, 1100)
            dock.show()
            try:
                dock._select_preset("urban_context")
                dock.custom_check.setChecked(True)
                dock._set_status("Finished — Points: 2. OSM data as of 2026-11-02 05:40:12 (UTC).")
                pump(0.3)
                results = _text_contrast_survey(dock)
                low = sorted((value[0], key, value[1:]) for key, value in results.items()
                             if value[0] < 4.5)
                check(not low, f"{theme}: " + "; ".join(
                    f"{key[2]!r} {ratio:.2f}:1 (ink {where[1]} on {where[0]}, {where[2]})"
                    for ratio, key, where in low[:8]))
                # ... and the two lines this release adds under the status card.
                countdown = dockmod._CONTINUE_WAIT.format(wait=dockmod._wait_text(40))
                flagged |= {key("QLabel", "statusNotice", "A notice under the status."),
                            key("QLabel", "mutedText", countdown)}
                dock._show_outcome("Finished — Points: 2.", Qgis.MessageLevel.Success,
                                   "A notice under the status.")
                dock.continue_wait.setText(countdown)
                dock.continue_wait.setVisible(True)
                pump(0.2)
                _measure_on_screen(dock, results, "footer")
            finally:
                dock.setParent(None)
                dock.deleteLater()
                pump(0.1)
            missing = sorted(key for key in flagged if key not in results)
            check(not missing, f"{theme}: not measured: {missing}")
            check(len(results) >= 50, f"{theme}: only {len(results)} texts measured.")
            low = sorted((value[0], key) for key, value in results.items() if value[0] < 4.5)
            check(not low, f"{theme}: below 4.5:1: {low}")
            summary.append(f"{theme} {len(results)} texts, worst "
                           f"{min(value[0] for value in results.values()):.2f}:1")
    finally:
        QApplication.setPalette(saved_palette)
        QgsApplication.setUITheme("default")
        pump(0.2)
    return "; ".join(summary)


def check_recent_rows_and_preset_ids(rig, recorder):
    # PRESET indexes are Processing option indexes, which follow the
    # append-only PRESET_ENUM_ORDER rather than the catalogue's display order.
    from zero2agent_osm_downloader.processing.osm_algorithms import PRESET_ENUM_ORDER

    dock = rig.dock
    key = "zero2agent_osm_downloader/recent_downloads"
    QgsSettings().setValue(key, "[]")
    dock._populate_recent()
    buildings = PRESET_ENUM_ORDER.index("buildings")
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    for box in (QgsRectangle(27.10, 38.40, 27.14, 38.44), QgsRectangle(2.32, 48.84, 2.38, 48.86)):
        dock._pending_request = ("zero2agentosm:download_preset", {"PRESET": [buildings]},
                                 QgsReferencedRectangle(box, wgs84))
        dock._current_label = "Buildings"
        dock._current_style_scope = ""
        dock._current_where = ""
        dock._remember_download("Polygons: 3")
    rows = [dock.recent_combo.itemText(index) for index in range(dock.recent_combo.count())]
    check(len(rows) == 2 and rows[0] != rows[1], f"Two cities give the same Recent row: {rows}")
    check("48.85° N, 2.35° E" in rows[0] and "38.42° N, 27.12° E" in rows[1],
          f"Recent rows do not say where: {rows}")
    stored = json.loads(QgsSettings().value(key))
    check(all(entry["presets"] == ["buildings"] and "PRESET" not in entry["parameters"]
              for entry in stored), f"History stores indexes, not preset ids: {stored}")
    # An entry written by 1.5.1: PRESET index 3 was Buildings in that catalogue.
    legacy = {"label": "Buildings", "algorithm": "zero2agentosm:download_preset",
              "parameters": {"PRESET": [3]}, "when": "2026-10-28T09:12:00",
              "extent": {"xmin": 27.1, "ymin": 38.4, "xmax": 27.14, "ymax": 38.43,
                         "crs": "EPSG:4326"}, "summary": "Polygons: 110", "style_scope": ""}
    QgsSettings().setValue(key, json.dumps([legacy]))
    dock._populate_recent()
    dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("temporary"))
    recorder.clear()
    dock._rerun_recent()
    check(recorder.submitted, f"Run again on a 1.5.1 entry did not start: {recorder.messages}")
    sent = recorder.submitted[0][1]["PRESET"]
    check([PRESET_ENUM_ORDER[index] for index in sent] == ["buildings"],
          f"Run again on a 1.5.1 entry ran {[PRESET_ENUM_ORDER[index] for index in sent]}.")
    QgsSettings().setValue(key, "[]")
    dock._populate_recent()
    return "Recent rows name where; history stores preset ids and reads 1.5.1 index entries"


def check_preset_indexes_follow_processing_order(rig, recorder):
    """The dock sends PRESET indexes in Processing's append-only order.

    The catalogue lists the newer Everyday amenities preset near the top,
    while Processing appends it last so saved models keep their datasets; a
    dock that numbered presets by display order downloaded the wrong data.
    """
    from zero2agent_osm_downloader.processing.osm_algorithms import PRESET_ENUM_ORDER

    dock = rig.dock
    dock.tabs.setCurrentWidget(dock.download_tab)
    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("map"))
    rig.canvas.setExtent(KONAK_VIEW)
    dock.custom_check.setChecked(False)
    dock.save_mode_combo.setCurrentIndex(dock.save_mode_combo.findData("temporary"))
    sent = {}
    for preset_id in ("everyday_amenities", "buildings"):
        dock._select_preset(preset_id)
        recorder.clear()
        dock._download()
        check(len(recorder.submitted) == 1,
              f"{preset_id} did not start: {recorder.messages}")
        indexes = recorder.submitted[0][1]["PRESET"]
        sent[preset_id] = [PRESET_ENUM_ORDER[index] for index in indexes]
    check(sent == {"everyday_amenities": ["everyday_amenities"], "buildings": ["buildings"]},
          f"The dock sent other datasets than the ones ticked: {sent}")
    return "ticked presets reach Processing as the same datasets (append-only order)"


def check_everyday_amenities_listed(rig):
    dock = rig.dock
    dock.custom_check.setChecked(False)
    dock.group_combo.setCurrentIndex(dock.group_combo.findData("urban_context"))
    titles = [dock.dataset_list.item(index).text() for index in range(dock.dataset_list.count())]
    check("Everyday amenities (15-minute city)" in titles, f"Urban Context lists {titles}")
    return "Urban Context lists Everyday amenities (15-minute city)"


def check_unload(rig):
    from qgis.PyQt.QtWidgets import QDockWidget

    plugin = rig.plugin
    action = plugin.action
    plugin.unload()
    QgsApplication.instance().sendPostedEvents()
    pump(0.1)
    check(plugin.dock is None and plugin.action is None, "unload() kept the dock or the action.")
    check(not rig.window.findChildren(QDockWidget, "Zero2AgentOsmDownloaderDock"),
          "The dock is still a child of the main window after unload.")
    del action
    return "unload detaches the dock and the actions"


def run_checks():
    from zero2agent_osm_downloader.dialogs import dock as dockmod

    recorder = _Recorder(dockmod)
    snapshot = {key: QgsSettings().value(key) for key in SETTINGS}
    for key in SETTINGS:
        QgsSettings().remove(key)
    results = []
    rig = None
    try:
        rig = Rig()
        recorder.attach(rig.dock)
        steps = (
            ("first open, empty map", lambda: check_first_open_on_an_empty_map(rig, recorder)),
            ("footer and width", lambda: check_footer_and_width(rig)),
            ("basemap order", check_basemap_goes_under_the_data),
            ("map style", lambda: check_topic_never_changes_the_map_style(rig, recorder)),
            ("query defaults", lambda: check_query_tab_defaults(rig, recorder)),
            ("command examples", lambda: check_shipped_commands_route_in_the_dock(rig)),
            ("place search", lambda: check_place_search(rig, recorder)),
            ("place without a boundary", lambda: check_place_without_a_boundary(rig, recorder)),
            ("plain words", lambda: check_plain_words(rig, recorder)),
            ("geopackage", lambda: check_geopackage(rig, recorder)),
            ("report a problem", lambda: check_report_and_about(rig)),
            ("theme contrast", lambda: check_theme_contrast(rig)),
            ("text contrast on pixels", lambda: check_text_contrast_on_pixels(rig)),
            ("preset indexes", lambda: check_preset_indexes_follow_processing_order(rig, recorder)),
            ("recent and history", lambda: check_recent_rows_and_preset_ids(rig, recorder)),
            ("15-minute preset", lambda: check_everyday_amenities_listed(rig)),
            ("unload", lambda: check_unload(rig)),
        )
        for name, step in steps:
            try:
                results.append((name, True, step()))
            except Exception as error:  # noqa: BLE001 - one check, one verdict
                traceback.print_exc()
                results.append((name, False, f"{type(error).__name__}: {error}"))
    finally:
        for key, value in snapshot.items():
            if value is None:
                QgsSettings().remove(key)
            else:
                QgsSettings().setValue(key, value)
        QgsProject.instance().clear()
    return results


def main(exit_process=False):
    plugin_parent = str(PLUGIN_DIR.parent)
    if plugin_parent not in sys.path:
        sys.path.insert(0, plugin_parent)
    app = QgsApplication.instance()
    owns_app = app is None
    profile = None
    if owns_app:
        profile = tempfile.TemporaryDirectory(prefix="zero2agent-dock-ux-")
        app = QgsApplication([], True, profile.name, "external")
        app.initQgis()
    from zero2agent_osm_downloader.tests._offline import isolate_network

    # Off every public host before anything runs; the QGIS-wide guard only
    # in a QGIS this run started.
    isolate_network(guard_qgis=owns_app)
    print("=" * 62)
    print(f" 02Agent OSM Downloader - dock UX ({Qgis.version()}, {QgsApplication.platformName()})")
    print("=" * 62)
    results = []
    try:
        results = run_checks()
    except Exception as error:  # noqa: BLE001 - a broken rig is a failed test
        traceback.print_exc()
        results = [("rig", False, f"{type(error).__name__}: {error}")]
    passed = sum(1 for _name, ok, _note in results if ok)
    for name, ok, note in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {note}")
    print("-" * 62)
    print(f"  {passed}/{len(results)} passed")
    ok = bool(results) and passed == len(results)
    if owns_app:
        sys.stdout.flush()
        sys.stderr.flush()
        if exit_process:
            os._exit(0 if ok else 1)
    return ok


if __name__ == "__main__":
    sys.exit(0 if main(exit_process=True) else 1)
