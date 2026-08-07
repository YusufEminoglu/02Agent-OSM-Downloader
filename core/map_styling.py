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

from .map_themes import map_theme


BUILDING_EXPRESSION = (
    "CASE"
    " WHEN lower(coalesce(\"building\",'')) IN ('apartments','residential','house',"
    "'detached','terrace','dormitory','bungalow','semidetached_house','hut')"
    " THEN 'building_residential'"
    " WHEN lower(coalesce(\"building\",'')) IN ('commercial','retail','office',"
    "'supermarket','kiosk','hotel') THEN 'building_commercial'"
    " WHEN lower(coalesce(\"building\",'')) IN ('industrial','warehouse',"
    "'manufacture','hangar','factory') THEN 'building_industrial'"
    " WHEN lower(coalesce(\"building\",'')) IN ('church','mosque','temple',"
    "'synagogue','cathedral','chapel') THEN 'building_worship'"
    " WHEN coalesce(\"building\",'') <> '' THEN 'building_civic'"
    " ELSE '' END"
)

POLYGON_EXPRESSION = (
    "CASE"
    f" WHEN ({BUILDING_EXPRESSION}) <> '' THEN ({BUILDING_EXPRESSION})"
    " WHEN lower(coalesce(\"natural\",'')) IN ('water','bay','wetland')"
    "   OR lower(coalesce(\"query_key\",'')) IN ('water','waterway') THEN 'water'"
    " WHEN lower(coalesce(\"amenity\",'')) = 'parking'"
    "   OR lower(coalesce(\"landuse\",'')) = 'parking' THEN 'parking'"
    " WHEN lower(coalesce(\"highway\",'')) IN ('pedestrian','footway','living_street')"
    "   OR lower(coalesce(\"amenity\",'')) = 'marketplace' THEN 'pedestrian'"
    " WHEN lower(coalesce(\"leisure\",'')) IN ('park','garden','pitch','playground',"
    "'dog_park','golf_course') OR lower(coalesce(\"landuse\",'')) IN ('forest',"
    "'grass','recreation_ground','meadow','village_green','orchard','vineyard',"
    "'farmland','allotments','greenfield','cemetery')"
    "   OR lower(coalesce(\"natural\",'')) IN ('wood','scrub','grassland','heath')"
    " THEN 'green'"
    " WHEN lower(coalesce(\"amenity\",'')) <> '' THEN 'civic'"
    " ELSE 'other' END"
)

LINE_EXPRESSION = (
    "CASE"
    " WHEN lower(coalesce(\"query_key\",'')) IN ('water','waterway')"
    "   OR lower(coalesce(\"natural\",'')) = 'water' THEN 'water'"
    " WHEN coalesce(\"railway\",'') <> '' THEN 'rail'"
    " WHEN lower(coalesce(\"highway\",'')) IN ('motorway','trunk','motorway_link',"
    "'trunk_link') THEN 'major'"
    " WHEN lower(coalesce(\"highway\",'')) IN ('primary','primary_link') THEN 'primary'"
    " WHEN lower(coalesce(\"highway\",'')) IN ('secondary','secondary_link') THEN 'secondary'"
    " WHEN lower(coalesce(\"highway\",'')) IN ('tertiary','tertiary_link') THEN 'tertiary'"
    " WHEN lower(coalesce(\"highway\",'')) IN ('residential','unclassified',"
    "'living_street','road') THEN 'residential'"
    " WHEN lower(coalesce(\"highway\",'')) IN ('service','track') THEN 'service'"
    " WHEN lower(coalesce(\"highway\",'')) IN ('footway','path','pedestrian','steps',"
    "'corridor','bridleway','cycleway') THEN 'active'"
    " ELSE 'other' END"
)

POINT_EXPRESSION = (
    "CASE"
    " WHEN lower(coalesce(\"natural\",'')) = 'tree' THEN 'tree'"
    " WHEN coalesce(\"public_transport\",'') <> '' OR lower(coalesce(\"railway\",''))"
    "   IN ('station','halt','tram_stop') THEN 'transport'"
    " WHEN lower(coalesce(\"amenity\",'')) IN ('hospital','clinic','doctors',"
    "'pharmacy','dentist') THEN 'health'"
    " WHEN lower(coalesce(\"amenity\",'')) IN ('school','university','college',"
    "'kindergarten','library') THEN 'education'"
    " WHEN lower(coalesce(\"amenity\",'')) IN ('place_of_worship') THEN 'worship'"
    " WHEN lower(coalesce(\"amenity\",'')) IN ('fire_station','police',"
    "'ambulance_station') THEN 'emergency'"
    " WHEN coalesce(\"tourism\",'') <> '' THEN 'tourism'"
    " WHEN coalesce(\"sport\",'') <> '' THEN 'sport'"
    " ELSE 'other' END"
)


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


def _line_renderer(theme: dict):
    major = theme["roads_major"]
    minor = theme["roads_minor"]
    entries = (
        ("water", "Waterways", _line(theme["water"], 1.15)),
        ("rail", "Railways", _line(theme["base"], 0.95, True)),
        ("major", "Motorway and trunk", _line(major, 1.55)),
        ("primary", "Primary roads", _line(major, 1.28)),
        ("secondary", "Secondary roads", _line(major, 1.05)),
        ("tertiary", "Tertiary roads", _line(major, 0.82)),
        ("residential", "Residential roads", _line(minor, 0.66)),
        ("service", "Service roads", _line(minor, 0.48)),
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


def apply_map_theme(layer, theme_id: str) -> str:
    """Apply a semantic renderer and return the resolved theme id."""
    from .map_themes import DEFAULT_MAP_THEME, MAP_THEMES

    resolved_id = theme_id if theme_id in MAP_THEMES else DEFAULT_MAP_THEME
    theme = map_theme(resolved_id)
    geometry = layer.geometryType()
    if geometry == QgsWkbTypes.GeometryType.PolygonGeometry:
        renderer = _polygon_renderer(theme)
    elif geometry == QgsWkbTypes.GeometryType.LineGeometry:
        renderer = _line_renderer(theme)
    elif geometry == QgsWkbTypes.GeometryType.PointGeometry:
        renderer = _point_renderer(theme)
    else:
        return resolved_id
    layer.setRenderer(renderer)
    layer.setCustomProperty("zero2agent/map_theme", resolved_id)
    layer.triggerRepaint()
    return resolved_id
