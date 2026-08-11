"""Semantic native-QGIS styling for 02Agent OSM result layers."""
from __future__ import annotations

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsRendererCategory,
    QgsWkbTypes,
)

from .map_styling_expressions import (
    BUILDING_EXPRESSION,
    LINE_EXPRESSION,
    POINT_EXPRESSION,
    POLYGON_EXPRESSION,
)
from .map_themes import map_theme


ROAD_WIDTH_MODES = ("highway", "uniform")

__all__ = [
    "BUILDING_EXPRESSION",
    "LINE_EXPRESSION",
    "POINT_EXPRESSION",
    "POLYGON_EXPRESSION",
    "ROAD_WIDTH_MODES",
    "apply_map_theme",
]


def _shade(color: str, factor: float) -> str:
    rgb = [int(color[index:index + 2], 16) for index in (1, 3, 5)]
    if factor <= 1:
        result = [round(value * factor) for value in rgb]
    else:
        amount = min(1.0, factor - 1.0)
        result = [round(value + (255 - value) * amount) for value in rgb]
    return "#%02x%02x%02x" % tuple(result)


def _fill(color: str, outline: str | None = None, opacity: float = 0.82):
    symbol = QgsFillSymbol.createSimple({
        "color": color,
        "outline_color": outline or _shade(color, 0.72),
        "outline_width": "0.18",
        "style": "solid",
    })
    symbol.setOpacity(opacity)
    return symbol


def _line(color: str, width: float, dashed: bool = False):
    properties = {
        "color": color, "width": str(width),
        "capstyle": "round", "joinstyle": "round",
    }
    if dashed:
        properties["line_style"] = "dash"
    return QgsLineSymbol.createSimple(properties)


def _marker(color: str, size: float = 2.4, shape: str = "circle"):
    return QgsMarkerSymbol.createSimple({
        "name": shape, "color": color, "size": str(size),
        "outline_color": _shade(color, 0.62), "outline_width": "0.25",
    })


def _renderer(expression: str, entries):
    categories = [
        QgsRendererCategory(value, symbol, label)
        for value, label, symbol in entries
    ]
    return QgsCategorizedSymbolRenderer(expression, categories)


def _polygon_renderer(theme: dict):
    buildings = theme["building_colors"]
    entries = []
    labels = {
        "residential": "Residential buildings",
        "commercial": "Commercial buildings",
        "industrial": "Industrial buildings",
        "civic": "Civic buildings",
        "worship": "Worship buildings",
        "other": "Other buildings",
    }
    for category, color in buildings.items():
        entries.append((f"building_{category}", labels[category], _fill(color)))
    entries.extend((
        ("water", "Water", _fill(theme["water"], opacity=0.76)),
        ("green", "Green space", _fill(theme["greens"], opacity=0.74)),
        ("parking", "Parking", _fill(_shade(theme["base"], 1.55), opacity=0.68)),
        ("pedestrian", "Pedestrian space", _fill(_shade(theme["base"], 1.78), opacity=0.66)),
        ("civic", "Civic and amenities", _fill(buildings["civic"])),
        ("other", "Other polygons", _fill(buildings["other"], opacity=0.66)),
    ))
    return _renderer(POLYGON_EXPRESSION, entries)


def _line_renderer(theme: dict, road_width_mode: str = "highway"):
    major = theme["roads_major"]
    minor = theme["roads_minor"]
    widths = {
        "major": 1.55,
        "primary": 1.28,
        "secondary": 1.05,
        "tertiary": 0.82,
        "residential": 0.66,
        "service": 0.48,
    }
    if road_width_mode != "highway":
        widths.update({category: 0.82 for category in widths})
    entries = (
        ("water", "Waterways", _line(theme["water"], 1.15)),
        ("rail", "Railways", _line(theme["base"], 0.95, True)),
        ("transit", "Transit routes", _line(theme["roads_major"], 0.72, True)),
        ("major", "Motorway and trunk", _line(major, widths["major"])),
        ("primary", "Primary roads", _line(major, widths["primary"])),
        ("secondary", "Secondary roads", _line(major, widths["secondary"])),
        ("tertiary", "Tertiary roads", _line(major, widths["tertiary"])),
        ("residential", "Residential roads", _line(minor, widths["residential"])),
        ("service", "Service roads", _line(minor, widths["service"])),
        ("active", "Walking and cycling", _line(theme["greens"], 0.55, True)),
        ("other", "Other lines", _line(theme["base"], 0.58)),
    )
    return _renderer(LINE_EXPRESSION, entries)


def _point_renderer(theme: dict):
    buildings = theme["building_colors"]
    entries = (
        ("tree", "Trees", _marker(theme["trees"], 2.2)),
        ("transport", "Public transport", _marker(theme["roads_major"], 2.8, "square")),
        ("health", "Healthcare", _marker("#d1495b", 2.8, "cross")),
        ("education", "Education", _marker(buildings["civic"], 2.7, "diamond")),
        ("worship", "Worship", _marker(buildings["worship"], 2.8, "triangle")),
        ("emergency", "Emergency", _marker("#c1121f", 2.9, "cross")),
        ("tourism", "Tourism", _marker(buildings["commercial"], 2.6, "star")),
        ("sport", "Sport", _marker(theme["greens"], 2.6)),
        ("other", "Other points", _marker(theme["base"], 2.3)),
    )
    return _renderer(POINT_EXPRESSION, entries)


def apply_map_theme(
    layer, theme_id: str, road_width_mode: str = "highway"
) -> str:
    """Apply a semantic renderer and return the resolved theme id."""
    from .map_themes import DEFAULT_MAP_THEME, MAP_THEMES

    resolved_id = theme_id if theme_id in MAP_THEMES else DEFAULT_MAP_THEME
    theme = map_theme(resolved_id)
    geometry = layer.geometryType()
    if geometry == QgsWkbTypes.GeometryType.PolygonGeometry:
        renderer = _polygon_renderer(theme)
    elif geometry == QgsWkbTypes.GeometryType.LineGeometry:
        renderer = _line_renderer(
            theme,
            road_width_mode if road_width_mode in ROAD_WIDTH_MODES else "highway",
        )
    elif geometry == QgsWkbTypes.GeometryType.PointGeometry:
        renderer = _point_renderer(theme)
    else:
        return resolved_id
    layer.setRenderer(renderer)
    layer.setCustomProperty("zero2agent/map_theme", resolved_id)
    layer.setCustomProperty(
        "zero2agent/road_width_mode",
        road_width_mode if road_width_mode in ROAD_WIDTH_MODES else "highway",
    )
    layer.triggerRepaint()
    return resolved_id
