"""Render deterministic light/dark dock previews for visual QA."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import QgsApplication, QgsSettings


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


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "windows")
    output = Path(sys.argv[1]).resolve()
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

    try:
        for theme, dark in (("light", False), ("dark", True)):
            QApplication.setPalette(_palette(dark))
            for tab_name in ("download", "query", "query_expanded"):
                dock = AgentOsmDock(None)
                dock.setPalette(QApplication.palette())
                dock.widget().setPalette(QApplication.palette())
                dock.resize(440, 900)
                if tab_name.startswith("query"):
                    dock.tabs.setCurrentWidget(dock.query_tab)
                if tab_name == "query_expanded":
                    dock.query_preview_button.setChecked(True)
                dock.show()
                app.processEvents()
                target = output / f"{theme}_{tab_name}.png"
                if not dock.grab().save(str(target), "PNG"):
                    raise RuntimeError(f"Could not save {target}")
                print(target)
                dock.close()
                dock.deleteLater()
                app.processEvents()
    finally:
        app.exitQgis()
        profile.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
