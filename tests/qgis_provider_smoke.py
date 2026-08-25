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
        from zero2agent_osm_downloader.core.map_styling_expressions import (
            RENDERER_EXPRESSIONS,
            expression_values,
        )
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

        for name, layer in output_layers.items():
            resolved = apply_map_theme(layer, "aegean")
            renderer = layer.renderer()
            if resolved != "aegean" or not isinstance(
                renderer, QgsCategorizedSymbolRenderer
            ):
                raise RuntimeError(f"{name} did not receive a semantic renderer.")
            # A hand-counted total goes stale the moment a category is added,
            # and it never catches the failure that actually matters: a value
            # the expression emits with no category draws nothing at all.
            emitted = expression_values(renderer.classAttribute())
            styled = {str(category.value()) for category in renderer.categories()}
            if emitted - styled:
                raise RuntimeError(
                    f"{name} renders nothing for {sorted(emitted - styled)}."
                )
            if styled - emitted:
                raise RuntimeError(
                    f"{name} has legend entries nothing can match: "
                    f"{sorted(styled - emitted)}."
                )
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
        places = self._check_places_styling()
        closed = self._check_closed_ways_split_by_tags()
        basemap = self._check_osm_as_is_looks_like_a_basemap()
        return {
            "RESULT": (
                "Urban form produced 1/1/1, advanced ALL filtering produced "
                f"0/0/1, {tiled}, {boundary}, {places}, {closed}, and "
                f"{basemap}."
            )
        }

    def _check_osm_as_is_looks_like_a_basemap(self) -> str:
        """OSM As-Is has to render the way openstreetmap.org does.

        What makes that map read as a map rather than as a diagram is road
        *casing*: a dark outline with the carriageway drawn on top of it, and
        every casing painted before every carriageway so junctions join
        cleanly. A single-stroke line cannot look like it however it is
        coloured, so the structure is asserted, not just the colours.
        """
        from qgis.core import (
            QgsExpression,
            QgsExpressionContext,
            QgsExpressionContextUtils,
            QgsFeature,
            QgsVectorLayer,
        )

        from zero2agent_osm_downloader.core.map_styling import apply_map_theme
        from zero2agent_osm_downloader.core.schema import FIELD_NAMES

        fields = "&".join(f"field={name}:string" for name in FIELD_NAMES)
        lines = QgsVectorLayer(
            f"LineString?crs=epsg:3857&{fields}", "as_is_lines", "memory"
        )
        if not lines.isValid():
            raise RuntimeError("Could not build the OSM As-Is test layer.")
        if apply_map_theme(lines, "osm_as_is") != "osm_as_is":
            raise RuntimeError("The OSM As-Is theme did not resolve.")
        renderer = lines.renderer()
        if not renderer.usingSymbolLevels():
            raise RuntimeError(
                "OSM As-Is roads render without symbol levels, so every road "
                "paints its casing over the road it meets."
            )
        # Categories own their symbols; holding only the symbol leaves a
        # dangling pointer once the category is collected.
        categories = {
            str(category.value()): category
            for category in renderer.categories()
        }
        for value in ("major", "primary", "secondary", "tertiary",
                      "residential", "service"):
            symbol = categories[value].symbol()
            # Direction arrows ride along as a marker line; the road itself is
            # the two strokes.
            strokes = [
                symbol.symbolLayer(index)
                for index in range(symbol.symbolLayerCount())
                if type(symbol.symbolLayer(index)).__name__
                == "QgsSimpleLineSymbolLayer"
            ]
            if len(strokes) != 2:
                raise RuntimeError(
                    f"The '{value}' road has {len(strokes)} stroke(s); a road "
                    "is a casing plus a carriageway."
                )
            casing, carriageway = strokes
            if casing.width() <= carriageway.width():
                raise RuntimeError(
                    f"The '{value}' casing ({casing.width()}) is not wider "
                    f"than its carriageway ({carriageway.width()})."
                )
            if casing.renderingPass() >= carriageway.renderingPass():
                raise RuntimeError(
                    f"The '{value}' casing does not render before the "
                    "carriageways."
                )
        # Width has to fall with the hierarchy, and a residential street has
        # to read as a street: at hairline widths the render looks like a
        # wireframe next to the real OSM map.
        def _carriageway_width(value: str) -> float:
            symbol = categories[value].symbol()
            return [
                symbol.symbolLayer(index).width()
                for index in range(symbol.symbolLayerCount())
                if type(symbol.symbolLayer(index)).__name__
                == "QgsSimpleLineSymbolLayer"
            ][1]

        widths = {
            value: _carriageway_width(value)
            for value in ("major", "primary", "secondary", "tertiary",
                          "residential", "service")
        }
        ordered = ["major", "primary", "secondary", "tertiary",
                   "residential", "service"]
        for wider, narrower in zip(ordered, ordered[1:]):
            if widths[wider] <= widths[narrower]:
                raise RuntimeError(
                    f"'{wider}' ({widths[wider]}) is not wider than "
                    f"'{narrower}' ({widths[narrower]})."
                )
        if widths["residential"] < 2.0:
            raise RuntimeError(
                f"A residential street is {widths['residential']} mm wide; "
                "that reads as a hairline, not as a street."
            )
        # A footpath has no carriageway -- it is a dashed line, and it must
        # stay thinner than the streets it runs beside.
        active = categories["active"].symbol()
        if active.symbolLayerCount() != 1:
            raise RuntimeError("A footway should be a single dashed stroke.")
        if active.symbolLayer(0).width() >= widths["service"]:
            raise RuntimeError(
                "Footways are drawn as wide as service roads, so sidewalks "
                "read as the street network."
            )
        if not lines.labelsEnabled():
            raise RuntimeError("OSM As-Is roads carry no street-name labels.")
        label_settings = lines.labeling().settings()
        placement = label_settings.placement
        if int(placement) == 0:
            raise RuntimeError(
                "Street names use the default AroundPoint placement, which "
                "does not follow the road."
            )
        # A route relation is named for the service, not for the street its
        # members run along. Labelling it writes "EuroVelo 8 - Mediterranean
        # Route" across the whole network.
        if not label_settings.isExpression:
            raise RuntimeError(
                "OSM As-Is labels the raw name field, so route relations are "
                "labelled along every street they follow."
            )
        expression = QgsExpression(label_settings.fieldName)
        if expression.hasParserError():
            raise RuntimeError(
                f"The label expression does not parse: "
                f"{expression.parserErrorString()}"
            )
        context = QgsExpressionContext()
        context.appendScopes(
            QgsExpressionContextUtils.globalProjectLayerScopes(lines)
        )
        expression.prepare(context)
        for tags, expected in (
            ({"name": "312. Sokak", "highway": "residential"}, "312. Sokak"),
            ({"name": "T2: Halkapinar - Fahrettin Altay", "route": "tram"}, None),
            # OSM does not write the sea's name along the shore, nor the
            # city's name across the streets.
            ({"name": "Turk Ege Denizi kiyisi", "natural": "coastline"}, None),
            ({"name": "Izmir (sehir)", "place": "city"}, None),
        ):
            feature = QgsFeature(lines.fields())
            for key, value in tags.items():
                feature.setAttribute(key, value)
            context.setFeature(feature)
            if expression.evaluate(context) != expected:
                raise RuntimeError(
                    f"A feature tagged {tags} labels as "
                    f"{expression.evaluate(context)!r}, expected {expected!r}."
                )

        points = QgsVectorLayer(
            f"Point?crs=epsg:3857&{fields}", "as_is_points", "memory"
        )
        apply_map_theme(points, "osm_as_is")
        polygons = QgsVectorLayer(
            f"Polygon?crs=epsg:3857&{fields}", "as_is_polygons", "memory"
        )
        apply_map_theme(polygons, "osm_as_is")
        fills = {
            str(category.value()): category
            for category in polygons.renderer().categories()
        }
        building = fills["building_residential"].symbol().color().name()
        if building.lower() != "#d9d0c9":
            raise RuntimeError(
                f"OSM As-Is buildings are {building}, not the standard "
                "OSM building colour."
            )
        # Boundaries belong to the Places group. OSM As-Is neither downloads
        # them nor offers the reader a legend entry for them.
        for layer, styled in (
            ("polygon", polygons.renderer()),
            ("line", renderer),
            ("point", points.renderer()),
        ):
            admin = sorted(
                str(category.value())
                for category in styled.categories()
                if str(category.value()).startswith("admin_")
            )
            if admin:
                raise RuntimeError(
                    f"The OSM As-Is {layer} legend still offers {admin}."
                )
        if not polygons.renderer().usingSymbolLevels():
            raise RuntimeError(
                "OSM As-Is polygons render without symbol levels, so the "
                "urban blocks paint over whatever is stored after them."
            )
        levels = {
            str(category.value()):
                category.symbol().symbolLayer(0).renderingPass()
            for category in polygons.renderer().categories()
        }
        ground = levels["urban_residential"]
        if min(levels.values()) < ground:
            raise RuntimeError(
                "Something draws below the urban blocks; they are the ground."
            )
        for value in ("green", "water", "parking", "pedestrian", "civic"):
            if levels[value] <= ground:
                raise RuntimeError(
                    f"'{value}' ({levels[value]}) draws at or below the urban "
                    f"blocks ({ground})."
                )
        for building in (
            "building_residential", "building_commercial", "building_other",
        ):
            if levels[building] <= levels["green"]:
                raise RuntimeError(
                    f"'{building}' draws at or below the green space."
                )
        # "Other" is the catch-all. It must stay visibly neutral so an
        # unexpected polygon cannot pass itself off as the urban ground.
        blocks = {
            str(category.value()): category
            for category in polygons.renderer().categories()
        }
        if blocks["other"].label() == blocks["urban_residential"].label():
            raise RuntimeError(
                "The catch-all is labelled as urban blocks again, so anything "
                "that fails to classify is read as the city's ground."
            )
        # Street names belong *on* the carriageway, the way OSM writes them.
        flags = int(label_settings.lineSettings().placementFlags())
        from qgis.core import QgsLabeling

        on_line = int(
            getattr(
                getattr(QgsLabeling, "LinePlacementFlag", QgsLabeling), "OnLine"
            )
        )
        if not flags & on_line:
            raise RuntimeError(
                f"Street names are placed with flags {flags}, which does not "
                "include OnLine, so they float beside the road."
            )

        # A POI name is written in its family's colour and smaller than a
        # street name. One flat grey at street size is a wall of text.
        point_settings = points.labeling().settings()
        if point_settings.format().size() >= label_settings.format().size():
            raise RuntimeError(
                "POI names are set as large as street names."
            )
        if not point_settings.dataDefinedProperties().hasActiveProperties():
            raise RuntimeError(
                "POI names are all one colour, so the map cannot be skimmed "
                "by family the way OSM's can."
            )

        # POIs draw as the icon the reader expects, and the road network's own
        # nodes stay out of the way instead of being an unexplained scatter.
        from zero2agent_osm_downloader.core.map_styling import (
            CARTO_ICONS,
            _svg_exists,
        )

        pois = {
            str(category.value()): category
            for category in points.renderer().categories()
        }
        for category, (default_icon, _mapping) in CARTO_ICONS.items():
            if category not in pois:
                raise RuntimeError(f"'{category}' has no point category.")
            layer_kind = type(
                pois[category].symbol().symbolLayer(0)
            ).__name__
            # An install without the icon set falls back to a plain marker on
            # purpose; a missing SVG would otherwise draw a broken-image box.
            if _svg_exists(default_icon) and layer_kind != "QgsSvgMarkerSymbolLayer":
                raise RuntimeError(
                    f"'{category}' renders as {layer_kind} even though "
                    f"{default_icon} is installed."
                )
        for category, (_default, mapping) in CARTO_ICONS.items():
            for _condition, icon in mapping:
                if not _svg_exists(icon):
                    continue
                properties = pois[category].symbol().symbolLayer(
                    0
                ).dataDefinedProperties()
                if not properties.hasActiveProperties():
                    raise RuntimeError(
                        f"'{category}' has per-type icons available but picks "
                        "one icon for every feature."
                    )
                break
        # The bundled icons are parametrised. Leaving the fill unset renders
        # every one of them solid black.
        for category in CARTO_ICONS:
            symbol_layer = pois[category].symbol().symbolLayer(0)
            if type(symbol_layer).__name__ != "QgsSvgMarkerSymbolLayer":
                continue
            fill = symbol_layer.fillColor().name().lower()
            if fill in ("#000000", "#ffffff"):
                raise RuntimeError(
                    f"The '{category}' icon renders {fill}; its OSM family "
                    "colour was never applied."
                )
        junction = pois["junction"].symbol().size()
        for busier in ("shop", "food", "health"):
            if junction >= pois[busier].symbol().size():
                raise RuntimeError(
                    f"Junction nodes ({junction}) draw as large as "
                    f"'{busier}' places; they are road furniture."
                )
        # A crossing is where people cross; it earns the zebra rather than
        # being filed away as a grey traffic dot.
        from zero2agent_osm_downloader.core.map_styling import plugin_svg

        zebra = plugin_svg("crossing.svg")
        if not os.path.isfile(zebra):
            raise RuntimeError(
                f"The crossing icon is missing from the plugin: {zebra}"
            )
        if "crossing" not in pois:
            raise RuntimeError("Pedestrian crossings have no category.")
        crossing_layer = type(pois["crossing"].symbol().symbolLayer(0)).__name__
        if crossing_layer != "QgsSvgMarkerSymbolLayer":
            raise RuntimeError(
                f"Crossings render as {crossing_layer}, not as the zebra icon."
            )

        # Kerbs and barriers are the footway's edge, not somewhere to go.
        if "furniture" not in pois:
            raise RuntimeError(
                "Kerbs and barriers have no category, so they fall back into "
                "'other' as an unexplained dot on every corner."
            )
        if pois["furniture"].symbol().size() >= pois["shop"].symbol().size():
            raise RuntimeError("Kerbs draw as large as shops.")

        # One-way streets carry OSM's direction arrows; a waterway is not a
        # road and takes none.
        road = categories["residential"].symbol()
        arrows = [
            road.symbolLayer(index)
            for index in range(road.symbolLayerCount())
            if type(road.symbolLayer(index)).__name__
            == "QgsMarkerLineSymbolLayer"
        ]
        if not arrows:
            raise RuntimeError("Roads carry no one-way direction arrows.")
        sub_layer = arrows[0].subSymbol().symbolLayer(0)
        if not sub_layer.dataDefinedProperties().hasActiveProperties():
            raise RuntimeError(
                "The direction arrow is not data-defined, so every street "
                "draws arrows whether it is one-way or not."
            )
        # One arrow where the street name is written. On an interval the
        # arrows stipple down the whole road and the map stops being readable.
        from qgis.core import Qgis

        central = int(Qgis.MarkerLinePlacement.CentralPoint)
        placement = (
            arrows[0].placements()
            if hasattr(arrows[0], "placements")
            else arrows[0].placement()
        )
        if not int(placement) & central:
            raise RuntimeError(
                f"Direction arrows are placed with {int(placement)}, not once "
                "at the centre of the street."
            )
        water = categories["water"].symbol()
        if any(
            type(water.symbolLayer(index)).__name__
            == "QgsMarkerLineSymbolLayer"
            for index in range(water.symbolLayerCount())
        ):
            raise RuntimeError("Waterways draw one-way road arrows.")
        return (
            "OSM As-Is draws cased roads, centred street names, OSM icons "
            "and quiet junction nodes"
        )

    def _check_closed_ways_split_by_tags(self) -> str:
        """A closed way becomes a polygon only when its tags enclose an area.

        OSM has no polygon primitive.  Splitting on closure alone turned every
        footpath loop, roundabout and racetrack into a polygon -- which is how
        an "OSM as-is" download filled its polygon layer with footways.
        """
        from zero2agent_osm_downloader.core.catalog import TagSpec, get_preset
        from zero2agent_osm_downloader.processing.osm_algorithms import (
            _kinds_for_element,
        )

        ring = [
            {"lat": 38.4100, "lon": 27.1200},
            {"lat": 38.4100, "lon": 27.1210},
            {"lat": 38.4110, "lon": 27.1210},
            {"lat": 38.4100, "lon": 27.1200},
        ]
        # The wildcard request asks for line *and* polygon, so the tags are
        # the only thing that can decide.
        wildcard = (
            TagSpec("*", "", "line"),
            TagSpec("*", "", "polygon"),
        )
        expected = {
            "footway loop": ({"highway": "footway"}, "line"),
            "roundabout": ({"highway": "residential", "junction": "roundabout"}, "line"),
            "pedestrian square": ({"highway": "pedestrian", "area": "yes"}, "polygon"),
            "building": ({"building": "yes"}, "polygon"),
            "park": ({"leisure": "park"}, "polygon"),
            "fence": ({"barrier": "fence"}, "line"),
        }
        for label, (tags, want) in expected.items():
            element = {"type": "way", "id": 1, "tags": tags, "geometry": ring}
            kinds = _kinds_for_element(element, wildcard)
            got = [kind for kind, _matches in kinds]
            if got != [want]:
                raise RuntimeError(
                    f"A closed {label} split as {got or 'nothing'}, "
                    f"expected ['{want}']."
                )

        # An administrative boundary is the relation; the closed ways inside
        # it are ring fragments.  Reading those as areas is what turned one
        # neighbourhood into a pile of overlapping part-polygons.
        admin_specs = get_preset("admin_level_8").tags
        admin_tags = {"boundary": "administrative", "admin_level": "8"}
        fragment = {"type": "way", "id": 3, "tags": admin_tags, "geometry": ring}
        kinds = [kind for kind, _m in _kinds_for_element(fragment, admin_specs)]
        if kinds != ["line"]:
            raise RuntimeError(
                f"A closed admin boundary way split as {kinds}, expected "
                "['line'] -- only the relation carries the area."
            )
        # ...and the relation must still give both, because the preset asks
        # for the boundary as an area and as a line.
        admin_relation = {
            "type": "relation",
            "id": 4,
            "tags": dict(admin_tags, type="boundary"),
            "members": [],
        }
        kinds = [kind for kind, _m in _kinds_for_element(admin_relation, admin_specs)]
        if "polygon" not in kinds or "line" not in kinds:
            raise RuntimeError(
                f"An admin boundary relation split as {kinds}, expected both "
                "line and polygon."
            )

        # A circular bus route closes up and is still a route.
        route = {
            "type": "relation",
            "id": 2,
            "tags": {"type": "route", "route": "bus"},
            "members": [],
        }
        kinds = [kind for kind, _matches in _kinds_for_element(route, wildcard)]
        if kinds != ["line"]:
            raise RuntimeError(
                f"A circular route relation split as {kinds}, expected ['line']."
            )
        return "closed ways split by tag, not by closure"

    def _check_places_styling(self) -> str:
        """Places layers must be read as places, not as urban morphology.

        The Places presets download administrative boundaries.  Rendering them
        through the morphology renderer is not a cosmetic slip — every boundary
        falls into the catch-all category and the legend offers the reader
        building, land-use and road entries that the layer cannot contain.
        """
        from qgis.core import (
            QgsCategorizedSymbolRenderer,
            QgsFeature,
            QgsVectorLayer,
        )

        from zero2agent_osm_downloader.core.map_styling import apply_map_theme
        from zero2agent_osm_downloader.core.map_styling_expressions import (
            ADMIN_LEVEL_STYLES,
            PLACES_LINE_EXPRESSION,
            PLACES_POINT_EXPRESSION,
            PLACES_POLYGON_EXPRESSION,
            expression_values,
        )
        from zero2agent_osm_downloader.core.schema import FIELD_NAMES

        fields = "&".join(f"field={name}:string" for name in FIELD_NAMES)
        expected = {
            "Polygon": PLACES_POLYGON_EXPRESSION,
            "LineString": PLACES_LINE_EXPRESSION,
            "Point": PLACES_POINT_EXPRESSION,
        }
        # Every route into the Places renderers, so none of them can rot: the
        # dock stamps the custom property, while Processing outputs only carry
        # the attributes the download wrote.
        scopes = ("property", "attributes")
        for geometry, expression in expected.items():
            for scope in scopes:
                layer = QgsVectorLayer(
                    f"{geometry}?crs=epsg:4326&{fields}", f"places_{geometry}", "memory"
                )
                if not layer.isValid():
                    raise RuntimeError(f"Could not build a {geometry} test layer.")
                if scope == "property":
                    layer.setCustomProperty("zero2agent/style_scope", "places")
                else:
                    feature = QgsFeature(layer.fields())
                    feature.setAttribute("theme", "Places")
                    feature.setAttribute("preset_id", "admin_level_6")
                    if not layer.dataProvider().addFeature(feature):
                        raise RuntimeError("Could not seed the Places test layer.")
                    layer.updateExtents()
                resolved = apply_map_theme(layer, "aegean")
                renderer = layer.renderer()
                if resolved != "aegean" or not isinstance(
                    renderer, QgsCategorizedSymbolRenderer
                ):
                    raise RuntimeError(
                        f"A Places {geometry} layer got no categorised renderer."
                    )
                if renderer.classAttribute() != expression:
                    raise RuntimeError(
                        f"A Places {geometry} layer ({scope}) was styled as "
                        "urban morphology, not as places."
                    )
                styled = {
                    str(category.value()) for category in renderer.categories()
                }
                if styled != expression_values(expression):
                    raise RuntimeError(
                        f"The Places {geometry} legend does not match its "
                        "expression."
                    )
                missing_admin = {
                    f"admin_{level}" for level in ADMIN_LEVEL_STYLES
                } - styled
                if missing_admin:
                    raise RuntimeError(
                        f"The Places {geometry} legend is missing "
                        f"{sorted(missing_admin)}."
                    )
                morphology = {
                    value
                    for value in styled
                    if value.startswith("building_")
                    or value in {"green", "parking", "pedestrian", "major",
                                 "primary", "secondary", "tertiary",
                                 "residential", "service", "tree", "health",
                                 "education", "tourism", "sport"}
                }
                if morphology:
                    raise RuntimeError(
                        f"The Places {geometry} legend still offers morphology "
                        f"categories: {sorted(morphology)}."
                    )
                if not layer.labelsEnabled():
                    raise RuntimeError(
                        f"A Places {geometry} layer carries no name labels."
                    )
        return (
            "Places point/line/polygon layers style as administrative places "
            "through both the dock and Processing routes"
        )

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


def main(exit_process: bool = False) -> bool:
    """Bootstrap headless QGIS, register the provider, run the check.

    ``exit_process`` leaves through :func:`os._exit` once the result has been
    printed, and is for the standalone runners only -- never when the check is
    invoked from inside a live QGIS session.
    """
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
            if exit_process:
                # Returning from here destroys this frame, which drops the
                # last reference to the QgsApplication that exitQgis() has
                # already torn down.  When a check failed, the objects it
                # left alive make that destructor deadlock, and the runner
                # then times out after minutes with the diagnosis stranded
                # in a buffered pipe.  The verdict is printed and flushed, so
                # leave now rather than unwinding into the deadlock.
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(0 if ok else 1)
    return ok


if __name__ == "__main__":
    # Normally this does not return -- see `exit_process`.  It does when the
    # application was already running, and then the exit code still matters.
    sys.exit(0 if main(exit_process=True) else 1)
else:
    main()
