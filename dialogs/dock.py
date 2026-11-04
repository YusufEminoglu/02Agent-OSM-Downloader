"""Compact, theme-aware dock for curated and command-assisted OSM downloads."""
from __future__ import annotations

import configparser
import contextlib
import math
import os
import re
import time
import traceback
from datetime import datetime
from typing import Optional, Tuple
from urllib.parse import quote, urlencode

from qgis.PyQt.QtCore import QEvent, QSize, QSysInfo, Qt, QTimer, QUrl, pyqtSignal
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
    QLayout,
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
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsMessageLog,
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
    COMMAND_EXAMPLES,
    GROUPS,
    PRESETS,
    TagSpec,
    group_context,
    get_preset,
    interpret_prompt,
    presets_for_group,
)
from ..core.places import normalize_place_name
from ..core.basemap import add_osm_basemap
from ..core.query import (
    MAX_ADVANCED_FILTERS,
    MAX_BBOX_AREA_KM2,
    MAX_REGEX_VALUE_LENGTH,
    MAX_SELECTORS,
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
_FALLBACK_SETTING = "zero2agent_osm_downloader/allow_fallback"
#: The Postpass option, as the Processing parameter ALLOW_FALLBACK names it.
#: The line break keeps the checkbox inside a 350 px dock (one line is about
#: 340 px of text on its own).
_FALLBACK_LABEL = (
    "If the OpenStreetMap servers are busy,\nuse Geofabrik's Postpass"
)

MANUAL_URL = "https://geophilo.com/zero2agent_osm_downloader/manual.html"
ISSUES_URL = "https://github.com/YusufEminoglu/02Agent-OSM-Downloader/issues/new"
#: A failure class is a short identifier such as "rate_limited". Anything
#: else is not put into a public issue, so a place name cannot leak into it.
_FAILURE_KIND_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_LOG_TAG = "02Agent OSM Downloader"


def _breakable_path(path: object) -> str:
    """A path a word-wrapped label can break: zero-width spaces after each
    separator and every 16 characters inside a longer name.

    A path has no spaces, so to a word-wrapped label it is one word, and a
    footer label holding a long one widened the 350 px dock to fit (measured
    411 px on 3.44). The plugin's log keeps the path as it is, for copying.
    """
    pieces = []
    for part in re.split(r"([\\/])", str(path)):
        if part in ("\\", "/"):
            pieces.append(part + "​")
        else:
            pieces.append(
                "​".join(part[start:start + 16] for start in range(0, len(part), 16))
            )
    return "".join(pieces)


def _keep_text_height(label) -> None:
    """Keep a word-wrapped footer label at least as tall as its text.

    Tabbed with Browser at 1366x728 the dock is about 270 px tall, and the
    layout has to take that height from somewhere: without a floor it took
    it from the footer, so a long closing message shown with a notice and
    the Continue countdown lost its last line. With one, the scroll area
    above gives way instead. The label's own maximum still caps the floor.
    """
    # Measured with no floor: heightForWidth never answers less than the
    # current minimum, so a floor left from a long message would stay.
    label.setMinimumHeight(0)
    width = label.width()
    if width <= 50 or not label.text():
        return
    needed = label.heightForWidth(width)
    if needed > 0:
        label.setMinimumHeight(min(needed, label.maximumHeight()))


def _preset_enum_order() -> Tuple[str, ...]:
    """Preset ids in the Processing PRESET option order.

    That order is append-only, so a saved model keeps its datasets when the
    catalogue grows, and it differs from the display order of ``PRESETS``:
    every PRESET index the dock sends or reads goes through it.
    """
    from ..processing.osm_algorithms import PRESET_ENUM_ORDER
    return PRESET_ENUM_ORDER
_EMPTY_MAP_MESSAGE = (
    "The map is empty, so there is no area to download yet. Choose 'Named "
    "place' under Extent and search for a town, or press Add OSM basemap and "
    "zoom to your area."
)
_PLACE_ONLY_MESSAGE = (
    "Found the place “{place}”, but not what to download. Tick a dataset, or "
    "type a tag such as amenity=restaurant."
)
#: The primary button while a download runs (it is disabled then).
_DOWNLOADING_TEXT = "Downloading…"
#: Where the whole story of a download is, in QGIS's own menu words.
_PROCESSING_LOG = "View > Panels > Log Messages > Processing"
_DETAILS_HINT = f"Details: {_PROCESSING_LOG}."
_CANCELED_MESSAGE = "Download canceled. Nothing was added."
_INCOMPLETE_TIP = (
    "Finished with gaps. Run again within a day to fetch only the missing parts."
)
#: Shown in place of Continue while the servers rest after the errors that
#: left the gaps: pressed then, Continue would ask no server at all.
_CONTINUE_WAIT = (
    "Continue becomes available in about {wait}, then fetches only the "
    "missing parts."
)
_SERVERS_RESTING = "The servers are resting after errors"
#: A named place without a mapped boundary (area id 0) downloads its rectangle.
_NO_BOUNDARY_NOTE = "This place has no mapped boundary; its rectangle is used."
_CLIP_TIP = (
    "An exact boundary follows the mapped administrative edge, so "
    "neighbouring areas are not included."
)
#: The GeoPackage could not be written; the download is kept either way.
_GPKG_FOLDER_FAILURE = (
    "Could not write the GeoPackage: the folder {folder} cannot be created. "
    "The layers are kept as temporary layers; choose another file under Save to."
)
_GPKG_WRITE_FAILURE = (
    "Could not write the GeoPackage {path}. The layers are kept as temporary "
    "layers; choose another file under Save to. Details: View > Panels > Log "
    "Messages > " + _LOG_TAG + "."
)
#: Steps of a running download that say which part it is on.
_PART_PHASES = frozenset(
    {"request", "waiting", "switch", "split", "part_done", "part_lost"}
)
#: Steps whose sentence stays on screen while the next server is asked, so
#: the reason for a switch or a wait can be read instead of flashing past.
_NOTE_PHASES = frozenset({"waiting", "switch", "split", "part_lost"})
#: The live status of a download that has not reported a step yet.
_IDLE_LIVE = {"part": 0, "parts": 0, "host": "", "note": "", "note_phase": ""}


def _elapsed_text(seconds: float) -> str:
    """Elapsed time as 1:52, or 1:02:03 past an hour."""
    whole = max(0, int(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _wait_text(seconds: float) -> str:
    """A wait as "40 s" (up to the next 5 s) or "3 min" (up to the next minute)."""
    whole = max(1, math.ceil(seconds))
    if whole <= 55:
        return f"{5 * math.ceil(whole / 5)} s"
    return f"{math.ceil(whole / 60)} min"


def _count(value: object) -> int:
    """A Processing number output as a whole number; 0 when absent."""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _setting_on(value: object, default: bool = True) -> bool:
    """A stored on/off setting; QgsSettings hands back a bool or its text."""
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().casefold()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    return default


def plugin_version() -> str:
    """The version in this plugin's metadata.txt, or "unknown"."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metadata.txt"
    )
    parser = configparser.ConfigParser(interpolation=None)
    with contextlib.suppress(OSError, configparser.Error, UnicodeDecodeError):
        parser.read(path, encoding="utf-8")
    return parser.get("general", "version", fallback="").strip() or "unknown"


def problem_report_url(failure_kind: str = "") -> str:
    """A GitHub issue form prefilled with versions and the last failure class.

    Only the plugin, QGIS and operating-system versions and the failure class
    are filled in: never an extent, coordinates or a place name.
    """
    kind = str(failure_kind or "").strip()
    if not kind:
        failure = "none recorded"
    elif _FAILURE_KIND_RE.match(kind):
        failure = kind
    else:
        failure = "unrecognised"
    version = plugin_version()
    body = "\n".join((
        "What happened, and what did you expect to happen?",
        "",
        "",
        "---",
        f"Plugin version: {version}",
        f"QGIS version: {Qgis.version()}",
        f"Operating system: {QSysInfo.prettyProductName()}",
        f"Last failure: {failure}",
    ))
    query = urlencode(
        {"title": f"Problem in 02AgentOSM {version}", "body": body},
        quote_via=quote,
    )
    return f"{ISSUES_URL}?{query}"


def _open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl.fromEncoded(url.encode("ascii")))


class _PlaceResolveTask(QgsTask):
    """Resolve a named place without blocking the QGIS interface."""

    def __init__(self, place: str, callback) -> None:
        super().__init__("02Agent: resolve named place", QgsTask.Flag.CanCancel)
        self.place = place
        self.callback = callback
        self.candidates: tuple = ()
        self.error_text = ""
        self.error_detail = ""
        # Canceling the task cancels this feedback too, which aborts a network
        # wait in progress instead of letting it run out its timeout.
        self.feedback = QgsProcessingFeedback()

    def cancel(self) -> None:
        self.feedback.cancel()
        super().cancel()

    def run(self) -> bool:
        if self.isCanceled():
            return False
        try:
            from ..processing.osm_algorithms import resolve_place

            self.candidates = resolve_place(self.place, self.feedback)
        except Exception as error:  # noqa: BLE001 - background boundary
            self.error_detail = str(error).strip() or type(error).__name__
            self.error_text = (
                "Could not look up this place: the place search did not "
                "answer. Check your connection and press Search again."
            )
            return False
        if not self.candidates:
            self.error_text = (
                f"No place matched “{self.place}”. Try a fuller name, for "
                "example Konak, Izmir, Turkey."
            )
            return False
        return not self.isCanceled()

    def finished(self, result: bool) -> None:
        callback, self.callback = self.callback, None
        if callback is not None:
            callback(self, bool(result))


class _LiveFeedback(QgsProcessingFeedback):
    """Download feedback that passes each step of the download to the dock.

    The algorithm calls `zero2agent_event` once per step (a server asked, a
    switch, a wait, a part done or lost) on the worker thread that runs the
    download. This only emits a signal: the dock receives it on the GUI
    thread through a queued connection, and nothing here touches a widget.
    Every step still reaches the Processing log, as before.
    """

    step_reported = pyqtSignal(object)

    def __init__(self, run_id: int) -> None:
        super().__init__()
        # Sent with every step, so the dock can ignore one from an earlier run.
        self.run_id = run_id
        # The "failed" step, kept for the closing message: written on the
        # worker thread, read on the GUI thread after the task has ended.
        self.failure: dict = {}

    def zero2agent_event(self, event) -> None:
        """One step of the download, on the worker thread (see the class)."""
        step = dict(event)
        if step.get("phase") == "failed":
            self.failure = step
        self.step_reported.emit((self.run_id, step))


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
    left a block of dead space above "Download options" instead of letting
    it move up.

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
        # The class of the last failed download ("rate_limited", ...), set by
        # the task code; "Report a problem" includes it, never the area.
        self._last_failure_kind = ""
        # A short place name or position for the Recent list.
        self._current_where = ""
        # A place command that named no dataset; kept until the place resolves.
        self._awaiting_dataset_place = ""
        self._first_show_done = False
        # The running download as the status line shows it. Each run gets a
        # number, so a late step from an earlier run is ignored.
        self._run_id = 0
        self._run_started = 0.0
        self._live = dict(_IDLE_LIVE)
        self._canceling = False
        self._idle_download_text = ""
        # The Recent entry Continue runs again after a download with gaps.
        self._continue_entry: Optional[dict] = None
        # The gaps message while the servers rest, and the same message once
        # one is ready, with its notice: (resting, ready, notice) or None.
        self._gap_outcome: Optional[tuple] = None
        # Why the last result could not be saved to the GeoPackage, or "".
        self._save_failure = ""
        # The clip the user chose; a place without a boundary only lends it out.
        self._place_clip_choice = "boundary"
        # Moves the elapsed time on once a second while a download runs.
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(1000)
        self._live_timer.timeout.connect(self._render_live_status)
        # Counts down to Continue while the servers rest.
        self._continue_timer = QTimer(self)
        self._continue_timer.setInterval(1000)
        self._continue_timer.timeout.connect(self._refresh_continue)
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
        eyebrow = QLabel("DOWNLOAD OPENSTREETMAP DATA")
        eyebrow.setObjectName("heroEyebrow")
        eyebrow.setWordWrap(True)
        title = QLabel("02Agent")
        title.setObjectName("heroTitle")
        subtitle = QLabel("Pick data, pick an area, press Download.")
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        hero_copy.addWidget(eyebrow)
        hero_copy.addWidget(title)
        hero_copy.addWidget(subtitle)
        hero_top.addLayout(hero_copy, 1)
        hero_layout.addLayout(hero_top)

        # Two quiet links where a status badge used to be: help, and a way
        # to tell the author when something goes wrong.
        links = QHBoxLayout()
        links.setContentsMargins(0, 0, 0, 0)
        links.setSpacing(6)
        links.addStretch(1)
        self.manual_button = QPushButton("Manual")
        self.manual_button.setObjectName("linkButton")
        self.manual_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manual_button.setToolTip("Open the online manual.")
        self.manual_button.clicked.connect(self._open_manual)
        self.report_problem_button = QPushButton("Report a problem")
        self.report_problem_button.setObjectName("linkButton")
        self.report_problem_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.report_problem_button.setToolTip(
            "Open a GitHub issue form with the plugin, QGIS and system "
            "versions filled in. No map area or place name is included."
        )
        self.report_problem_button.clicked.connect(self._report_problem)
        links.addWidget(self.manual_button)
        links.addWidget(self.report_problem_button)
        hero_layout.addLayout(links)
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
        # No stretch factor: the tabs should take only the height their
        # content needs. A stretch factor here would let the tab widget claim
        # every pixel a tall docked panel happens to have, leaving a block of
        # empty space inside the tab instead of showing the "Download
        # options" section below it.
        # Never taller than the page on show needs.
        tab_policy = self.tabs.sizePolicy()
        tab_policy.setVerticalPolicy(QSizePolicy.Policy.Maximum)
        self.tabs.setSizePolicy(tab_policy)
        layout.addWidget(self.tabs)
        self._fit_tabs_to_current_page()

        self.run_group = QGroupBox("Download options")
        run_form = QFormLayout(self.run_group)
        run_form.setContentsMargins(9, 8, 9, 9)
        # A row too wide for a 350 px dock puts its field under its label
        # instead of widening the dock.
        run_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
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
        self.extent_combo.addItem("Named place", "place")
        self.extent_combo.addItem("Selected features (active layer)", "selection")
        self.extent_combo.addItem("Draw a rectangle on the map", "drawn")
        self.extent_combo.addItem("Around a point (radius)", "around")
        self.extent_combo.setToolTip(
            f"Areas larger than {MAX_TILE_AREA_KM2:,.0f} km² are downloaded in "
            f"parts, up to {MAX_BBOX_AREA_KM2:,.0f} km² in total."
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
            "Any place name OpenStreetMap's place search understands."
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
        self.place_clip_combo.setToolTip(_CLIP_TIP)
        self.place_clip_combo.currentIndexChanged.connect(
            self._place_clip_changed
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
        self.save_mode_combo.addItem("GeoPackage", "gpkg")
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

        self.fallback_check = QCheckBox(_FALLBACK_LABEL)
        self.fallback_check.setAccessibleName(" ".join(_FALLBACK_LABEL.split()))
        self.fallback_check.setToolTip(
            "When no OpenStreetMap server can deliver part of a download, ask "
            "Geofabrik's Postpass service (postpass.geofabrik.de) for that "
            "part. The status and the layer details name the server that "
            "delivered the data."
        )
        # Optional and off by default; a choice stored as on keeps it on.
        self.fallback_check.setChecked(
            _setting_on(QgsSettings().value(_FALLBACK_SETTING, False), default=False)
        )
        self.fallback_check.toggled.connect(self._fallback_changed)
        run_form.addRow(self.fallback_check)

        self.basemap_button = QPushButton("Add OSM basemap")
        self.basemap_button.setToolTip(
            "Add the OpenStreetMap map as a background to the current project."
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

        # Download, Cancel, Clear cache, progress and status live in the
        # footer built below, outside the scroll area.
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.download_button = QPushButton("Download")
        self.download_button.setObjectName("primaryButton")
        self.download_button.setToolTip(
            "Create temporary point, line, and polygon layers."
        )
        self.download_button.clicked.connect(self._download)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setToolTip("Stop the download or place search in progress.")
        # A place search is stopped first; cancel() then stops a download.
        self.cancel_button.clicked.connect(self._cancel_place_search)
        self.cancel_button.clicked.connect(self.cancel)
        self.clear_cache_button = QPushButton("Clear cache")
        self.clear_cache_button.setObjectName("quietButton")
        self.clear_cache_button.setToolTip(
            "Delete the OpenStreetMap answers this plugin keeps on disk, so the "
            "next download asks the servers again."
        )
        self.clear_cache_button.clicked.connect(self._clear_cache)
        buttons.addWidget(self.download_button, 1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.clear_cache_button)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        recent_row = QHBoxLayout()
        self.recent_combo = QComboBox()
        self.recent_combo.setAccessibleName("Recent downloads")
        self.recent_combo.setToolTip(
            "Run a previous download again. Within an hour of the first run "
            "the answer may come from the cache."
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
        # A long message must not push the scroll area out of the dock.
        self.status.setMaximumHeight(self.status.fontMetrics().height() * 6 + 16)
        # Under a closing message: what the download wants the user to know
        # (the algorithm's NOTICE) and a GeoPackage that could not be
        # written. Its own line, so the status card's height cap cannot cut
        # it off; any new status clears it.
        self.status_notice = QLabel()
        self.status_notice.setObjectName("statusNotice")
        self.status_notice.setWordWrap(True)
        self.status_notice.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.status_notice.setVisible(False)
        # Collect any height the dock area has to spare down here. Without
        # this the surplus goes to the only expanding child -- the tab widget
        # -- which pushes "Download options" to the bottom of a tall panel and
        # leaves dead space above it whenever a tab is short.
        layout.addStretch(1)

        # A docked panel is only as tall as the space QGIS happens to give
        # its dock area, which is routinely shorter than everything above
        # (hero + tabs + Download options). This scroll area holds all of it,
        # and the footer below holds what must never scroll away.
        root_scroll = QScrollArea()
        root_scroll.setObjectName("agentOsmRootScroll")
        root_scroll.setWidgetResizable(True)
        root_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        root_scroll.setWidget(root)
        self._root_scroll = root_scroll

        # After a download with gaps: the same request again, so only the
        # missing parts are fetched. It sits under the status that says so,
        # on its own row, and only while there is such a download: next to
        # Download it would widen the 350 px dock.
        self.continue_button = QPushButton("Continue")
        self.continue_button.setToolTip(
            "Run the download that finished with gaps again. The parts that "
            "arrived come from the cache; only the missing parts are fetched."
        )
        self.continue_button.clicked.connect(self._continue_download)
        self.continue_button.setEnabled(False)
        self.continue_button.setVisible(False)
        # While the servers rest, a countdown stands where Continue would be.
        self.continue_wait = QLabel()
        self.continue_wait.setObjectName("mutedText")
        self.continue_wait.setWordWrap(True)
        self.continue_wait.setVisible(False)
        continue_row = QHBoxLayout()
        # Aligned rather than stretched: Continue keeps its own width, and
        # the countdown, shown only while Continue is hidden, takes the row.
        continue_row.addWidget(
            self.continue_button, 0, Qt.AlignmentFlag.AlignLeft
        )
        continue_row.addWidget(self.continue_wait, 1)

        # Measured on QGIS 3.44 and 4.2 at 1366x728: tabbed with Browser the
        # dock is about 270 px tall, and Download sat 730 px below the fold.
        # The footer keeps Download, Cancel, progress and status on screen.
        self.footer = QFrame()
        self.footer.setObjectName("agentOsmFooter")
        footer_layout = QVBoxLayout(self.footer)
        footer_layout.setContentsMargins(10, 6, 10, 7)
        footer_layout.setSpacing(5)
        footer_layout.addLayout(buttons)
        footer_layout.addWidget(self.progress)
        footer_layout.addWidget(self.status)
        footer_layout.addWidget(self.status_notice)
        footer_layout.addLayout(continue_row)

        shell = QWidget(self)
        shell.setObjectName("agentOsmShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        shell_layout.addWidget(root_scroll, 1)
        shell_layout.addWidget(self.footer)

        # A combo is otherwise as wide as its longest item, and one long
        # place or Recent label widened the whole dock past 350 px.
        for combo in root.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(8)
        # Layouts measured while the widgets were built cached each combo at
        # its old width, and a hidden widget's size change does not reach
        # them; without this the dock stayed too wide until the next relayout.
        for item_layout in root.findChildren(QLayout):
            item_layout.invalidate()

        self.setWidget(shell)
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

        preset_group = QGroupBox("Ready-made datasets")
        preset_form = QFormLayout(preset_group)
        # Labels above their fields, so label width never widens the dock.
        preset_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.group_combo = QComboBox()
        self.group_combo.setToolTip(
            "Choose a topic. This picks datasets; the look of the result is "
            "set by Map style below."
        )
        self.group_combo.currentIndexChanged.connect(self._group_changed)
        self.dataset_list = QListWidget()
        self.dataset_list.setObjectName("datasetList")
        self.dataset_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection
        )
        self.dataset_list.setMaximumHeight(184)
        self.dataset_list.setToolTip(
            "Tick one or more datasets. They download together as one request."
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
        preset_form.addRow("Topic", self.group_combo)
        preset_form.addRow("Datasets", self.dataset_list)
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
        self.custom_check.setToolTip(
            "Download any OSM tag instead, such as amenity=cafe."
        )
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

        heading_row = QHBoxLayout()
        heading = QLabel("QUERY BY OSM TAGS")
        heading.setObjectName("heroEyebrow")
        heading.setWordWrap(True)
        # The tag reference sits where tags are typed.
        self.map_features_button = QPushButton("OSM Map Features")
        self.map_features_button.setObjectName("mapFeaturesButton")
        self.map_features_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.map_features_button.setToolTip(
            "Open the OpenStreetMap Map Features reference."
        )
        self.map_features_button.clicked.connect(self._open_map_features)
        heading_row.addWidget(heading, 1)
        heading_row.addWidget(self.map_features_button)
        outer.addLayout(heading_row)
        hint = QLabel(
            f"Combine up to {MAX_ADVANCED_FILTERS} OSM tags (key=value). "
            "Match any downloads objects with at least one of them; Match all "
            "needs every tag on the same object. The area comes from "
            "Download options below."
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

        self.query_regex_check = QCheckBox("Treat values as patterns (regex)")
        self.query_regex_check.setToolTip(
            "Renders [\"key\"~\"value\"] instead of [\"key\"=\"value\"]. "
            f"Patterns accept up to {MAX_REGEX_VALUE_LENGTH} characters; "
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
            "Add a tag to see the Overpass query it builds."
        )
        self.query_preview.setAccessibleName("Generated Overpass query preview")
        outer.addWidget(self.query_preview)
        self.query_preview.setVisible(False)
        safety = QLabel(
            "The query text cannot be edited and no other server can be chosen."
        )
        safety.setObjectName("mutedText")
        safety.setWordWrap(True)
        outer.addWidget(safety)
        outer.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        # The tab starts empty on Match any + All geometries. It used to open
        # on an example set to Match all + Polygons, so the first query a
        # user typed found nothing or was refused.
        self._clear_query_filters()
        return scroll

    def _build_command_tab(self) -> QWidget:
        tab = QWidget()
        prompt_layout = QVBoxLayout(tab)
        prompt_layout.setContentsMargins(10, 10, 10, 10)
        prompt_layout.setSpacing(8)
        heading = QLabel("DESCRIBE WHAT TO DOWNLOAD")
        heading.setObjectName("heroEyebrow")
        heading.setWordWrap(True)
        prompt_layout.addWidget(heading)
        hint = QLabel(
            "Type what you need in English or Turkish, such as “restaurants "
            "in Paris”, or an OSM tag such as amenity=cafe. Add “without X” "
            "to leave something out; small misspellings are fine."
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
        examples_label = QLabel("Examples")
        examples_label.setObjectName("mutedText")
        prompt_layout.addWidget(examples_label)
        command_examples = QHBoxLayout()
        self.command_example_combo = QComboBox()
        # Each example routes as its words say: tests/test_catalog.py.
        for example in COMMAND_EXAMPLES:
            self.command_example_combo.addItem(example)
        load_command = QPushButton("Load example")
        load_command.clicked.connect(
            lambda: self.prompt.setPlainText(self.command_example_combo.currentText())
        )
        command_examples.addWidget(self.command_example_combo, 1)
        command_examples.addWidget(load_command)
        prompt_layout.addLayout(command_examples)
        self.interpret_button = QPushButton("Prepare download")
        self.interpret_button.setObjectName("primaryButton")
        self.interpret_button.setToolTip(
            "Fill in the Presets or Query tab from the command. Nothing is "
            "downloaded until you press Download."
        )
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
            "02SmartModeler finds this plugin's four Processing tools "
            "through QGIS and can run three of them: preset, custom tag and "
            "query downloads. Every download still needs your approval."
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
            "Open 02SmartModeler's shared, secret-safe provider profiles."
        )
        self.ai_connections_button.clicked.connect(
            self._open_ai_connections
        )
        self.agent_workspace_button = QPushButton("Agent Workspace")
        self.agent_workspace_button.setToolTip(
            "Open 02SmartModeler's supervised agent dock."
        )
        self.agent_workspace_button.clicked.connect(
            self._open_agent_workspace
        )
        agent_layout.addWidget(self.ai_connections_button)
        agent_layout.addWidget(self.agent_workspace_button)

        security = QLabel(
            "API keys stay in 02SmartModeler's process memory or the encrypted "
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
        self.theme_context.setText(f"{focus}\n{related}" if related else focus)
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
        # Ticking OSM As-Is datasets used to switch the Map style combo, which
        # saved that choice and restyled every earlier layer. Those datasets
        # now get the as-is style for their own run only (current_map_theme).
        preset_ids = self._selected_preset_ids()
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
        """Empty every filter and go back to Match any + All geometries."""
        for edit in (
            *self.query_key_edits,
            *self.query_value_edits,
            self.query_exclude_key_edit,
            self.query_exclude_value_edit,
        ):
            edit.blockSignals(True)
            edit.clear()
            edit.blockSignals(False)
        for combo, default in (
            (self.query_match_combo, "any"),
            (self.query_geometry_combo, "all"),
        ):
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(default)))
            combo.blockSignals(False)
        self.query_regex_check.blockSignals(True)
        self.query_regex_check.setChecked(False)
        self.query_regex_check.blockSignals(False)
        self._update_query_preview()

    @staticmethod
    def _query_error_text(error: Exception, mode: str, filters) -> str:
        """A query error, plus the way out when Match all repeats a key."""
        text = str(error)
        keys = [key.strip() for key, _value in filters if key.strip()]
        if mode == "all" and len(keys) != len(set(keys)):
            text = (
                f"{text} An object has only one value per key, so Match all "
                "can never find it. Switch Match to 'Match any tag (OR)'."
            )
        return text

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
            self.query_preview.setPlainText(
                "This query cannot run: "
                + self._query_error_text(error, mode, filters)
            )
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

    @staticmethod
    def _intent_tags(intent) -> tuple:
        """Every tag an intent asks for, as TagSpecs, extra tags last."""
        if intent.mode == "custom":
            specs = [TagSpec(
                intent.key, "" if intent.value == "*" else intent.value,
                intent.geometry,
            )]
        else:
            specs = [
                spec
                for preset_id in (intent.preset_id, *intent.extra_preset_ids)
                if preset_id
                for spec in get_preset(preset_id).tags
            ]
        specs.extend(
            TagSpec(key, "" if value == "*" else value, geometry)
            for key, value, geometry in intent.extra_tags
        )
        return tuple(specs)

    @staticmethod
    def _query_tab_pairs(specs) -> tuple:
        """The specs as Query tab rows, or () when the tab cannot hold them.

        The tab takes plain key=value rows, at most MAX_ADVANCED_FILTERS of
        them; a qualified selector (admin_level) or the "*" key has no row.
        """
        pairs: list = []
        for spec in specs:
            if spec.key == "*" or spec.qualifiers:
                return ()
            if (spec.key, spec.value) not in pairs:
                pairs.append((spec.key, spec.value))
        return tuple(pairs) if len(pairs) <= MAX_ADVANCED_FILTERS else ()

    def _interpret(self) -> None:
        raw_text = self.prompt.toPlainText()
        intent = interpret_prompt(raw_text)
        self._remember_command(raw_text, intent)
        self._awaiting_dataset_place = ""
        if intent.mode == "place_only":
            # A place, but nothing recognisable to download: set the area and
            # say so, rather than pick a dataset the command never named.
            self.custom_check.setChecked(False)
            self._clear_datasets()
            self._awaiting_dataset_place = intent.place_name
            self._place_extent_ui(intent.place_name)
            self.command_place_label.setText(
                f"Place: {intent.place_name}  ·  resolving map extent..."
            )
            self._set_status(_PLACE_ONLY_MESSAGE.format(place=intent.place_name))
            self.tabs.setCurrentWidget(self.download_tab)
            return
        left_out = ""
        if intent.extra_tags:
            # "Parks and playgrounds": a preset plus a tag it lacks. The Query
            # tab downloads them together (Match any) when they fit in it.
            pairs = self._query_tab_pairs(self._intent_tags(intent))
            if pairs:
                self._route_exclusion_to_query_tab(
                    tuple(TagSpec(key, value, "point") for key, value in pairs),
                    intent,
                )
                if intent.place_name:
                    self._place_extent_ui(intent.place_name)
                    self.command_place_label.setText(
                        f"Place: {intent.place_name}  ·  resolving map extent..."
                    )
                names = ", ".join(f"{key}={value or '*'}" for key, value in pairs)
                self._set_status(
                    f"Matched {names}. They download together as one request "
                    "(Match any). Review the Query tab and download."
                )
                self.tabs.setCurrentWidget(self.query_tab)
                return
            left_out = " Not included: " + ", ".join(
                f"{key}={value}" for key, value, _geometry in intent.extra_tags
            ) + " (add it on the Query tab)."
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
                "Resolving the place extent before download." + left_out
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
            # A keyword count is not a probability, so no percentage; a
            # guess from a misspelling is called a guess.
            self._set_status(
                (
                    f"Closest match: {preset.processing_label}. Check it is "
                    "what you meant, then download."
                    if intent.confidence <= 0.6
                    else f"Matched {preset.processing_label}. Review and download."
                )
                + left_out
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
                f"Matched the OSM tag {intent.key}={intent.value or '*'}. "
                "Review and download." + left_out
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
        elif intent.mode == "place_only":
            summary = f"{intent.place_name}, no dataset named"
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
        if intent.extra_tags:
            summary = f"{summary} + " + ", ".join(
                f"{key}={value}" for key, value, _geometry in intent.extra_tags
            )
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
        self._sync_place_clip()

    def _clear_place(self) -> None:
        if self._place_task is not None:
            self._place_task.cancel()
            self._place_task = None
        # The cancelled task's callback bails out, so nothing else would
        # re-enable the button it disabled.
        self.place_search_button.setEnabled(True)
        if self._task is None:
            self.cancel_button.setEnabled(False)
        self._awaiting_dataset_place = ""
        self._place_name = ""
        self._forget_place_matches()
        self.command_place_label.setText("Place: not selected")
        self._refresh_run_summary()
        self._refresh_extent_note()

    def _cancel_place_search(self) -> None:
        """Stop a place search in progress (Cancel; cancel() stops a download).

        The search callback ignores a task that is no longer current, so the
        controls it would have restored are restored here, at once, instead
        of after the network wait the search was stuck in.
        """
        task, self._place_task = self._place_task, None
        if task is None:
            return
        with contextlib.suppress(RuntimeError):
            task.cancel()
        self._awaiting_dataset_place = ""
        self.place_search_button.setEnabled(True)
        if self._task is None:
            self.cancel_button.setEnabled(False)
        self.command_place_label.setText(
            f"Place: {self._place_name}  ·  search canceled"
        )
        self._set_status("Place search canceled.")
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
        self._awaiting_dataset_place = ""
        self._forget_place_matches()
        self.place_search_button.setEnabled(False)
        self._set_status(f"Searching for “{place}” …")
        self._refresh_extent_note()
        self._resolve_place_extent(place)

    def _resolve_place_extent(self, place: str) -> None:
        """Resolve and zoom the canvas while keeping the dock responsive."""
        if self._place_task is not None:
            self._place_task.cancel()
            self._place_task = None
        try:
            normalize_place_name(place)
        except ValueError as error:
            self.place_search_button.setEnabled(True)
            self._set_status(str(error))
            return
        task = _PlaceResolveTask(place, self._place_resolved)
        self._place_task = task
        if not QgsApplication.taskManager().addTask(task):
            self._place_task = None
            self.place_search_button.setEnabled(True)
            self.command_place_label.setText(
                f"Place: {place}  ·  search could not start"
            )
            self._set_status(
                "QGIS could not start the place search. Press Search again."
            )
            return
        # A slow geocoder can keep this running for minutes; Cancel stops it.
        self.cancel_button.setEnabled(True)

    def _place_resolved(self, task: _PlaceResolveTask, successful: bool) -> None:
        if task is not self._place_task:
            return
        self._place_task = None
        self.place_search_button.setEnabled(True)
        if self._task is None:
            self.cancel_button.setEnabled(False)
        awaiting, self._awaiting_dataset_place = self._awaiting_dataset_place, ""
        if not successful or not task.candidates:
            if task.error_detail:
                QgsMessageLog.logMessage(
                    f"Place search failed: {task.error_detail}", _LOG_TAG
                )
            self.command_place_label.setText(
                f"Place: {self._place_name}  ·  "
                + ("search failed" if task.error_detail else "no match")
            )
            self._set_status(
                task.error_text
                or "The place search did not finish. Press Search again."
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
        self._sync_place_clip()
        self._zoom_to_place(candidate.bbox)
        self.command_place_label.setText(
            f"Place: {candidate.label}  ·  map view updated"
        )
        matches = len(task.candidates)
        if awaiting:
            # The command named a place and nothing to download: the area is
            # now set, the dataset is still the user's to choose.
            self._set_status(_PLACE_ONLY_MESSAGE.format(place=awaiting))
        else:
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
        self._sync_place_clip()
        self._zoom_to_place(candidate.bbox)
        self.command_place_label.setText(f"Place: {candidate.label}")
        self._refresh_run_summary()
        self._refresh_extent_note()

    def _place_clip_changed(self, *_args) -> None:
        """Remember the user's clip choice; refresh the request note."""
        if self.place_clip_combo.isEnabled():
            self._place_clip_choice = self._place_clip()
        self._refresh_extent_note()

    def _sync_place_clip(self) -> None:
        """Offer the exact boundary only for a place that has one.

        A place with no mapped boundary (area id 0) is downloaded as its
        rectangle. The clip choice used to read "Exact mapped boundary" all
        the same; now it shows Rectangular extent and stays locked until a
        place with a boundary is chosen, which gets the user's choice back.
        Off the Presets and Query tabs it is locked as before.
        """
        if not hasattr(self, "place_clip_combo"):
            return
        candidate = self._current_place_candidate()
        boundless = candidate is not None and candidate.area_id <= 0
        combo = self.place_clip_combo
        wanted = "rectangle" if boundless else self._place_clip_choice
        combo.blockSignals(True)
        combo.setCurrentIndex(max(0, combo.findData(wanted)))
        combo.blockSignals(False)
        on_request_tab = hasattr(self, "tabs") and self.tabs.currentWidget() in (
            self.download_tab, self.query_tab
        )
        combo.setEnabled(on_request_tab and not boundless)
        combo.setToolTip(_NO_BOUNDARY_NOTE if boundless else _CLIP_TIP)

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
            mode = self._extent_mode()
            if mode in ("map", "active"):
                self.extent_note.setText(self._missing_extent_message())
            elif mode == "place":
                self.extent_note.setText(
                    f"{self._output_note()}  •  Search for a place to see the "
                    "request size."
                )
            else:
                self.extent_note.setText(
                    f"{self._output_note()}  •  choose an extent to see the "
                    "request size."
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
                f"{tiles} request{plural}"
            )
            return
        if self._extent_mode() == "around":
            self.extent_note.setText(
                f"{self._output_note()}  •  {self.around_radius.value():,} m radius  •  "
                f"{tiles} request{plural}"
            )
            return
        size = f"{self._output_note()}  •  {area:,.1f} km²  •  {tiles} request{plural}"
        candidate = (
            self._current_place_candidate() if self._extent_mode() == "place" else None
        )
        if candidate is not None and candidate.area_id <= 0:
            # Said here, before the download, rather than only in the log.
            self.extent_note.setText(f"{_NO_BOUNDARY_NOTE}\n{size}")
            return
        self.extent_note.setText(size)

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
                    "02SmartModeler is not loaded. Open Plugins > Manage and "
                    "Install Plugins, search for “02SmartModeler”, install "
                    "and enable it, then reopen this tab. The Command tab "
                    "works without it."
                )
            )
            return
        profile = str(info.get("profile_name") or "Unnamed")
        provider = str(info.get("provider_name") or "Unknown provider")
        model = str(info.get("model") or "").strip()
        detail = f"02SmartModeler profile: {profile}  ·  {provider}"
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
            on_download_tab = self.tabs.widget(index) in (
                self.download_tab, self.query_tab
            )
            self.run_group.setVisible(on_download_tab)
            # The footer stays, so Cancel and the status remain in reach;
            # Download itself only means something on Presets and Query.
            self.download_button.setVisible(on_download_tab)
        self._sync_place_clip()
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
            if layer is None or self._is_empty(layer.extent()):
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
        # A new project's canvas extent is the null rectangle (1.8e308 to
        # -1.8e308), not an area: it used to reach the query as one and end in
        # "The selected extent is outside WGS84 bounds."
        if self._is_empty(canvas.extent()):
            return None
        return QgsReferencedRectangle(
            canvas.extent(),
            canvas.mapSettings().destinationCrs(),
        )

    @staticmethod
    def _is_empty(rectangle) -> bool:
        return rectangle is None or rectangle.isNull() or rectangle.isEmpty()

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
            if self._is_empty(canvas.extent()):
                return None
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
            return _EMPTY_MAP_MESSAGE
        if mode == "map":
            return _EMPTY_MAP_MESSAGE
        if self.iface is not None and self.iface.activeLayer() is not None:
            return (
                "The active layer has no features yet, so it has no area. "
                "Choose another layer or another Extent option."
            )
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
            "Download it anyway? Choose No to pick a smaller area or a "
            "filtered dataset instead.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _refuse_without_geopackage(self) -> bool:
        """True (and say so) when GeoPackage output has no file to write.

        Checked before the download starts: it used to run in full and only
        then report that nothing could be saved.
        """
        if self._save_mode() != "gpkg" or self.gpkg_widget.filePath().strip():
            return False
        QMessageBox.warning(
            self, "02Agent OSM Downloader", "Choose a GeoPackage file first."
        )
        with contextlib.suppress(AttributeError, RuntimeError):
            self.gpkg_widget.setFocus()
        return True

    def _download(self) -> None:
        if self._task is not None:
            return
        if self.tabs.currentWidget() not in (self.download_tab, self.query_tab):
            return
        if self._refuse_without_geopackage():
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
        around_place = (
            self._extent_mode() == "around"
            and str(self.around_centre_combo.currentData() or "") == "place"
        )
        where_candidate = candidate or (
            self._current_place_candidate() if around_place else None
        )
        self._current_where = where_candidate.name if where_candidate else ""
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
                    self._query_error_text(error, mode, filters),
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
            # Display order for the label and the theme; Processing indexes
            # from its own append-only order.
            enum_order = _preset_enum_order()
            chosen_presets = [
                preset for preset in PRESETS
                if preset.preset_id in preset_ids and preset.preset_id in enum_order
            ]
            preset_indexes = [
                enum_order.index(preset.preset_id) for preset in chosen_presets
            ]
            if not preset_indexes:
                self._set_status("Tick at least one dataset.")
                return
            chosen_tags = [tag for preset in chosen_presets for tag in preset.tags]
            try:
                normalized_specs(chosen_tags)
            except QueryError as error:
                if len(chosen_tags) > MAX_SELECTORS:
                    QMessageBox.warning(
                        self,
                        "Too many datasets at once",
                        f"These datasets need more than {MAX_SELECTORS} tag "
                        "filters. Tick fewer and press Download again.",
                    )
                else:
                    QMessageBox.warning(self, "02Agent OSM Downloader", str(error))
                return
            heavy_bbox = self._wgs_bbox()
            heavy_area = bbox_area_km2(*heavy_bbox) if heavy_bbox else 0.0
            if is_heavy_wildcard(
                chosen_tags, heavy_area
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
                preset.title for preset in chosen_presets
            )
            selected_groups = {preset.group_id for preset in chosen_presets}
            self._current_style_scope = self._style_scope(selected_groups)
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
        """Run one download request; remembered under Recent when it ends."""
        self._current_extent = extent
        self._pending_request = (algorithm_id, dict(parameters), extent)
        parameters = dict(parameters)
        parameters.update(
            {
                "EXTENT": extent,
                "OUTPUT_POINTS": "TEMPORARY_OUTPUT",
                "OUTPUT_LINES": "TEMPORARY_OUTPUT",
                "OUTPUT_POLYGONS": "TEMPORARY_OUTPUT",
                # A setting, not part of the request Recent stores: every
                # run, Run again and Continue follow the current choice.
                "ALLOW_FALLBACK": self.fallback_check.isChecked(),
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
        self._run_id += 1
        feedback = _LiveFeedback(self._run_id)
        feedback.progressChanged.connect(self._set_progress)
        # Steps are reported on the download's worker thread; queued, each
        # one is handled here on the GUI thread, where widgets may change.
        feedback.step_reported.connect(
            self._download_step, Qt.ConnectionType.QueuedConnection
        )
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
        self._canceling = False
        self._live = dict(_IDLE_LIVE)
        self._run_started = time.monotonic()
        # Continue belongs to the download that just finished with gaps;
        # it comes back only if this one ends with gaps too.
        self._offer_continue(None)
        if self.download_button.text() != _DOWNLOADING_TEXT:
            self._idle_download_text = self.download_button.text()
        self.download_button.setText(_DOWNLOADING_TEXT)
        self.download_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setValue(0)
        self._render_live_status()
        self._live_timer.start()
        if not QgsApplication.taskManager().addTask(task):
            self._reset_task_state()
            self._set_status("QGIS could not start the download task.")
            QMessageBox.critical(
                self,
                "02Agent OSM Downloader",
                "QGIS could not start the download task.",
            )

    def _set_progress(self, value: float) -> None:
        """QGIS emits float progress while QProgressBar accepts only int."""
        self.progress.setValue(max(0, min(100, int(round(value)))))

    def _download_step(self, report) -> None:
        """One step of the running download, delivered on the GUI thread."""
        run_id, step = report
        if run_id != self._run_id or self._task is None:
            return  # a step of a download that has already ended
        phase = str(step.get("phase") or "")
        live = self._live
        if phase in _PART_PHASES:
            parts = _count(step.get("parts"))
            if parts:
                live["part"], live["parts"] = _count(step.get("part")), parts
        if phase == "request":
            live["host"] = str(step.get("host") or "")
            # A wait is over once the server is asked; the reason for a
            # switch, a split or a lost part stays readable meanwhile.
            if live["note_phase"] == "waiting":
                live["note"] = ""
        elif phase in _NOTE_PHASES:
            live["host"] = ""
            live["note"] = str(step.get("detail") or "")
            live["note_phase"] = phase
        elif phase == "part_done":
            live["host"] = ""
            live["note"] = ""
        self._render_live_status()

    def _render_live_status(self) -> None:
        """What is downloading, for how long, which part and which server.

        Called for every step and once a second by the timer, for example
        "Downloading Buildings — 1:52 · part 3 of 12 · asking
        overpass.private.coffee", with the reason for the last switch, wait
        or split on a second line.
        """
        if self._task is None:
            self._live_timer.stop()
            return
        # Changing the request mid-download relabels the button through
        # _refresh_run_summary; while it is disabled it says what is going on.
        if self.download_button.text() != _DOWNLOADING_TEXT:
            self.download_button.setText(_DOWNLOADING_TEXT)
        if self._canceling:
            self._set_status("Canceling download …")
            return
        live = self._live
        elapsed = _elapsed_text(time.monotonic() - self._run_started)
        pieces = [f"Downloading {self._current_label} — {elapsed}"]
        if live["parts"] > 1 and live["part"]:
            pieces.append(f"part {live['part']} of {live['parts']}")
        if live["host"]:
            pieces.append(f"asking {live['host']}")
        text = " · ".join(pieces)
        if live["note"]:
            text = f"{text}\n{live['note']}"
        self._set_status(text)

    def _finished(self, successful: bool, results: dict) -> None:
        context = self._context
        feedback = self._feedback
        results = results if isinstance(results, dict) else {}
        # The closing message needs the feedback: read it before
        # _reset_task_state() lets it go.
        failure, category = (
            ("", "") if successful else self._failure_sentence(feedback)
        )
        # Canceled: by this dock, from QGIS's task list (a "canceled" step),
        # or before the task started (a canceled feedback and no sentence).
        canceled = self._canceling or category == "canceled" or (
            not failure and feedback is not None and feedback.isCanceled()
        )
        added = []
        add_error = ""
        # Set again by _add_result_layers when this result cannot be saved.
        self._save_failure = ""
        try:
            if successful and context is not None:
                added = self._add_result_layers(results, context)
        except Exception as error:  # noqa: BLE001 - async Qt callback boundary
            successful = False
            add_error = f"{type(error).__name__}: {error}"
            QgsMessageLog.logMessage(
                "The downloaded data could not be added to the project.\n"
                + traceback.format_exc(),
                _LOG_TAG,
                Qgis.MessageLevel.Critical,
            )
        finally:
            self._reset_task_state()

        self.progress.setValue(100 if successful else 0)
        if add_error:
            self._last_failure_kind = "error"
            self._show_outcome(
                "The data arrived but could not be added to the project "
                f"({add_error}). Details: View > Panels > Log Messages > "
                f"{_LOG_TAG}.",
                Qgis.MessageLevel.Critical,
            )
        elif not successful and canceled:
            # Canceling is the user's own choice, not a failure to report.
            self._set_status(_CANCELED_MESSAGE)
        elif not successful:
            self._last_failure_kind = category
            sentence = failure or "The download stopped before any data arrived."
            self._show_outcome(
                f"{sentence} {_DETAILS_HINT}", Qgis.MessageLevel.Critical
            )
        else:
            self._show_delivered(results, added)

    def _show_delivered(self, results: dict, added: list) -> None:
        """The closing message of a download that delivered data.

        With parts missing it says so, never "Finished —": the entry joins
        Recent marked incomplete and Continue fetches only the missing parts;
        while the servers rest after the errors that left the gaps, it says
        that too and Continue waits for them. Under the message: a GeoPackage
        that could not be written, and the algorithm's NOTICE.
        """
        counts = ", ".join(added) if added else "no matching features"
        facts = self._result_facts(results)
        notice = "\n".join(
            line for line in (
                self._save_failure, str(results.get("NOTICE") or "").strip()
            ) if line
        )
        missing = _count(results.get("TILES_MISSING"))
        if not missing:
            self._last_failure_kind = ""
            if added:
                self._remember_download(counts)
            self._show_outcome(
                f"Finished — {counts}.{facts}",
                Qgis.MessageLevel.Warning if self._save_failure
                else Qgis.MessageLevel.Success,
                notice,
            )
            return
        total = max(missing, _count(results.get("TILES_TOTAL")))
        # Remembered even when the parts that arrived matched nothing: the
        # missing parts may hold data, and Continue re-runs this entry.
        entry = self._remember_download(counts, incomplete=True)
        self._last_failure_kind = "gaps"
        rest = self._server_rest()

        def gaps(action: str) -> str:
            return (
                f"Finished with gaps: {missing} of {total} parts are missing, so "
                f"some data is missing. {action} {counts[:1].upper()}{counts[1:]}."
                f"{facts}"
            )

        outcome = None
        if entry is None:
            text = gaps(
                f"{_SERVERS_RESTING}; run the same download again in about "
                f"{_wait_text(rest)} to fetch only those." if rest
                else "Run the same download again to fetch only those."
            )
        elif rest:
            text = gaps(
                f"{_SERVERS_RESTING}; Continue fetches only those once they "
                "are ready."
            )
            outcome = (text, gaps("Press Continue to fetch only those."), notice)
        else:
            text = gaps("Press Continue to fetch only those.")
        self._show_outcome(text, Qgis.MessageLevel.Warning, notice)
        self._offer_continue(entry, outcome)

    @staticmethod
    def _result_facts(results: dict) -> str:
        """The data date and the servers that delivered, then what was left out.

        FEATURES_OUTSIDE and FEATURES_DROPPED are read only when the
        algorithm reports them. A matched object outside the chosen area (a
        place's boundary, an around circle) was left out on purpose, so it is
        never counted among the objects that could not be built.
        """
        stamp = str(results.get("OSM_TIMESTAMP") or "").replace("T", " ").rstrip("Z")
        source = str(results.get("DATA_SOURCE") or "").strip()
        if source == "local cache":
            source = "the local cache"
        if stamp and source:
            facts = f" OSM data as of {stamp} (UTC) from {source}."
        elif stamp:
            facts = f" OSM data as of {stamp} (UTC)."
        elif source:
            facts = f" OSM data from {source}."
        else:
            facts = ""
        outside = _count(results.get("FEATURES_OUTSIDE"))
        if outside == 1:
            facts += (
                " 1 matched object was left out because it lies outside the "
                "chosen area."
            )
        elif outside:
            facts += (
                f" {outside:,} matched objects were left out because they lie "
                "outside the chosen area."
            )
        dropped = _count(results.get("FEATURES_DROPPED"))
        if dropped:
            noun = "object" if dropped == 1 else "objects"
            facts += (
                f" {dropped:,} {noun} could not be built (details in "
                f"{_PROCESSING_LOG})."
            )
        return facts

    @staticmethod
    def _failure_sentence(feedback) -> tuple:
        """(sentence, failure class) of a download that ended without data.

        The algorithm's "failed" step carries the exception's sentence and
        its class. QGIS also writes that sentence last into the task's log,
        which is the fallback when no step arrived. The sentence is "" when
        neither holds one.
        """
        failure = getattr(feedback, "failure", None) or {}
        sentence = str(failure.get("message") or failure.get("detail") or "")
        sentence = sentence.strip()
        if not sentence and feedback is not None:
            with contextlib.suppress(AttributeError, RuntimeError, TypeError):
                lines = str(feedback.textLog()).splitlines()
                sentence = next(
                    (line.strip() for line in reversed(lines) if line.strip()), ""
                )
        if sentence and sentence[-1] not in ".!?…":
            sentence = f"{sentence}."
        category = str(failure.get("category") or "").strip()
        if not _FAILURE_KIND_RE.match(category):
            category = "error"
        return sentence, category

    def _show_outcome(self, text: str, level, notice: str = "") -> None:
        """Show a closing message and its notice; out of sight, the message
        bar repeats both.

        A dock tabbed with Browser is easily covered during a long wait, so
        a result nobody can see in the dock goes to the QGIS message bar.
        """
        self._set_status(text)
        self._show_notice(notice)
        if self.iface is None or self._on_screen():
            return
        # Success fades after the user's message timeout; a failure or gaps
        # stay until dismissed, because they ask for a decision.
        duration = -1 if level == Qgis.MessageLevel.Success else 0
        message = " ".join([text, *notice.splitlines()])
        with contextlib.suppress(AttributeError, RuntimeError, TypeError):
            self.iface.messageBar().pushMessage(
                "02Agent OSM Downloader", message, level, duration
            )

    def _on_screen(self) -> bool:
        """True when the dock can be seen: shown, and not behind another tab.

        Measured on 3.44 and 4.2: a dock tabbed behind Browser still reports
        isVisible(), but its visible region is empty.
        """
        return self.isVisible() and not self.visibleRegion().isEmpty()

    def _offer_continue(
        self, entry: Optional[dict], gap_outcome: Optional[tuple] = None
    ) -> None:
        """Offer Continue for this Recent entry, or take it away (None).

        ``gap_outcome`` is (message while resting, message once ready,
        notice): the closing message is brought up to date when a server is
        ready again, unless something else has replaced it meanwhile.
        """
        self._continue_entry = entry
        self._gap_outcome = gap_outcome if entry is not None else None
        self._refresh_continue()

    def _refresh_continue(self) -> None:
        """Continue for the entry on offer: ready, counting down, or hidden.

        Continue right after the gaps used to do nothing at all: the servers
        that failed rest for 45 s to 2 min after the errors, and a download
        waits for a resting server only up to 30 s. So while every server a
        download would ask is resting, Continue stays disabled and a
        countdown stands in its place; checked once a second, it comes back
        as soon as one is ready.
        """
        entry = self._continue_entry
        rest = self._server_rest() if entry is not None else 0.0
        offered = entry is not None and not rest
        self.continue_button.setEnabled(offered)
        self.continue_button.setVisible(offered)
        self.continue_wait.setVisible(bool(rest))
        if rest:
            self.continue_wait.setText(_CONTINUE_WAIT.format(wait=_wait_text(rest)))
            _keep_text_height(self.continue_wait)
            if not self._continue_timer.isActive():
                self._continue_timer.start()
            return
        self._continue_timer.stop()
        outcome, self._gap_outcome = self._gap_outcome, None
        if offered and outcome is not None and self.status.text() == outcome[0]:
            # The message said the servers were resting; one is ready now.
            self._set_status(outcome[1])
            self._show_notice(outcome[2])

    def _server_rest(self) -> float:
        """Seconds until a server a download would ask is ready; 0 when one is.

        The network code answers (seconds_until_a_server_is_ready, for the
        Overpass hosts and, while the Postpass option is on, Postpass); a
        build without that answer never waits, as before.
        """
        from ..processing import osm_algorithms

        probe = getattr(osm_algorithms, "seconds_until_a_server_is_ready", None)
        if not callable(probe):
            return 0.0
        rest = 0.0
        # Asked once a second by a timer: a fault in the answer may cost the
        # countdown, never an error in the middle of QGIS.
        with contextlib.suppress(Exception):
            rest = float(probe(self.fallback_check.isChecked()))
        return rest if math.isfinite(rest) and rest > 0 else 0.0

    def _continue_download(self) -> None:
        """Run the download that finished with gaps again, unchanged.

        It is the stored request, sent through the Run again path, so the
        parts that arrived come from the cache (kept a day for an unfinished
        download) and only the missing parts are asked for.
        """
        if self._continue_entry is not None:
            self._rerun_entry(self._continue_entry)

    def _add_result_layers(
        self,
        results: dict,
        context: QgsProcessingContext,
    ) -> list[str]:
        project = QgsProject.instance()
        self._save_failure = ""
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
        source = str(results.get("DATA_SOURCE") or "").strip()
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
            apply_provenance(layer, stamp, self._current_label, source)
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
            # The data is downloaded; a file problem must not lose it. The
            # layers stay as temporary layers and the reason is shown under
            # the closing message, in words: the Python error text that used
            # to be shown goes to this plugin's log tab.
            try:
                layers = write_geopackage(styled, path, self._current_label, project)
                added.append(f"saved to {_breakable_path(path)}")
            except OSError as error:
                # A folder that cannot be created: a removed drive, a file
                # where a folder should be. It used to escape here and leave
                # an empty group with nothing in it.
                folder = os.path.dirname(os.path.abspath(path))
                QgsMessageLog.logMessage(
                    f"{_GPKG_FOLDER_FAILURE.format(folder=folder)} ({error})",
                    _LOG_TAG, Qgis.MessageLevel.Warning,
                )
                self._save_failure = _GPKG_FOLDER_FAILURE.format(
                    folder=_breakable_path(folder)
                )
            except RuntimeError as error:
                self._save_failure = _GPKG_WRITE_FAILURE.format(
                    path=_breakable_path(path)
                )
                QgsMessageLog.logMessage(
                    f"The GeoPackage {path} could not be written: {error}",
                    _LOG_TAG, Qgis.MessageLevel.Warning,
                )
        for layer in layers:
            project.addMapLayer(layer, False)
            group.addLayer(layer)
        return added

    def _save_mode(self) -> str:
        return str(self.save_mode_combo.currentData() or "temporary")

    def _output_note(self) -> str:
        if self._save_mode() == "gpkg":
            path = self.gpkg_widget.filePath().strip()
            # "Will save": nothing has been written yet when this is shown.
            return f"Will save to {path.split('/')[-1].split(chr(92))[-1]}" if path else "GeoPackage (choose a file)"
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

    def _fallback_changed(self, checked: bool) -> None:
        """Remember whether Postpass may answer when the servers are busy."""
        QgsSettings().setValue(_FALLBACK_SETTING, bool(checked))
        # Postpass may be ready while the Overpass servers rest, or not.
        if self._continue_entry is not None:
            self._refresh_continue()

    def _history(self) -> list:
        return download_history.loads(QgsSettings().value(_HISTORY_SETTING, "[]"))

    def _populate_recent(self) -> None:
        entries = self._history()
        self.recent_combo.clear()
        for entry in entries:
            row = self._recent_row(entry)
            tip = [str(entry.get("summary") or "")]
            if entry.get("incomplete"):
                # In front, where a narrow combo still shows it.
                row = f"(incomplete) {row}"
                tip.append(_INCOMPLETE_TIP)
            self.recent_combo.addItem(row, entry)
            self.recent_combo.setItemData(
                self.recent_combo.count() - 1,
                "\n".join(line for line in [row, *tip] if line),
                Qt.ItemDataRole.ToolTipRole,
            )
        if not entries:
            self.recent_combo.addItem("No downloads yet", None)
        self.recent_combo.setEnabled(bool(entries))
        self.rerun_button.setEnabled(bool(entries))

    def _recent_row(self, entry: dict) -> str:
        """One Recent line: what, where and when.

        The same datasets downloaded over two cities used to give two
        identical lines; the place name or the area's centre tells them apart.
        """
        label = str(entry.get("label") or "")
        when = str(entry.get("when") or "").replace("T", " ")[:16]
        where = str(entry.get("where") or "") or self._where_from_record(
            entry.get("extent") or {}
        )
        parts = [label]
        if where and where not in label:
            parts.append(where)
        parts.append(when)
        return "  ·  ".join(part for part in parts if part)

    @staticmethod
    def _where_from_record(record: dict) -> str:
        """The centre of a stored extent as "38.42° N, 27.13° E", or ""."""
        try:
            rectangle = QgsRectangle(
                float(record["xmin"]), float(record["ymin"]),
                float(record["xmax"]), float(record["ymax"]),
            )
            crs = QgsCoordinateReferenceSystem(str(record.get("crs") or "EPSG:4326"))
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            if crs.isValid() and crs != wgs84:
                rectangle = QgsCoordinateTransform(
                    crs, wgs84, QgsProject.instance()
                ).transformBoundingBox(rectangle)
        except (KeyError, TypeError, ValueError, QgsCsException, RuntimeError):
            return ""
        latitude = (rectangle.yMinimum() + rectangle.yMaximum()) / 2.0
        longitude = (rectangle.xMinimum() + rectangle.xMaximum()) / 2.0
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            return ""
        return (
            f"{abs(latitude):.2f}° {'N' if latitude >= 0 else 'S'}, "
            f"{abs(longitude):.2f}° {'E' if longitude >= 0 else 'W'}"
        )

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

    def _remember_download(
        self, summary: str, incomplete: bool = False
    ) -> Optional[dict]:
        """Put the request that just ended under Recent; the entry, or None.

        ``incomplete`` marks a download that finished with parts missing;
        a later complete run of the same request replaces the entry.
        """
        if self._pending_request is None:
            return None
        algorithm_id, parameters, extent = self._pending_request
        self._pending_request = None
        presets: list = []
        if algorithm_id in download_history.PRESET_ALGORITHMS:
            # PRESET indexes point into today's catalogue; the entry keeps
            # the ids, which survive a catalogue change.
            value = parameters.get("PRESET", [])
            enum_order = _preset_enum_order()
            try:
                presets = [
                    enum_order[int(index)]
                    for index in (value if isinstance(value, list) else [value])
                ]
            except (IndexError, TypeError, ValueError):
                return None
        try:
            record = self._extent_record(extent)
            entry = download_history.make_entry(
                self._current_label, algorithm_id, parameters, record,
                datetime.now().isoformat(timespec="seconds"),
                summary, self._current_style_scope, presets,
                self._current_where or self._where_from_record(record),
                incomplete=incomplete,
            )
        except (ValueError, QgsCsException, RuntimeError):
            return None
        entries = download_history.add(self._history(), entry)
        QgsSettings().setValue(_HISTORY_SETTING, download_history.dumps(entries))
        self._populate_recent()
        return entry

    def _rerun_recent(self) -> None:
        """Run the chosen Recent entry again.

        An entry marked incomplete runs exactly as Continue does: the parts
        that arrived come from the cache, only the missing ones are fetched.
        """
        if self._task is not None:
            return
        entry = self.recent_combo.currentData()
        if not isinstance(entry, dict):
            return
        self._rerun_entry(entry)

    def _rerun_entry(self, entry: dict) -> None:
        """Send a remembered request again, exactly as it was stored."""
        if self._task is not None:
            return
        if self._refuse_without_geopackage():
            return
        try:
            parameters = download_history.request_parameters(
                entry, _preset_enum_order()
            )
        except ValueError:
            self._set_status(
                "This recent download names a dataset this version no longer "
                "has. Choose the datasets again and press Download."
            )
            return
        box = entry["extent"]
        extent = QgsReferencedRectangle(
            QgsRectangle(box["xmin"], box["ymin"], box["xmax"], box["ymax"]),
            QgsCoordinateReferenceSystem(box["crs"]),
        )
        self._current_label = entry["label"]
        self._current_style_scope = entry.get("style_scope", "")
        self._current_where = entry.get("where", "")
        self._submit(entry["algorithm"], parameters, extent)

    @staticmethod
    def _style_scope(group_ids) -> str:
        """How a run's layers are styled: "places", "osm_as_is" or "".

        "" means the Map style chosen in the dock.
        """
        groups = set(group_ids)
        if "osm_as_is" in groups:
            return "osm_as_is"
        return "places" if groups == {"places"} else ""

    def current_map_theme(self) -> str:
        """Return the map style for the layers of the current download.

        OSM As-Is datasets arrive in the OSM As-Is style; everything else
        uses the style chosen in the dock. Ticking datasets never changes
        that choice, so earlier layers are never restyled behind the user's
        back.
        """
        if self._current_style_scope == "osm_as_is":
            return "osm_as_is"
        return self._chosen_map_theme()

    def _chosen_map_theme(self) -> str:
        """Return the persistent map style selected in the dock."""
        return str(self.map_theme_combo.currentData() or DEFAULT_MAP_THEME)

    def current_road_width_mode(self) -> str:
        """Return the selected road-width strategy."""
        value = str(self.road_width_combo.currentData() or "highway")
        return value if value in ROAD_WIDTH_MODES else "highway"

    def _map_theme_changed(self, *_args) -> None:
        """Refresh the palette preview and persist the user's selection."""
        if not hasattr(self, "map_theme_combo"):
            return
        theme_id = self._chosen_map_theme()
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
        theme_id = self._chosen_map_theme()
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

    def _open_manual(self) -> None:
        """Open the online manual in the user's browser."""
        _open_url(MANUAL_URL)

    def problem_report_url(self) -> str:
        """The prefilled issue form for this dock's last failure, if any."""
        return problem_report_url(getattr(self, "_last_failure_kind", ""))

    def _report_problem(self) -> None:
        """Open a GitHub issue form with versions and the last failure class."""
        _open_url(self.problem_report_url())

    @staticmethod
    def _style_layer(
        layer: QgsVectorLayer, theme_id: str, road_width_mode: str = "highway"
    ) -> None:
        apply_map_theme(layer, theme_id, road_width_mode)

    def _reset_task_state(self) -> None:
        self._task = None
        self._feedback = None
        self._context = None
        self._canceling = False
        self._live_timer.stop()
        if self.download_button.text() == _DOWNLOADING_TEXT:
            self.download_button.setText(self._idle_download_text or "Download")
        self.download_button.setEnabled(True)
        # A place search may still be running, and Cancel stops it.
        self.cancel_button.setEnabled(self._place_task is not None)
        # The button names the request on show again ("Run advanced query").
        self._refresh_run_summary()

    def _set_status(self, text: str) -> None:
        self.status.setText(str(text))
        # The footer caps the card's height; the tooltip always has it all.
        self.status.setToolTip(str(text))
        _keep_text_height(self.status)
        # A notice belongs to the closing message it was shown with.
        self._show_notice("")

    def _show_notice(self, notice: str) -> None:
        """The line under the status card, hidden when there is nothing to say."""
        self.status_notice.setText(notice)
        self.status_notice.setVisible(bool(notice))
        _keep_text_height(self.status_notice)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # A new width re-wraps the footer's text, so its floor moves too.
        for name in ("status", "status_notice", "continue_wait"):
            label = getattr(self, name, None)
            if label is not None:
                _keep_text_height(label)

    def cancel(self) -> None:
        if self._place_task is not None:
            with contextlib.suppress(Exception):
                self._place_task.cancel()
            self._place_task = None
        if self._task is not None:
            # Said before canceling: when the download stops at once, the
            # task can end and _finished show the outcome inside the calls
            # below (measured on 3.44 and 4.2). The live status keeps this
            # sentence until then.
            self._canceling = True
            self._set_status("Canceling download …")
        if self._feedback is not None:
            self._feedback.cancel()
        if self._task is not None:
            with contextlib.suppress(Exception):
                self._task.cancel()

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
        if not self._first_show_done:
            self._first_show_done = True
            self._start_at_place_search_on_empty_map()

    def _start_at_place_search_on_empty_map(self) -> None:
        """On first open over an empty map, start at the place search.

        An empty map has no area to download, so "Current map view" was a
        dead end for the commonest first step. Named place is selected and
        its box focused: type a town, press Enter, then Download.
        """
        if self.iface is None or self._extent_mode() != "map":
            return
        extent = None
        with contextlib.suppress(AttributeError, RuntimeError):
            extent = self.iface.mapCanvas().extent()
        if extent is None or not self._is_empty(extent):
            return
        self.extent_combo.setCurrentIndex(self.extent_combo.findData("place"))
        self.tabs.setCurrentWidget(self.download_tab)
        QTimer.singleShot(0, self._focus_place_box)

    def _focus_place_box(self) -> None:
        """Focus the place box and scroll it into view in a short dock."""
        self.place_edit.setFocus()
        self._root_scroll.ensureWidgetVisible(self.place_edit, 0, 40)

    def _apply_theme(self) -> None:
        root = self.widget()
        if root is None or getattr(self, "_theme_refreshing", False):
            return
        self._theme_refreshing = True
        try:
            apply_adaptive_theme(root)
        finally:
            self._theme_refreshing = False
