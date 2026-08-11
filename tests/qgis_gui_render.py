"""Render real dock screenshots for visual QA and the online manual.

    python-qgis-ltr.bat -m zero2agent_osm_downloader.tests.qgis_gui_render <out>

Every image is a genuine grab of the real widget, never a mock-up. The place
panel is filled by actually calling the geocoder, so the manual shows what the
plugin really returns; pass --offline to skip those frames instead of drawing
invented results.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import QgsApplication, QgsProcessingFeedback, QgsSettings

# Each frame: file suffix, tab attribute, and a setup callback name.
FRAMES = (
    ("presets", "download_tab", ""),
    ("place", "download_tab", "place"),
    ("tiled", "download_tab", "tiled"),
    ("query", "query_tab", ""),
    ("query_expanded", "query_tab", "expand"),
    ("command", "command_tab", "command"),
    ("agent", "connections_tab", ""),
)
FRAME_HEIGHTS = {"query_expanded": 1180, "command": 620, "agent": 560}
DEMO_PLACE = "Konak, Izmir, Turkey"
# Large enough that its rectangle has to be split into several requests.
DEMO_WIDE_PLACE = "Amsterdam, Netherlands"


def _palette(dark: bool) -> QPalette:
    palette = QPalette()
    colors = (
        {
            "window": "#252A31",
            "base": "#171B20",
            "text": "#EEF2F5",
            "button": "#303740",
            "button_text": "#EEF2F5",
            "highlight": "#4DAD7A",
            "disabled": "#89929C",
        }
        if dark
        else {
            "window": "#F3F6F4",
            "base": "#FFFFFF",
            "text": "#1C2A24",
            "button": "#F8FAF9",
            "button_text": "#1C2A24",
            "highlight": "#2F7D5B",
            "disabled": "#7A847F",
        }
    )
    palette.setColor(QPalette.ColorRole.Window, QColor(colors["window"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(colors["base"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(colors["window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(colors["button"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(colors["button_text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(colors["highlight"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.WindowText,
        QColor(colors["disabled"]),
    )
    return palette


def _real_place_candidates(place: str):
    """Resolve a demo place for real, so the screenshot shows real data."""
    from zero2agent_osm_downloader.processing.osm_algorithms import resolve_place

    return resolve_place(place, QgsProcessingFeedback())


def _fill_place_panel(dock, candidates, place: str, clip: str = "boundary") -> None:
    from qgis.PyQt.QtCore import Qt

    dock.extent_combo.setCurrentIndex(dock.extent_combo.findData("place"))
    dock.place_clip_combo.setCurrentIndex(
        dock.place_clip_combo.findData(clip)
    )
    dock.place_edit.setText(place)
    dock.place_combo.blockSignals(True)
    dock.place_combo.clear()
    for candidate in candidates:
        label = candidate.label
        if candidate.admin_level:
            label = f"{label}  ·  level {candidate.admin_level}"
        elif candidate.kind:
            label = f"{label}  ·  {candidate.kind}"
        dock.place_combo.addItem(label, candidate)
    dock.place_combo.setEnabled(True)
    dock.place_combo.blockSignals(False)
    dock._place_candidate = candidates[0]
    dock._place_name = candidates[0].name
    dock._refresh_run_summary()
    dock._refresh_extent_note()
    for index in range(dock.dataset_list.count()):
        dock.dataset_list.item(index).setCheckState(Qt.CheckState.Checked)


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "windows")
    arguments = [item for item in sys.argv[1:] if not item.startswith("--")]
    offline = "--offline" in sys.argv
    output = Path(arguments[0] if arguments else "gui_render").resolve()
    output.mkdir(parents=True, exist_ok=True)
    plugin_dir = Path(__file__).resolve().parent.parent
    if str(plugin_dir.parent) not in sys.path:
        sys.path.insert(0, str(plugin_dir.parent))

    profile = tempfile.TemporaryDirectory(prefix="zero2agent-gui-")
    app = QgsApplication([], False, profile.name, "external")
    app.initQgis()
    QgsSettings().setValue("zero2agent_osm_downloader/map_theme", "atlas")
    QApplication.setStyle("Fusion")
    from zero2agent_osm_downloader.dialogs.dock import AgentOsmDock

    candidates = ()
    wide_candidates = ()
    if not offline:
        for place, target in (
            (DEMO_PLACE, "candidates"),
            (DEMO_WIDE_PLACE, "wide_candidates"),
        ):
            resolved = _real_place_candidates(place)
            if not resolved:
                raise RuntimeError(
                    f"The geocoder returned no result for {place!r}; re-run "
                    "with --offline to skip the place frames rather than "
                    "documenting invented results."
                )
            print(f"resolved {len(resolved)} real candidates for {place}")
            if target == "candidates":
                candidates = resolved
            else:
                wide_candidates = resolved

    written = []
    try:
        for theme, dark in (("light", False), ("dark", True)):
            QApplication.setPalette(_palette(dark))
            for name, tab_attribute, setup in FRAMES:
                if setup == "place" and not candidates:
                    continue
                if setup == "tiled" and not wide_candidates:
                    continue
                dock = AgentOsmDock(None)
                dock.setPalette(QApplication.palette())
                dock.widget().setPalette(QApplication.palette())
                # The query preview needs room or the panel it expands into is
                # scrolled out of the grab; the short tabs would otherwise be
                # mostly empty space.
                dock.resize(440, FRAME_HEIGHTS.get(name, 940))
                dock.tabs.setCurrentWidget(getattr(dock, tab_attribute))
                if setup == "place":
                    _fill_place_panel(dock, candidates, DEMO_PLACE)
                elif setup == "tiled":
                    _fill_place_panel(
                        dock, wide_candidates, DEMO_WIDE_PLACE, "rectangle"
                    )
                elif setup == "expand":
                    dock.query_preview_button.setChecked(True)
                elif setup == "command":
                    dock.prompt.setPlainText(
                        "Download public transport in Tokyo, Japan"
                    )
                dock.show()
                app.processEvents()
                target = output / f"{theme}_{name}.png"
                if not dock.grab().save(str(target), "PNG"):
                    raise RuntimeError(f"Could not save {target}")
                written.append(target)
                print(target)
                dock.close()
                dock.deleteLater()
                app.processEvents()
    finally:
        app.exitQgis()
        profile.cleanup()
    print(f"{len(written)} screenshots written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
