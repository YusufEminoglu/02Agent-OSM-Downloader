"""Opt-in live Overpass acceptance test for the advanced endpoint.

Run from the directory containing the plugin package:

    python-qgis.bat -m zero2agent_osm_downloader.tests.qgis_live_overpass --live

The canonical offline suite does not invoke this module.  It uses a small,
bounded Izmir extent, fixed plugin endpoints and temporary QGIS outputs only.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProcessingUtils,
    QgsProject,
    QgsRectangle,
    QgsReferencedRectangle,
)


def main() -> bool:
    if "--live" not in sys.argv:
        print("SKIP: pass --live to call the public Overpass mirrors.")
        return True

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    plugin_dir = Path(__file__).resolve().parent.parent
    if str(plugin_dir.parent) not in sys.path:
        sys.path.insert(0, str(plugin_dir.parent))

    profile = tempfile.TemporaryDirectory(prefix="zero2agent-live-")
    app = QgsApplication([], False, profile.name, "external")
    app.initQgis()
    plugins_path = os.path.join(QgsApplication.prefixPath(), "python", "plugins")
    if plugins_path not in sys.path:
        sys.path.append(plugins_path)

    from processing.core.Processing import Processing
    from qgis.analysis import QgsNativeAlgorithms
    import processing

    from zero2agent_osm_downloader.processing.provider import AgentOsmProvider

    native = QgsNativeAlgorithms()
    provider = AgentOsmProvider()
    QgsApplication.processingRegistry().addProvider(native)
    Processing.initialize()
    QgsApplication.processingRegistry().addProvider(provider)

    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    ok = False
    try:
        results = processing.run(
            "zero2agentosm:download_advanced",
            {
                "MATCH_MODE": 0,
                "GEOMETRY": 0,
                "KEY_1": "building",
                "VALUE_1": "",
                "KEY_2": "highway",
                "VALUE_2": "",
                "KEY_3": "",
                "VALUE_3": "",
                "KEY_4": "",
                "VALUE_4": "",
                "EXTENT": QgsReferencedRectangle(
                    QgsRectangle(27.1260, 38.4160, 27.1290, 38.4190),
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
        counts = []
        for output in (
            "OUTPUT_POINTS",
            "OUTPUT_LINES",
            "OUTPUT_POLYGONS",
        ):
            layer = QgsProcessingUtils.mapLayerFromString(
                results[output], context, True
            )
            if layer is None:
                raise RuntimeError(f"{output} did not resolve to a vector layer.")
            fields = set(layer.fields().names())
            if not {"osm_id", "tags_json", "matched_tags"} <= fields:
                raise RuntimeError(f"{output} is missing provenance fields.")
            counts.append(layer.featureCount())
        if sum(counts) <= 0:
            raise RuntimeError("Live Overpass response contained no usable features.")
        print(
            "LIVE OVERPASS PASS: "
            f"points={counts[0]}, lines={counts[1]}, polygons={counts[2]}"
        )
        ok = True
    except Exception as error:  # network acceptance boundary
        print(f"LIVE OVERPASS FAIL: {type(error).__name__}: {error}")
    finally:
        QgsProject.instance().clear()
        QgsApplication.processingRegistry().removeProvider(provider)
        QgsApplication.processingRegistry().removeProvider(native)
        app.exitQgis()
        profile.cleanup()
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
