"""Categorisation expressions for the result-layer renderers.

These live apart from `map_styling` so they can be checked without QGIS. A
renderer expression that names a column the download no longer writes fails
quietly — the category simply never matches and the features render as
"other" — so `STYLING_COLUMNS` exists to let a test assert that every column
an expression reads is really in the layer schema.
"""
from __future__ import annotations

import re
from typing import Dict, Set, Tuple

#: Boundary levels the renderers draw, as ``level -> (label, color, width)``.
#: This lives beside the expressions because ``POLYGON_EXPRESSION`` and friends
#: emit ``admin_<level>`` category values from it — the two have to agree, and
#: keeping them apart is how a legend entry stops matching without anyone
#: noticing.  ``map_styling`` re-exports it.
ADMIN_LEVEL_STYLES: Dict[str, Tuple[str, str, float]] = {
    "2": ("Country boundaries", "#6b4c9a", 1.70),
    "3": ("Regional boundaries", "#7b5aa6", 1.55),
    "4": ("Province boundaries", "#8a68ad", 1.40),
    "5": ("Intermediate boundaries", "#9a76b5", 1.25),
    "6": ("District boundaries", "#aa84bd", 1.10),
    "7": ("Subdistrict boundaries", "#ba93c5", 0.95),
    "8": ("Village or local-settlement boundaries", "#c9a1cd", 0.82),
    "9": ("Intermediate local boundaries", "#d5afd2", 0.72),
    "10": ("Neighborhood boundaries", "#dfbdd8", 0.64),
}

_ADMIN_BRANCH = (
    " WHEN lower(coalesce(\"boundary\",'')) = 'administrative'"
    "   AND coalesce(\"admin_level\",'') <> ''"
    " THEN concat('admin_', coalesce(\"admin_level\",''))"
)

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
    " WHEN lower(coalesce(\"building\",'')) IN ('civic','public','government',"
    "'school','university','college','kindergarten','hospital','clinic',"
    "'museum','library','courthouse','fire_station','train_station',"
    "'transportation','stadium','sports_hall') THEN 'building_civic'"
    " WHEN coalesce(\"building\",'') <> '' THEN 'building_other'"
    " ELSE '' END"
)

_POLYGON_BODY = (
    f" WHEN ({BUILDING_EXPRESSION}) <> '' THEN ({BUILDING_EXPRESSION})"
    # A relation tagged `boundary=administrative` reaches this body whenever
    # it either has no `admin_level` for `_ADMIN_BRANCH` to key on (analytical
    # themes), or the theme never runs that branch at all (OSM As-Is, which
    # does not download Places -- see `apply_map_theme`). Left uncaught it
    # fell into 'other' or, worse, matched a `landuse` value and painted as
    # an urban block or green space the size of a whole district.
    " WHEN lower(coalesce(\"boundary\",'')) = 'administrative' THEN 'boundary'"
    " WHEN lower(coalesce(\"natural\",'')) IN ('water','bay','wetland')"
    "   OR lower(coalesce(\"query_key\",'')) IN ('water','waterway') THEN 'water'"
    " WHEN lower(coalesce(\"amenity\",'')) = 'parking'"
    "   OR lower(coalesce(\"landuse\",'')) = 'parking' THEN 'parking'"
    " WHEN lower(coalesce(\"highway\",'')) IN ('pedestrian','footway','living_street')"
    "   OR lower(coalesce(\"amenity\",'')) = 'marketplace' THEN 'pedestrian'"
    " WHEN lower(coalesce(\"leisure\",'')) IN ('pitch','stadium','sports_centre',"
    "'track','ice_rink','swimming_pool')"
    "   OR coalesce(\"sport\",'') <> '' THEN 'sport'"
    " WHEN lower(coalesce(\"leisure\",'')) IN ('park','garden','playground',"
    "'dog_park','golf_course') OR lower(coalesce(\"landuse\",'')) IN ('forest',"
    "'grass','recreation_ground','meadow','village_green','orchard','vineyard',"
    "'farmland','allotments','greenfield','cemetery')"
    "   OR lower(coalesce(\"natural\",'')) IN ('wood','scrub','grassland','heath')"
    " THEN 'green'"
    # The grey-beige ground an OSM city sits on is `landuse`, and it has to
    # be asked for by name. Left to the catch-all it was whatever failed to
    # classify -- so an administrative area that slipped in was drawn as an
    # urban block, and a quarter with no landuse mapped showed nothing.
    " WHEN lower(coalesce(\"landuse\",'')) = 'retail' THEN 'urban_retail'"
    " WHEN lower(coalesce(\"landuse\",'')) IN ('commercial','office')"
    " THEN 'urban_commercial'"
    " WHEN lower(coalesce(\"landuse\",'')) IN ('industrial','port','quarry',"
    "'landfill') THEN 'urban_industrial'"
    " WHEN lower(coalesce(\"landuse\",'')) IN ('residential','garages',"
    "'education','religious','institutional','civic_admin','construction',"
    "'brownfield','greyfield','railway','military') THEN 'urban_residential'"
    " WHEN lower(coalesce(\"amenity\",'')) <> '' THEN 'civic'"
    " ELSE 'other' END"
)

#: The analytical renderers classify administrative boundaries too.
POLYGON_EXPRESSION = "CASE" + _ADMIN_BRANCH + _POLYGON_BODY

#: OSM As-Is never downloads boundaries -- they belong to the Places
#: group -- so its legend must not offer admin categories either.
CARTO_POLYGON_EXPRESSION = "CASE" + _POLYGON_BODY

PLACES_POLYGON_EXPRESSION = (
    "CASE"
    " WHEN lower(coalesce(\"boundary\",'')) = 'administrative'"
    "   AND coalesce(\"admin_level\",'') <> ''"
    " THEN concat('admin_', coalesce(\"admin_level\",''))"
    " WHEN coalesce(\"place\",'') <> '' THEN 'place_area'"
    " ELSE 'other' END"
)

_LINE_BODY = (
    " WHEN lower(coalesce(\"query_key\",'')) IN ('water','waterway')"
    "   OR lower(coalesce(\"natural\",'')) = 'water' THEN 'water'"
    " WHEN coalesce(\"railway\",'') <> '' THEN 'rail'"
    " WHEN coalesce(\"route\",'') <> '' THEN 'transit'"
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

#: The analytical renderers classify administrative boundaries too.
LINE_EXPRESSION = "CASE" + _ADMIN_BRANCH + _LINE_BODY

#: OSM As-Is never downloads boundaries -- they belong to the Places
#: group -- so its legend must not offer admin categories either.
CARTO_LINE_EXPRESSION = "CASE" + _LINE_BODY

PLACES_LINE_EXPRESSION = (
    "CASE"
    " WHEN lower(coalesce(\"boundary\",'')) = 'administrative'"
    "   AND coalesce(\"admin_level\",'') <> ''"
    " THEN concat('admin_', coalesce(\"admin_level\",''))"
    " ELSE 'other' END"
)

_POINT_BODY = (
    " WHEN coalesce(\"place\",'') <> '' THEN 'place'"
    " WHEN lower(coalesce(\"natural\",'')) = 'tree' THEN 'tree'"
    " WHEN coalesce(\"public_transport\",'') <> '' OR lower(coalesce(\"railway\",''))"
    "   IN ('station','halt','tram_stop')"
    "   OR lower(coalesce(\"highway\",'')) = 'bus_stop'"
    "   OR lower(coalesce(\"amenity\",'')) IN ('parking','fuel','taxi',"
    "'bus_station','ferry_terminal') THEN 'transport'"
    " WHEN lower(coalesce(\"amenity\",'')) IN ('hospital','clinic','doctors',"
    "'pharmacy','dentist') THEN 'health'"
    " WHEN lower(coalesce(\"amenity\",'')) IN ('school','university','college',"
    "'kindergarten','library') THEN 'education'"
    " WHEN lower(coalesce(\"amenity\",'')) IN ('place_of_worship') THEN 'worship'"
    " WHEN lower(coalesce(\"amenity\",'')) IN ('fire_station','police',"
    "'ambulance_station') THEN 'emergency'"
    " WHEN lower(coalesce(\"amenity\",'')) IN ('restaurant','cafe','bar',"
    "'pub','fast_food','ice_cream','biergarten','food_court') THEN 'food'"
    " WHEN coalesce(\"shop\",'') <> '' THEN 'shop'"
    " WHEN coalesce(\"tourism\",'') <> '' THEN 'tourism'"
    " WHEN coalesce(\"sport\",'') <> '' THEN 'sport'"
    # Junction and traffic nodes are the road network's own furniture, not
    # places. Left in the catch-all they became a dot per crossing and per
    # turning circle -- the unexplained scatter over the street grid.
    # A crossing is where people cross, so it gets the zebra rather than
    # being filed away with the traffic furniture.
    " WHEN lower(coalesce(\"highway\",'')) = 'crossing' THEN 'crossing'"
    " WHEN lower(coalesce(\"highway\",'')) IN ('traffic_signals',"
    "'mini_roundabout','turning_circle','turning_loop','stop','give_way',"
    "'motorway_junction','speed_camera','street_lamp','passing_place',"
    "'elevator','milestone') OR coalesce(\"direction\",'') <> ''"
    " THEN 'junction'"
    # Kerbs, bollards, gates and gaps: the edge of the footway and the things
    # standing on it. They are furniture, not places, and left in the
    # catch-all they were an unexplained dot on every corner.
    " WHEN coalesce(\"kerb\",'') <> '' OR coalesce(\"barrier\",'') <> ''"
    " THEN 'furniture'"
    " ELSE 'other' END"
)

#: The analytical renderers classify administrative boundaries too.
POINT_EXPRESSION = "CASE" + _ADMIN_BRANCH + _POINT_BODY

#: OSM As-Is never downloads boundaries -- they belong to the Places
#: group -- so its legend must not offer admin categories either.
CARTO_POINT_EXPRESSION = "CASE" + _POINT_BODY

PLACES_POINT_EXPRESSION = (
    "CASE"
    " WHEN lower(coalesce(\"boundary\",'')) = 'administrative'"
    "   AND coalesce(\"admin_level\",'') <> ''"
    " THEN concat('admin_', coalesce(\"admin_level\",''))"
    " WHEN coalesce(\"place\",'') <> '' THEN 'place'"
    " ELSE 'other' END"
)

_COLUMN_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"')

STYLING_COLUMNS: Set[str] = {
    name
    for expression in (
        POLYGON_EXPRESSION,
        CARTO_POLYGON_EXPRESSION,
        CARTO_LINE_EXPRESSION,
        CARTO_POINT_EXPRESSION,
        PLACES_POLYGON_EXPRESSION,
        LINE_EXPRESSION,
        PLACES_LINE_EXPRESSION,
        POINT_EXPRESSION,
        PLACES_POINT_EXPRESSION,
    )
    for name in _COLUMN_RE.findall(expression)
}


_EMITTED_RE = re.compile(r"(?:THEN|ELSE)\s+'([^']*)'")
_ADMIN_CONCAT = "concat('admin_'"


def expression_values(expression: str) -> Set[str]:
    """Every category value ``expression`` can evaluate to.

    A categorised renderer draws nothing at all for a value it has no category
    for, and a category whose value the expression never produces is a legend
    entry the map can never show.  Both fail silently, so the renderers are
    checked against this instead of against a hand-counted total.
    """
    values = {value for value in _EMITTED_RE.findall(expression) if value}
    if _ADMIN_CONCAT in expression:
        values.update(f"admin_{level}" for level in ADMIN_LEVEL_STYLES)
    return values


#: Renderer expressions keyed by the ``map_styling`` factory that pairs with
#: them, so a test can walk every pair without importing QGIS to find them.
RENDERER_EXPRESSIONS = {
    "carto_polygon": CARTO_POLYGON_EXPRESSION,
    "carto_line": CARTO_LINE_EXPRESSION,
    "carto_point": CARTO_POINT_EXPRESSION,
    "polygon": POLYGON_EXPRESSION,
    "places_polygon": PLACES_POLYGON_EXPRESSION,
    "line": LINE_EXPRESSION,
    "places_line": PLACES_LINE_EXPRESSION,
    "point": POINT_EXPRESSION,
    "places_point": PLACES_POINT_EXPRESSION,
}
