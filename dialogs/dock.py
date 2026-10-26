"""Compact, theme-aware dock for curated and command-assisted OSM downloads."""
from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Optional

from qgis.PyQt.QtCore import QEvent, QSize, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
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
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsProcessingAlgRunnerTask,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsPointXY,
    QgsProject,
    QgsReferencedRectangle,
    QgsRectangle,
    QgsSettings,
    QgsTask,
    QgsVectorLayer,
)
from qgis.gui import QgsFileWidget, QgsMapToolExtent

from ..core import history as download_history
from ..core.around import MAX_RADIUS_M, MIN_RADIUS_M, circle_bbox
from ..core.persist import apply_provenance, write_geopackage

from ..core.catalog import (
    GROUPS,
    PRESETS,
    TagSpec,
    group_context,
    get_preset,
    interpret_prompt,
    presets_for_group,
)
from ..core.basemap import add_osm_basemap
from ..core.query import (
    MAX_ADVANCED_FILTERS,
    MAX_BBOX_AREA_KM2,
    MAX_REGEX_VALUE_LENGTH,
    MAX_TILE_AREA_KM2,
    QueryError,
    advanced_specs,
    bbox_area_km2,
    is_heavy_wildcard,
    normalize_tag,
    normalized_specs,
    preview_query,
    tile_bbox,
)
from ..core.map_styling import apply_map_theme, carto_ground_layer
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
_SAVE_MODE_SETTING = "zero2agent_osm_downloader/save_mode"
_GPKG_PATH_SETTING = "zero2agent_osm_downloader/geopackage_path"
_HISTORY_SETTING = "zero2agent_osm_downloader/recent_downloads"


class _PlaceResolveTask(QgsTask):
    """Resolve a named place without blocking the QGIS interface."""

    def __init__(self, place: str, callback) -> None:
        super().__init__("02Agent: resolve named place", QgsTask.CanCancel)
        self.place = place
        self.callback = callback
        self.candidates: tuple = ()
        self.error_text = ""

    def run(self) -> bool:
        if self.isCanceled():
            return False
        try:
            from ..processing.osm_algorithms import resolve_place

            self.candidates = resolve_place(
                self.place, QgsProcessingFeedback()
            )
            if not self.candidates:
                self.error_text = f"No place matched '{self.place}'."
                return False
            return not self.isCanceled()
        except Exception as error:  # noqa: BLE001 - background boundary
            self.error_text = str(error).strip() or "Place lookup failed."
            return False

    def finished(self, result: bool) -> None:
        callback, self.callback = self.callback, None
        if callback is not None:
            callback(self, bool(result))

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


class _FittedTabWidget(QTabWidget):
    """A tab widget only as tall as the page currently on show.

    Two Qt defaults combine badly here.  QTabWidget reports the height of its
    *tallest* page, and a QScrollArea page reports a fixed guess rather than
    the height of what it holds.  Together they kept the tab strip at the
    Command page's height, so collapsing the custom-tag panel on a short tab
    left a block of dead space above "Run download" instead of letting it
    move up.

    The hint here only ever shrinks the widget: a page taller than the default
    keeps scrolling inside itself exactly as before.
    """

    def _page_height(self) -> int:
        page = self.currentWidget()
        if page is None:
            return 0
        content = page.widget() if isinstance(page, QScrollArea) else None
        return (content or page).sizeHint().height()

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        height = self._page_height()
        if height <= 0:
            return hint
        margins = self.contentsMargins()
        needed = (
            height
            + self.tabBar().sizeHint().height()
            + margins.top()
            + margins.bottom()
        )
        return QSize(hint.width(), min(hint.height(), needed))

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(hint.width(), min(hint.height(), self.sizeHint().height()))


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
        self._current_style_scope = ""
        self._current_extent = None
        self._place_name = ""
        self._place_candidate = None
        self._place_task = None
        self._extra_preset_ids: tuple[str, ...] = ()
        self._theme_refreshing = False
        self._drawn_extent = None
        self._extent_tool = None
        self._previous_map_tool = None
        self._pending_request = None
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

        badge_column = QVBoxLayout()
        badge_column.setContentsMargins(0, 0, 0, 0)
        badge_column.setSpacing(4)
        hero_badge = QLabel("OSM\nREADY")
        hero_badge.setObjectName("heroBadge")
        hero_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_badge.setToolTip("OpenStreetMap acquisition tools")
        self.map_features_button = QPushButton("OSM Map Features")
        self.map_features_button.setObjectName("mapFeaturesButton")
        self.map_features_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.map_features_button.setToolTip(
            "Open the OpenStreetMap Map Features reference."
        )
        self.map_features_button.clicked.connect(self._open_map_features)
        badge_column.addWidget(hero_badge)
        badge_column.addWidget(self.map_features_button)
        badge_column.addStretch(1)
        hero_top.addLayout(badge_column)
        hero_layout.addLayout(hero_top)

        workflow_hint = QLabel("Choose a workflow, review the request, then download.")
        workflow_hint.setObjectName("heroHint")
        workflow_hint.setWordWrap(True)
        hero_layout.addWidget(workflow_hint)
        layout.addWidget(hero)

        self.tabs = _FittedTabWidget()
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
        # A second, compact way to add the OSM basemap: it stays reachable
        # from any tab, right next to the tab bar, instead of only inside the
        # Run download section that may need scrolling to reach.
        self.basemap_button_corner = QPushButton("+ OSM basemap")
        self.basemap_button_corner.setObjectName("cornerButton")
        self.basemap_button_corner.setCursor(Qt.CursorShape.PointingHandCursor)
        self.basemap_button_corner.setToolTip(
            "Add the standard OpenStreetMap XYZ basemap to the current project."
        )
        self.basemap_button_corner.clicked.connect(self._add_basemap)
        self.tabs.setCornerWidget(
            self.basemap_button_corner, Qt.Corner.TopRightCorner
        )
        # No stretch factor: the tabs should take only the height their
        # content needs. A stretch factor here would let the tab widget claim
        # every pixel a tall docked panel happens to have, leaving a block of
        # empty space inside the tab instead of showing the "Run download"
        # section below it.
        # Never taller than the page on show needs.
        tab_policy = self.tabs.sizePolicy()
        tab_policy.setVerticalPolicy(QSizePolicy.Policy.Maximum)
        self.tabs.setSizePolicy(tab_policy)
        layout.addWidget(self.tabs)
        self._fit_tabs_to_current_page()

        self.run_group = QGroupBox("Run download")
        run_form = QFormLayout(self.run_group)
        run_form.setContentsMargins(9, 8, 9, 9)
        self.run_summary = QLabel()
        self.run_summary.setObjectName("runSummary")
        self.run_summary.setWordWrap(True)
        # A word-wrapped label reports the height of a single line unless the
        # layout is told to ask it how tall it is at the width it actually
        # gets. Without this a summary that wraps is clipped, and the clipped
        # second line reads as text printed over the line above it.
        summary_policy = self.run_summary.sizePolicy()
        summary_policy.setHeightForWidth(True)
        self.run_summary.setSizePolicy(summary_policy)
        self.run_summary.setMinimumHeight(
            self.run_summary.fontMetrics().height() * 2
        )
        run_form.addRow(self.run_summary)
        self.extent_combo = QComboBox()
        self.extent_combo.addItem("Current map view", "map")
        self.extent_combo.addItem("Active layer extent", "active")
        self.extent_combo.addItem("Named place (geocoded)", "place")
        self.extent_combo.addItem("Selected features (active layer)", "selection")
        self.extent_combo.addItem("Draw a rectangle on the map", "drawn")
        self.extent_combo.addItem("Around a point (radius)", "around")
        self.extent_combo.setToolTip(
            "Requests are transformed to WGS84. Anything larger than "
            f"{MAX_TILE_AREA_KM2:,.0f} km² is split into tiled requests, up to "
            f"{MAX_BBOX_AREA_KM2:,.0f} km² in total."
        )
        self.extent_combo.currentIndexChanged.connect(self._extent_mode_changed)
        run_form.addRow("Extent", self.extent_combo)

        self.place_panel = QWidget()
        place_layout = QVBoxLayout(self.place_panel)
        place_layout.setContentsMargins(0, 0, 0, 0)
        place_layout.setSpacing(4)
        search_row = QHBoxLayout()
        search_row.setSpacing(5)
        self.place_edit = QLineEdit()
        self.place_edit.setPlaceholderText("Konak, Izmir, Turkey")
        self.place_edit.setClearButtonEnabled(True)
        self.place_edit.setToolTip(
            "Any place name the OpenStreetMap Nominatim geocoder understands."
        )
        self.place_edit.returnPressed.connect(self._search_place)
        self.place_search_button = QPushButton("Search")
        self.place_search_button.setObjectName("quietButton")
        self.place_search_button.clicked.connect(self._search_place)
        search_row.addWidget(self.place_edit, 1)
        search_row.addWidget(self.place_search_button)
        place_layout.addLayout(search_row)
        self.place_combo = QComboBox()
        self.place_combo.setToolTip("Choose which matched place to download.")
        self.place_combo.setEnabled(False)
        self.place_combo.currentIndexChanged.connect(self._place_selected)
        place_layout.addWidget(self.place_combo)
        self.place_clip_combo = QComboBox()
        self.place_clip_combo.addItem("Exact mapped boundary", "boundary")
        self.place_clip_combo.addItem("Rectangular extent", "rectangle")
        self.place_clip_combo.setToolTip(
            "An exact boundary follows the mapped administrative edge, so "
            "neighbouring areas are not included."
        )
        self.place_clip_combo.currentIndexChanged.connect(
            self._refresh_extent_note
        )
        place_layout.addWidget(self.place_clip_combo)
        run_form.addRow(self.place_panel)
        self.place_panel.setVisible(False)

        self.draw_panel = QWidget()
        draw_layout = QHBoxLayout(self.draw_panel)
        draw_layout.setContentsMargins(0, 0, 0, 0)
        self.draw_button = QPushButton("Draw on map")
        self.draw_button.setObjectName("quietButton")
        self.draw_button.setToolTip("Drag a rectangle on the map canvas.")
        self.draw_button.clicked.connect(self._start_drawing)
        self.draw_label = QLabel("No rectangle drawn yet.")
        self.draw_label.setObjectName("mutedText")
        self.draw_label.setWordWrap(True)
        draw_layout.addWidget(self.draw_button)
        draw_layout.addWidget(self.draw_label, 1)
        run_form.addRow(self.draw_panel)
        self.draw_panel.setVisible(False)

        self.around_panel = QWidget()
        around_layout = QHBoxLayout(self.around_panel)
        around_layout.setContentsMargins(0, 0, 0, 0)
        self.around_centre_combo = QComboBox()
        self.around_centre_combo.addItem("Map view centre", "map")
        self.around_centre_combo.addItem("Centre of selected features", "selection")
        self.around_centre_combo.addItem("Searched place", "place")
        self.around_centre_combo.setToolTip(
            "Features that touch a circle around this point are kept."
        )
        self.around_centre_combo.currentIndexChanged.connect(self._extent_mode_changed)
        self.around_radius = QSpinBox()
        self.around_radius.setRange(int(MIN_RADIUS_M), int(MAX_RADIUS_M))
        self.around_radius.setSingleStep(50)
        self.around_radius.setValue(500)
        self.around_radius.setSuffix(" m")
        self.around_radius.setAccessibleName("Search radius in metres")
        self.around_radius.valueChanged.connect(self._refresh_extent_note)
        around_layout.addWidget(self.around_centre_combo, 1)
        around_layout.addWidget(self.around_radius)
        run_form.addRow(self.around_panel)
        self.around_panel.setVisible(False)

        save_row = QHBoxLayout()
        self.save_mode_combo = QComboBox()
        self.save_mode_combo.addItem("Temporary layers", "temporary")
        self.save_mode_combo.addItem("GeoPackage file", "gpkg")
        self.save_mode_combo.setToolTip(
            "Temporary layers disappear when QGIS closes. A GeoPackage keeps "
            "the data and its style; one file can collect many downloads."
        )
        self.gpkg_widget = QgsFileWidget()
        self.gpkg_widget.setStorageMode(
            getattr(QgsFileWidget, "StorageMode", QgsFileWidget).SaveFile
        )
        self.gpkg_widget.setFilter("GeoPackage (*.gpkg)")
        self.gpkg_widget.setFilePath(str(QgsSettings().value(_GPKG_PATH_SETTING, "") or ""))
        stored_save = str(QgsSettings().value(_SAVE_MODE_SETTING, "temporary") or "temporary")
        self.save_mode_combo.setCurrentIndex(max(0, self.save_mode_combo.findData(stored_save)))
        self.save_mode_combo.currentIndexChanged.connect(self._save_mode_changed)
        self.gpkg_widget.fileChanged.connect(self._save_mode_changed)
        save_row.addWidget(self.save_mode_combo)
        save_row.addWidget(self.gpkg_widget, 1)
        run_form.addRow("Save to", save_row)

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

        self.extent_note = QLabel()
        self.extent_note.setObjectName("mutedText")
        self.extent_note.setWordWrap(True)
        run_form.addRow(self.extent_note)

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
        self.clear_cache_button = QPushButton("Clear cache")
        self.clear_cache_button.setObjectName("quietButton")
        self.clear_cache_button.setToolTip(
            "Clear cached Overpass and Nominatim responses stored on disk."
        )
        self.clear_cache_button.clicked.connect(self._clear_cache)
        buttons.addWidget(self.download_button, 1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.clear_cache_button)
        run_form.addRow(buttons)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        run_form.addRow(self.progress)

        recent_row = QHBoxLayout()
        self.recent_combo = QComboBox()
        self.recent_combo.setAccessibleName("Recent downloads")
        self.recent_combo.setToolTip(
            "Run a previous download again with today's OSM data."
        )
        self.rerun_button = QPushButton("Run again")
        self.rerun_button.setObjectName("quietButton")
        self.rerun_button.clicked.connect(self._rerun_recent)
        recent_row.addWidget(self.recent_combo, 1)
        recent_row.addWidget(self.rerun_button)
        run_form.addRow("Recent", recent_row)
        self._populate_recent()
        self._save_mode_changed()
        layout.addWidget(self.run_group)

        self.status = QLabel("Ready.")
        self.status.setObjectName("statusCard")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.status)
        # Collect any height the dock area has to spare down here. Without
        # this the surplus goes to the only expanding child -- the tab widget
        # -- which pushes "Run download" to the bottom of a tall panel and
        # leaves dead space above it whenever a tab is short.
        layout.addStretch(1)

        # A docked panel is only as tall as the space QGIS happens to give
        # its dock area, which is routinely shorter than everything above
        # (hero + tabs + Run download + status). Without this outer scroll
        # area that overflow was simply clipped with no way to reach the
        # Download button — floating the dock into its own window worked
        # only because a top-level window can grow past the docked height.
        root_scroll = QScrollArea()
        root_scroll.setObjectName("agentOsmRootScroll")
        root_scroll.setWidgetResizable(True)
        root_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        root_scroll.setWidget(root)

        self.setWidget(root_scroll)
        self.setMinimumWidth(350)
        self._apply_theme()
        self._refresh_agent_connection()
        self._tab_changed(self.tabs.currentIndex())
        if self.iface is not None:
            # Keep the request-size estimate honest while the user pans and
            # zooms, so an oversized extent is visible before the download.
            with contextlib.suppress(AttributeError, RuntimeError, TypeError):
                self.iface.mapCanvas().extentsChanged.connect(
                    self._refresh_extent_note
                )
        self._refresh_extent_note()

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
        self.dataset_list.setMaximumHeight(184)
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

        custom_group = QFrame()
        custom_group.setObjectName("customTagPanel")
        custom_layout = QVBoxLayout(custom_group)
        custom_layout.setContentsMargins(9, 8, 9, 9)
        custom_layout.setSpacing(6)
        custom_header = QHBoxLayout()
        custom_header.setSpacing(6)
        self.custom_check = QPushButton("Custom tag  +")
        self.custom_check.setObjectName("customToggleButton")
        self.custom_check.setCheckable(True)
        self.custom_check.setChecked(False)
        self.custom_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.custom_check.setToolTip("Open a bounded custom OSM key/value request.")
        self.custom_check.toggled.connect(self._sync_custom_fields)
        custom_badge = QLabel("key=value")
        custom_badge.setObjectName("customTagBadge")
        custom_header.addWidget(self.custom_check, 1)
        custom_header.addWidget(custom_badge)
        custom_layout.addLayout(custom_header)
        self.custom_fields_panel = QFrame()
        self.custom_fields_panel.setObjectName("customTagFields")
        custom_form = QFormLayout(self.custom_fields_panel)
        custom_form.setContentsMargins(0, 2, 0, 0)
        custom_form.setSpacing(6)
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
        custom_form.addRow("Key", self.key_edit)
        custom_form.addRow("Value", self.value_edit)
        custom_form.addRow("Geometry", self.geometry_combo)
        custom_layout.addWidget(self.custom_fields_panel)
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
            f"Combine up to {MAX_ADVANCED_FILTERS} validated OSM tags, "
            "optionally as a regex, and optionally exclude one more tag. "
            "The generated Overpass query is read-only; the selected map or "
            "layer extent is inserted only when the download starts."
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

        self.query_regex_check = QCheckBox("Match values as regex (~) instead of exact equality")
        self.query_regex_check.setToolTip(
            "Renders [\"key\"~\"value\"] instead of [\"key\"=\"value\"]. "
            f"Regex values accept up to {MAX_REGEX_VALUE_LENGTH} characters; "
            "quotes, backslashes and semicolons are still blocked."
        )
        self.query_regex_check.toggled.connect(self._update_query_preview)
        outer.addWidget(self.query_regex_check)

        filter_actions = QHBoxLayout()
        self.query_filter_status = QLabel(f"0 / {MAX_ADVANCED_FILTERS} filters")
        self.query_filter_status.setObjectName("mutedText")
        clear_filters = QPushButton("Clear filters")
        clear_filters.setObjectName("quietButton")
        clear_filters.clicked.connect(self._clear_query_filters)
        filter_actions.addWidget(self.query_filter_status)
        filter_actions.addStretch(1)
        filter_actions.addWidget(clear_filters)
        outer.addLayout(filter_actions)

        exclude_group = QGroupBox("Exclude tag (optional)")
        exclude_form = QFormLayout(exclude_group)
        self.query_exclude_key_edit = QLineEdit()
        self.query_exclude_key_edit.setPlaceholderText("e.g. amenity")
        self.query_exclude_key_edit.setClearButtonEnabled(True)
        self.query_exclude_value_edit = QLineEdit()
        self.query_exclude_value_edit.setPlaceholderText("blank or * = any value")
        self.query_exclude_value_edit.setClearButtonEnabled(True)
        self.query_exclude_key_edit.textChanged.connect(self._refresh_run_summary)
        self.query_exclude_value_edit.textChanged.connect(self._refresh_run_summary)
        exclude_form.addRow("OSM key", self.query_exclude_key_edit)
        exclude_form.addRow("Value", self.query_exclude_value_edit)
        outer.addWidget(exclude_group)
        exclude_note = QLabel(
            "Excluded features are dropped after the OSM response arrives — "
            "this is not part of the Overpass query text below."
        )
        exclude_note.setObjectName("mutedText")
        exclude_note.setWordWrap(True)
        outer.addWidget(exclude_note)

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
            "Describe a preset in English or Turkish, or enter a safe "
            "key=value tag. Add \"without X\" to exclude a tag, and small "
            "misspellings are tolerated. The command is interpreted locally."
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        prompt_layout.addWidget(hint)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "Example: download public transport in Tokyo\n"
            "Example: download building=* data as polygons\n"
            "Example: download parking without charging in Berlin"
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
            "Konak'taki parkları indir",
            "Kadıköy'de otobüs duraklarını göster",
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
        privacy = QLabel(
            "Commands are interpreted on this computer. Only a place name is "
            "sent out, and only to resolve it; no other command text or "
            "project data leaves QGIS."
        )
        privacy.setObjectName("mutedText")
        privacy.setWordWrap(True)
        prompt_layout.addWidget(privacy)

        history_label = QLabel("Recent commands")
        history_label.setObjectName("mutedText")
        prompt_layout.addWidget(history_label)
        self.command_history_list = QListWidget()
        self.command_history_list.setMaximumHeight(110)
        self.command_history_list.setToolTip(
            "Click a past command to load it back into the box above."
        )
        self.command_history_list.itemClicked.connect(
            self._load_command_from_history
        )
        prompt_layout.addWidget(self.command_history_list)
        self._command_history: list[str] = []

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
            "02Agent Smart Modeler discovers the four bounded Processing endpoints "
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
            "Open 02Agent Smart Modeler's shared, secret-safe provider profiles."
        )
        self.ai_connections_button.clicked.connect(
            self._open_ai_connections
        )
        self.agent_workspace_button = QPushButton("Agent Workspace")
        self.agent_workspace_button.setToolTip(
            "Open 02Agent Smart Modeler's supervised agent dock."
        )
        self.agent_workspace_button.clicked.connect(
            self._open_agent_workspace
        )
        agent_layout.addWidget(self.ai_connections_button)
        agent_layout.addWidget(self.agent_workspace_button)

        security = QLabel(
            "API keys stay in 02Agent Smart Modeler's process memory or the encrypted "
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
        self._extra_preset_ids = ()
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
        self._extra_preset_ids = ()
        self.dataset_list.blockSignals(True)
        for index in range(self.dataset_list.count()):
            self.dataset_list.item(index).setCheckState(
                Qt.CheckState.Unchecked
            )
        self.dataset_list.blockSignals(False)
        self._dataset_changed()

    def _selected_preset_ids(self) -> tuple[str, ...]:
        checked = [
            str(self.dataset_list.item(index).data(Qt.ItemDataRole.UserRole) or "")
            for index in range(self.dataset_list.count())
            if self.dataset_list.item(index).checkState() == Qt.CheckState.Checked
        ]
        for extra_id in self._extra_preset_ids:
            if extra_id and extra_id not in checked:
                checked.append(extra_id)
        return tuple(checked)

    def _dataset_changed(self, *_args) -> None:
        preset_ids = self._selected_preset_ids()
        if any(get_preset(preset_id).group_id == "osm_as_is" for preset_id in preset_ids):
            theme_index = self.map_theme_combo.findData("osm_as_is")
            if theme_index >= 0 and self.map_theme_combo.currentIndex() != theme_index:
                self.map_theme_combo.setCurrentIndex(theme_index)
        if self._extra_preset_ids:
            self.dataset_count.setText(
                f"{len(preset_ids)} selected (across themes)"
            )
        else:
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
        self._extra_preset_ids = ()
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

    def _query_value_mode(self) -> str:
        return "regex" if self.query_regex_check.isChecked() else "exact"

    def _query_exclude(self) -> tuple[str, str]:
        return (
            self.query_exclude_key_edit.text().strip(),
            self.query_exclude_value_edit.text().strip(),
        )

    def _load_query_example(self) -> None:
        data = self.query_example_combo.currentData()
        if not isinstance(data, dict):
            return
        for edit in (*self.query_key_edits, *self.query_value_edits):
            edit.blockSignals(True)
            edit.clear()
        self.query_regex_check.setChecked(False)
        self.query_exclude_key_edit.clear()
        self.query_exclude_value_edit.clear()
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
        value_mode = self._query_value_mode()
        try:
            specs = advanced_specs(
                filters, self._query_geometries(), mode, value_mode
            )
            self.query_preview.setPlainText(
                preview_query(specs, mode, value_mode)
            )
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
        raw_text = self.prompt.toPlainText()
        intent = interpret_prompt(raw_text)
        self._remember_command(raw_text, intent)
        if intent.mode == "place" and intent.exclude_key:
            preset = get_preset(intent.preset_id or "place_centres")
            self._route_exclusion_to_query_tab(preset.tags, intent)
            self._place_extent_ui(intent.place_name)
            self.command_place_label.setText(
                f"Place: {intent.place_name}  ·  resolving map extent..."
            )
            self._set_status(
                f"Place command matched {preset.title}, excluding "
                f"{intent.exclude_key}={intent.exclude_value or '*'}. "
                "Resolving the place extent — review the Query tab."
            )
            self.tabs.setCurrentWidget(self.query_tab)
            return
        if intent.mode == "place":
            self.custom_check.setChecked(False)
            self._select_preset(intent.preset_id or "place_centres")
            self._check_extra_presets(intent.extra_preset_ids)
            self._place_extent_ui(intent.place_name)
            self.command_place_label.setText(
                f"Place: {intent.place_name}  ·  resolving map extent..."
            )
            preset = get_preset(intent.preset_id or "place_centres")
            self._set_status(
                f"Place command matched {preset.processing_label}. "
                "Resolving the place extent before download."
            )
            self.tabs.setCurrentWidget(self.download_tab)
            return
        if intent.mode == "preset" and intent.exclude_key:
            preset = get_preset(intent.preset_id)
            self._route_exclusion_to_query_tab(preset.tags, intent)
            self._set_status(
                f"Matched {preset.title}, excluding "
                f"{intent.exclude_key}={intent.exclude_value or '*'}. "
                "Review the Query tab and download."
            )
            self.tabs.setCurrentWidget(self.query_tab)
            return
        if intent.mode == "preset":
            self.custom_check.setChecked(False)
            self._select_preset(intent.preset_id)
            self._check_extra_presets(intent.extra_preset_ids)
            preset = get_preset(intent.preset_id)
            self._set_status(
                f"Matched {preset.processing_label} "
                f"({intent.confidence:.0%}). Review and download."
            )
            self.tabs.setCurrentWidget(self.download_tab)
            return
        if intent.mode == "custom" and intent.exclude_key:
            spec = TagSpec(
                intent.key,
                "" if intent.value == "*" else intent.value,
                intent.geometry,
            )
            self._route_exclusion_to_query_tab((spec,), intent)
            if intent.place_name:
                self._place_extent_ui(intent.place_name)
                self.command_place_label.setText(
                    f"Place: {intent.place_name}  ·  resolving map extent..."
                )
            self._set_status(
                f"Matched {intent.key}={intent.value or '*'}, excluding "
                f"{intent.exclude_key}={intent.exclude_value or '*'}. "
                "Review the Query tab and download."
            )
            self.tabs.setCurrentWidget(self.query_tab)
            return
        if intent.mode == "custom":
            self.custom_check.setChecked(True)
            self.key_edit.setText(intent.key)
            self.value_edit.setText(intent.value or "*")
            geometry_index = ("point", "line", "polygon").index(
                intent.geometry
            )
            self.geometry_combo.setCurrentIndex(geometry_index)
            if intent.place_name:
                self._place_extent_ui(intent.place_name)
                self.command_place_label.setText(
                    f"Place: {intent.place_name}  ·  resolving map extent..."
                )
            self._set_status(
                "Matched a custom OSM tag. Review and download."
            )
            self.tabs.setCurrentWidget(self.download_tab)
            return
        self._set_status(
            "No confident match. Choose a preset or enter key=value."
        )

    def _place_extent_ui(self, place_name: str) -> None:
        """Point the shared extent controls at a resolved place name."""
        self._place_name = place_name
        self._forget_place_matches()
        self.place_edit.setText(place_name)
        place_index = self.extent_combo.findData("place")
        if place_index >= 0:
            self.extent_combo.setCurrentIndex(place_index)
        self.place_search_button.setEnabled(False)
        self._resolve_place_extent(place_name)

    def _check_extra_presets(self, extra_preset_ids: tuple[str, ...]) -> None:
        """Additionally check presets the command also named (same or cross-group)."""
        if not extra_preset_ids:
            self._extra_preset_ids = ()
            return
        visible_ids = set()
        self.dataset_list.blockSignals(True)
        for index in range(self.dataset_list.count()):
            item = self.dataset_list.item(index)
            item_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
            visible_ids.add(item_id)
            if item_id in extra_preset_ids:
                item.setCheckState(Qt.CheckState.Checked)
        self.dataset_list.blockSignals(False)
        self._extra_preset_ids = tuple(
            pid for pid in extra_preset_ids if pid not in visible_ids
        )
        self._dataset_changed()

    def _route_exclusion_to_query_tab(self, positive_tags, intent) -> None:
        """Expand a matched preset's tags into the Query tab, with the excluded tag.

        Exclusion is only implemented for the Advanced Query algorithm (it is
        applied after the OSM response arrives), so a command carrying a
        "without X" clause is redirected there instead of the Presets tab,
        pre-filled with the tags the command already matched.
        """
        self.custom_check.setChecked(False)
        pairs: list[tuple[str, str]] = []
        for tag in positive_tags:
            pair = (tag.key, tag.value)
            if pair not in pairs:
                pairs.append(pair)
        pairs = pairs[:MAX_ADVANCED_FILTERS]
        for edit in (*self.query_key_edits, *self.query_value_edits):
            edit.blockSignals(True)
            edit.clear()
        for index, (key, value) in enumerate(pairs):
            self.query_key_edits[index].setText(key)
            self.query_value_edits[index].setText(value or "*")
        for edit in (*self.query_key_edits, *self.query_value_edits):
            edit.blockSignals(False)
        self.query_match_combo.setCurrentIndex(0)
        self.query_geometry_combo.setCurrentIndex(0)
        self.query_regex_check.setChecked(False)
        self.query_exclude_key_edit.setText(intent.exclude_key)
        self.query_exclude_value_edit.setText(intent.exclude_value)
        self._update_query_preview()

    def _remember_command(self, raw_text: str, intent) -> None:
        text = " ".join(str(raw_text or "").split())
        if not text:
            return
        if intent.mode == "none":
            summary = "no match"
        elif intent.mode == "custom":
            summary = f"{intent.key}={intent.value or '*'}"
        else:
            label = get_preset(
                intent.preset_id or "place_centres"
            ).title
            summary = (
                f"{label} in {intent.place_name}" if intent.place_name
                else label
            )
            if intent.exclude_key:
                summary = f"{summary} (excl. {intent.exclude_key})"
        entry = self._display_label(f"{text}  →  {summary}", limit=90)
        if entry in self._command_history:
            self._command_history.remove(entry)
        self._command_history.insert(0, entry)
        del self._command_history[10:]
        self.command_history_list.blockSignals(True)
        self.command_history_list.clear()
        for item_text in self._command_history:
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, text)
            self.command_history_list.addItem(item)
        self.command_history_list.blockSignals(False)

    def _load_command_from_history(self, item: QListWidgetItem) -> None:
        original = item.data(Qt.ItemDataRole.UserRole)
        if original:
            self.prompt.setPlainText(str(original))

    def _forget_place_matches(self) -> None:
        """Drop any resolved place so a stale match cannot be downloaded.

        `_current_place_candidate` reads the combo first, so leaving an old
        entry selected while a new search is pending would quietly download the
        previous place if that search failed.
        """
        self._place_candidate = None
        self.place_combo.blockSignals(True)
        self.place_combo.clear()
        self.place_combo.setEnabled(False)
        self.place_combo.blockSignals(False)

    def _clear_place(self) -> None:
        if self._place_task is not None:
            self._place_task.cancel()
            self._place_task = None
        # The cancelled task's callback bails out, so nothing else would
        # re-enable the button it disabled.
        self.place_search_button.setEnabled(True)
        self._place_name = ""
        self._forget_place_matches()
        self.command_place_label.setText("Place: not selected")
        self._refresh_run_summary()
        self._refresh_extent_note()

    def _extent_mode(self) -> str:
        return str(self.extent_combo.currentData() or "map")

    def _place_clip(self) -> str:
        return str(self.place_clip_combo.currentData() or "boundary")

    def _extent_mode_changed(self, *_args) -> None:
        mode = self._extent_mode()
        around_place = (
            mode == "around"
            and str(self.around_centre_combo.currentData() or "") == "place"
        )
        place_mode = mode == "place" or around_place
        self.place_panel.setVisible(place_mode)
        self.place_clip_combo.setVisible(mode == "place")
        self.draw_panel.setVisible(mode == "drawn")
        self.around_panel.setVisible(mode == "around")
        if place_mode and not self.place_edit.text().strip() and self._place_name:
            self.place_edit.setText(self._place_name)
        self._refresh_run_summary()
        self._refresh_extent_note()

    def _current_place_candidate(self):
        data = self.place_combo.currentData()
        return data if data is not None else self._place_candidate

    def _search_place(self) -> None:
        place = self.place_edit.text().strip()
        if not place:
            self.place_edit.setFocus()
            self._set_status("Enter a place name to search.")
            return
        self._place_name = place
        self._forget_place_matches()
        self.place_search_button.setEnabled(False)
        self._set_status(f"Searching for “{place}” …")
        self._refresh_extent_note()
        self._resolve_place_extent(place)

    def _resolve_place_extent(self, place: str) -> None:
        """Resolve and zoom the canvas while keeping the dock responsive."""
        if self._place_task is not None:
            self._place_task.cancel()
        task = _PlaceResolveTask(place, self._place_resolved)
        self._place_task = task
        if not QgsApplication.taskManager().addTask(task):
            self._place_task = None
            self.place_search_button.setEnabled(True)
            self.command_place_label.setText(
                f"Place: {place}  ·  download-time lookup will be used"
            )

    def _place_resolved(self, task: _PlaceResolveTask, successful: bool) -> None:
        if task is not self._place_task:
            return
        self._place_task = None
        self.place_search_button.setEnabled(True)
        if not successful or not task.candidates:
            self.command_place_label.setText(
                f"Place: {self._place_name}  ·  lookup will retry on download"
            )
            self._set_status(
                task.error_text
                or f"Could not resolve {self._place_name} yet. "
                "Review the request; download will retry the lookup."
            )
            self._refresh_extent_note()
            return

        self.place_combo.blockSignals(True)
        self.place_combo.clear()
        for candidate in task.candidates:
            label = candidate.label
            if candidate.admin_level:
                label = f"{label}  ·  level {candidate.admin_level}"
            elif candidate.kind:
                label = f"{label}  ·  {candidate.kind}"
            self.place_combo.addItem(label, candidate)
        self.place_combo.setEnabled(True)
        self.place_combo.blockSignals(False)

        candidate = task.candidates[0]
        self._place_candidate = candidate
        self._zoom_to_place(candidate.bbox)
        self.command_place_label.setText(
            f"Place: {candidate.label}  ·  map view updated"
        )
        matches = len(task.candidates)
        self._set_status(
            f"Resolved {candidate.label}"
            + (f" — {matches} matches, pick another if needed." if matches > 1
               else ". Review the request and download.")
        )
        self._refresh_run_summary()
        self._refresh_extent_note()

    def _place_selected(self, *_args) -> None:
        candidate = self._current_place_candidate()
        if candidate is None:
            return
        self._place_candidate = candidate
        self._place_name = candidate.name or self._place_name
        self._zoom_to_place(candidate.bbox)
        self.command_place_label.setText(f"Place: {candidate.label}")
        self._refresh_run_summary()
        self._refresh_extent_note()

    def _uses_exact_boundary(self) -> bool:
        """True when the pending download follows a mapped place boundary."""
        if self._extent_mode() != "place":
            return False
        if self._place_clip() != "boundary":
            return False
        candidate = self._current_place_candidate()
        return candidate is not None and candidate.area_id > 0

    def _wgs_bbox(self):
        """Return the pending request extent as WGS84 south, west, north, east."""
        rectangle = self._extent()
        if rectangle is None:
            return None
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        source = rectangle.crs()
        if source.isValid() and source != wgs84:
            try:
                transform = QgsCoordinateTransform(
                    source, wgs84, QgsProject.instance()
                )
                rectangle = transform.transformBoundingBox(rectangle)
            except (QgsCsException, RuntimeError, ValueError):
                # This runs on every canvas move to refresh a display-only
                # estimate.  A projection that cannot represent the current
                # view must leave the note blank, never raise a dialog.
                return None
        return (
            rectangle.yMinimum(), rectangle.xMinimum(),
            rectangle.yMaximum(), rectangle.xMaximum(),
        )

    def _refresh_extent_note(self, *_args) -> None:
        """Show the request size before the user commits to a download."""
        if not hasattr(self, "extent_note"):
            return
        bbox = self._wgs_bbox()
        if bbox is None:
            self.extent_note.setText(
                f"{self._output_note()}  •  choose an extent to see the request size."
            )
            return
        area = bbox_area_km2(*bbox)
        try:
            tiles = len(tile_bbox(bbox))
        except QueryError as error:
            if self._uses_exact_boundary():
                candidate = self._current_place_candidate()
                self.extent_note.setText(
                    f"{candidate.name} spans about {area:,.0f} km², beyond the "
                    f"{MAX_BBOX_AREA_KM2:,.0f} km² limit. Choose a smaller "
                    "administrative unit."
                )
            else:
                self.extent_note.setText(str(error))
            return
        plural = "s" if tiles != 1 else ""
        if self._uses_exact_boundary():
            candidate = self._current_place_candidate()
            self.extent_note.setText(
                f"{self._output_note()}  •  exact boundary of {candidate.name} "
                f"inside a {area:,.0f} km² rectangle  •  "
                f"{tiles} bounded request{plural}"
            )
            return
        if self._extent_mode() == "around":
            self.extent_note.setText(
                f"{self._output_note()}  •  {self.around_radius.value():,} m radius  •  "
                f"{tiles} bounded request{plural}"
            )
            return
        self.extent_note.setText(
            f"{self._output_note()}  •  {area:,.1f} km²  •  "
            f"{tiles} bounded request{plural}"
        )

    def _zoom_to_place(self, bbox) -> None:
        if self.iface is None:
            return
        try:
            south, west, north, east = tuple(float(value) for value in bbox)
            canvas = self.iface.mapCanvas()
            rectangle = QgsRectangle(west, south, east, north)
            destination = canvas.mapSettings().destinationCrs()
            source = QgsCoordinateReferenceSystem("EPSG:4326")
            if destination.isValid() and destination != source:
                transform = QgsCoordinateTransform(
                    source, destination, QgsProject.instance()
                )
                rectangle = transform.transformBoundingBox(rectangle)
            if rectangle.isEmpty():
                return
            canvas.setExtent(rectangle)
            canvas.refresh()
        except (TypeError, ValueError, RuntimeError):
            # A failed preview must never prevent the normal download flow.
            return

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
                    "02Agent Smart Modeler is not loaded. Open Plugins > Manage and "
                    "Install Plugins, search for “02Agent Smart Modeler”, install "
                    "and enable it, then reopen this tab. The offline command "
                    "router remains available without it."
                )
            )
            return
        profile = str(info.get("profile_name") or "Unnamed")
        provider = str(info.get("provider_name") or "Unknown provider")
        model = str(info.get("model") or "").strip()
        detail = f"02Agent Smart Modeler profile: {profile}  ·  {provider}"
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

    def _fit_tabs_to_current_page(self) -> None:
        """Re-ask the tab widget for its height after a page changed shape."""
        if hasattr(self, "tabs"):
            self.tabs.updateGeometry()

    def _tab_changed(self, index: int) -> None:
        self._fit_tabs_to_current_page()
        if self.tabs.widget(index) is self.connections_tab:
            self._refresh_agent_connection()
        if hasattr(self, "run_group"):
            self.run_group.setVisible(
                self.tabs.widget(index) in (self.download_tab, self.query_tab)
            )
        if hasattr(self, "place_clip_combo"):
            self.place_clip_combo.setEnabled(
                self.tabs.widget(index) in (self.download_tab, self.query_tab)
            )
        self._refresh_run_summary()
        self._refresh_extent_note()

    def _sync_custom_fields(self, checked: bool) -> None:
        self.group_combo.setEnabled(not checked)
        self.dataset_list.setEnabled(not checked)
        self.key_edit.setEnabled(checked)
        self.value_edit.setEnabled(checked)
        self.geometry_combo.setEnabled(checked)
        if hasattr(self, "custom_fields_panel"):
            self.custom_fields_panel.setVisible(checked)
        if hasattr(self, "custom_check"):
            self.custom_check.setText(
                "Custom tag  -" if checked else "Custom tag  +"
            )
        if hasattr(self, "custom_fields_panel"):
            panel = self.custom_fields_panel.parentWidget()
            if panel is not None:
                layout = panel.layout()
                if layout is not None:
                    layout.setContentsMargins(
                        9, 8, 9, 9 if checked else 6
                    )
                    layout.setSpacing(6 if checked else 0)
                panel.setProperty("active", checked)
                panel.style().unpolish(panel)
                panel.style().polish(panel)
        self._fit_tabs_to_current_page()
        self._refresh_run_summary()

    def _refresh_run_summary(self, *_args) -> None:
        if not hasattr(self, "run_summary"):
            return
        current = self.tabs.currentWidget()
        candidate = (
            self._current_place_candidate()
            if self._extent_mode() == "place" else None
        )
        if current is self.query_tab:
            filters = self._query_filters()
            mode = str(self.query_match_combo.currentData() or "any")
            operator = " OR " if mode == "any" else " AND "
            summary = operator.join(
                f"{key or '?'}={value or '*'}" for key, value in filters
            )
            exclude_key, exclude_value = self._query_exclude()
            if exclude_key:
                summary = (
                    f"{summary}  (excluding {exclude_key}="
                    f"{exclude_value or '*'})"
                )
            if summary and candidate is not None:
                summary = f"{summary}  ·  {candidate.name}"
            self.run_summary.setText(
                self._display_label(summary or "Add at least one query filter.")
            )
            self.download_button.setText("Run advanced query")
            return
        if current is self.download_tab:
            if self.custom_check.isChecked():
                key = self.key_edit.text().strip() or "OSM key"
                value = self.value_edit.text().strip() or "*"
                summary = f"{key}={value}"
                if candidate is not None:
                    summary = f"{summary}  ·  {candidate.name}"
                self.run_summary.setText(summary)
                self.download_button.setText("Download custom tag")
            else:
                preset_ids = self._selected_preset_ids()
                title = ", ".join(
                    get_preset(preset_id).title for preset_id in preset_ids
                ) or "Choose a dataset"
                if candidate is not None:
                    title = f"{title}  ·  {candidate.name}"
                self.run_summary.setText(f"Preset  •  {title}")
                self.download_button.setText(
                    "Download place datasets" if candidate is not None
                    else "Download datasets"
                )

    def _extent(self) -> Optional[QgsReferencedRectangle]:
        """Return the map or layer extent behind the current selection."""
        mode = self._extent_mode()
        if mode == "place":
            candidate = self._current_place_candidate()
            if candidate is None:
                return None
            south, west, north, east = candidate.bbox
            return QgsReferencedRectangle(
                QgsRectangle(west, south, east, north),
                QgsCoordinateReferenceSystem("EPSG:4326"),
            )
        if mode == "drawn":
            return self._drawn_extent
        if mode == "around":
            centre = self._around_centre()
            if centre is None:
                return None
            south, west, north, east = circle_bbox(
                centre[0], centre[1], float(self.around_radius.value())
            )
            return QgsReferencedRectangle(
                QgsRectangle(west, south, east, north),
                QgsCoordinateReferenceSystem("EPSG:4326"),
            )
        if self.iface is None:
            return None
        if mode == "active":
            layer = self.iface.activeLayer()
            if layer is None:
                return None
            return QgsReferencedRectangle(layer.extent(), layer.crs())
        if mode == "selection":
            layer = self.iface.activeLayer()
            if not isinstance(layer, QgsVectorLayer) or layer.selectedFeatureCount() == 0:
                return None
            box = layer.boundingBoxOfSelected()
            if box.isEmpty() or box.width() <= 0 or box.height() <= 0:
                return None
            return QgsReferencedRectangle(box, layer.crs())
        canvas = self.iface.mapCanvas()
        return QgsReferencedRectangle(
            canvas.extent(),
            canvas.mapSettings().destinationCrs(),
        )

    def _around_centre(self):
        """(lon, lat) in WGS84 of the around-search centre, or None."""
        source = str(self.around_centre_combo.currentData() or "map")
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if source == "place":
            candidate = self._current_place_candidate()
            if candidate is None:
                return None
            south, west, north, east = candidate.bbox
            return (west + east) / 2.0, (south + north) / 2.0
        if self.iface is None:
            return None
        if source == "selection":
            layer = self.iface.activeLayer()
            if not isinstance(layer, QgsVectorLayer) or layer.selectedFeatureCount() == 0:
                return None
            point, crs = layer.boundingBoxOfSelected().center(), layer.crs()
        else:
            canvas = self.iface.mapCanvas()
            point, crs = canvas.center(), canvas.mapSettings().destinationCrs()
        if crs.isValid() and crs != wgs84:
            try:
                point = QgsCoordinateTransform(
                    crs, wgs84, QgsProject.instance()
                ).transform(QgsPointXY(point))
            except (QgsCsException, RuntimeError, ValueError):
                return None
        return point.x(), point.y()

    def _missing_extent_message(self) -> str:
        mode = self._extent_mode()
        if mode == "place":
            return "Search for a place first."
        if mode == "selection":
            return ("Select one or more features in the active layer first. A "
                    "single point has no area - use 'Around a point' for it.")
        if mode == "drawn":
            return "Click 'Draw on map' and drag a rectangle first."
        if mode == "around":
            source = str(self.around_centre_combo.currentData() or "map")
            if source == "place":
                return "Search for a place first; its centre is the search centre."
            if source == "selection":
                return "Select one or more features in the active layer first."
        return "Select an active layer or use the current map view."

    def _start_drawing(self) -> None:
        if self.iface is None:
            return
        canvas = self.iface.mapCanvas()
        self._previous_map_tool = canvas.mapTool()
        tool = QgsMapToolExtent(canvas)
        tool.extentChanged.connect(self._extent_drawn)
        self._extent_tool = tool
        canvas.setMapTool(tool)
        self._set_status("Drag a rectangle on the map …")

    def _extent_drawn(self, rectangle) -> None:
        if rectangle is None or rectangle.isEmpty():
            return
        crs = (
            self.iface.mapCanvas().mapSettings().destinationCrs()
            if self.iface is not None else QgsCoordinateReferenceSystem("EPSG:4326")
        )
        self._drawn_extent = QgsReferencedRectangle(QgsRectangle(rectangle), crs)
        if self.iface is not None and self._previous_map_tool is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                self.iface.mapCanvas().setMapTool(self._previous_map_tool)
        self._extent_tool = None
        self.draw_label.setText("Rectangle drawn — see the request size below.")
        self._set_status("Rectangle captured. Review the request and download.")
        self._refresh_run_summary()
        self._refresh_extent_note()

    def _confirm_heavy_request(self, area_km2: float) -> bool:
        answer = QMessageBox.question(
            self,
            "Large unfiltered download",
            f"'All tagged objects' downloads every tagged OSM object in "
            f"{area_km2:,.1f} km² - typically ten to fifty times the features "
            "of a filtered dataset over the same ground, and slow to draw. "
            "Continue, or choose a smaller area or a filtered dataset?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _download(self) -> None:
        if self._task is not None:
            return
        if self.tabs.currentWidget() not in (self.download_tab, self.query_tab):
            return
        extent = self._extent()
        if extent is None:
            QMessageBox.warning(
                self, "02Agent OSM Downloader", self._missing_extent_message()
            )
            return
        self._current_extent = extent
        boundary_download = self._uses_exact_boundary()
        candidate = (
            self._current_place_candidate()
            if self._extent_mode() == "place" else None
        )
        wgs_bbox = self._wgs_bbox()
        if wgs_bbox is not None:
            try:
                tile_bbox(wgs_bbox)
            except QueryError as error:
                QMessageBox.warning(
                    self, "02Agent OSM Downloader", str(error)
                )
                return
        if self.tabs.currentWidget() is self.query_tab:
            algorithm_id = "zero2agentosm:download_advanced"
            filters = self._query_filters()
            mode = str(self.query_match_combo.currentData() or "any")
            value_mode = self._query_value_mode()
            try:
                advanced_specs(filters, self._query_geometries(), mode, value_mode)
            except QueryError as error:
                QMessageBox.warning(
                    self,
                    "02Agent OSM Downloader",
                    str(error),
                )
                return
            exclude_key, exclude_value = self._query_exclude()
            if exclude_key:
                try:
                    exclude_key, exclude_value = normalize_tag(
                        exclude_key, exclude_value
                    )
                except QueryError as error:
                    QMessageBox.warning(
                        self, "02Agent OSM Downloader", str(error)
                    )
                    return
            parameters = {
                "MATCH_MODE": 0 if mode == "any" else 1,
                "GEOMETRY": self.query_geometry_combo.currentIndex(),
                "VALUE_MODE": value_mode == "regex",
                "EXCLUDE_KEY": exclude_key,
                "EXCLUDE_VALUE": exclude_value,
            }
            if boundary_download and candidate is not None:
                parameters["AREA_ID"] = str(candidate.area_id)
            for index in range(MAX_ADVANCED_FILTERS):
                key, value = filters[index] if index < len(filters) else ("", "")
                parameters[f"KEY_{index + 1}"] = key
                parameters[f"VALUE_{index + 1}"] = value
            operator = " OR " if mode == "any" else " AND "
            label = operator.join(
                f"{key}={value or '*'}" for key, value in filters
            ) + (f" NOT {exclude_key}={exclude_value or '*'}" if exclude_key else "")
            if candidate is not None:
                label = f"{label} — {candidate.name}"
            self._current_label = self._display_label(label)
            self._current_style_scope = ""
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
            if boundary_download and candidate is not None:
                parameters["AREA_ID"] = str(candidate.area_id)
            label = f"{key}={parameters['VALUE'] or '*'}"
            if candidate is not None:
                label = f"{label} — {candidate.name}"
            self._current_label = self._display_label(label)
            self._current_style_scope = ""
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
            heavy_bbox = self._wgs_bbox()
            heavy_area = bbox_area_km2(*heavy_bbox) if heavy_bbox else 0.0
            if is_heavy_wildcard(
                (tag for index in preset_indexes for tag in PRESETS[index].tags),
                heavy_area,
            ) and not self._confirm_heavy_request(heavy_area):
                self._set_status("Large unfiltered download canceled.")
                return
            if boundary_download and candidate is not None:
                # Only the boundary path needs the place endpoint, and it is
                # handed the already-resolved area so it does not geocode the
                # name a second time and land on a different match.
                algorithm_id = "zero2agentosm:download_place"
                parameters = {
                    "PRESET": preset_indexes,
                    "PLACE": candidate.name,
                    "CLIP": 0,
                    "AREA_ID": str(candidate.area_id),
                }
            else:
                # A rectangle needs no name at all: the extent below already
                # is the place the user confirmed on the map.
                algorithm_id = "zero2agentosm:download_preset"
                parameters = {"PRESET": preset_indexes}
            self._current_label = ", ".join(
                PRESETS[index].title for index in preset_indexes
            )
            selected_groups = {
                PRESETS[index].group_id for index in preset_indexes
            }
            self._current_style_scope = (
                "places" if selected_groups == {"places"} else ""
            )
            if candidate is not None:
                self._current_label = (
                    f"{self._current_label} — {candidate.name}"
                )
        if self._extent_mode() == "around":
            centre = self._around_centre()
            if centre is None:
                QMessageBox.warning(
                    self, "02Agent OSM Downloader", self._missing_extent_message()
                )
                return
            radius = int(self.around_radius.value())
            parameters["AROUND_X"] = round(centre[0], 7)
            parameters["AROUND_Y"] = round(centre[1], 7)
            parameters["AROUND_METERS"] = float(radius)
            self._current_label = f"{self._current_label} — within {radius:,} m"
        self._submit(algorithm_id, parameters, extent)

    def _submit(self, algorithm_id: str, parameters: dict, extent) -> None:
        """Run one download request; remembered for 'Run again' on success."""
        self._current_extent = extent
        self._pending_request = (algorithm_id, dict(parameters), extent)
        parameters = dict(parameters)
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
            stamp = str((results or {}).get("OSM_TIMESTAMP") or "") if isinstance(results, dict) else ""
            when = f" OSM data as of {stamp.replace('T', ' ').rstrip('Z')} UTC." if stamp else ""
            self._set_status(f"Finished — {detail}.{when}")
            if added:
                self._remember_download(detail)
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
        # Inserted at index 0 (the top of the tree) rather than appended, so
        # a fresh download is immediately visible instead of landing below
        # whatever layers are already in the project.
        group = project.layerTreeRoot().insertGroup(
            0, f"02Agent — {self._current_label}"
        )
        added = []
        names = {
            "OUTPUT_POINTS": "Points",
            "OUTPUT_LINES": "Lines",
            "OUTPUT_POLYGONS": "Polygons",
        }
        stamp = str(results.get("OSM_TIMESTAMP") or "")
        styled = []
        for output, suffix in names.items():
            value = results.get(output)
            layer = context.takeResultLayer(value) if value else None
            if not isinstance(layer, QgsVectorLayer):
                continue
            if layer.featureCount() <= 0:
                layer.deleteLater()
                continue
            layer.setName(f"{self._current_label} — {suffix}")
            if self._current_style_scope:
                layer.setCustomProperty(
                    "zero2agent/style_scope",
                    self._current_style_scope,
                )
            self._style_layer(
                layer,
                self.current_map_theme(),
                self.current_road_width_mode(),
            )
            apply_provenance(layer, stamp, self._current_label)
            styled.append((layer, suffix.lower()))
            added.append(f"{suffix}: {layer.featureCount():,}")
        if not added:
            project.layerTreeRoot().removeChildNode(group)
            return added
        if (
            self._current_extent is not None
            and self._current_style_scope != "places"
            and self.current_map_theme() == "osm_as_is"
        ):
            # Added last, so it lands at the bottom of the group: whatever
            # category a preset missed or a feature failed to classify into
            # still reads as OSM ground instead of blank canvas.
            ground = carto_ground_layer(
                self._current_extent, self._current_extent.crs()
            )
            ground.setName(f"{self._current_label} — Ground")
            styled.append((ground, "ground"))
        layers = [layer for layer, _suffix in styled]
        if self._save_mode() == "gpkg":
            path = self.gpkg_widget.filePath().strip()
            try:
                layers = write_geopackage(styled, path, self._current_label, project)
                added.append(f"saved to {path}")
            except RuntimeError as error:
                # The data is downloaded; a file problem must not lose it.
                # The layers stay as temporary layers and the reason is shown.
                added.append(f"NOT saved ({error}) - kept as temporary layers")
        for layer in layers:
            project.addMapLayer(layer, False)
            group.addLayer(layer)
        return added

    def _save_mode(self) -> str:
        return str(self.save_mode_combo.currentData() or "temporary")

    def _output_note(self) -> str:
        if self._save_mode() == "gpkg":
            path = self.gpkg_widget.filePath().strip()
            return f"Saved to {path.split('/')[-1].split(chr(92))[-1]}" if path else "GeoPackage (choose a file)"
        return "Temporary layers"

    def _save_mode_changed(self, *_args) -> None:
        gpkg = self._save_mode() == "gpkg"
        self.gpkg_widget.setVisible(gpkg)
        settings = QgsSettings()
        settings.setValue(_SAVE_MODE_SETTING, self._save_mode())
        settings.setValue(_GPKG_PATH_SETTING, self.gpkg_widget.filePath().strip())
        self.download_button.setToolTip(
            "Download and save point, line and polygon layers into the GeoPackage."
            if gpkg else "Create temporary point, line, and polygon layers."
        )
        self._refresh_extent_note()

    def _history(self) -> list:
        return download_history.loads(QgsSettings().value(_HISTORY_SETTING, "[]"))

    def _populate_recent(self) -> None:
        entries = self._history()
        self.recent_combo.clear()
        for entry in entries:
            when = entry["when"].replace("T", " ")[:16]
            self.recent_combo.addItem(f"{entry['label']}  ·  {when}", entry)
        if not entries:
            self.recent_combo.addItem("No downloads yet", None)
        self.recent_combo.setEnabled(bool(entries))
        self.rerun_button.setEnabled(bool(entries))

    @staticmethod
    def _extent_record(extent) -> dict:
        rectangle, crs = QgsRectangle(extent), extent.crs()
        if not crs.isValid() or not crs.authid():
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            rectangle = QgsCoordinateTransform(
                crs, wgs84, QgsProject.instance()
            ).transformBoundingBox(rectangle)
            crs = wgs84
        return {
            "xmin": rectangle.xMinimum(), "ymin": rectangle.yMinimum(),
            "xmax": rectangle.xMaximum(), "ymax": rectangle.yMaximum(),
            "crs": crs.authid(),
        }

    def _remember_download(self, summary: str) -> None:
        if self._pending_request is None:
            return
        algorithm_id, parameters, extent = self._pending_request
        self._pending_request = None
        try:
            entry = download_history.make_entry(
                self._current_label, algorithm_id, parameters,
                self._extent_record(extent),
                datetime.now().isoformat(timespec="seconds"),
                summary, self._current_style_scope,
            )
        except (ValueError, QgsCsException, RuntimeError):
            return
        entries = download_history.add(self._history(), entry)
        QgsSettings().setValue(_HISTORY_SETTING, download_history.dumps(entries))
        self._populate_recent()

    def _rerun_recent(self) -> None:
        if self._task is not None:
            return
        entry = self.recent_combo.currentData()
        if not isinstance(entry, dict):
            return
        box = entry["extent"]
        extent = QgsReferencedRectangle(
            QgsRectangle(box["xmin"], box["ymin"], box["xmax"], box["ymax"]),
            QgsCoordinateReferenceSystem(box["crs"]),
        )
        self._current_label = entry["label"]
        self._current_style_scope = entry.get("style_scope", "")
        self._submit(entry["algorithm"], dict(entry["parameters"]), extent)

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
        self._restyle_project_layers()

    def _road_width_changed(self, *_args) -> None:
        """Persist the road-width strategy for future and existing result layers."""
        if not hasattr(self, "road_width_combo"):
            return
        mode = self.current_road_width_mode()
        QgsSettings().setValue(_ROAD_WIDTH_SETTING, mode)
        self.road_width_combo.setToolTip(
            "Road lines use OSM highway categories for width."
            if mode == "highway"
            else "Road lines use one consistent width."
        )
        self._restyle_project_layers()

    def _restyle_project_layers(self) -> None:
        """Restyle existing 02Agent vector layers in the current QGIS project."""
        project = QgsProject.instance()
        if project is None:
            return
        theme_id = self.current_map_theme()
        road_width_mode = self.current_road_width_mode()
        for layer in project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            if not layer.customProperty("zero2agent/map_theme"):
                continue
            self._style_layer(layer, theme_id, road_width_mode)

    def _clear_cache(self) -> None:
        """Clear the persistent SQLite query cache on disk."""
        from ..processing.osm_algorithms import clear_cache

        removed = clear_cache()
        if removed:
            self._set_status(
                f"Cleared {removed:,} cached OSM response{'s' if removed != 1 else ''}."
            )
        else:
            self._set_status("The OSM response cache is already empty.")

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

    def _open_map_features(self) -> None:
        """Open the public OSM tag reference in the user's browser."""
        QDesktopServices.openUrl(
            QUrl("https://wiki.openstreetmap.org/wiki/Map_features")
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
        if self._place_task is not None:
            with contextlib.suppress(Exception):
                self._place_task.cancel()
            self._place_task = None
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
