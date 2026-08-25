"""Semantic native-QGIS styling for 02Agent OSM result layers."""
from __future__ import annotations

import contextlib
import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsFillSymbol,
    QgsLineSymbol,
    QgsProperty,
    QgsSimpleLineSymbolLayer,
    QgsSvgMarkerSymbolLayer,
    QgsSymbolLayer,
    QgsMarkerLineSymbolLayer,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsRendererCategory,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)

from .map_styling_expressions import (
    ADMIN_LEVEL_STYLES,
    BUILDING_EXPRESSION,
    CARTO_LINE_EXPRESSION,
    CARTO_POINT_EXPRESSION,
    CARTO_POLYGON_EXPRESSION,
    LINE_EXPRESSION,
    POINT_EXPRESSION,
    PLACES_LINE_EXPRESSION,
    PLACES_POINT_EXPRESSION,
    PLACES_POLYGON_EXPRESSION,
    POLYGON_EXPRESSION,
)
from .map_themes import map_theme


ROAD_WIDTH_MODES = ("highway", "uniform")
__all__ = [
    "ADMIN_LEVEL_STYLES",
    "BUILDING_EXPRESSION",
    "LINE_EXPRESSION",
    "POINT_EXPRESSION",
    "PLACES_LINE_EXPRESSION",
    "PLACES_POINT_EXPRESSION",
    "PLACES_POLYGON_EXPRESSION",
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
    symbol = QgsLineSymbol.createSimple(properties)
    cap_enum = getattr(Qt, "PenCapStyle", None)
    join_enum = getattr(Qt, "PenJoinStyle", None)
    cap = getattr(cap_enum, "RoundCap", None) or getattr(Qt, "RoundCap", None)
    join = getattr(join_enum, "RoundJoin", None) or getattr(Qt, "RoundJoin", None)
    with contextlib.suppress(AttributeError, TypeError):
        layer = symbol.symbolLayer(0)
        if cap is not None:
            layer.setPenCapStyle(cap)
        if join is not None:
            layer.setPenJoinStyle(join)
    return symbol


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
    for level, (label, color, _width) in ADMIN_LEVEL_STYLES.items():
        entries.append(
            (
                f"admin_{level}",
                label,
                _fill(_shade(color, 1.55), outline=color, opacity=0.18),
            )
        )
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
        ("urban_residential", "Urban blocks",
         _fill(_shade(theme["base"], 1.62), opacity=0.55)),
        ("urban_retail", "Retail blocks",
         _fill(_shade(buildings["commercial"], 1.28), opacity=0.55)),
        ("urban_commercial", "Commercial blocks",
         _fill(_shade(buildings["commercial"], 1.45), opacity=0.55)),
        ("urban_industrial", "Industrial blocks",
         _fill(_shade(buildings["industrial"], 1.35), opacity=0.55)),
        ("water", "Water", _fill(theme["water"], opacity=0.76)),
        ("green", "Green space", _fill(theme["greens"], opacity=0.74)),
        ("parking", "Parking", _fill(_shade(theme["base"], 1.55), opacity=0.68)),
        ("pedestrian", "Pedestrian space", _fill(_shade(theme["base"], 1.78), opacity=0.66)),
        ("civic", "Civic and amenities", _fill(buildings["civic"])),
        ("other", "Other polygons", _fill(buildings["other"], opacity=0.66)),
    ))
    return _renderer(POLYGON_EXPRESSION, entries)


def _places_polygon_renderer(theme: dict):
    entries = []
    for level, (label, color, _width) in ADMIN_LEVEL_STYLES.items():
        entries.append(
            (
                f"admin_{level}",
                label,
                _fill(_shade(color, 1.62), outline=color, opacity=0.20),
            )
        )
    entries.extend((
        ("place_area", "Mapped place areas", _fill("#d6c3e5", "#7b5aa6", 0.24)),
        ("other", "Other place polygons", _fill(theme["base"], opacity=0.18)),
    ))
    return _renderer(PLACES_POLYGON_EXPRESSION, entries)


def _line_renderer(theme: dict, road_width_mode: str = "highway"):
    major = theme["roads_major"]
    minor = theme["roads_minor"]
    widths = {
        "major": 2.35,
        "primary": 1.85,
        "secondary": 1.45,
        "tertiary": 1.12,
        "residential": 0.86,
        "service": 0.62,
    }
    if road_width_mode != "highway":
        widths.update({category: 0.82 for category in widths})
    entries = tuple(
        (
            f"admin_{level}",
            label,
            _line(color, width, dashed=level not in {"2", "4", "6"}),
        )
        for level, (label, color, width) in ADMIN_LEVEL_STYLES.items()
    ) + (
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


def _places_line_renderer(theme: dict):
    entries = tuple(
        (
            f"admin_{level}",
            label,
            _line(color, width, dashed=level not in {"2", "4", "6", "8"}),
        )
        for level, (label, color, width) in ADMIN_LEVEL_STYLES.items()
    ) + (
        ("other", "Other place lines", _line(theme["base"], 0.55, True)),
    )
    return _renderer(PLACES_LINE_EXPRESSION, entries)


def _point_renderer(theme: dict):
    buildings = theme["building_colors"]
    entries = tuple(
        (
            f"admin_{level}",
            label.replace("boundaries", "centres"),
            _marker(color, 2.8 if level in {"2", "4", "6"} else 2.3, "square"),
        )
        for level, (label, color, _width) in ADMIN_LEVEL_STYLES.items()
    ) + (
        ("place", "Place centres", _marker("#7b5aa6", 2.6, "square")),
        ("tree", "Trees", _marker(theme["trees"], 2.2)),
        ("transport", "Public transport", _marker(theme["roads_major"], 2.8, "square")),
        ("health", "Healthcare", _marker("#d1495b", 2.8, "cross")),
        ("education", "Education", _marker(buildings["civic"], 2.7, "diamond")),
        ("worship", "Worship", _marker(buildings["worship"], 2.8, "triangle")),
        ("emergency", "Emergency", _marker("#c1121f", 2.9, "cross")),
        ("food", "Food and drink", _marker("#c77400", 2.5)),
        ("shop", "Shops", _marker("#ac39ac", 2.5, "square")),
        ("tourism", "Tourism", _marker(buildings["commercial"], 2.6, "star")),
        ("sport", "Sport", _marker(theme["greens"], 2.6)),
        ("crossing", "Pedestrian crossings", _crossing_marker()),
        ("junction", "Junction and traffic nodes", _marker(theme["base"], 1.3)),
        ("furniture", "Kerbs and barriers", _marker(theme["base"], 1.3, "square")),
        ("other", "Other points", _marker(theme["base"], 2.3)),
    )
    return _renderer(POINT_EXPRESSION, entries)


def _places_point_renderer(theme: dict):
    entries = tuple(
        (
            f"admin_{level}",
            label.replace("boundaries", "centres"),
            _marker(color, 3.1 if level in {"2", "4", "6"} else 2.5, "square"),
        )
        for level, (label, color, _width) in ADMIN_LEVEL_STYLES.items()
    ) + (
        ("place", "Place centres", _marker("#7b5aa6", 3.0, "square")),
        ("other", "Other place points", _marker(theme["base"], 2.3)),
    )
    return _renderer(PLACES_POINT_EXPRESSION, entries)


# --- Standard OpenStreetMap look -------------------------------------------
#
# The "OSM As-Is" theme is meant to read like openstreetmap.org, and the thing
# that makes that map look the way it does is not its colours -- it is road
# *casing*: every road is a dark outline with a lighter fill drawn on top of
# it.  A single-stroke line, whatever colour it is, reads as a diagram.
#
# Colours below are the standard carto values.
CARTO = {
    "bg": "#f2efe9",
    "building": ("#d9d0c9", "#c5b8ad"),
    "water": ("#aad3df", "#a0c8d8"),
    "green": ("#c8facc", "#b5e8b8"),
    "residential_area": ("#e0dfdf", "#d6d5d5"),
    "retail_area": ("#ffd6d1", "#f2c4bf"),
    "commercial_area": ("#f2dad9", "#e3c8c7"),
    "industrial_area": ("#ebdbe8", "#dcc9d8"),
    "pedestrian_area": ("#dddde8", "#c8c8d8"),
    "parking": ("#eeeeee", "#dcdcdc"),
    "civic_area": ("#f0f0d8", "#e2e2c4"),
}
#: OSM As-Is widens its whole visible stack from the plain analytical themes:
#: roads read 1.5x wider, so a residential street reads as a street next to
#: the carto colours instead of the hairline the analytical line weights use.
_CARTO_WIDTH_SCALE = 1.5
#: Labels grow further still -- a name at road width looks small once the
#: carriageway underneath it is already 1.5x as wide.
_CARTO_LABEL_SCALE = 1.8

#: `category: (fill, casing, width)` -- the road hierarchy, widest first.
CARTO_ROADS = {
    "major": ("#e892a2", "#dc2a67", 4.20 * _CARTO_WIDTH_SCALE),
    "primary": ("#fcd6a4", "#a06b00", 3.60 * _CARTO_WIDTH_SCALE),
    "secondary": ("#f7fabf", "#707d05", 3.00 * _CARTO_WIDTH_SCALE),
    "tertiary": ("#ffffff", "#8f8f8f", 2.60 * _CARTO_WIDTH_SCALE),
    "residential": ("#ffffff", "#bbbbbb", 2.20 * _CARTO_WIDTH_SCALE),
    "service": ("#ffffff", "#cccccc", 1.40 * _CARTO_WIDTH_SCALE),
}
#: Casing sits this much wider than the fill, in mm, per side.
_CASING = 0.35
#: Rendering passes. Every casing is drawn before every fill, so roads join
#: cleanly instead of each road stamping its own outline over its neighbour.
_PASS_CASING = 0
_PASS_FILL = 1


def _marker_line_placement(name: str):
    """A marker-line placement value across both QGIS generations."""
    for owner in (
        getattr(Qgis, "MarkerLinePlacement", None),
        QgsMarkerLineSymbolLayer,
        getattr(QgsMarkerLineSymbolLayer, "Placement", None),
    ):
        value = getattr(owner, name, None) if owner is not None else None
        if value is not None:
            return value
    return None


def _symbol_property(legacy: str, modern: str):
    """A symbol-layer property enum across both QGIS generations."""
    value = getattr(QgsSymbolLayer, legacy, None)
    if value is not None:
        return value
    return getattr(getattr(QgsSymbolLayer, "Property", None), modern, None)


def _stroke(color: str, width: float, dashed: bool = False):
    layer = QgsSimpleLineSymbolLayer.create({
        "color": color,
        "width": str(width),
        "capstyle": "round",
        "joinstyle": "round",
    })
    if dashed:
        with contextlib.suppress(AttributeError, TypeError):
            layer.setUseCustomDashPattern(True)
            layer.setCustomDashVector([2.2, 1.4])
    cap_enum = getattr(Qt, "PenCapStyle", None)
    join_enum = getattr(Qt, "PenJoinStyle", None)
    cap = getattr(cap_enum, "RoundCap", None) or getattr(Qt, "RoundCap", None)
    join = getattr(join_enum, "RoundJoin", None) or getattr(Qt, "RoundJoin", None)
    with contextlib.suppress(AttributeError, TypeError):
        if cap is not None:
            layer.setPenCapStyle(cap)
        if join is not None:
            layer.setPenJoinStyle(join)
    return layer


#: A one-way street draws its arrows; anything else draws them at zero size.
#: Data-defined size is the portable way to switch one symbol layer off per
#: feature -- `oneway=-1` runs against the digitising direction, so its arrow
#: is flipped rather than hidden.
_ONEWAY_SIZE = (
    "CASE WHEN lower(coalesce(\"oneway\",'')) IN ('yes','true','1','-1')"
    " THEN 1.7 ELSE 0 END"
)
_ONEWAY_ANGLE = (
    "CASE WHEN lower(coalesce(\"oneway\",'')) = '-1' THEN 180 ELSE 0 END"
)


def _oneway_arrows():
    """The direction arrows OSM draws along a one-way street."""
    arrow = QgsMarkerSymbol.createSimple({
        "name": "filled_arrowhead",
        "color": "#6b6b6b",
        "outline_style": "no",
        "size": "1.7",
        "angle": "0",
    })
    size_prop = _symbol_property("PropertySize", "Size")
    angle_prop = _symbol_property("PropertyAngle", "Angle")
    marker_layer = arrow.symbolLayer(0)
    with contextlib.suppress(AttributeError, TypeError):
        if size_prop is not None:
            marker_layer.setDataDefinedProperty(
                size_prop, QgsProperty.fromExpression(_ONEWAY_SIZE)
            )
        if angle_prop is not None:
            marker_layer.setDataDefinedProperty(
                angle_prop, QgsProperty.fromExpression(_ONEWAY_ANGLE)
            )
    line = QgsMarkerLineSymbolLayer()
    line.setSubSymbol(arrow)
    with contextlib.suppress(AttributeError, TypeError):
        line.setRotateSymbols(True)
        line.setRenderingPass(_PASS_FILL)
    # One arrow at the middle of the street, where its name is written --
    # a repeating interval stipples arrows down every one-way road and the
    # map stops being readable.
    central = _marker_line_placement("CentralPoint")
    if central is not None:
        for setter in ("setPlacements", "setPlacement"):
            method = getattr(line, setter, None)
            if method is None:
                continue
            try:
                method(central)
                break
            except (TypeError, ValueError):
                continue
    return line


def _road(fill: str, casing: str, width: float, oneway: bool = True):
    """A road as OSM draws one: a casing with the carriageway on top."""
    symbol = QgsLineSymbol()
    symbol.deleteSymbolLayer(0)
    under = _stroke(casing, width + 2 * _CASING)
    over = _stroke(fill, width)
    with contextlib.suppress(AttributeError, TypeError):
        under.setRenderingPass(_PASS_CASING)
        over.setRenderingPass(_PASS_FILL)
    symbol.appendSymbolLayer(under)
    symbol.appendSymbolLayer(over)
    if oneway:
        symbol.appendSymbolLayer(_oneway_arrows())
    return symbol


def _path(color: str, width: float = 0.5):
    """A footway or cycleway: a dashed line, no carriageway."""
    symbol = QgsLineSymbol()
    symbol.deleteSymbolLayer(0)
    layer = _stroke(color, width, dashed=True)
    with contextlib.suppress(AttributeError, TypeError):
        layer.setRenderingPass(_PASS_FILL)
    symbol.appendSymbolLayer(layer)
    return symbol


# --- OSM icons -------------------------------------------------------------
#
# QGIS ships the same icon family the OSM map uses, so the plugin can draw a
# pharmacy as a pharmacy without redistributing anyone's artwork. A different
# QGIS build may carry a different set, so every icon below is checked against
# the running install and anything missing falls back to a plain marker --
# a referenced-but-absent SVG renders as a broken-image box, which is worse
# than a dot.
#: The colour OSM gives each POI family. The bundled icons are parametrised
#: (`fill="param(fill)"`), so without one they all render black.
CARTO_ICON_COLORS = {
    # Healthcare is red on the OSM map, not pink.
    "health": "#bf0000",
    "shop": "#ac39ac",
    "food": "#734a08",
    "education": "#734a08",
    "worship": "#734a08",
    "emergency": "#bf0000",
    "transport": "#0092da",
    "tourism": "#0092da",
    "sport": "#728224",
}
#: `category: (default icon, ((expression, icon), ...))`
CARTO_ICONS = {
    "health": (
        "health/health_pharmacy.svg",
        (
            ("lower(\"amenity\") = 'hospital'", "health/health_hospital.svg"),
            ("lower(\"amenity\") IN ('doctors','clinic')", "health/health_doctors.svg"),
            ("lower(\"amenity\") = 'dentist'", "health/health_dentist.svg"),
        ),
    ),
    "education": (
        "symbol/education_school.svg",
        (
            ("lower(\"amenity\") IN ('university','college')",
             "symbol/education_university.svg"),
            ("lower(\"amenity\") = 'kindergarten'", "symbol/education_nursery.svg"),
            ("lower(\"amenity\") = 'library'", "amenity/amenity_library.svg"),
        ),
    ),
    "worship": ("landmark/amenity=place_of_worship.svg", ()),
    "emergency": (
        "emergency/amenity=police.svg",
        (("lower(\"amenity\") = 'fire_station'",
          "emergency/amenity=fire_station.svg"),),
    ),
    "food": (
        "entertainment/amenity=restaurant.svg",
        (
            ("lower(\"amenity\") = 'cafe'", "entertainment/amenity=cafe.svg"),
            ("lower(\"amenity\") = 'bar'", "entertainment/amenity=bar.svg"),
            ("lower(\"amenity\") = 'pub'", "entertainment/amenity=pub.svg"),
            ("lower(\"amenity\") = 'fast_food'",
             "entertainment/amenity=fast_food.svg"),
        ),
    ),
    "shop": (
        "shopping/shopping_convenience.svg",
        (
            ("lower(\"shop\") = 'supermarket'", "services/shop=supermarket.svg"),
            ("lower(\"shop\") = 'bakery'", "shopping/shopping_bakery.svg"),
            ("lower(\"shop\") = 'butcher'", "shopping/shopping_butcher.svg"),
            ("lower(\"shop\") = 'clothes'", "shopping/shopping_clothes.svg"),
            ("lower(\"shop\") = 'hairdresser'", "shopping/shopping_hairdresser.svg"),
            ("lower(\"shop\") IN ('bicycle','cycle')", "shopping/shopping_bicycle.svg"),
            ("lower(\"shop\") = 'car'", "shopping/shopping_car.svg"),
            ("lower(\"shop\") = 'books'", "shopping/shopping_book.svg"),
            ("lower(\"shop\") = 'jewelry'", "shopping/shopping_jewelry.svg"),
            ("lower(\"shop\") = 'mobile_phone'",
             "shopping/shopping_mobile_phone.svg"),
            ("lower(\"amenity\") = 'pharmacy'", "services/amenity=pharmacy.svg"),
        ),
    ),
    "transport": (
        "transport/transport_bus_stop2.svg",
        (
            ("lower(\"railway\") IN ('station','halt')",
             "transport/railway=station.svg"),
            ("lower(\"amenity\") = 'parking'", "transport/amenity=parking.svg"),
            ("lower(\"amenity\") = 'fuel'", "transport/transport_fuel.svg"),
            ("lower(\"amenity\") = 'taxi'", "transport/amenity=taxi.svg"),
        ),
    ),
    "tourism": (
        "tourist/tourist_monument.svg",
        (
            ("lower(\"tourism\") = 'museum'", "tourist/tourist_museum.svg"),
            ("lower(\"tourism\") = 'hotel'",
             "accommodation/accommodation_hotel.svg"),
            ("lower(\"tourism\") IN ('artwork','gallery')",
             "tourist/tourist_art_gallery.svg"),
            ("lower(\"tourism\") = 'viewpoint'", "tourist/tourist_view_point.svg"),
        ),
    ),
    "sport": (
        "sport/sport_leisure_centre.svg",
        (
            ("lower(\"sport\") = 'soccer'", "sport/sport_soccer.svg"),
            ("lower(\"sport\") = 'tennis'", "sport/sport_tennis.svg"),
            ("lower(\"sport\") = 'swimming'", "sport/sport_swimming_outdoor.svg"),
            ("lower(\"sport\") IN ('fitness','gym')", "sport/sport_gym.svg"),
        ),
    ),
}


#: Icons drawn for this plugin, for things the QGIS library has no symbol
#: for. A plugin-relative path is resolved here rather than registered into
#: the QGIS SVG search paths, which is a global the plugin should not touch.
PLUGIN_SVG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons", "osm"
)


def plugin_svg(name: str) -> str:
    return os.path.join(PLUGIN_SVG_DIR, name)


def _svg_exists(name: str) -> bool:
    if os.path.isabs(name):
        return os.path.isfile(name)
    for base in QgsApplication.svgPaths():
        if os.path.isfile(os.path.join(base, name)):
            return True
    return False


def _icon(category: str, size: float = 3.6 * _CARTO_WIDTH_SCALE):
    """The OSM icon for a POI category, or None if this install lacks it."""
    entry = CARTO_ICONS.get(category)
    if entry is None:
        return None
    default, mapping = entry
    if not _svg_exists(default):
        return None
    layer = QgsSvgMarkerSymbolLayer(default)
    layer.setSize(size)
    available = tuple(
        (condition, name) for condition, name in mapping if _svg_exists(name)
    )
    if available:
        cases = " ".join(
            f"WHEN {condition} THEN '{name}'" for condition, name in available
        )
        expression = f"CASE {cases} ELSE '{default}' END"
        prop = _symbol_property("PropertyName", "Name")
        if prop is not None:
            with contextlib.suppress(AttributeError, TypeError):
                layer.setDataDefinedProperty(
                    prop, QgsProperty.fromExpression(expression)
                )
    # The bundled icons take their colour from a `fill` parameter. Leaving it
    # unset is what made every one of them come out black.
    with contextlib.suppress(AttributeError, TypeError, ValueError):
        layer.setFillColor(QColor(CARTO_ICON_COLORS.get(category, "#734a08")))
        # A thin light halo keeps the glyph readable over buildings and roads.
        layer.setStrokeColor(QColor("#ffffff"))
        layer.setStrokeWidth(0.4)
    symbol = QgsMarkerSymbol()
    symbol.changeSymbolLayer(0, layer)
    return symbol


def _crossing_marker(size: float = 2.8 * _CARTO_WIDTH_SCALE):
    """The zebra, drawn from above, or a plain tick if the icon is missing."""
    path = plugin_svg("crossing.svg")
    if not _svg_exists(path):
        return _marker("#6b6b6b", 1.4, "square")
    layer = QgsSvgMarkerSymbolLayer(path)
    layer.setSize(size)
    with contextlib.suppress(AttributeError, TypeError, ValueError):
        layer.setFillColor(QColor("#5a5a5a"))
        layer.setStrokeColor(QColor("#ffffff"))
        layer.setStrokeWidth(0.3)
    symbol = QgsMarkerSymbol()
    symbol.changeSymbolLayer(0, layer)
    return symbol


def _carto_line_renderer(theme: dict):
    entries = (
        ("water", "Waterways", _road("#aad3df", "#a0c8d8", 1.60, oneway=False)),
        ("rail", "Railways", _path("#9a9a9a", 0.80)),
        ("transit", "Transit routes", _path("#b0b0b0", 0.45)),
    ) + tuple(
        (category, label, _road(fill, casing, width))
        for category, label, (fill, casing, width) in (
            ("major", "Motorway and trunk", CARTO_ROADS["major"]),
            ("primary", "Primary roads", CARTO_ROADS["primary"]),
            ("secondary", "Secondary roads", CARTO_ROADS["secondary"]),
            ("tertiary", "Tertiary roads", CARTO_ROADS["tertiary"]),
            ("residential", "Residential roads", CARTO_ROADS["residential"]),
            ("service", "Service roads", CARTO_ROADS["service"]),
        )
    ) + (
        # Sidewalks and cycle paths trace every street in a dense quarter;
        # OSM keeps them thin so they read as detail, not as the network.
        ("active", "Walking and cycling", _path("#fa8072", 0.40)),
        ("other", "Other lines", _path("#c2c2c2", 0.35)),
    )
    renderer = _renderer(CARTO_LINE_EXPRESSION, entries)
    # Without this the two passes above mean nothing and every road paints its
    # own casing over the road it meets.
    with contextlib.suppress(AttributeError, TypeError):
        renderer.setUsingSymbolLevels(True)
    return renderer


def _carto_polygon_renderer(theme: dict):
    """Land first, then what sits on it, then the buildings.

    A categorised renderer draws features in whatever order they come out of
    the layer, so the urban blocks would otherwise paint over the parks and
    buildings that happen to be stored before them. Symbol levels are the only
    thing that fixes an order inside one layer.
    """
    building_fill, building_line = CARTO["building"]
    # (value, label, fill, outline, level) -- level 0 is drawn first, at the
    # bottom. Urban blocks are the ground everything else sits on.
    rows = (
        ("urban_residential", "Urban blocks", *CARTO["residential_area"], 0),
        ("urban_retail", "Retail blocks", *CARTO["retail_area"], 0),
        ("urban_commercial", "Commercial blocks", *CARTO["commercial_area"], 0),
        ("urban_industrial", "Industrial blocks", *CARTO["industrial_area"], 0),
        # Whatever did not classify. Faint on purpose: it must never pass
        # itself off as the urban ground.
        ("other", "Other polygons", "#eeeeec", "#e2e2df", 0),
        ("green", "Green space", *CARTO["green"], 1),
        ("water", "Water", *CARTO["water"], 1),
        ("civic", "Civic and amenities", *CARTO["civic_area"], 2),
        ("parking", "Parking", *CARTO["parking"], 2),
        ("pedestrian", "Pedestrian space", *CARTO["pedestrian_area"], 2),
        ("building_residential", "Buildings", building_fill, building_line, 3),
        ("building_commercial", "Commercial buildings", building_fill, building_line, 3),
        ("building_industrial", "Industrial buildings", building_fill, building_line, 3),
        ("building_civic", "Civic buildings", building_fill, building_line, 3),
        ("building_worship", "Worship buildings", building_fill, building_line, 3),
        ("building_other", "Other buildings", building_fill, building_line, 3),
    )
    entries = []
    for value, label, fill_color, outline, level in rows:
        symbol = _fill(fill_color, outline, 1.0)
        with contextlib.suppress(AttributeError, TypeError):
            symbol.symbolLayer(0).setRenderingPass(level)
        entries.append((value, label, symbol))
    renderer = _renderer(CARTO_POLYGON_EXPRESSION, tuple(entries))
    with contextlib.suppress(AttributeError, TypeError):
        renderer.setUsingSymbolLevels(True)
    return renderer


def _carto_point_renderer(theme: dict):
    # Carto gives each POI family its own colour; these are its label colours,
    # and they are also the fallback when an install has no icon set.
    fallbacks = {
        "transport": _marker("#7981b0", 2.5 * _CARTO_WIDTH_SCALE, "square"),
        "health": _marker("#da0092", 2.5 * _CARTO_WIDTH_SCALE, "cross"),
        "education": _marker("#734a08", 2.4 * _CARTO_WIDTH_SCALE, "diamond"),
        "worship": _marker("#734a08", 2.5 * _CARTO_WIDTH_SCALE, "triangle"),
        "emergency": _marker("#d40000", 2.6 * _CARTO_WIDTH_SCALE, "cross"),
        "food": _marker("#c77400", 2.4 * _CARTO_WIDTH_SCALE),
        "shop": _marker("#ac39ac", 2.4 * _CARTO_WIDTH_SCALE, "square"),
        "tourism": _marker("#0092da", 2.4 * _CARTO_WIDTH_SCALE, "star"),
        "sport": _marker("#39ac39", 2.4 * _CARTO_WIDTH_SCALE),
    }
    labels = {
        "transport": "Public transport",
        "health": "Healthcare",
        "education": "Education",
        "worship": "Worship",
        "emergency": "Emergency",
        "food": "Food and drink",
        "shop": "Shops",
        "tourism": "Tourism",
        "sport": "Sport",
    }
    entries = [
        ("place", "Place centres", _marker("#777777", 2.6, "square")),
        ("tree", "Trees", _marker("#8cb765", 1.9)),
    ]
    for category, label in labels.items():
        entries.append(
            (category, label, _icon(category) or fallbacks[category])
        )
    entries.extend((
        # Traffic furniture belongs to the road, so it stays a faint tick
        # rather than competing with the places on the map.
        ("crossing", "Pedestrian crossings", _crossing_marker()),
        ("junction", "Junction and traffic nodes", _marker("#b0b0b0", 1.1)),
        ("furniture", "Kerbs and barriers", _marker("#c0b8ae", 1.1, "square")),
        ("other", "Other points", _marker("#9a9a9a", 1.5)),
    ))
    return _renderer(CARTO_POINT_EXPRESSION, tuple(entries))


def _feature_value(feature, field_name: str) -> str:
    with contextlib.suppress(KeyError, TypeError, AttributeError):
        value = feature[field_name]
        return str(value or "")
    return ""


def _is_places_layer(layer) -> bool:
    explicit = str(layer.customProperty("zero2agent/style_scope", "") or "")
    if explicit == "places":
        return True
    fields = layer.fields()
    if fields.indexFromName("preset_id") < 0 or fields.indexFromName("theme") < 0:
        return False
    with contextlib.suppress(Exception):
        for index, feature in enumerate(layer.getFeatures()):
            preset_id = _feature_value(feature, "preset_id")
            theme = _feature_value(feature, "theme")
            if (
                theme == "Places"
                or preset_id == "place_centres"
                or preset_id.startswith("admin_level_")
            ):
                return True
            if index >= 25:
                break
    return False


def _label_property(name: str):
    """A labelling property enum across both QGIS generations."""
    value = getattr(QgsPalLayerSettings, name, None)
    if value is not None:
        return value
    return getattr(getattr(QgsPalLayerSettings, "Property", None), name, None)


def _placement(name: str):
    """A label placement mode across both QGIS generations.

    QGIS 3 keeps these on `QgsPalLayerSettings`; QGIS 4 moved them to
    `Qgis.LabelPlacement`. Reading whichever exists keeps one styling path.
    """
    with contextlib.suppress(ImportError, AttributeError):
        from qgis.core import Qgis

        return getattr(Qgis.LabelPlacement, name)
    return getattr(QgsPalLayerSettings, name, None)


#: A route relation carries the name of the *service*, not of the street its
#: members run along. Labelling it writes "EuroVelo 8 - Mediterranean Route"
#: and "T2: Halkapinar - Fahrettin Altay" over the road network, which is
#: exactly the clutter the standard OSM map does not have.
_CARTO_LABEL_FIELD = (
    "CASE WHEN coalesce(\"route\",'') <> '' THEN NULL"
    " WHEN lower(coalesce(\"natural\",'')) = 'coastline' THEN NULL"
    " WHEN coalesce(\"place\",'') <> '' THEN NULL"
    " ELSE nullif(coalesce(\"name\",''),'') END"
)

#: OSM writes a POI's name in its family's colour, which is what lets a reader
#: skim the map. One flat grey turns the same information into a wall of text.
_CARTO_LABEL_COLOR = (
    "CASE"
    " WHEN coalesce(\"shop\",'') <> '' THEN '#ac39ac'"
    " WHEN lower(coalesce(\"amenity\",'')) IN ('pharmacy','hospital','clinic',"
    "'doctors','dentist') THEN '#bf0000'"
    " WHEN lower(coalesce(\"amenity\",'')) IN ('parking','fuel','taxi',"
    "'bus_station','ferry_terminal') OR coalesce(\"public_transport\",'') <> ''"
    " OR coalesce(\"tourism\",'') <> '' THEN '#0092da'"
    " WHEN coalesce(\"sport\",'') <> '' THEN '#728224'"
    " WHEN coalesce(\"amenity\",'') <> '' THEN '#734a08'"
    " ELSE '#4a4a4a' END"
)


def _apply_osm_labels(layer, theme: dict, carto: bool = False) -> None:
    name_index = layer.fields().indexFromName("name")
    if name_index < 0:
        return
    settings = QgsPalLayerSettings()
    if carto and layer.geometryType() == QgsWkbTypes.GeometryType.LineGeometry:
        settings.fieldName = _CARTO_LABEL_FIELD
        settings.isExpression = True
    else:
        settings.fieldName = "name"
    settings.enabled = True
    geometry = layer.geometryType()
    # Without a placement the default is AroundPoint, which on a line layer
    # does not follow the road -- which is why an "OSM as-is" download came
    # back with no street names on it at all.
    if geometry == QgsWkbTypes.GeometryType.LineGeometry:
        placement = _placement("Curved") or _placement("Line")
        if placement is not None:
            settings.placement = placement
        with contextlib.suppress(AttributeError, TypeError):
            line_settings = settings.lineSettings()
            # One name for a street, not one per OSM way fragment.
            line_settings.setMergeLines(True)
            # OSM writes the street name *along the carriageway*. The Qt
            # default puts it above the line, which reads as a label floating
            # beside the road it belongs to.
            from qgis.core import QgsLabeling

            flags = getattr(QgsLabeling, "LinePlacementFlag", QgsLabeling)
            on_line = getattr(flags, "OnLine", None)
            orientation = getattr(flags, "MapOrientation", None)
            if on_line is not None:
                line_settings.setPlacementFlags(
                    on_line | orientation if orientation is not None else on_line
                )
            settings.setLineSettings(line_settings)
    elif geometry == QgsWkbTypes.GeometryType.PolygonGeometry:
        placement = _placement("Horizontal")
        if placement is not None:
            settings.placement = placement
    else:
        placement = _placement("AroundPoint")
        if placement is not None:
            settings.placement = placement
    point_layer = (
        carto and geometry == QgsWkbTypes.GeometryType.PointGeometry
    )
    if point_layer:
        # Sits under the icon rather than on top of it, the way OSM does.
        with contextlib.suppress(AttributeError, TypeError):
            settings.dist = 1.4
        color_property = _label_property("Color")
        if color_property is not None:
            with contextlib.suppress(AttributeError, TypeError):
                settings.dataDefinedProperties().setProperty(
                    color_property,
                    QgsProperty.fromExpression(_CARTO_LABEL_COLOR),
                )
    text_format = QgsTextFormat()
    if carto:
        # Labels grow even more than the road network they sit next to --
        # otherwise wider carriageways make the street names underneath them
        # look small by comparison.
        base_size = 6.5 if point_layer else 7.5
        text_format.setSize(base_size * _CARTO_LABEL_SCALE)
    else:
        text_format.setSize(8)
    text_format.setColor(QColor("#4a4a4a" if carto else "#333333"))
    buffer = QgsTextBufferSettings()
    buffer.setEnabled(True)
    buffer.setSize((1.0 * _CARTO_LABEL_SCALE) if carto else 0.85)
    buffer.setColor(QColor(CARTO["bg"] if carto else theme["bg"]))
    text_format.setBuffer(buffer)
    settings.setFormat(text_format)
    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
    layer.setLabelsEnabled(True)


def apply_map_theme(
    layer, theme_id: str, road_width_mode: str = "highway"
) -> str:
    """Apply a semantic renderer and return the resolved theme id."""
    from .map_themes import DEFAULT_MAP_THEME, MAP_THEMES

    resolved_id = theme_id if theme_id in MAP_THEMES else DEFAULT_MAP_THEME
    theme = map_theme(resolved_id)
    geometry = layer.geometryType()
    places = _is_places_layer(layer)
    # The OSM As-Is theme is asked to look like openstreetmap.org, which takes
    # its own symbols -- road casing above all -- not a recoloured version of
    # the analytical ones.
    carto = resolved_id == "osm_as_is" and not places
    if geometry == QgsWkbTypes.GeometryType.PolygonGeometry:
        if places:
            renderer = _places_polygon_renderer(theme)
        elif carto:
            renderer = _carto_polygon_renderer(theme)
        else:
            renderer = _polygon_renderer(theme)
    elif geometry == QgsWkbTypes.GeometryType.LineGeometry:
        if places:
            renderer = _places_line_renderer(theme)
        elif carto:
            renderer = _carto_line_renderer(theme)
        else:
            renderer = _line_renderer(
                theme,
                road_width_mode if road_width_mode in ROAD_WIDTH_MODES else "highway",
            )
    elif geometry == QgsWkbTypes.GeometryType.PointGeometry:
        if places:
            renderer = _places_point_renderer(theme)
        elif carto:
            renderer = _carto_point_renderer(theme)
        else:
            renderer = _point_renderer(theme)
    else:
        return resolved_id
    layer.setRenderer(renderer)
    if (
        geometry == QgsWkbTypes.GeometryType.LineGeometry
        or resolved_id == "osm_as_is"
        or _is_places_layer(layer)
    ):
        _apply_osm_labels(layer, theme, carto=carto)
    layer.setCustomProperty("zero2agent/map_theme", resolved_id)
    layer.setCustomProperty(
        "zero2agent/road_width_mode",
        road_width_mode if road_width_mode in ROAD_WIDTH_MODES else "highway",
    )
    layer.triggerRepaint()
    return resolved_id
