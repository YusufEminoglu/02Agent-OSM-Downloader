"""Categorisation expressions for the result-layer renderers.

These live apart from `map_styling` so they can be checked without QGIS. A
renderer expression that names a column the download no longer writes fails
quietly — the category simply never matches and the features render as
"other" — so `STYLING_COLUMNS` exists to let a test assert that every column
an expression reads is really in the layer schema.
"""
from __future__ import annotations

import re
from typing import Set

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

_COLUMN_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"')

STYLING_COLUMNS: Set[str] = {
    name
    for expression in (
        POLYGON_EXPRESSION, LINE_EXPRESSION, POINT_EXPRESSION
    )
    for name in _COLUMN_RE.findall(expression)
}
