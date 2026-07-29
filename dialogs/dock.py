"""Dock interface for curated and prompt-assisted OSM downloads."""
from __future__ import annotations

import contextlib
from typing import Dict, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
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
)

from ..core.catalog import (
    GROUPS,
    PRESETS,
    get_preset,
    interpret_prompt,
    presets_for_group,
)

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
    def __init__(self, iface, parent=None) -> None:
        super().__init__("02Agent OSM Downloader", parent)
        self.setObjectName("Zero2AgentOsmDownloaderDock")
        self.iface = iface
        self._task = None
        self._feedback = None
        self._context = None
        self._current_label = ""
        self._build_ui()
        self._populate_groups()

    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)

        title = QLabel("02Agent OSM Downloader")
        title.setStyleSheet("font-size:18px;font-weight:700;color:#17324D;")
        subtitle = QLabel(
            "Curated OSM datasets · one request · temporary QGIS layers"
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color:#5B6B79;")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.agent_endpoint = QLabel(
            "Agent Protocol v1 · zero2agentosm endpoint ready"
        )
        self.agent_endpoint.setWordWrap(True)
        self.agent_endpoint.setToolTip(
            "SmartModeler and compatible agents discover the two bounded "
            "download endpoints through the live QGIS Processing registry."
        )
        self.agent_endpoint.setStyleSheet(
            "background:#E8F5F2;color:#12645A;padding:6px;"
            "border:1px solid #A8D8CF;border-radius:4px;font-weight:600;"
        )
        layout.addWidget(self.agent_endpoint)

        prompt_group = QGroupBox("Agent command")
        prompt_layout = QVBoxLayout(prompt_group)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "Example: download public transport data for the active layer\n"
            "or: building=* polygon"
        )
        self.prompt.setMaximumHeight(76)
        self.interpret_button = QPushButton("Interpret command")
        self.interpret_button.clicked.connect(self._interpret)
        prompt_layout.addWidget(self.prompt)
        prompt_layout.addWidget(self.interpret_button)
        layout.addWidget(prompt_group)

        preset_group = QGroupBox("Curated thematic preset")
        preset_form = QFormLayout(preset_group)
        self.group_combo = QComboBox()
        self.group_combo.currentIndexChanged.connect(self._group_changed)
        self.preset_combo = QComboBox()
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setStyleSheet("color:#506070;")
        preset_form.addRow("Group", self.group_combo)
        preset_form.addRow("Preset", self.preset_combo)
        preset_form.addRow(self.description)
        layout.addWidget(preset_group)

        custom_group = QGroupBox("Custom OSM tag")
        custom_form = QFormLayout(custom_group)
        self.custom_check = QCheckBox("Use custom tag instead of preset")
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("building")
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("* (any value)")
        self.geometry_combo = QComboBox()
        self.geometry_combo.addItems(["Point", "Line", "Polygon"])
        custom_form.addRow(self.custom_check)
        custom_form.addRow("Key", self.key_edit)
        custom_form.addRow("Value", self.value_edit)
        custom_form.addRow("Geometry", self.geometry_combo)
        layout.addWidget(custom_group)

        run_group = QGroupBox("Download")
        run_form = QFormLayout(run_group)
        self.extent_combo = QComboBox()
        self.extent_combo.addItem("Current map view", "map")
        self.extent_combo.addItem("Active layer extent", "active")
        run_form.addRow("Extent", self.extent_combo)
        buttons = QHBoxLayout()
        self.download_button = QPushButton("Download temporary layers")
        self.download_button.setStyleSheet(
            "QPushButton{background:#176B87;color:white;font-weight:600;"
            "padding:8px;border-radius:4px;}"
            "QPushButton:disabled{background:#9AA7B0;}"
        )
        self.download_button.clicked.connect(self._download)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.cancel_button)
        run_form.addRow(buttons)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        run_form.addRow(self.progress)
        layout.addWidget(run_group)

        self.status = QLabel("Ready.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(
            "background:#EEF4F7;color:#284B5B;padding:7px;border-radius:4px;"
        )
        layout.addWidget(self.status)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(root)
        self.setWidget(scroll)
        self.setMinimumWidth(360)

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
            self.status.setText(
                f"Matched preset: {preset.processing_label} "
                f"({intent.confidence:.0%}). Review and download."
            )
            return
        if intent.mode == "custom":
            self.custom_check.setChecked(True)
            self.key_edit.setText(intent.key)
            self.value_edit.setText(intent.value or "*")
            geometry_index = ("point", "line", "polygon").index(
                intent.geometry
            )
            self.geometry_combo.setCurrentIndex(geometry_index)
            self.status.setText(
                "Matched a custom OSM tag. Review and download."
            )
            return
        self.status.setText(
            "No confident preset match. Choose a preset or enter key=value."
        )

    def _extent(self) -> Optional[QgsReferencedRectangle]:
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
                QMessageBox.warning(self, "02Agent OSM Downloader", "Enter an OSM key.")
                return
            parameters = {
                "KEY": key,
                "VALUE": self.value_edit.text().strip(),
                "GEOMETRY": self.geometry_combo.currentIndex(),
            }
            self._current_label = f"{key}={parameters['VALUE'] or '*'}"
            group_id = "custom"
        else:
            algorithm_id = "zero2agentosm:download_preset"
            preset_id = str(self.preset_combo.currentData() or "")
            preset_index = next(
                (
                    index for index, preset in enumerate(PRESETS)
                    if preset.preset_id == preset_id
                ),
                -1,
            )
            if preset_index < 0:
                return
            preset = PRESETS[preset_index]
            parameters = {"PRESET": preset_index}
            self._current_label = preset.title
            group_id = preset.group_id
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
        feedback.progressChanged.connect(self.progress.setValue)
        task = QgsProcessingAlgRunnerTask(
            algorithm, parameters, context, feedback
        )
        task.executed.connect(
            lambda successful, results: self._finished(
                successful, results, group_id
            )
        )
        self._context = context
        self._feedback = feedback
        self._task = task
        self.download_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText(f"Downloading {self._current_label} ...")
        QgsApplication.taskManager().addTask(task)

    def _finished(self, successful: bool, results: dict, group_id: str) -> None:
        context = self._context
        added = []
        if successful and context is not None:
            project = QgsProject.instance()
            group = project.layerTreeRoot().addGroup(
                f"02Agent — {self._current_label}"
            )
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
                self._style_layer(layer, group_id)
                project.addMapLayer(layer, False)
                group.addLayer(layer)
                added.append(f"{suffix}: {layer.featureCount():,}")
            if not added:
                project.layerTreeRoot().removeChildNode(group)
        self._task = None
        self._feedback = None
        self._context = None
        self.download_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress.setValue(100 if successful else 0)
        if successful:
            detail = ", ".join(added) if added else "no matching features"
            self.status.setText(f"Finished — {detail}.")
        else:
            self.status.setText("Download failed or was canceled. Check Processing log.")

    @staticmethod
    def _style_layer(layer: QgsVectorLayer, group_id: str) -> None:
        color = QColor(_GROUP_COLORS.get(group_id, "#176B87"))
        renderer = layer.renderer()
        symbol = renderer.symbol() if renderer is not None else None
        if symbol is None:
            return
        symbol.setColor(color)
        if layer.geometryType().name == "Polygon":
            symbol.setOpacity(0.58)
        elif layer.geometryType().name == "Line":
            with contextlib.suppress(AttributeError):
                symbol.setWidth(0.8)
        layer.triggerRepaint()

    def cancel(self) -> None:
        if self._feedback is not None:
            self._feedback.cancel()
        if self._task is not None:
            with contextlib.suppress(Exception):
                self._task.cancel()
