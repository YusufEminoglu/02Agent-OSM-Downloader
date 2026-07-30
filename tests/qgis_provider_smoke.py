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
import time
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

        from zero2agent_osm_downloader.core.catalog import get_preset
        from zero2agent_osm_downloader.core.query import build_query
        from zero2agent_osm_downloader.dialogs.dock import AgentOsmDock
        from zero2agent_osm_downloader.dialogs.theme import dock_stylesheet
        from zero2agent_osm_downloader.processing.osm_algorithms import (
            _CACHE,
            _relation_polygon,
        )
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
        if (
            light_style == dark_style
            or "#f4f4f4" not in light_style
            or "#20242a" not in dark_style
        ):
            raise RuntimeError("Dock styling did not follow light/dark palettes.")

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
        previous_bridge = plugins.get("planx_smartmodeler")
        plugins["planx_smartmodeler"] = bridge
        try:
            dock = AgentOsmDock(None)
            if dock.tabs.count() != 3:
                raise RuntimeError(
                    "The compact Download/Command/Connections UI is incomplete."
                )
            if not dock.ai_connections_button.isEnabled():
                raise RuntimeError("AI Connections is a silent disabled action.")
            dock.custom_check.setChecked(True)
            if dock.group_combo.isEnabled() or not dock.key_edit.isEnabled():
                raise RuntimeError("Custom-tag controls did not switch modes.")
            if "DeepSeek API" not in dock.agent_connection.text():
                raise RuntimeError(
                    "SmartModeler profile is not visible in the dock."
                )
            dock._open_ai_connections()
            dock._open_agent_workspace()
            if (bridge.connections_opened, bridge.workspace_opened) != (1, 1):
                raise RuntimeError("SmartModeler public bridge buttons did not run.")
            dock.deleteLater()
        finally:
            if previous_bridge is None:
                plugins.pop("planx_smartmodeler", None)
            else:
                plugins["planx_smartmodeler"] = previous_bridge

        bbox = (38.4100, 27.1200, 38.4160, 27.1260)
        query = build_query(get_preset("urban_form").tags, bbox)
        _CACHE[query] = (
            time.monotonic(),
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
                "PRESET": 5,
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
        for name in ("OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POLYGONS"):
            layer = QgsProcessingUtils.mapLayerFromString(
                results[name], context, True
            )
            if layer is None:
                raise RuntimeError(f"{name} is not a layer.")
            counts[name] = layer.featureCount()
        if tuple(counts.values()) != (1, 1, 1):
            raise RuntimeError(f"Unexpected mixed output counts: {counts}")
        return {
            "RESULT": "Urban form preset produced 1 point, 1 line and 1 polygon."
        }


def main() -> bool:
    """Bootstrap headless QGIS, register the provider, run the check."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    plugin_dir = Path(__file__).resolve().parent.parent
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
