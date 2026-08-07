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


def _relative_luminance(color: QColor) -> float:
    channels = []
    for value in (color.redF(), color.greenF(), color.blueF()):
        channels.append(
            value / 12.92
            if value <= 0.04045
            else ((value + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(first: QColor, second: QColor) -> float:
    """Return the WCAG relative-luminance contrast ratio."""
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def dock_color_tokens(palette: QPalette | None = None) -> dict[str, str]:
    """Return the palette-derived colors used by the dock stylesheet."""
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

    white = QColor("#FFFFFF")
    accent = QColor("#62CFA0" if dark else "#2F7D5B")
    accent_hover = QColor("#76D9AF" if dark else "#256849")
    accent_text = _contrast_text(accent)
    surface = (
        _mix(window, white, 0.07)
        if dark
        else _mix(window, base, 0.72)
    )
    card = _mix(window, white, 0.12) if dark else base
    input_surface = _mix(base, white, 0.04) if dark else base
    hero = _mix(surface, accent, 0.13 if dark else 0.09)
    tab_active = _mix(surface, accent, 0.20 if dark else 0.12)
    border = _mix(text, surface, 0.76 if dark else 0.84)
    subtle = _mix(text, surface, 0.40 if dark else 0.37)
    status = _mix(accent, surface, 0.76 if dark else 0.86)
    code_surface = _mix(input_surface, surface, 0.16 if dark else 0.08)
    if contrast_ratio(input_text, input_surface) < 4.5:
        input_text = QColor(text)
    if contrast_ratio(button_text, button) < 4.5:
        button_text = QColor(text)
    selection = highlight if highlight.isValid() else accent

    return {
        "surface": _hex(surface),
        "card": _hex(card),
        "input_surface": _hex(input_surface),
        "hero": _hex(hero),
        "tab_active": _hex(tab_active),
        "code_surface": _hex(code_surface),
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


def dock_stylesheet(palette: QPalette | None = None) -> str:
    """Build an airy stylesheet from the current application palette."""
    values = dock_color_tokens(palette)
    return """
QWidget#agentOsmRoot {
    background: %(surface)s;
    color: %(text)s;
}
QFrame#heroPanel {
    background: %(hero)s;
    border: 1px solid %(border)s;
    border-radius: 9px;
}
QLabel#heroEyebrow {
    color: %(accent)s;
    font-size: 8pt;
    font-weight: 700;
}
QLabel#heroTitle {
    color: %(text)s;
    font-size: 15pt;
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
    background: %(surface)s;
}
QTabBar::tab {
    background: transparent;
    color: %(subtle)s;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 6px;
    padding: 7px 10px;
    margin: 2px 2px 3px 2px;
    min-width: 64px;
}
QTabBar::tab:selected {
    color: %(text)s;
    background: %(tab_active)s;
    border-bottom-color: %(accent)s;
    font-weight: 600;
}
QTabBar::tab:hover { color: %(accent)s; }
QGroupBox {
    background: %(card)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    margin-top: 10px;
    padding-top: 9px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 9px;
    padding: 0 5px;
    font-weight: 600;
}
QLineEdit, QPlainTextEdit, QComboBox {
    background: %(input_surface)s;
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
QPushButton#quietButton {
    background: transparent;
    color: %(accent)s;
    border-color: %(border)s;
    padding: 4px 9px;
}
QPushButton#quietButton:hover {
    background: %(tab_active)s;
    border-color: %(accent)s;
}
QLabel#runSummary {
    color: %(text)s;
    background: %(tab_active)s;
    border-radius: 5px;
    padding: 6px 8px;
    font-weight: 600;
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
    background: %(tab_active)s;
    border-radius: 3px;
    min-height: 6px;
    max-height: 6px;
}
QProgressBar::chunk { background: %(accent)s; border-radius: 3px; }
QPlainTextEdit#queryPreview {
    background: %(code_surface)s;
    font-family: Consolas, "Courier New", monospace;
    font-size: 9pt;
}
QScrollArea, QScrollArea > QWidget > QWidget {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: %(border)s;
    border-radius: 4px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover { background: %(accent)s; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QToolTip {
    background: %(card)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    padding: 4px;
}
""" % values


def apply_adaptive_theme(widget: QWidget) -> None:
    """Apply the active QGIS/Qt palette without overriding font preferences."""
    widget.setStyleSheet(dock_stylesheet(widget.palette()))
