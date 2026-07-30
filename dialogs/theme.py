"""Palette-aware styling for the downloader dock.

The dock deliberately derives every surface and text colour from the active Qt
palette.  That keeps it readable under QGIS light, dark, high-contrast, and
custom themes while retaining a restrained OpenStreetMap-inspired green accent.
"""
from __future__ import annotations

from qgis.PyQt.QtGui import QColor, QPalette
from qgis.PyQt.QtWidgets import QApplication, QWidget


def _hex(color: QColor) -> str:
    return color.name(QColor.NameFormat.HexRgb)


def _mix(first: QColor, second: QColor, amount: float) -> QColor:
    amount = max(0.0, min(1.0, float(amount)))
    return QColor(
        round(first.red() * (1.0 - amount) + second.red() * amount),
        round(first.green() * (1.0 - amount) + second.green() * amount),
        round(first.blue() * (1.0 - amount) + second.blue() * amount),
    )


def _contrast_text(background: QColor) -> QColor:
    luminance = (
        0.2126 * background.red()
        + 0.7152 * background.green()
        + 0.0722 * background.blue()
    )
    return QColor("#102019") if luminance >= 150 else QColor("#FFFFFF")


def dock_stylesheet(palette: QPalette | None = None) -> str:
    """Build a compact stylesheet from the current application palette."""
    active = palette or QApplication.palette()
    window = active.color(QPalette.ColorRole.Window)
    base = active.color(QPalette.ColorRole.Base)
    text = active.color(QPalette.ColorRole.WindowText)
    input_text = active.color(QPalette.ColorRole.Text)
    button = active.color(QPalette.ColorRole.Button)
    button_text = active.color(QPalette.ColorRole.ButtonText)
    highlight = active.color(QPalette.ColorRole.Highlight)
    disabled = active.color(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.WindowText,
    )
    dark = window.lightness() < 128

    accent = QColor("#55B98A" if dark else "#2F7D5B")
    accent_hover = QColor("#68C99C" if dark else "#256849")
    accent_text = _contrast_text(accent)
    border = _mix(text, window, 0.76 if dark else 0.82)
    panel = _mix(base, window, 0.34)
    subtle = _mix(text, window, 0.43 if dark else 0.50)
    status = _mix(accent, window, 0.84 if dark else 0.90)
    selection = highlight if highlight.isValid() else accent

    values = {
        "window": _hex(window),
        "base": _hex(base),
        "panel": _hex(panel),
        "text": _hex(text),
        "input_text": _hex(input_text),
        "button": _hex(button),
        "button_text": _hex(button_text),
        "border": _hex(border),
        "subtle": _hex(subtle),
        "disabled": _hex(disabled),
        "accent": _hex(accent),
        "accent_hover": _hex(accent_hover),
        "accent_text": _hex(accent_text),
        "status": _hex(status),
        "selection": _hex(selection),
    }
    return """
QWidget#agentOsmRoot {
    background: %(window)s;
    color: %(text)s;
}
QFrame#heroPanel {
    background: %(panel)s;
    border: 1px solid %(border)s;
    border-radius: 10px;
}
QLabel#heroEyebrow {
    color: %(accent)s;
    font-size: 8pt;
    font-weight: 700;
}
QLabel#heroTitle {
    color: %(text)s;
    font-size: 17pt;
    font-weight: 650;
}
QLabel#mutedText, QLabel#descriptionText, QLabel#connectionDetail {
    color: %(subtle)s;
}
QLabel#endpointPill {
    color: %(accent)s;
    background: %(status)s;
    border: 1px solid %(accent)s;
    border-radius: 9px;
    padding: 3px 8px;
    font-weight: 600;
}
QTabWidget::pane {
    border: 1px solid %(border)s;
    border-radius: 8px;
    top: -1px;
    background: %(window)s;
}
QTabBar::tab {
    background: transparent;
    color: %(subtle)s;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 8px 12px;
    min-width: 68px;
}
QTabBar::tab:selected {
    color: %(text)s;
    border-bottom-color: %(accent)s;
    font-weight: 600;
}
QTabBar::tab:hover { color: %(accent)s; }
QGroupBox {
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    margin-top: 10px;
    padding-top: 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 9px;
    padding: 0 5px;
    font-weight: 600;
}
QLineEdit, QPlainTextEdit, QComboBox {
    background: %(base)s;
    color: %(input_text)s;
    border: 1px solid %(border)s;
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: %(selection)s;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {
    border: 2px solid %(accent)s;
}
QPushButton {
    background: %(button)s;
    color: %(button_text)s;
    border: 1px solid %(border)s;
    border-radius: 6px;
    padding: 7px 11px;
}
QPushButton:hover { border-color: %(accent)s; color: %(accent)s; }
QPushButton:focus { border: 2px solid %(accent)s; }
QPushButton:disabled { color: %(disabled)s; }
QPushButton#primaryButton {
    background: %(accent)s;
    color: %(accent_text)s;
    border-color: %(accent)s;
    font-weight: 650;
}
QPushButton#primaryButton:hover {
    background: %(accent_hover)s;
    color: %(accent_text)s;
}
QLabel#statusCard {
    background: %(status)s;
    color: %(text)s;
    border-left: 3px solid %(accent)s;
    border-radius: 5px;
    padding: 7px 9px;
}
QProgressBar {
    border: none;
    background: %(panel)s;
    border-radius: 3px;
    min-height: 6px;
    max-height: 6px;
}
QProgressBar::chunk { background: %(accent)s; border-radius: 3px; }
QScrollArea { border: none; background: transparent; }
""" % values


def apply_adaptive_theme(widget: QWidget) -> None:
    """Apply the active QGIS/Qt palette without overriding font preferences."""
    widget.setStyleSheet(dock_stylesheet(widget.palette()))
