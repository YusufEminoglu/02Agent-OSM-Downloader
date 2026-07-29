"""Real-QGIS deterministic smoke test for the mixed-geometry preset."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from qgis.core import (
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
        from zero2agent_osm_downloader.processing.osm_algorithms import _CACHE

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
