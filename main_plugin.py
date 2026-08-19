"""QGIS lifecycle for 02Agent OSM Downloader."""
from __future__ import annotations

import contextlib
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction, QIcon
from qgis.PyQt.QtWidgets import QDockWidget, QMessageBox
from qgis.core import QgsApplication

from .processing.provider import AgentOsmProvider


class AgentOsmDownloaderPlugin:
    MENU_NAME = "&02Agent OSM Downloader"

    def __init__(self, iface) -> None:
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.about_action = None
        self.dock = None
        self.provider = None

    def initProcessing(self) -> None:
        if self.provider is not None:
            return
        registry = QgsApplication.processingRegistry()
        if registry.providerById(AgentOsmProvider.PROVIDER_ID) is None:
            provider = AgentOsmProvider()
            if registry.addProvider(provider):
                self.provider = provider

    def initGui(self) -> None:
        self.initProcessing()
        if self.iface is None:
            return
        icon = QIcon(os.path.join(self.plugin_dir, "icons", "icon.png"))
        self.action = QAction(icon, "Open 02Agent OSM Downloader", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.triggered.connect(self._toggle_dock)
        self.iface.addPluginToMenu(self.MENU_NAME, self.action)
        self.iface.addToolBarIcon(self.action)

        self.about_action = QAction("About", self.iface.mainWindow())
        self.about_action.triggered.connect(self._show_about)
        self.iface.addPluginToMenu(self.MENU_NAME, self.about_action)

    def unload(self) -> None:
        if self.dock is not None:
            self.dock.cancel()
            self.iface.removeDockWidget(self.dock)
            self.dock.setParent(None)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginMenu(self.MENU_NAME, self.action)
            self.iface.removeToolBarIcon(self.action)
            with contextlib.suppress(TypeError):
                self.action.triggered.disconnect(self._toggle_dock)
            self.action.setParent(None)
            self.action.deleteLater()
            self.action = None
        if self.about_action is not None:
            self.iface.removePluginMenu(self.MENU_NAME, self.about_action)
            with contextlib.suppress(TypeError):
                self.about_action.triggered.disconnect(self._show_about)
            self.about_action.setParent(None)
            self.about_action.deleteLater()
            self.about_action = None
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

    def _toggle_dock(self, visible: bool) -> None:
        if self.dock is None:
            from .dialogs.dock import AgentOsmDock

            self.dock = AgentOsmDock(self.iface, self.iface.mainWindow())
            self.dock.visibilityChanged.connect(self.action.setChecked)
            self.iface.addDockWidget(
                Qt.DockWidgetArea.LeftDockWidgetArea,
                self.dock,
            )
            # Tab it together with the Browser panel — the standard QGIS
            # panel that sits above Layers — instead of stacking it as its
            # own separate row, so it opens exactly where a user expects to
            # find it. Falls back to the plain stacked placement above if a
            # differently configured QGIS has no "Browser" dock widget.
            main_window = self.iface.mainWindow()
            browser = main_window.findChild(QDockWidget, "Browser")
            if browser is not None and browser is not self.dock:
                main_window.tabifyDockWidget(browser, self.dock)
            self.dock.setVisible(True)
            self.dock.raise_()
            return
        self.dock.setVisible(bool(visible))
        if visible:
            self.dock.raise_()

    def _show_about(self) -> None:
        QMessageBox.about(
            self.iface.mainWindow(),
            "02Agent OSM Downloader",
            (
                "<h3>02Agent OSM Downloader</h3>"
                "<p><b>AI-agent-ready OpenStreetMap acquisition for QGIS 4.</b></p>"
                "<p>Choose from 15 thematic groups and 30 curated urban-analysis "
                "presets, combine datasets, build a structured four-filter ANY/ALL "
                "query, resolve named administrative places, request "
                "a custom OSM tag, or use the English offline command "
                "router.</p>"
                "<p><b>Agent Protocol v1</b><br>"
                "Provider: <code>zero2agentosm</code><br>"
                "Endpoints: <code>download_preset</code>, "
                "<code>download_place</code>, "
                "<code>download_custom_tag</code> and "
                "<code>download_advanced</code></p>"
                "<p>02Agent Smart Modeler discovers these endpoints through the live "
                "QGIS Processing registry, validates their signatures, and runs "
                "them only after an explicit approval card.</p>"
                "<p>GPL-3.0-or-later &bull; Yusuf Eminoglu</p>"
            ),
        )
