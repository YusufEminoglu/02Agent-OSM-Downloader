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


#: WCAG AA for normal text.
MIN_TEXT_CONTRAST = 4.5


#: Text colours to fall back on, dark first.
_FALLBACK_TEXT = ("#102019", "#FFFFFF", "#000000")


def _readable(color: QColor, *backgrounds: QColor) -> QColor:
    """``color`` if it reads on every background, else a contrasting text colour.

    A QGIS theme can pair its text and window colours badly for this dock:
    under Blend of Gray the palette's text is near-black on a near-black
    surface (1.03:1). Text that fails AA on any background is replaced by the
    fallback colour whose weakest contrast over those backgrounds is best.
    """
    if all(contrast_ratio(color, item) >= MIN_TEXT_CONTRAST for item in backgrounds):
        return QColor(color)
    return max(
        (QColor(name) for name in _FALLBACK_TEXT),
        key=lambda candidate: min(contrast_ratio(candidate, item) for item in backgrounds),
    )


def _muted(text: QColor, amount: float, *backgrounds: QColor) -> QColor:
    """Text mixed toward the first background, but never below AA contrast."""
    while amount > 0.0:
        candidate = _mix(text, backgrounds[0], amount)
        if all(contrast_ratio(candidate, item) >= MIN_TEXT_CONTRAST for item in backgrounds):
            return candidate
        amount = round(amount - 0.05, 2)
    return QColor(text)


def _ink(color: QColor, toward: QColor, *backgrounds: QColor) -> QColor:
    """``color`` moved toward ``toward`` until it reads on every background.

    The accent green doubles as a text colour: links, the header eyebrow,
    the count pill, the quiet buttons. As it stands it fell short of AA on
    some of the surfaces it is drawn on (the count pill measured 4.02:1 in
    the light theme, 3.57:1 in QGIS 4's dark default). Mixing it toward the
    text colour darkens it on light themes and lightens it on dark ones,
    one small step at a time, so it stays as green as it can.
    """
    amount = 0.0
    while amount <= 1.0:
        candidate = _mix(color, toward, amount)
        if all(contrast_ratio(candidate, item) >= MIN_TEXT_CONTRAST for item in backgrounds):
            return candidate
        amount = round(amount + 0.05, 2)
    return _readable(toward, *backgrounds)


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
    status = _mix(accent, surface, 0.76 if dark else 0.86)
    # Text on every surface it is drawn on: the dock background, the cards,
    # the header, the status card and the tab highlight. The palette's own
    # text colour is kept whenever it reads; muted and input text follow.
    text = _readable(text, surface, card, hero, status, tab_active)
    border = _mix(text, surface, 0.76 if dark else 0.84)
    subtle = _muted(text, 0.40 if dark else 0.37, surface, card, hero)
    code_surface = _mix(input_surface, surface, 0.16 if dark else 0.08)
    if contrast_ratio(input_text, input_surface) < MIN_TEXT_CONTRAST:
        input_text = _readable(text, input_surface)
    if contrast_ratio(button_text, button) < MIN_TEXT_CONTRAST:
        button_text = _readable(text, button)
    selection = highlight if highlight.isValid() else accent
    # The accent as text, on every surface it is drawn on as text. The plain
    # accent stays for fills and borders, under its own contrasting text.
    accent_ink = _ink(accent, text, surface, card, hero, status, tab_active)
    accent_hover_ink = _ink(accent_hover, text, surface, card, hero, status, tab_active)
    # A plain button under the mouse: the accent on the button's own colour,
    # which a theme can make light while the dock's surfaces are dark.
    button_hover_text = _ink(accent, button_text, button)

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
        "accent_ink": _hex(accent_ink),
        "accent_hover_ink": _hex(accent_hover_ink),
        "button_hover_text": _hex(button_hover_text),
        "status": _hex(status),
        "selection": _hex(selection),
    }


def dock_stylesheet(palette: QPalette | None = None) -> str:
    """Build an airy stylesheet from the current application palette."""
    values = dock_color_tokens(palette)
    return """
QWidget#agentOsmShell, QWidget#agentOsmRoot {
    background: %(surface)s;
    color: %(text)s;
}
QLabel, QCheckBox {
    color: %(text)s;
}
QFrame#agentOsmFooter {
    background: %(surface)s;
    border: none;
    border-top: 1px solid %(border)s;
}
QFrame#heroPanel {
    background: %(hero)s;
    border: 1px solid %(border)s;
    border-radius: 9px;
}
QLabel#heroMark {
    background: %(accent)s;
    color: %(accent_text)s;
    border: 1px solid %(accent)s;
    border-radius: 10px;
    font-size: 12pt;
    font-weight: 800;
}
QLabel#heroEyebrow {
    color: %(accent_ink)s;
    font-size: 8pt;
    font-weight: 700;
}
QLabel#heroTitle {
    color: %(text)s;
    font-size: 16pt;
    font-weight: 700;
}
QPushButton#mapFeaturesButton {
    background: transparent;
    color: %(accent_ink)s;
    border: 1px solid %(border)s;
    border-radius: 6px;
    padding: 4px 7px;
    font-size: 7pt;
    font-weight: 600;
}
QPushButton#mapFeaturesButton:hover {
    background: %(tab_active)s;
    border-color: %(accent)s;
}
QPushButton#linkButton {
    background: transparent;
    color: %(accent_ink)s;
    border: none;
    padding: 1px 2px;
    min-height: 0px;
    font-size: 8pt;
}
QPushButton#linkButton:hover {
    color: %(accent_hover_ink)s;
    text-decoration: underline;
}
QPushButton#linkButton:focus {
    border: 1px dotted %(accent)s;
}
QLabel#mutedText, QLabel#descriptionText, QLabel#connectionDetail {
    color: %(subtle)s;
}
QLabel#countPill {
    color: %(accent_ink)s;
    background: %(status)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
    padding: 3px 7px;
    font-weight: 650;
}
QLabel#endpointPill {
    color: %(accent_ink)s;
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
    padding: 6px 8px;
    margin: 2px 1px 3px 1px;
    min-width: 48px;
}
QTabBar::tab:selected {
    color: %(text)s;
    background: %(tab_active)s;
    border-bottom-color: %(accent)s;
    font-weight: 600;
}
QTabBar::tab:hover { color: %(accent_ink)s; }
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
    color: %(text)s;
    font-weight: 600;
}
QFrame#customTagPanel {
    background: %(card)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
}
QFrame#customTagPanel[active="true"] {
    border-color: %(accent)s;
    background: %(tab_active)s;
}
QFrame#customTagFields {
    background: transparent;
    border: none;
}
QPushButton#customToggleButton {
    background: transparent;
    color: %(text)s;
    border: none;
    border-radius: 5px;
    padding: 5px 7px;
    text-align: left;
    font-weight: 700;
}
QPushButton#customToggleButton:hover {
    background: %(status)s;
    color: %(accent_ink)s;
}
QPushButton#customToggleButton:checked {
    color: %(accent_ink)s;
}
QLabel#customTagBadge {
    color: %(accent_ink)s;
    background: %(status)s;
    border: 1px solid %(border)s;
    border-radius: 7px;
    padding: 3px 7px;
    font-size: 7pt;
    font-weight: 650;
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
QListWidget#datasetList {
    background: %(input_surface)s;
    color: %(input_text)s;
    border: 1px solid %(border)s;
    border-radius: 6px;
    padding: 3px;
}
QListWidget#datasetList::item {
    padding: 5px 4px;
    border-radius: 4px;
}
QListWidget#datasetList::item:hover {
    background: %(tab_active)s;
}
QListWidget#datasetList::item:selected {
    background: %(selection)s;
    color: %(accent_text)s;
}
QCheckBox {
    spacing: 6px;
}
QPushButton {
    background: %(button)s;
    color: %(button_text)s;
    border: 1px solid %(border)s;
    border-radius: 6px;
    padding: 7px 11px;
    min-height: 17px;
}
QPushButton:hover { border-color: %(accent)s; color: %(button_hover_text)s; }
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
QPushButton#primaryButton:disabled {
    background: %(tab_active)s;
    color: %(disabled)s;
    border-color: %(border)s;
    font-weight: 400;
}
QPushButton#quietButton {
    background: transparent;
    color: %(accent_ink)s;
    border-color: %(border)s;
    padding: 4px 9px;
}
QPushButton#quietButton:hover {
    background: %(tab_active)s;
    border-color: %(accent)s;
}
QPushButton#quietButton:disabled {
    color: %(disabled)s;
    border-color: %(border)s;
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
QLabel#statusNotice {
    padding: 0px 9px 0px 12px;
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
