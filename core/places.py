"""Safe place-name query construction and administrative result ranking."""
from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from typing import Any, Dict, Tuple


MAX_PLACE_NAME_LENGTH = 120
MAX_PLACE_RESULTS = 25
_UNSAFE_PLACE_RE = re.compile(r'[\x00-\x1f\x7f"\\]')


@dataclass(frozen=True)
class PlaceCandidate:
    name: str
    label: str
    kind: str
    admin_level: str
    bbox: Tuple[float, float, float, float]
    osm_type: str = ""
    osm_id: int = 0
    source: str = "overpass"
    has_area: bool = False

    @property
    def area_id(self) -> int:
        """Return this place's Overpass area id, or 0 when it has none.

        Overpass derives an area from the OSM object that describes the
        boundary, which lets a download follow the real administrative edge
        instead of the corners of a bounding box.

        `has_area` matters as much as the object type.  Overpass only builds
        areas for boundary and place polygons, so a river or route relation
        would otherwise produce an in-range id that matches nothing — and an
        area query that matches nothing returns an empty download with no
        error to explain it.
        """
        if self.osm_id <= 0 or not self.has_area:
            return 0
        if self.osm_type == "relation":
            return 3_600_000_000 + self.osm_id
        if self.osm_type == "way":
            return 2_400_000_000 + self.osm_id
        return 0


def normalize_place_name(value: object) -> str:
    text = " ".join(str(value or "").split()).strip(" ,;:-")
    if not text or len(text) > MAX_PLACE_NAME_LENGTH:
        raise ValueError("Enter a place name up to 120 characters.")
    if _UNSAFE_PLACE_RE.search(text):
        raise ValueError("The place name contains unsupported characters.")
    return text


def _regex_term(value: str) -> str:
    # Overpass accepts a PCRE-style regular expression.  User text is escaped
    # before it ever reaches the query; the input remains a place name, never
    # an editable Overpass fragment.
    return re.escape(value).replace("\\ ", "\\s+")


def build_place_query(value: object) -> str:
    name = normalize_place_name(value)
    # Query the final name token as well as the complete phrase.  This makes
    # “Izmir Konak” useful when OSM stores the administrative relation as
    # name=Konak and keeps the parent context in another tag.
    tokens = name.split()
    patterns = [f"^{_regex_term(name)}$"]
    if len(tokens) > 1:
        patterns.append(f"^{_regex_term(tokens[-1])}$")
    pattern = "(?:" + "|".join(patterns) + ")"
    selectors = []
    for key in ("name", "official_name", "alt_name", "name:en", "name:tr", "int_name"):
        selectors.extend(
            (
                f'  relation["boundary"="administrative"]["{key}"~"{pattern}",i];',
                f'  way["boundary"="administrative"]["{key}"~"{pattern}",i];',
            )
        )
    for key in ("name", "official_name", "alt_name", "name:en", "name:tr", "int_name"):
        selectors.append(f'  node["place"]["{key}"~"{pattern}",i];')
    return (
        "[out:json][timeout:25];\n(\n"
        + "\n".join(dict.fromkeys(selectors))
        + "\n);\nout tags center bb;"
    )


def _bbox(element: Dict[str, Any]) -> Tuple[float, float, float, float] | None:
    bounds = element.get("bounds")
    if isinstance(bounds, dict):
        with contextlib.suppress(KeyError, TypeError, ValueError):
            return (
                float(bounds["minlat"]), float(bounds["minlon"]),
                float(bounds["maxlat"]), float(bounds["maxlon"]),
            )
    center = element.get("center")
    point = center if isinstance(center, dict) else element
    try:
        lat, lon = float(point["lat"]), float(point["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    # A point-only place can still be used to zoom the map without inventing a
    # large extent.  The bounded download will ask the user to zoom if needed.
    return (lat - 0.002, lon - 0.002, lat + 0.002, lon + 0.002)


def _context(tags: Dict[str, Any]) -> str:
    for key in (
        "addr:city", "addr:state", "is_in:city", "is_in:state",
        "is_in", "name:en", "official_name",
    ):
        value = str(tags.get(key) or "").strip()
        if value:
            return value
    return ""


def _score(tags: Dict[str, Any], requested: str, element: Dict[str, Any]) -> int:
    requested_folded = requested.casefold()
    exact = any(
        str(tags.get(key) or "").casefold() == requested_folded
        for key in ("name", "official_name", "alt_name", "name:en", "name:tr", "int_name")
    )
    score = 100 if exact else 0
    if element.get("type") == "relation" and tags.get("boundary") == "administrative":
        score += 40
    if tags.get("boundary") == "administrative":
        score += 10
    try:
        level = int(str(tags.get("admin_level") or "99"))
    except ValueError:
        level = 99
    score += max(0, 20 - level)
    if _context(tags):
        score += 2
    return score


def parse_place_candidates(payload: object, requested: object) -> Tuple[PlaceCandidate, ...]:
    name = normalize_place_name(requested)
    requested_parts = name.split()
    data = payload if isinstance(payload, dict) else {}
    elements = data.get("elements")
    if not isinstance(elements, list):
        return ()
    candidates = []
    seen = set()
    for element in elements[:MAX_PLACE_RESULTS]:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
        bbox = _bbox(element)
        display_name = str(tags.get("name") or tags.get("official_name") or name).strip()
        context = _context(tags)
        if not context and len(requested_parts) > 1:
            context = " ".join(requested_parts[:-1])
        label = f"{display_name} — {context}" if context and context.casefold() != display_name.casefold() else display_name
        key = (display_name.casefold(), tuple(round(value, 6) for value in bbox or ()))
        if bbox is None or key in seen:
            continue
        seen.add(key)
        try:
            osm_id = int(element.get("id") or 0)
        except (TypeError, ValueError):
            osm_id = 0
        candidates.append(
            (
                _score(tags, name, element),
                PlaceCandidate(
                    display_name,
                    label,
                    "administrative" if tags.get("boundary") == "administrative" else str(tags.get("place") or "place"),
                    str(tags.get("admin_level") or ""),
                    bbox,
                    str(element.get("type") or ""),
                    osm_id,
                    "overpass",
                    # This query only ever asks for administrative ways and
                    # relations, plus place nodes; only the former have areas.
                    tags.get("boundary") == "administrative"
                    and element.get("type") in ("relation", "way"),
                ),
            )
        )
    candidates.sort(key=lambda item: (-item[0], item[1].label.casefold()))
    return tuple(item[1] for item in candidates)
