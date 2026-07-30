"""Compact, theme-aware dock for curated and command-assisted OSM downloads."""
from __future__ import annotations

import contextlib
from typing import Dict, Optional

from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsApplication,
    QgsProcessingAlgRunnerTask,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsReferencedRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)

from ..core.catalog import (
    GROUPS,
    PRESETS,
    get_preset,
    interpret_prompt,
    presets_for_group,
)
from ..core.smartmodeler_bridge import (
    connection_info,
    open_connections,
    open_workspace,
)
from .theme import apply_adaptive_theme

_GROUP_COLORS: Dict[str, str] = {
    "network": "#D95F02",
    "morphology": "#6A3D9A",
    "green_blue": "#1B9E77",
    "public_transport": "#1F78B4",
    "religious": "#8C510A",
    "tourism": "#E7298A",
    "sport": "#33A02C",
    "bike": "#00A6A6",
    "car": "#666666",
    "traffic": "#E31A1C",
    "health": "#D73027",
    "education": "#4575B4",
    "emergency": "#B2182B",
}


class AgentOsmDock(QDockWidget):
    """Primary downloader UI with no browser or plugin-UI automation."""

    def __init__(self, iface, parent=None) -> None:
        super().__init__("02Agent OSM Downloader", parent)
        self.setObjectName("Zero2AgentOsmDownloaderDock")
        self.iface = iface
        self._task = None
        self._feedback = None
        self._context = None
        self._current_label = ""
        self._current_group_id = ""
        self._theme_refreshing = False
        self._build_ui()
        self._populate_groups()

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("agentOsmRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(9, 9, 9, 9)
        layout.setSpacing(8)

        hero = QFrame()
        hero.setObjectName("heroPanel")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(12, 10, 12, 10)
        hero_layout.setSpacing(2)
        eyebrow = QLabel("OPENSTREETMAP ACQUISITION")
        eyebrow.setObjectName("heroEyebrow")
        title = QLabel("02Agent")
        title.setObjectName("heroTitle")
        subtitle = QLabel("Bounded downloads for QGIS workflows")
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        hero_layout.addWidget(eyebrow)
        hero_layout.addWidget(title)
        hero_layout.addWidget(subtitle)
        layout.addWidget(hero)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.download_tab = self._build_download_tab()
        self.command_tab = self._build_command_tab()
        self.connections_tab = self._build_connections_tab()
        self.tabs.addTab(self.download_tab, "Download")
        self.tabs.addTab(self.command_tab, "Command")
        self.tabs.addTab(self.connections_tab, "Connections")
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs, 1)

        run_group = QGroupBox("Download")
        run_form = QFormLayout(run_group)
        run_form.setContentsMargins(9, 8, 9, 9)
        self.extent_combo = QComboBox()
        self.extent_combo.addItem("Current map view", "map")
        self.extent_combo.addItem("Active layer extent", "active")
        self.extent_combo.setToolTip(
            "Requests are transformed to WGS84 and limited to 100 km²."
        )
        run_form.addRow("Extent", self.extent_combo)

        buttons = QHBoxLayout()
        self.download_button = QPushButton("Download")
        self.download_button.setObjectName("primaryButton")
        self.download_button.setToolTip(
            "Create temporary point, line, and polygon layers."
        )
        self.download_button.clicked.connect(self._download)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        buttons.addWidget(self.download_button, 1)
        buttons.addWidget(self.cancel_button)
        run_form.addRow(buttons)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        run_form.addRow(self.progress)
        layout.addWidget(run_group)

        self.status = QLabel("Ready.")
        self.status.setObjectName("statusCard")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.status)

        self.setWidget(root)
        self.setMinimumWidth(350)
        self._apply_theme()
        self._refresh_agent_connection()

    def _build_download_tab(self) -> QWidget:
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(9, 9, 9, 9)
        outer.setSpacing(8)

        preset_group = QGroupBox("Curated preset")
        preset_form = QFormLayout(preset_group)
        self.group_combo = QComboBox()
        self.group_combo.setToolTip("Choose an urban-analysis theme.")
        self.group_combo.currentIndexChanged.connect(self._group_changed)
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Choose a bounded set of OSM tags.")
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        self.description = QLabel()
        self.description.setObjectName("descriptionText")
        self.description.setWordWrap(True)
        self.description.setMinimumHeight(42)
        preset_form.addRow("Theme", self.group_combo)
        preset_form.addRow("Dataset", self.preset_combo)
        preset_form.addRow(self.description)
        outer.addWidget(preset_group)

        custom_group = QGroupBox("Custom tag")
        custom_form = QFormLayout(custom_group)
        self.custom_check = QCheckBox("Use a custom OSM key/value")
        self.custom_check.toggled.connect(self._sync_custom_fields)
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("building")
        self.key_edit.setClearButtonEnabled(True)
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("*  (any value)")
        self.value_edit.setClearButtonEnabled(True)
        self.geometry_combo = QComboBox()
        self.geometry_combo.addItems(["Point", "Line", "Polygon"])
        custom_form.addRow(self.custom_check)
        custom_form.addRow("Key", self.key_edit)
        custom_form.addRow("Value", self.value_edit)
        custom_form.addRow("Geometry", self.geometry_combo)
        outer.addWidget(custom_group)
        outer.addStretch(1)
        self._sync_custom_fields(False)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _build_command_tab(self) -> QWidget:
        tab = QWidget()
        prompt_layout = QVBoxLayout(tab)
        prompt_layout.setContentsMargins(10, 10, 10, 10)
        prompt_layout.setSpacing(8)
        heading = QLabel("OFFLINE COMMAND ROUTER")
        heading.setObjectName("heroEyebrow")
        prompt_layout.addWidget(heading)
        hint = QLabel(
            "Describe a preset in English or Turkish, or enter a safe "
            "key=value tag. The command is interpreted locally."
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        prompt_layout.addWidget(hint)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "Example: download public transport for the active layer\n"
            "Example: building=* polygon"
        )
        self.prompt.setMinimumHeight(115)
        self.prompt.setMaximumHeight(180)
        prompt_layout.addWidget(self.prompt)
        self.interpret_button = QPushButton("Interpret command")
        self.interpret_button.setObjectName("primaryButton")
        self.interpret_button.clicked.connect(self._interpret)
        prompt_layout.addWidget(self.interpret_button)
        privacy = QLabel("No command text or project data leaves QGIS.")
        privacy.setObjectName("mutedText")
        privacy.setWordWrap(True)
        prompt_layout.addWidget(privacy)
        prompt_layout.addStretch(1)
        return tab

    def _build_connections_tab(self) -> QWidget:
        tab = QWidget()
        agent_layout = QVBoxLayout(tab)
        agent_layout.setContentsMargins(10, 10, 10, 10)
        agent_layout.setSpacing(9)
        self.agent_endpoint = QLabel(
            "Agent Protocol v1  ·  zero2agentosm ready"
        )
        self.agent_endpoint.setObjectName("endpointPill")
        self.agent_endpoint.setWordWrap(True)
        agent_layout.addWidget(self.agent_endpoint)

        intro = QLabel(
            "SmartModeler discovers the two bounded Processing endpoints "
            "through QGIS. Downloads still require explicit approval."
        )
        intro.setObjectName("mutedText")
        intro.setWordWrap(True)
        agent_layout.addWidget(intro)

        self.agent_connection = QLabel()
        self.agent_connection.setObjectName("connectionDetail")
        self.agent_connection.setWordWrap(True)
        self.agent_connection.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        agent_layout.addWidget(self.agent_connection)

        self.ai_connections_button = QPushButton("AI Connections")
        self.ai_connections_button.setObjectName("primaryButton")
        self.ai_connections_button.setToolTip(
            "Open SmartModeler's shared, secret-safe provider profiles."
        )
        self.ai_connections_button.clicked.connect(
            self._open_ai_connections
        )
        self.agent_workspace_button = QPushButton("Agent Workspace")
        self.agent_workspace_button.setToolTip(
            "Open SmartModeler's supervised agent dock."
        )
        self.agent_workspace_button.clicked.connect(
            self._open_agent_workspace
        )
        agent_layout.addWidget(self.ai_connections_button)
        agent_layout.addWidget(self.agent_workspace_button)

        security = QLabel(
            "API keys stay in SmartModeler's process memory or the encrypted "
            "QGIS Authentication Database. This plugin never reads them."
        )
        security.setObjectName("mutedText")
        security.setWordWrap(True)
        agent_layout.addWidget(security)
        agent_layout.addStretch(1)
        return tab

    def _populate_groups(self) -> None:
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        for group_id, title in GROUPS:
            self.group_combo.addItem(title, group_id)
        self.group_combo.blockSignals(False)
        self._group_changed()

    def _group_changed(self) -> None:
        group_id = self.group_combo.currentData()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for preset in presets_for_group(str(group_id or "")):
            self.preset_combo.addItem(preset.title, preset.preset_id)
        self.preset_combo.blockSignals(False)
        self._preset_changed()

    def _preset_changed(self) -> None:
        preset_id = str(self.preset_combo.currentData() or "")
        if not preset_id:
            self.description.clear()
            return
        self.description.setText(get_preset(preset_id).description)

    def _select_preset(self, preset_id: str) -> None:
        preset = get_preset(preset_id)
        group_index = self.group_combo.findData(preset.group_id)
        if group_index >= 0:
            self.group_combo.setCurrentIndex(group_index)
        preset_index = self.preset_combo.findData(preset_id)
        if preset_index >= 0:
            self.preset_combo.setCurrentIndex(preset_index)

    def _interpret(self) -> None:
        intent = interpret_prompt(self.prompt.toPlainText())
        if intent.mode == "preset":
            self.custom_check.setChecked(False)
            self._select_preset(intent.preset_id)
            preset = get_preset(intent.preset_id)
            self._set_status(
                f"Matched {preset.processing_label} "
                f"({intent.confidence:.0%}). Review and download."
            )
            self.tabs.setCurrentWidget(self.download_tab)
            return
        if intent.mode == "custom":
            self.custom_check.setChecked(True)
            self.key_edit.setText(intent.key)
            self.value_edit.setText(intent.value or "*")
            geometry_index = ("point", "line", "polygon").index(
                intent.geometry
            )
            self.geometry_combo.setCurrentIndex(geometry_index)
            self._set_status(
                "Matched a custom OSM tag. Review and download."
            )
            self.tabs.setCurrentWidget(self.download_tab)
            return
        self._set_status(
            "No confident match. Choose a preset or enter key=value."
        )

    def _refresh_agent_connection(self) -> None:
        info = connection_info()
        ready = bool(info.get("available"))
        self.ai_connections_button.setEnabled(True)
        self.agent_workspace_button.setEnabled(ready)
        if not ready:
            error = str(info.get("error") or "").strip()
            self.agent_connection.setText(
                error
                or (
                    "SmartModeler GIS is not loaded. Open Plugins > Manage and "
                    "Install Plugins, search for “SmartModeler GIS”, install "
                    "and enable it, then reopen this tab. The offline command "
                    "router remains available without it."
                )
            )
            return
        profile = str(info.get("profile_name") or "Unnamed")
        provider = str(info.get("provider_name") or "Unknown provider")
        model = str(info.get("model") or "").strip()
        detail = f"SmartModeler profile: {profile}  ·  {provider}"
        if model:
            detail = f"{detail}  ·  {model}"
        if not info.get("agent_chat_enabled"):
            detail = f"{detail}  ·  connected chat is Offline"
        self.agent_connection.setText(detail)

    def _open_ai_connections(self) -> None:
        result = open_connections(self)
        if not result.ok:
            QMessageBox.information(self, "AI Connections", result.message)
        self._refresh_agent_connection()

    def _open_agent_workspace(self) -> None:
        result = open_workspace()
        if not result.ok:
            QMessageBox.information(self, "Agent Workspace", result.message)

    def _tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.connections_tab:
            self._refresh_agent_connection()

    def _sync_custom_fields(self, checked: bool) -> None:
        self.group_combo.setEnabled(not checked)
        self.preset_combo.setEnabled(not checked)
        self.key_edit.setEnabled(checked)
        self.value_edit.setEnabled(checked)
        self.geometry_combo.setEnabled(checked)

    def _extent(self) -> Optional[QgsReferencedRectangle]:
        if self.iface is None:
            return None
        if self.extent_combo.currentData() == "active":
            layer = self.iface.activeLayer()
            if layer is None:
                return None
            return QgsReferencedRectangle(layer.extent(), layer.crs())
        canvas = self.iface.mapCanvas()
        return QgsReferencedRectangle(
            canvas.extent(),
            canvas.mapSettings().destinationCrs(),
        )

    def _download(self) -> None:
        if self._task is not None:
            return
        extent = self._extent()
        if extent is None:
            QMessageBox.warning(
                self,
                "02Agent OSM Downloader",
                "Select an active layer or use the current map view.",
            )
            return
        if self.custom_check.isChecked():
            algorithm_id = "zero2agentosm:download_custom_tag"
            key = self.key_edit.text().strip()
            if not key:
                QMessageBox.warning(
                    self,
                    "02Agent OSM Downloader",
                    "Enter an OSM key.",
                )
                self.key_edit.setFocus()
                return
            parameters = {
                "KEY": key,
                "VALUE": self.value_edit.text().strip(),
                "GEOMETRY": self.geometry_combo.currentIndex(),
            }
            self._current_label = f"{key}={parameters['VALUE'] or '*'}"
            self._current_group_id = "custom"
        else:
            algorithm_id = "zero2agentosm:download_preset"
            preset_id = str(self.preset_combo.currentData() or "")
            preset_index = next(
                (
                    index
                    for index, preset in enumerate(PRESETS)
                    if preset.preset_id == preset_id
                ),
                -1,
            )
            if preset_index < 0:
                self._set_status("Choose a valid preset.")
                return
            preset = PRESETS[preset_index]
            parameters = {"PRESET": preset_index}
            self._current_label = preset.title
            self._current_group_id = preset.group_id
        parameters.update(
            {
                "EXTENT": extent,
                "OUTPUT_POINTS": "TEMPORARY_OUTPUT",
                "OUTPUT_LINES": "TEMPORARY_OUTPUT",
                "OUTPUT_POLYGONS": "TEMPORARY_OUTPUT",
            }
        )
        registry = QgsApplication.processingRegistry()
        algorithm = registry.createAlgorithmById(algorithm_id)
        if algorithm is None:
            QMessageBox.critical(
                self,
                "02Agent OSM Downloader",
                "The Processing provider is not available.",
            )
            return

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        context.setTransformContext(QgsProject.instance().transformContext())
        feedback = QgsProcessingFeedback()
        feedback.progressChanged.connect(self._set_progress)
        task = QgsProcessingAlgRunnerTask(
            algorithm,
            parameters,
            context,
            feedback,
        )
        task.executed.connect(self._finished)
        self._context = context
        self._feedback = feedback
        self._task = task
        self.download_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self._set_status(f"Downloading {self._current_label} …")
        if not QgsApplication.taskManager().addTask(task):
            self._reset_task_state()
            QMessageBox.critical(
                self,
                "02Agent OSM Downloader",
                "QGIS could not start the download task.",
            )

    def _set_progress(self, value: float) -> None:
        """QGIS emits float progress while QProgressBar accepts only int."""
        self.progress.setValue(max(0, min(100, int(round(value)))))

    def _finished(self, successful: bool, results: dict) -> None:
        context = self._context
        added = []
        try:
            if successful and context is not None:
                added = self._add_result_layers(
                    results if isinstance(results, dict) else {},
                    context,
                )
        except Exception as error:  # noqa: BLE001 - async Qt callback boundary
            successful = False
            self._set_status(
                f"Results could not be added: {type(error).__name__}. "
                "Check the Processing log."
            )
        finally:
            self._reset_task_state()

        self.progress.setValue(100 if successful else 0)
        if successful:
            detail = ", ".join(added) if added else "no matching features"
            self._set_status(f"Finished — {detail}.")
        elif not self.status.text().startswith("Results could not"):
            self._set_status(
                "Download failed or was canceled. Check the Processing log."
            )

    def _add_result_layers(
        self,
        results: dict,
        context: QgsProcessingContext,
    ) -> list[str]:
        project = QgsProject.instance()
        group = project.layerTreeRoot().addGroup(
            f"02Agent — {self._current_label}"
        )
        added = []
        names = {
            "OUTPUT_POINTS": "Points",
            "OUTPUT_LINES": "Lines",
            "OUTPUT_POLYGONS": "Polygons",
        }
        for output, suffix in names.items():
            value = results.get(output)
            layer = context.takeResultLayer(value) if value else None
            if not isinstance(layer, QgsVectorLayer):
                continue
            if layer.featureCount() <= 0:
                layer.deleteLater()
                continue
            layer.setName(f"{self._current_label} — {suffix}")
            self._style_layer(layer, self._current_group_id)
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            added.append(f"{suffix}: {layer.featureCount():,}")
        if not added:
            project.layerTreeRoot().removeChildNode(group)
        return added

    @staticmethod
    def _style_layer(layer: QgsVectorLayer, group_id: str) -> None:
        color = QColor(_GROUP_COLORS.get(group_id, "#2F7D5B"))
        renderer = layer.renderer()
        symbol = renderer.symbol() if renderer is not None else None
        if symbol is None:
            return
        symbol.setColor(color)
        geometry_type = layer.geometryType()
        if geometry_type == QgsWkbTypes.GeometryType.PolygonGeometry:
            symbol.setOpacity(0.58)
        elif geometry_type == QgsWkbTypes.GeometryType.LineGeometry:
            with contextlib.suppress(AttributeError):
                symbol.setWidth(0.8)
        layer.triggerRepaint()

    def _reset_task_state(self) -> None:
        self._task = None
        self._feedback = None
        self._context = None
        self.download_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def _set_status(self, text: str) -> None:
        self.status.setText(str(text))

    def cancel(self) -> None:
        if self._feedback is not None:
            self._feedback.cancel()
        if self._task is not None:
            with contextlib.suppress(Exception):
                self._task.cancel()
            self._set_status("Canceling download …")

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
        ):
            self._apply_theme()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_agent_connection()

    def _apply_theme(self) -> None:
        root = self.widget()
        if root is None or getattr(self, "_theme_refreshing", False):
            return
        self._theme_refreshing = True
        try:
            apply_adaptive_theme(root)
        finally:
            self._theme_refreshing = False
