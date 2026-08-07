"""Processing provider for 02Agent OSM Downloader."""
from __future__ import annotations

import os

from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProcessingProvider

from .osm_algorithms import (
    DownloadAdvancedQueryAlgorithm,
    DownloadCustomTagAlgorithm,
    DownloadPresetAlgorithm,
)


class AgentOsmProvider(QgsProcessingProvider):
    PROVIDER_ID = "zero2agentosm"

    def id(self) -> str:
        return self.PROVIDER_ID

    def name(self) -> str:
        return "02Agent OSM Downloader"

    def longName(self) -> str:
        return "02Agent OSM Downloader — Structured OSM Acquisition"

    def icon(self) -> QIcon:
        root = os.path.dirname(os.path.dirname(__file__))
        return QIcon(os.path.join(root, "icons", "icon.png"))

    def loadAlgorithms(self) -> None:
        self.addAlgorithm(DownloadPresetAlgorithm())
        self.addAlgorithm(DownloadCustomTagAlgorithm())
        self.addAlgorithm(DownloadAdvancedQueryAlgorithm())
