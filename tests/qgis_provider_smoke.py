"""Real-QGIS deterministic smoke test for the mixed-geometry preset.

Run from the directory that *contains* the plugin folder:

    python-qgis-ltr.bat -m zero2agent_osm_downloader.tests.qgis_provider_smoke
    python-qgis.bat     -m zero2agent_osm_downloader.tests.qgis_provider_smoke

The check itself is a QgsProcessingAlgorithm so it can also be run from inside a
live QGIS session, but the module must still execute something when launched
directly — otherwise it exits 0 having asserted nothing, and every wrapper
reports a green test that never ran.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingOutputString,
    QgsProcessingUtils,
    QgsProject,
    QgsReferencedRectangle,
    QgsRectangle,
)


class AgentOsmProviderSmoke(QgsProcessingAlgorithm):
    def name(self) -> str:
        return "zero2agent_osm_provider_smoke"

    def displayName(self) -> str:
        return "02Agent OSM provider smoke test"

    def group(self) -> str:
        return "Tests"

    def groupId(self) -> str:
        return "tests"

    def createInstance(self):
        return AgentOsmProviderSmoke()

    def initAlgorithm(self, _configuration=None) -> None:
        self.addOutput(QgsProcessingOutputString("RESULT", "Test result"))

    def processAlgorithm(self, _parameters, _context, feedback):
        source_root = Path(os.environ["ZERO2AGENT_OSM_SOURCE_ROOT"]).resolve()
        parent = str(source_root.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)

        from zero2agent_osm_downloader.core.catalog import PRESETS, get_preset
        from zero2agent_osm_downloader.core.basemap import add_osm_basemap
        from zero2agent_osm_downloader.core.query import (
            advanced_specs,
            build_query,
        )
        from zero2agent_osm_downloader.core.map_styling import apply_map_theme
        from zero2agent_osm_downloader.core.map_themes import MAP_THEMES
        from zero2agent_osm_downloader.dialogs.dock import AgentOsmDock
        from zero2agent_osm_downloader.dialogs.theme import (
            contrast_ratio,
            dock_color_tokens,
            dock_stylesheet,
        )
        from zero2agent_osm_downloader.processing import osm_algorithms
        from zero2agent_osm_downloader.processing.osm_algorithms import (
            _relation_polygon,
        )

        # This test seeds every response it needs.  Without this guard a cache
        # key that quietly stops matching turns a deterministic check into a
        # live Overpass download that appears to hang; make it fail instead.
        def _cache_only(query, _feedback):
            payload = osm_algorithms._cached(query)
            if payload is None:
                raise RuntimeError(
                    "A smoke-test query was not seeded into the cache:\n"
                    f"{query}"
                )
            return payload

        osm_algorithms._fetch_json = _cache_only
        from qgis.PyQt.QtGui import QColor, QPalette
        from qgis.PyQt.QtWidgets import QProgressBar

        progress = QProgressBar()
        AgentOsmDock._set_progress(
            SimpleNamespace(progress=progress),
            42.6,
        )
        if progress.value() != 43:
            raise RuntimeError(
                "Floating-point progress was not converted to int."
            )

        relation_geometry = _relation_polygon(
            {
                "members": [
                    {
                        "type": "way",
                        "role": "outer",
                        "geometry": [
                            {"lat": 38.410, "lon": 27.120},
                            {"lat": 38.410, "lon": 27.121},
                            {"lat": 38.411, "lon": 27.121},
                        ],
                    },
                    {
                        "type": "way",
                        "role": "outer",
                        "geometry": [
                            {"lat": 38.411, "lon": 27.121},
                            {"lat": 38.411, "lon": 27.120},
                            {"lat": 38.410, "lon": 27.120},
                        ],
                    },
                ]
            }
        )
        if relation_geometry is None or relation_geometry.isEmpty():
            raise RuntimeError(
                "Fragmented multipolygon members were not assembled."
            )

        light = QPalette()
        light.setColor(QPalette.ColorRole.Window, QColor("#F4F4F4"))
        light.setColor(QPalette.ColorRole.WindowText, QColor("#202020"))
        light.setColor(QPalette.ColorRole.Base, QColor("#FFFFFF"))
        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Window, QColor("#20242A"))
        dark.setColor(QPalette.ColorRole.WindowText, QColor("#F2F4F6"))
        dark.setColor(QPalette.ColorRole.Base, QColor("#15181D"))
        light_style = dock_stylesheet(light)
        dark_style = dock_stylesheet(dark)
        light_tokens = dock_color_tokens(light)
        dark_tokens = dock_color_tokens(dark)
        if light_style == dark_style:
            raise RuntimeError("Dock styling did not follow light/dark palettes.")
        for name, tokens in (("light", light_tokens), ("dark", dark_tokens)):
            if tokens["surface"] not in (
                light_style if name == "light" else dark_style
            ):
                raise RuntimeError(f"The {name} workspace color was not applied.")
            for foreground, background in (
                ("text", "surface"),
                ("input_text", "input_surface"),
                ("subtle", "surface"),
                ("accent_text", "accent"),
            ):
                ratio = contrast_ratio(
                    QColor(tokens[foreground]), QColor(tokens[background])
                )
                if ratio < 4.5:
                    raise RuntimeError(
                        f"The {name} {foreground}/{background} contrast is {ratio:.2f}."
                    )

        from qgis.utils import plugins

        class _SmartModelerBridgeStub:
            connections_opened = 0
            workspace_opened = 0

            @staticmethod
            def agent_connection_info():
                return {
                    "profile_name": "Research",
                    "provider_id": "deepseek",
                    "provider_name": "DeepSeek API",
                    "model": "deepseek-chat",
                    "agent_chat_enabled": True,
                }

            def open_ai_connections(self):
                self.connections_opened += 1
                return True

            def open_agent_workspace(self):
                self.workspace_opened += 1

        bridge = _SmartModelerBridgeStub()
        previous_bridge = plugins.get("zero2smartmodeler")
        plugins["zero2smartmodeler"] = bridge
        try:
            dock = AgentOsmDock(None)
            initial_map_theme = dock.current_map_theme()
            if dock.tabs.count() != 4:
                raise RuntimeError(
                    "The Download/Query/Command/Connections UI is incomplete."
                )
            if dock.map_theme_combo.count() != len(MAP_THEMES):
                raise RuntimeError("The complete map-theme catalog is not visible.")
            urban_index = dock.group_combo.findData("urban_context")
            dock.group_combo.setCurrentIndex(urban_index)
            if dock.dataset_list.count() < 3 or "Public transport context" not in {
                dock.dataset_list.item(index).text()
                for index in range(dock.dataset_list.count())
            }:
                raise RuntimeError("Urban Context lacks related transit datasets.")
            if "Public Transport" not in dock.theme_context.text():
                raise RuntimeError("Theme relationship guidance is missing.")
            if dock.current_road_width_mode() != "highway":
                raise RuntimeError("Highway-category road widths are not default.")
            if dock.road_width_combo.findData("uniform") < 0:
                raise RuntimeError("Uniform road-width fallback is missing.")
            if dock.command_example_combo.count() < 12 or not any(
                "Tokyo" in dock.command_example_combo.itemText(index)
                for index in range(dock.command_example_combo.count())
            ):
                raise RuntimeError("Global command examples are incomplete.")
            cyber_index = dock.map_theme_combo.findData("cyber")
            dock.map_theme_combo.setCurrentIndex(cyber_index)
            if dock.current_map_theme() != "cyber":
                raise RuntimeError("The selected map theme was not retained.")
            if len(dock.map_theme_swatches) != 6 or not all(
                swatch.styleSheet() for swatch in dock.map_theme_swatches
            ):
                raise RuntimeError("The map-theme swatch preview is incomplete.")
            if not dock.ai_connections_button.isEnabled():
                raise RuntimeError("AI Connections is a silent disabled action.")
            dock.custom_check.setChecked(True)
            if dock.group_combo.isEnabled() or not dock.key_edit.isEnabled():
                raise RuntimeError("Custom-tag controls did not switch modes.")
            if "wheelchair" not in dock.query_preview.toPlainText():
                raise RuntimeError("The advanced query preview was not generated.")
            dock.tabs.setCurrentWidget(dock.query_tab)
            if dock.run_group.isHidden() or "advanced" not in (
                dock.download_button.text().casefold()
            ):
                raise RuntimeError("The query run action is not contextual.")
            dock.query_preview_button.setChecked(True)
            if dock.query_preview.isHidden():
                raise RuntimeError("The generated query preview did not expand.")
            dock.query_preview_button.setChecked(False)
            if not dock.query_preview.isHidden():
                raise RuntimeError("The generated query preview did not collapse.")
            dock.tabs.setCurrentWidget(dock.command_tab)
            if not dock.run_group.isHidden():
                raise RuntimeError("Command tab exposes an unrelated download action.")
            if "DeepSeek API" not in dock.agent_connection.text():
                raise RuntimeError(
                    "02Agent Smart Modeler profile is not visible in the dock."
                )
            dock._open_ai_connections()
            dock._open_agent_workspace()
            if (bridge.connections_opened, bridge.workspace_opened) != (1, 1):
                raise RuntimeError("02Agent Smart Modeler public bridge buttons did not run.")
            initial_index = dock.map_theme_combo.findData(initial_map_theme)
            dock.map_theme_combo.setCurrentIndex(max(0, initial_index))
            dock.deleteLater()
        finally:
            if previous_bridge is None:
                plugins.pop("zero2smartmodeler", None)
            else:
                plugins["zero2smartmodeler"] = previous_bridge

        basemap_project = QgsProject()
        basemap, added = add_osm_basemap(basemap_project)
        if not added or not basemap.isValid():
            raise RuntimeError("The OSM XYZ basemap could not be created.")
        same_basemap, added_again = add_osm_basemap(basemap_project)
        if added_again or same_basemap.id() != basemap.id():
            raise RuntimeError("OSM basemap duplicate protection failed.")
        basemap_project.removeMapLayer(basemap.id())

        bbox = (38.4100, 27.1200, 38.4160, 27.1260)
        query = build_query(get_preset("urban_form").tags, bbox)
        osm_algorithms._cache().set(
            query,
            {
                "elements": [
                    {
                        "type": "node",
                        "id": 1,
                        "lat": 38.4120,
                        "lon": 27.1220,
                        "tags": {"place": "neighbourhood", "name": "Centre"},
                    },
                    {
                        "type": "way",
                        "id": 2,
                        "tags": {"barrier": "fence"},
                        "geometry": [
                            {"lat": 38.4110, "lon": 27.1210},
                            {"lat": 38.4120, "lon": 27.1220},
                        ],
                    },
                    {
                        "type": "way",
                        "id": 3,
                        "tags": {"building": "yes"},
                        "geometry": [
                            {"lat": 38.4130, "lon": 27.1230},
                            {"lat": 38.4130, "lon": 27.1240},
                            {"lat": 38.4140, "lon": 27.1240},
                            {"lat": 38.4140, "lon": 27.1230},
                            {"lat": 38.4130, "lon": 27.1230},
                        ],
                    },
                ]
            },
        )

        import processing

        algorithm = QgsApplication.processingRegistry().algorithmById(
            "zero2agentosm:download_preset"
        )
        if algorithm is None:
            raise RuntimeError("Agent protocol endpoint is not registered.")
        if not {"agent", "openstreetmap"} <= set(algorithm.tags()):
            raise RuntimeError("Agent protocol endpoint search tags are missing.")
        if not algorithm.helpUrl().endswith("AGENT_PROTOCOL.md"):
            raise RuntimeError("Agent protocol help URL is missing.")

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        results = processing.run(
            "zero2agentosm:download_preset",
            {
                "PRESET": next(
                    index
                    for index, preset in enumerate(PRESETS)
                    if preset.preset_id == "urban_form"
                ),
                "EXTENT": QgsReferencedRectangle(
                    QgsRectangle(27.1200, 38.4100, 27.1260, 38.4160),
                    QgsCoordinateReferenceSystem.fromEpsgId(4326),
                ),
                "OUTPUT_POINTS": "TEMPORARY_OUTPUT",
                "OUTPUT_LINES": "TEMPORARY_OUTPUT",
                "OUTPUT_POLYGONS": "TEMPORARY_OUTPUT",
            },
            context=context,
            feedback=feedback,
            is_child_algorithm=True,
        )
        counts = {}
        output_layers = {}
        for name in ("OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POLYGONS"):
            layer = QgsProcessingUtils.mapLayerFromString(
                results[name], context, True
            )
            if layer is None:
                raise RuntimeError(f"{name} is not a layer.")
            counts[name] = layer.featureCount()
            output_layers[name] = layer
        if tuple(counts.values()) != (1, 1, 1):
            raise RuntimeError(f"Unexpected mixed output counts: {counts}")

        from qgis.core import QgsCategorizedSymbolRenderer, QgsExpression

        expected_categories = {
            "OUTPUT_POINTS": 9,
            "OUTPUT_LINES": 11,
            "OUTPUT_POLYGONS": 12,
        }
        for name, layer in output_layers.items():
            resolved = apply_map_theme(layer, "aegean")
            renderer = layer.renderer()
            if resolved != "aegean" or not isinstance(
                renderer, QgsCategorizedSymbolRenderer
            ):
                raise RuntimeError(f"{name} did not receive a semantic renderer.")
            if len(renderer.categories()) != expected_categories[name]:
                raise RuntimeError(f"{name} has incomplete style categories.")
            if layer.customProperty("zero2agent/map_theme") != "aegean":
                raise RuntimeError(f"{name} did not record its map theme.")
            expression = QgsExpression(renderer.classAttribute())
            if expression.hasParserError():
                raise RuntimeError(
                    f"{name} has an invalid style expression: "
                    f"{expression.parserErrorString()}"
                )
        expected_colors = {
            "OUTPUT_POINTS": ("tree", "#476a4c"),
            "OUTPUT_LINES": ("major", "#e76f51"),
            "OUTPUT_POLYGONS": ("building_residential", "#eadcc8"),
        }
        for name, (category_value, expected_color) in expected_colors.items():
            categories = {
                str(category.value()): category
                for category in output_layers[name].renderer().categories()
            }
            actual = categories[category_value].symbol().color().name().lower()
            if actual != expected_color:
                raise RuntimeError(
                    f"{name} {category_value} is {actual}, expected {expected_color}."
                )

        advanced = QgsApplication.processingRegistry().algorithmById(
            "zero2agentosm:download_advanced"
        )
        if advanced is None:
            raise RuntimeError("The structured advanced endpoint is not registered.")
        advanced_query = build_query(
            advanced_specs(
                (("amenity", "school"), ("wheelchair", "yes")),
                ("polygon",),
                "all",
            ),
            bbox,
            "all",
        )
        osm_algorithms._cache().set(
            advanced_query,
            {
                "elements": [
                    {
                        "type": "way",
                        "id": 10,
                        "tags": {
                            "amenity": "school",
                            "wheelchair": "yes",
                            "name": "Accessible School",
                        },
                        "geometry": [
                            {"lat": 38.4130, "lon": 27.1230},
                            {"lat": 38.4130, "lon": 27.1240},
                            {"lat": 38.4140, "lon": 27.1240},
                            {"lat": 38.4140, "lon": 27.1230},
                            {"lat": 38.4130, "lon": 27.1230},
                        ],
                    },
                    {
                        "type": "way",
                        "id": 11,
                        "tags": {"amenity": "school"},
                        "geometry": [
                            {"lat": 38.4110, "lon": 27.1210},
                            {"lat": 38.4110, "lon": 27.1220},
                            {"lat": 38.4120, "lon": 27.1220},
                            {"lat": 38.4120, "lon": 27.1210},
                            {"lat": 38.4110, "lon": 27.1210},
                        ],
                    },
                ]
            },
        )
        advanced_results = processing.run(
            "zero2agentosm:download_advanced",
            {
                "MATCH_MODE": 1,
                "GEOMETRY": 3,
                "KEY_1": "amenity",
                "VALUE_1": "school",
                "KEY_2": "wheelchair",
                "VALUE_2": "yes",
                "KEY_3": "",
                "VALUE_3": "",
                "KEY_4": "",
                "VALUE_4": "",
                "EXTENT": QgsReferencedRectangle(
                    QgsRectangle(27.1200, 38.4100, 27.1260, 38.4160),
                    QgsCoordinateReferenceSystem.fromEpsgId(4326),
                ),
                "OUTPUT_POINTS": "TEMPORARY_OUTPUT",
                "OUTPUT_LINES": "TEMPORARY_OUTPUT",
                "OUTPUT_POLYGONS": "TEMPORARY_OUTPUT",
            },
            context=context,
            feedback=feedback,
            is_child_algorithm=True,
        )
        advanced_counts = []
        for name in ("OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POLYGONS"):
            layer = QgsProcessingUtils.mapLayerFromString(
                advanced_results[name], context, True
            )
            if layer is None:
                raise RuntimeError(f"Advanced {name} is not a layer.")
            advanced_counts.append(layer.featureCount())
        if tuple(advanced_counts) != (0, 0, 1):
            raise RuntimeError(
                f"Unexpected advanced output counts: {advanced_counts}"
            )

        tiled = self._check_wide_extent_tiling(context, feedback)
        boundary = self._check_place_boundary_download(context, feedback)
        return {
            "RESULT": (
                "Urban form produced 1/1/1, advanced ALL filtering produced "
                f"0/0/1, {tiled}, and {boundary}."
            )
        }

    @staticmethod
    def _route_relation(identifier: int, offset: float) -> dict:
        """A route relation split across two member ways, as Overpass sends it."""
        return {
            "type": "relation",
            "id": identifier,
            "tags": {"route": "bus", "name": f"Line {identifier}"},
            "members": [
                {
                    "type": "way",
                    "role": "",
                    "geometry": [
                        {"lat": 38.4100 + offset, "lon": 27.1100},
                        {"lat": 38.4100 + offset, "lon": 27.1150},
                    ],
                },
                {
                    "type": "way",
                    "role": "",
                    "geometry": [
                        {"lat": 38.4100 + offset, "lon": 27.2500},
                        {"lat": 38.4100 + offset, "lon": 27.2550},
                    ],
                },
            ],
        }

    def _check_wide_extent_tiling(self, context, feedback) -> str:
        """A city-scale extent must tile, merge and keep multi-part lines."""
        import processing

        from zero2agent_osm_downloader.core.catalog import PRESETS, get_preset
        from zero2agent_osm_downloader.core.query import build_queries
        from zero2agent_osm_downloader.processing import osm_algorithms

        wide_bbox = (38.40, 27.10, 38.55, 27.30)
        specs = get_preset("urban_transit").tags
        queries = build_queries(specs, wide_bbox)
        if len(queries) < 4:
            raise RuntimeError(
                f"A {len(queries)}-tile plan does not exercise tiling."
            )
        # The same relation is returned by every tile it touches; each tile
        # also carries one stop of its own.
        shared_relation = self._route_relation(500, 0.0)
        for index, query in enumerate(queries):
            osm_algorithms._cache().set(
                query,
                {
                    "elements": [
                        dict(shared_relation),
                        {
                            "type": "node",
                            "id": 600 + index,
                            "lat": 38.4100,
                            "lon": 27.1100,
                            "tags": {
                                "highway": "bus_stop",
                                "name": f"Stop {index}",
                            },
                        },
                    ]
                },
            )

        results = processing.run(
            "zero2agentosm:download_preset",
            {
                "PRESET": [
                    index
                    for index, preset in enumerate(PRESETS)
                    if preset.preset_id == "urban_transit"
                ],
                "EXTENT": QgsReferencedRectangle(
                    QgsRectangle(27.10, 38.40, 27.30, 38.55),
                    QgsCoordinateReferenceSystem.fromEpsgId(4326),
                ),
                "OUTPUT_POINTS": "TEMPORARY_OUTPUT",
                "OUTPUT_LINES": "TEMPORARY_OUTPUT",
                "OUTPUT_POLYGONS": "TEMPORARY_OUTPUT",
            },
            context=context,
            feedback=feedback,
            is_child_algorithm=True,
        )
        points = QgsProcessingUtils.mapLayerFromString(
            results["OUTPUT_POINTS"], context, True
        )
        lines = QgsProcessingUtils.mapLayerFromString(
            results["OUTPUT_LINES"], context, True
        )
        if points is None or lines is None:
            raise RuntimeError("The tiled download produced no layers.")
        if points.featureCount() != len(queries):
            raise RuntimeError(
                f"Expected one stop per tile, got {points.featureCount()} "
                f"from {len(queries)} tiles."
            )
        # The relation appears in every tile and must survive as one feature.
        if lines.featureCount() != 1:
            raise RuntimeError(
                f"Tile seams duplicated a relation into "
                f"{lines.featureCount()} features."
            )
        feature = next(lines.getFeatures())
        geometry = feature.geometry()
        if geometry.isEmpty() or not geometry.isMultipart():
            raise RuntimeError(
                "A multi-part route relation did not reach the line sink."
            )
        if len(geometry.asMultiPolyline()) != 2:
            raise RuntimeError(
                "The disjoint parts of a route relation were dropped."
            )
        if feature["route"] != "bus" or feature["tourism"] != "":
            raise RuntimeError(
                f"Attribute columns are misaligned: route={feature['route']!r} "
                f"tourism={feature['tourism']!r}."
            )
        return (
            f"a {len(queries)}-tile extent merged to "
            f"{points.featureCount()} stops and 1 multi-part route"
        )

    def _check_place_boundary_download(self, context, feedback) -> str:
        """A resolved place must download through an area filter, not a box."""
        import processing

        from zero2agent_osm_downloader.core.catalog import PRESETS, get_preset
        from zero2agent_osm_downloader.core.query import build_area_query
        from zero2agent_osm_downloader.processing import osm_algorithms

        area_id = 3_600_223_474
        place_bbox = (38.4100, 27.1200, 38.4160, 27.1260)
        query = build_area_query(
            get_preset("urban_form").tags, area_id, place_bbox
        )
        if "area(3600223474)->.searchArea;" not in query:
            raise RuntimeError("The boundary query lost its area filter.")
        if "(area.searchArea)(38.4100000," not in query:
            raise RuntimeError(
                "The boundary query is not also bounded by the extent."
            )
        osm_algorithms._cache().set(
            query,
            {
                "elements": [
                    {
                        "type": "way",
                        "id": 900,
                        "tags": {"building": "yes", "name": "Inside boundary"},
                        "geometry": [
                            {"lat": 38.4130, "lon": 27.1230},
                            {"lat": 38.4130, "lon": 27.1240},
                            {"lat": 38.4140, "lon": 27.1240},
                            {"lat": 38.4140, "lon": 27.1230},
                            {"lat": 38.4130, "lon": 27.1230},
                        ],
                    }
                ]
            },
        )
        results = processing.run(
            "zero2agentosm:download_place",
            {
                "PLACE": "Konak, Izmir",
                "CLIP": 0,
                # Pre-resolved, so the run must not call the geocoder at all.
                "AREA_ID": str(area_id),
                "PRESET": [
                    index
                    for index, preset in enumerate(PRESETS)
                    if preset.preset_id == "urban_form"
                ],
                "EXTENT": QgsReferencedRectangle(
                    QgsRectangle(27.1200, 38.4100, 27.1260, 38.4160),
                    QgsCoordinateReferenceSystem.fromEpsgId(4326),
                ),
                "OUTPUT_POINTS": "TEMPORARY_OUTPUT",
                "OUTPUT_LINES": "TEMPORARY_OUTPUT",
                "OUTPUT_POLYGONS": "TEMPORARY_OUTPUT",
            },
            context=context,
            feedback=feedback,
            is_child_algorithm=True,
        )
        polygons = QgsProcessingUtils.mapLayerFromString(
            results["OUTPUT_POLYGONS"], context, True
        )
        if polygons is None or polygons.featureCount() != 1:
            raise RuntimeError(
                "The pre-resolved boundary download produced no polygon."
            )
        if next(polygons.getFeatures())["name"] != "Inside boundary":
            raise RuntimeError("The boundary download returned the wrong feature.")

        # An area carries no size of its own, so the extent ceiling must still
        # apply when one is supplied. Otherwise any caller could pair a
        # country-sized area id with a small extent and escape every limit.
        from qgis.core import QgsProcessingException

        refused = False
        try:
            processing.run(
                "zero2agentosm:download_place",
                {
                    "PLACE": "Oversized",
                    "CLIP": 0,
                    "AREA_ID": str(area_id),
                    "PRESET": [
                        index
                        for index, preset in enumerate(PRESETS)
                        if preset.preset_id == "urban_form"
                    ],
                    "EXTENT": QgsReferencedRectangle(
                        QgsRectangle(20.0, 30.0, 24.0, 34.0),
                        QgsCoordinateReferenceSystem.fromEpsgId(4326),
                    ),
                    "OUTPUT_POINTS": "TEMPORARY_OUTPUT",
                    "OUTPUT_LINES": "TEMPORARY_OUTPUT",
                    "OUTPUT_POLYGONS": "TEMPORARY_OUTPUT",
                },
                context=context,
                feedback=feedback,
                is_child_algorithm=True,
            )
        except QgsProcessingException:
            refused = True
        if not refused:
            raise RuntimeError(
                "An oversized boundary request bypassed the extent ceiling."
            )
        return (
            "a pre-resolved boundary download produced 1 polygon and an "
            "oversized one was refused"
        )


def main() -> bool:
    """Bootstrap headless QGIS, register the provider, run the check."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    plugin_dir = Path(
        os.environ.get(
            "ZERO2AGENT_OSM_SOURCE_ROOT",
            str(Path(__file__).resolve().parent.parent),
        )
    ).resolve()
    os.environ.setdefault("ZERO2AGENT_OSM_SOURCE_ROOT", str(plugin_dir))
    if str(plugin_dir.parent) not in sys.path:
        sys.path.insert(0, str(plugin_dir.parent))

    app = QgsApplication.instance()
    owns_app = app is None
    profile = None
    if owns_app:
        profile = tempfile.TemporaryDirectory(prefix="zero2agent-smoke-")
        app = QgsApplication([], False, profile.name, "external")
        app.initQgis()

    # Processing ships under the QGIS plugins directory, which is not on
    # sys.path outside the application.
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
    print(" 02Agent OSM Downloader - provider smoke")
    print("=" * 62)
    ok = False
    try:
        from qgis.core import QgsProcessingFeedback

        result = AgentOsmProviderSmoke().processAlgorithm(
            {}, QgsProcessingContext(), QgsProcessingFeedback()
        )
        print(f"  [PASS] {result['RESULT']}")
        ok = True
    except Exception as error:  # a failed assertion is a failed test, not a crash
        print(f"  [FAIL] {type(error).__name__}: {error}")
    finally:
        print("-" * 62)
        print(f"  {1 if ok else 0}/1 passed")
        QgsProject.instance().clear()
        if owns_app:
            app.exitQgis()
            profile.cleanup()
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
else:
    main()
