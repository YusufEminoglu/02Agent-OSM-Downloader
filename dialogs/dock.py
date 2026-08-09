"""Compact, theme-aware dock for curated and command-assisted OSM downloads."""
from __future__ import annotations

import contextlib
from typing import Optional

from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDockWidget,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
    QgsSettings,
    QgsVectorLayer,
)

from ..core.catalog import (
    GROUPS,
    PRESETS,
    group_context,
    get_preset,
    interpret_prompt,
    presets_for_group,
)
from ..core.basemap import add_osm_basemap
from ..core.query import (
    MAX_ADVANCED_FILTERS,
    QueryError,
    advanced_specs,
    normalize_tag,
    normalized_specs,
    preview_query,
)
from ..core.map_styling import apply_map_theme
from ..core.map_styling import ROAD_WIDTH_MODES
from ..core.map_themes import (
    DEFAULT_MAP_THEME,
    map_theme,
    map_theme_items,
    map_theme_swatches,
)
from ..core.smartmodeler_bridge import (
    connection_info,
    open_connections,
    open_workspace,
)
from .theme import apply_adaptive_theme

_MAP_THEME_SETTING = "zero2agent_osm_downloader/map_theme"
_ROAD_WIDTH_SETTING = "zero2agent_osm_downloader/road_width_mode"

_ADVANCED_EXAMPLES = (
    (
        "Schools with wheelchair access",
        "all",
        "polygon",
        (("amenity", "school"), ("wheelchair", "yes")),
    ),
    (
        "Protected or dedicated cycleways",
        "any",
        "line",
        (("highway", "cycleway"), ("cycleway", "track")),
    ),
    (
        "Public green spaces",
        "any",
        "polygon",
        (("leisure", "park"), ("leisure", "garden"), ("landuse", "grass")),
    ),
    (
        "Named public transport stops",
        "all",
        "point",
        (("public_transport", "platform"), ("name", "")),
    ),
    (
        "Hospitals with emergency access", "all", "point",
        (("amenity", "hospital"), ("emergency", "yes")),
    ),
    (
        "Libraries and community centres", "any", "point",
        (("amenity", "library"), ("amenity", "community_centre")),
    ),
    (
        "Accessible public toilets", "all", "point",
        (("amenity", "toilets"), ("wheelchair", "yes")),
    ),
    (
        "Restaurants with outdoor seating", "all", "point",
        (("amenity", "restaurant"), ("outdoor_seating", "yes")),
    ),
    (
        "Electric vehicle charging", "any", "point",
        (("amenity", "charging_station"), ("amenity", "fuel")),
    ),
    (
        "Historic places and monuments", "any", "all",
        (("historic", "monument"), ("historic", "memorial"), ("tourism", "museum")),
    ),
    (
        "Pedestrian-friendly streets", "any", "line",
        (("highway", "pedestrian"), ("highway", "living_street"), ("highway", "footway")),
    ),
    (
        "Parks with playgrounds", "all", "polygon",
        (("leisure", "park"), ("leisure", "playground")),
    ),
    (
        "Water and waterways", "any", "all",
        (("natural", "water"), ("waterway", ""), ("landuse", "reservoir")),
    ),
    (
        "Buildings with height data", "all", "polygon",
        (("building", ""), ("height", "")),
    ),
)

_QUERY_KEY_SUGGESTIONS = (
    "amenity", "building", "boundary", "highway", "historic", "landuse",
    "leisure", "natural", "name", "place", "public_transport", "railway",
    "shop", "tourism", "wheelchair", "waterway", "wikidata",
)
_QUERY_VALUE_SUGGESTIONS = (
    "*", "yes", "no", "school", "hospital", "park", "restaurant", "library",
    "residential", "pedestrian", "administrative", "tree", "water",
)


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
        self._place_name = ""
        self._theme_refreshing = False
        self._build_ui()
        self._populate_groups()

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("agentOsmRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 9)
        layout.setSpacing(7)

        hero = QFrame()
        hero.setObjectName("heroPanel")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(12, 7, 12, 7)
        hero_layout.setSpacing(4)

        hero_top = QHBoxLayout()
        hero_top.setSpacing(9)
        hero_mark = QLabel("02")
        hero_mark.setObjectName("heroMark")
        hero_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_mark.setFixedSize(37, 37)
        hero_top.addWidget(hero_mark)

        hero_copy = QVBoxLayout()
        hero_copy.setContentsMargins(0, 0, 0, 0)
        hero_copy.setSpacing(0)
        eyebrow = QLabel("OPENSTREETMAP ACQUISITION")
        eyebrow.setObjectName("heroEyebrow")
        title = QLabel("02Agent")
        title.setObjectName("heroTitle")
        subtitle = QLabel("Bounded OSM downloads for QGIS workflows")
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        hero_copy.addWidget(eyebrow)
        hero_copy.addWidget(title)
        hero_copy.addWidget(subtitle)
        hero_top.addLayout(hero_copy, 1)

        hero_badge = QLabel("OSM\nREADY")
        hero_badge.setObjectName("heroBadge")
        hero_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_badge.setToolTip("OpenStreetMap acquisition tools")
        hero_top.addWidget(hero_badge)
        hero_layout.addLayout(hero_top)

        workflow_hint = QLabel("Choose a workflow, review the request, then download.")
        workflow_hint.setObjectName("heroHint")
        workflow_hint.setWordWrap(True)
        hero_layout.addWidget(workflow_hint)
        layout.addWidget(hero)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.download_tab = self._build_download_tab()
        self.query_tab = self._build_query_tab()
        self.command_tab = self._build_command_tab()
        self.connections_tab = self._build_connections_tab()
        self.tabs.addTab(self.download_tab, "Presets")
        self.tabs.addTab(self.query_tab, "Query")
        self.tabs.addTab(self.command_tab, "Command")
        self.tabs.addTab(self.connections_tab, "Agent")
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs, 1)

        self.run_group = QGroupBox("Run download")
        run_form = QFormLayout(self.run_group)
        run_form.setContentsMargins(9, 8, 9, 9)
        self.run_summary = QLabel()
        self.run_summary.setObjectName("runSummary")
        self.run_summary.setWordWrap(True)
        run_form.addRow(self.run_summary)
        self.extent_combo = QComboBox()
        self.extent_combo.addItem("Current map view", "map")
        self.extent_combo.addItem("Active layer extent", "active")
        self.extent_combo.setToolTip(
            "Requests are transformed to WGS84 and limited to 100 km²."
        )
        run_form.addRow("Extent", self.extent_combo)

        self.basemap_button = QPushButton("Add OSM basemap")
        self.basemap_button.setToolTip(
            "Add the standard OpenStreetMap XYZ basemap to the current project."
        )
        self.basemap_button.clicked.connect(self._add_basemap)
        run_form.addRow(self.basemap_button)

        self.map_theme_combo = QComboBox()
        self.map_theme_combo.setAccessibleName("Downloaded layer map style")
        for theme_id, label in map_theme_items():
            self.map_theme_combo.addItem(label, theme_id)
        stored_theme = str(
            QgsSettings().value(_MAP_THEME_SETTING, DEFAULT_MAP_THEME)
            or DEFAULT_MAP_THEME
        )
        stored_index = self.map_theme_combo.findData(stored_theme)
        self.map_theme_combo.setCurrentIndex(max(0, stored_index))
        self.map_theme_combo.currentIndexChanged.connect(
            self._map_theme_changed
        )
        run_form.addRow("Map style", self.map_theme_combo)

        self.road_width_combo = QComboBox()
        self.road_width_combo.addItem(
            "By OSM highway category (recommended)", "highway"
        )
        self.road_width_combo.addItem("Uniform road width", "uniform")
        stored_width = str(
            QgsSettings().value(_ROAD_WIDTH_SETTING, "highway") or "highway"
        )
        stored_width_index = self.road_width_combo.findData(stored_width)
        self.road_width_combo.setCurrentIndex(max(0, stored_width_index))
        self.road_width_combo.currentIndexChanged.connect(
            self._road_width_changed
        )
        self.road_width_combo.setToolTip(
            "Use OSM highway classes such as motorway, primary, residential "
            "and service to assign line widths."
        )
        run_form.addRow("Road width", self.road_width_combo)

        map_theme_preview = QWidget()
        map_theme_preview.setObjectName("mapThemePreview")
        preview_layout = QHBoxLayout(map_theme_preview)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(3)
        self.map_theme_swatches = []
        for _index in range(6):
            swatch = QFrame()
            swatch.setFixedHeight(13)
            swatch.setMinimumWidth(22)
            preview_layout.addWidget(swatch, 1)
            self.map_theme_swatches.append(swatch)
        self.map_theme_caption = QLabel()
        self.map_theme_caption.setObjectName("mutedText")
        self.map_theme_caption.setWordWrap(True)
        theme_preview_column = QVBoxLayout()
        theme_preview_column.setContentsMargins(0, 0, 0, 0)
        theme_preview_column.setSpacing(3)
        theme_preview_column.addWidget(map_theme_preview)
        theme_preview_column.addWidget(self.map_theme_caption)
        run_form.addRow(theme_preview_column)
        self._map_theme_changed()

        limits = QLabel("Temporary layers  •  maximum request area: 100 km²")
        limits.setObjectName("mutedText")
        limits.setWordWrap(True)
        run_form.addRow(limits)

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
        layout.addWidget(self.run_group)

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
        self._tab_changed(self.tabs.currentIndex())

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
        self.dataset_list = QListWidget()
        self.dataset_list.setObjectName("datasetList")
        self.dataset_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection
        )
        self.dataset_list.setMaximumHeight(132)
        self.dataset_list.setToolTip(
            "Select one or more datasets from this theme. They run as one bounded request."
        )
        self.dataset_list.itemChanged.connect(self._dataset_changed)
        dataset_actions = QHBoxLayout()
        dataset_actions.setSpacing(5)
        self.dataset_count = QLabel("0 selected")
        self.dataset_count.setObjectName("countPill")
        select_all = QPushButton("Select all")
        select_all.setObjectName("quietButton")
        select_all.clicked.connect(self._select_all_datasets)
        clear_datasets = QPushButton("Clear")
        clear_datasets.setObjectName("quietButton")
        clear_datasets.clicked.connect(self._clear_datasets)
        dataset_actions.addWidget(self.dataset_count)
        dataset_actions.addStretch(1)
        dataset_actions.addWidget(select_all)
        dataset_actions.addWidget(clear_datasets)
        self.theme_context = QLabel()
        self.theme_context.setObjectName("mutedText")
        self.theme_context.setWordWrap(True)
        self.theme_context.setMinimumHeight(42)
        self.description = QLabel()
        self.description.setObjectName("descriptionText")
        self.description.setWordWrap(True)
        self.description.setMinimumHeight(42)
        self.description.setMaximumHeight(64)
        preset_form.addRow("Theme", self.group_combo)
        preset_form.addRow("Dataset (multi-select)", self.dataset_list)
        preset_form.addRow(dataset_actions)
        preset_form.addRow(self.theme_context)
        preset_form.addRow(self.description)
        outer.addWidget(preset_group)

        custom_group = QGroupBox("Custom tag")
        custom_form = QFormLayout(custom_group)
        self.custom_check = QCheckBox("Use a custom OSM key/value")
        self.custom_check.toggled.connect(self._sync_custom_fields)
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("building")
        self.key_edit.setClearButtonEnabled(True)
        self.key_edit.textChanged.connect(self._refresh_run_summary)
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("*  (any value)")
        self.value_edit.setClearButtonEnabled(True)
        self.value_edit.textChanged.connect(self._refresh_run_summary)
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

    def _build_query_tab(self) -> QWidget:
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(9, 9, 9, 9)
        outer.setSpacing(8)

        heading = QLabel("STRUCTURED ADVANCED QUERY")
        heading.setObjectName("heroEyebrow")
        outer.addWidget(heading)
        hint = QLabel(
            "Combine up to four validated OSM tags. The generated Overpass "
            "query is read-only; the selected map or layer extent is inserted "
            "only when the download starts."
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        example_row = QHBoxLayout()
        self.query_example_combo = QComboBox()
        for title, mode, geometry, filters in _ADVANCED_EXAMPLES:
            self.query_example_combo.addItem(
                title,
                {"mode": mode, "geometry": geometry, "filters": filters},
            )
        self.query_example_button = QPushButton("Load example")
        self.query_example_button.clicked.connect(self._load_query_example)
        example_row.addWidget(self.query_example_combo, 1)
        example_row.addWidget(self.query_example_button)
        outer.addLayout(example_row)

        options = QFormLayout()
        self.query_match_combo = QComboBox()
        self.query_match_combo.addItem("Match any tag (OR)", "any")
        self.query_match_combo.addItem("Match all tags (AND)", "all")
        self.query_geometry_combo = QComboBox()
        self.query_geometry_combo.addItem("All geometries", "all")
        self.query_geometry_combo.addItem("Points", "point")
        self.query_geometry_combo.addItem("Lines", "line")
        self.query_geometry_combo.addItem("Polygons", "polygon")
        options.addRow("Match", self.query_match_combo)
        options.addRow("Geometry", self.query_geometry_combo)
        outer.addLayout(options)

        tag_group = QGroupBox("Tag filters")
        tag_grid = QGridLayout(tag_group)
        tag_grid.addWidget(QLabel("OSM key"), 0, 0)
        tag_grid.addWidget(QLabel("Value (* = any)"), 0, 1)
        self.query_key_edits = []
        self.query_value_edits = []
        key_completer = QCompleter(list(_QUERY_KEY_SUGGESTIONS), self)
        key_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        value_completer = QCompleter(list(_QUERY_VALUE_SUGGESTIONS), self)
        value_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        for row in range(MAX_ADVANCED_FILTERS):
            key_edit = QLineEdit()
            key_edit.setPlaceholderText("amenity" if row == 0 else "optional")
            key_edit.setClearButtonEnabled(True)
            key_edit.setCompleter(key_completer)
            value_edit = QLineEdit()
            value_edit.setPlaceholderText("school or *")
            value_edit.setClearButtonEnabled(True)
            value_edit.setCompleter(value_completer)
            key_edit.textChanged.connect(self._update_query_preview)
            value_edit.textChanged.connect(self._update_query_preview)
            tag_grid.addWidget(key_edit, row + 1, 0)
            tag_grid.addWidget(value_edit, row + 1, 1)
            self.query_key_edits.append(key_edit)
            self.query_value_edits.append(value_edit)
        outer.addWidget(tag_group)

        filter_actions = QHBoxLayout()
        self.query_filter_status = QLabel("0 / 4 filters")
        self.query_filter_status.setObjectName("mutedText")
        clear_filters = QPushButton("Clear filters")
        clear_filters.setObjectName("quietButton")
        clear_filters.clicked.connect(self._clear_query_filters)
        filter_actions.addWidget(self.query_filter_status)
        filter_actions.addStretch(1)
        filter_actions.addWidget(clear_filters)
        outer.addLayout(filter_actions)

        self.query_match_combo.currentIndexChanged.connect(
            self._update_query_preview
        )
        self.query_geometry_combo.currentIndexChanged.connect(
            self._update_query_preview
        )
        preview_row = QHBoxLayout()
        self.query_preview_label = QLabel("Generated query")
        self.query_preview_button = QPushButton("Show preview")
        self.query_preview_button.setObjectName("quietButton")
        self.query_preview_button.setCheckable(True)
        self.query_preview_button.toggled.connect(self._toggle_query_preview)
        preview_row.addWidget(self.query_preview_label)
        preview_row.addStretch(1)
        preview_row.addWidget(self.query_preview_button)
        outer.addLayout(preview_row)
        self.query_preview = QPlainTextEdit()
        self.query_preview.setObjectName("queryPreview")
        self.query_preview.setReadOnly(True)
        self.query_preview.setMaximumHeight(145)
        self.query_preview.setPlaceholderText(
            "Add a tag filter to preview the bounded Overpass query."
        )
        self.query_preview.setAccessibleName("Generated Overpass query preview")
        outer.addWidget(self.query_preview)
        self.query_preview.setVisible(False)
        safety = QLabel(
            "Raw query editing, arbitrary endpoints and file outputs are disabled."
        )
        safety.setObjectName("mutedText")
        safety.setWordWrap(True)
        outer.addWidget(safety)
        outer.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        self._load_query_example()
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
            "Describe a preset in English, or enter a safe "
            "key=value tag. The command is interpreted locally."
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        prompt_layout.addWidget(hint)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "Example: download public transport in Tokyo\n"
            "Example: download building=* data as polygons"
        )
        self.prompt.setMinimumHeight(115)
        self.prompt.setMaximumHeight(180)
        prompt_layout.addWidget(self.prompt)
        examples_label = QLabel("Global command examples")
        examples_label.setObjectName("mutedText")
        prompt_layout.addWidget(examples_label)
        command_examples = QHBoxLayout()
        self.command_example_combo = QComboBox()
        for example in (
            "Download administrative places in Konak, Izmir, Turkey",
            "Download administrative places in Van, Turkey",
            "Download buildings in London, United Kingdom",
            "Download roads and buildings in Paris, France",
            "Download public transport in Tokyo, Japan",
            "Download parks in Nairobi, Kenya",
            "Download healthcare in Toronto, Canada",
            "Download schools in Melbourne, Australia",
            "Download cycle network in Copenhagen, Denmark",
            "Download green and blue infrastructure in Amsterdam, Netherlands",
            "Download buildings in New York City, United States",
            "Download administrative places in São Paulo, Brazil",
            "Download public transport in Singapore",
            "Download roads in Cape Town, South Africa",
            "building=* polygon",
            "Download wheelchair=yes points",
            "Download parks and playgrounds in Seoul, South Korea",
        ):
            self.command_example_combo.addItem(example)
        load_command = QPushButton("Load example")
        load_command.clicked.connect(
            lambda: self.prompt.setPlainText(self.command_example_combo.currentText())
        )
        command_examples.addWidget(self.command_example_combo, 1)
        command_examples.addWidget(load_command)
        prompt_layout.addLayout(command_examples)
        self.interpret_button = QPushButton("Interpret command")
        self.interpret_button.setObjectName("primaryButton")
        self.interpret_button.clicked.connect(self._interpret)
        prompt_layout.addWidget(self.interpret_button)
        place_row = QHBoxLayout()
        self.command_place_label = QLabel("Place: not selected")
        self.command_place_label.setObjectName("mutedText")
        self.command_place_label.setWordWrap(True)
        clear_place = QPushButton("Clear place")
        clear_place.setObjectName("quietButton")
        clear_place.clicked.connect(self._clear_place)
        place_row.addWidget(self.command_place_label, 1)
        place_row.addWidget(clear_place)
        prompt_layout.addLayout(place_row)
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
        self.dataset_list.blockSignals(True)
        self.dataset_list.clear()
        for preset in presets_for_group(str(group_id or "")):
            item = QListWidgetItem(preset.title)
            item.setData(Qt.ItemDataRole.UserRole, preset.preset_id)
            item.setToolTip(preset.description)
            item.setFlags(
                item.flags() | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(Qt.CheckState.Unchecked)
            self.dataset_list.addItem(item)
        focus, related = group_context(str(group_id or ""))
        self.theme_context.setText(
            f"Theme focus: {focus}\n{related}" if related
            else f"Theme focus: {focus}"
        )
        if self.dataset_list.count():
            self.dataset_list.item(0).setCheckState(Qt.CheckState.Checked)
        self.dataset_list.blockSignals(False)
        self._dataset_changed()

    def _select_all_datasets(self) -> None:
        self.dataset_list.blockSignals(True)
        for index in range(self.dataset_list.count()):
            self.dataset_list.item(index).setCheckState(
                Qt.CheckState.Checked
            )
        self.dataset_list.blockSignals(False)
        self._dataset_changed()

    def _clear_datasets(self) -> None:
        self.dataset_list.blockSignals(True)
        for index in range(self.dataset_list.count()):
            self.dataset_list.item(index).setCheckState(
                Qt.CheckState.Unchecked
            )
        self.dataset_list.blockSignals(False)
        self._dataset_changed()

    def _selected_preset_ids(self) -> tuple[str, ...]:
        return tuple(
            str(self.dataset_list.item(index).data(Qt.ItemDataRole.UserRole) or "")
            for index in range(self.dataset_list.count())
            if self.dataset_list.item(index).checkState() == Qt.CheckState.Checked
        )

    def _dataset_changed(self, *_args) -> None:
        preset_ids = self._selected_preset_ids()
        self.dataset_count.setText(
            f"{len(preset_ids)} of {self.dataset_list.count()} selected"
        )
        if not preset_ids:
            self.description.setText(
                "Select one or more datasets to build a combined request."
            )
            self._refresh_run_summary()
            return
        descriptions = [
            get_preset(preset_id).description for preset_id in preset_ids
        ]
        self.description.setText(" ".join(descriptions))
        self._refresh_run_summary()

    def _select_preset(self, preset_id: str) -> None:
        preset = get_preset(preset_id)
        group_index = self.group_combo.findData(preset.group_id)
        if group_index >= 0:
            self.group_combo.setCurrentIndex(group_index)
        for index in range(self.dataset_list.count()):
            item = self.dataset_list.item(index)
            checked = item.data(Qt.ItemDataRole.UserRole) == preset_id
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )

    def _clear_query_filters(self) -> None:
        for edit in (*self.query_key_edits, *self.query_value_edits):
            edit.clear()
        self._update_query_preview()

    def _query_filters(self) -> list[tuple[str, str]]:
        return [
            (key_edit.text().strip(), value_edit.text().strip())
            for key_edit, value_edit in zip(
                self.query_key_edits, self.query_value_edits
            )
            if key_edit.text().strip() or value_edit.text().strip()
        ]

    def _query_geometries(self) -> tuple[str, ...]:
        geometry = str(self.query_geometry_combo.currentData() or "all")
        return (
            ("point", "line", "polygon")
            if geometry == "all"
            else (geometry,)
        )

    def _load_query_example(self) -> None:
        data = self.query_example_combo.currentData()
        if not isinstance(data, dict):
            return
        for edit in (*self.query_key_edits, *self.query_value_edits):
            edit.blockSignals(True)
            edit.clear()
        filters = tuple(data.get("filters") or ())[:MAX_ADVANCED_FILTERS]
        for index, (key, value) in enumerate(filters):
            self.query_key_edits[index].setText(str(key))
            self.query_value_edits[index].setText(str(value or "*"))
        for edit in (*self.query_key_edits, *self.query_value_edits):
            edit.blockSignals(False)
        mode_index = self.query_match_combo.findData(data.get("mode"))
        geometry_index = self.query_geometry_combo.findData(data.get("geometry"))
        if mode_index >= 0:
            self.query_match_combo.setCurrentIndex(mode_index)
        if geometry_index >= 0:
            self.query_geometry_combo.setCurrentIndex(geometry_index)
        self._update_query_preview()

    def _update_query_preview(self, *_args) -> None:
        filters = self._query_filters()
        if hasattr(self, "query_filter_status"):
            self.query_filter_status.setText(
                f"{len(filters)} / {MAX_ADVANCED_FILTERS} filters"
            )
        if not filters:
            self.query_preview.clear()
            self._refresh_run_summary()
            return
        mode = str(self.query_match_combo.currentData() or "any")
        try:
            specs = advanced_specs(filters, self._query_geometries(), mode)
            self.query_preview.setPlainText(preview_query(specs, mode))
        except QueryError as error:
            self.query_preview.setPlainText(f"Invalid structured query: {error}")
        self._refresh_run_summary()

    def _toggle_query_preview(self, visible: bool) -> None:
        self.query_preview.setVisible(bool(visible))
        self.query_preview_button.setText(
            "Hide preview" if visible else "Show preview"
        )

    @staticmethod
    def _display_label(text: object, limit: int = 120) -> str:
        clean = " ".join(str(text or "").split())
        return clean if len(clean) <= limit else f"{clean[:limit - 1]}…"

    def _interpret(self) -> None:
        intent = interpret_prompt(self.prompt.toPlainText())
        if intent.mode == "place":
            self.custom_check.setChecked(False)
            self._select_preset(intent.preset_id or "administrative_places")
            self._place_name = intent.place_name
            self.command_place_label.setText(
                f"Place: {intent.place_name}  ·  resolved when download starts"
            )
            preset = get_preset(intent.preset_id or "administrative_places")
            self._set_status(
                f"Place command matched {preset.processing_label}. "
                "Review the dataset selection and download."
            )
            self.tabs.setCurrentWidget(self.download_tab)
            return
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

    def _clear_place(self) -> None:
        self._place_name = ""
        self.command_place_label.setText("Place: not selected")
        self._refresh_run_summary()

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
        if hasattr(self, "run_group"):
            self.run_group.setVisible(
                self.tabs.widget(index) in (self.download_tab, self.query_tab)
            )
        self._refresh_run_summary()

    def _sync_custom_fields(self, checked: bool) -> None:
        self.group_combo.setEnabled(not checked)
        self.dataset_list.setEnabled(not checked)
        self.key_edit.setEnabled(checked)
        self.value_edit.setEnabled(checked)
        self.geometry_combo.setEnabled(checked)
        self._refresh_run_summary()

    def _refresh_run_summary(self, *_args) -> None:
        if not hasattr(self, "run_summary"):
            return
        current = self.tabs.currentWidget()
        if current is self.query_tab:
            filters = self._query_filters()
            mode = str(self.query_match_combo.currentData() or "any")
            operator = " OR " if mode == "any" else " AND "
            summary = operator.join(
                f"{key or '?'}={value or '*'}" for key, value in filters
            )
            self.run_summary.setText(
                self._display_label(summary or "Add at least one query filter.")
            )
            self.download_button.setText("Run advanced query")
            return
        if current is self.download_tab:
            if self.custom_check.isChecked():
                key = self.key_edit.text().strip() or "OSM key"
                value = self.value_edit.text().strip() or "*"
                self.run_summary.setText(f"Custom tag  •  {key}={value}")
                self.download_button.setText("Download custom tag")
            else:
                preset_ids = self._selected_preset_ids()
                title = ", ".join(
                    get_preset(preset_id).title for preset_id in preset_ids
                ) or "Choose a dataset"
                if self._place_name:
                    title = f"{title}  ·  {self._place_name}"
                self.run_summary.setText(f"Preset  •  {title}")
                self.download_button.setText(
                    "Download place datasets" if self._place_name
                    else "Download datasets"
                )

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
        if self.tabs.currentWidget() not in (self.download_tab, self.query_tab):
            return
        extent = self._extent()
        if extent is None:
            QMessageBox.warning(
                self,
                "02Agent OSM Downloader",
                "Select an active layer or use the current map view.",
            )
            return
        if self.tabs.currentWidget() is self.query_tab:
            algorithm_id = "zero2agentosm:download_advanced"
            filters = self._query_filters()
            mode = str(self.query_match_combo.currentData() or "any")
            try:
                advanced_specs(filters, self._query_geometries(), mode)
            except QueryError as error:
                QMessageBox.warning(
                    self,
                    "02Agent OSM Downloader",
                    str(error),
                )
                return
            parameters = {
                "MATCH_MODE": 0 if mode == "any" else 1,
                "GEOMETRY": self.query_geometry_combo.currentIndex(),
            }
            for index in range(MAX_ADVANCED_FILTERS):
                key, value = filters[index] if index < len(filters) else ("", "")
                parameters[f"KEY_{index + 1}"] = key
                parameters[f"VALUE_{index + 1}"] = value
            operator = " OR " if mode == "any" else " AND "
            self._current_label = self._display_label(
                operator.join(
                    f"{key}={value or '*'}" for key, value in filters
                )
            )
        elif self.custom_check.isChecked():
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
            try:
                key, value = normalize_tag(
                    key,
                    self.value_edit.text().strip(),
                )
            except QueryError as error:
                QMessageBox.warning(
                    self,
                    "02Agent OSM Downloader",
                    str(error),
                )
                return
            parameters = {
                "KEY": key,
                "VALUE": value,
                "GEOMETRY": self.geometry_combo.currentIndex(),
            }
            self._current_label = self._display_label(
                f"{key}={parameters['VALUE'] or '*'}"
            )
        else:
            preset_ids = self._selected_preset_ids()
            preset_indexes = [
                index for index, preset in enumerate(PRESETS)
                if preset.preset_id in preset_ids
            ]
            if not preset_indexes:
                self._set_status("Choose a valid preset.")
                return
            try:
                normalized_specs(
                    tag
                    for index in preset_indexes
                    for tag in PRESETS[index].tags
                )
            except QueryError as error:
                QMessageBox.warning(
                    self,
                    "Too many combined dataset selectors",
                    f"{error} Select fewer related datasets and try again.",
                )
                return
            algorithm_id = (
                "zero2agentosm:download_place" if self._place_name
                else "zero2agentosm:download_preset"
            )
            parameters = {"PRESET": preset_indexes}
            if self._place_name:
                parameters["PLACE"] = self._place_name
            self._current_label = ", ".join(
                PRESETS[index].title for index in preset_indexes
            )
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
            self._style_layer(
                layer,
                self.current_map_theme(),
                self.current_road_width_mode(),
            )
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            added.append(f"{suffix}: {layer.featureCount():,}")
        if not added:
            project.layerTreeRoot().removeChildNode(group)
        return added

    def current_map_theme(self) -> str:
        """Return the selected persistent cartographic theme identifier."""
        return str(self.map_theme_combo.currentData() or DEFAULT_MAP_THEME)

    def current_road_width_mode(self) -> str:
        """Return the selected road-width strategy."""
        value = str(self.road_width_combo.currentData() or "highway")
        return value if value in ROAD_WIDTH_MODES else "highway"

    def _map_theme_changed(self, *_args) -> None:
        """Refresh the palette preview and persist the user's selection."""
        if not hasattr(self, "map_theme_combo"):
            return
        theme_id = self.current_map_theme()
        values = map_theme(theme_id)
        for swatch, color in zip(
            self.map_theme_swatches,
            map_theme_swatches(theme_id),
        ):
            swatch.setStyleSheet(
                f"background-color: {color}; border: 1px solid #707070; "
                "border-radius: 3px;"
            )
            swatch.setToolTip(color)
        self.map_theme_caption.setText(values["description"])
        self.map_theme_combo.setToolTip(
            f"{values['label']} — {values['description']}"
        )
        QgsSettings().setValue(_MAP_THEME_SETTING, theme_id)

    def _road_width_changed(self, *_args) -> None:
        """Persist the road-width strategy for future result layers."""
        if not hasattr(self, "road_width_combo"):
            return
        mode = self.current_road_width_mode()
        QgsSettings().setValue(_ROAD_WIDTH_SETTING, mode)
        self.road_width_combo.setToolTip(
            "Road lines use OSM highway categories for width."
            if mode == "highway"
            else "Road lines use one consistent width."
        )

    def _add_basemap(self) -> None:
        """Add or reveal the marked OpenStreetMap XYZ layer."""
        try:
            _layer, added = add_osm_basemap(QgsProject.instance())
        except (TypeError, ValueError) as error:
            QMessageBox.warning(self, "OpenStreetMap basemap", str(error))
            return
        self._set_status(
            "OpenStreetMap basemap added. Attribution: OpenStreetMap contributors."
            if added
            else "OpenStreetMap basemap is already in the project and is now visible."
        )

    @staticmethod
    def _style_layer(
        layer: QgsVectorLayer, theme_id: str, road_width_mode: str = "highway"
    ) -> None:
        apply_map_theme(layer, theme_id, road_width_mode)

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
