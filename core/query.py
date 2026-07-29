"""Bounded Overpass query construction shared by UI and Processing."""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, Tuple

from .catalog import GEOMETRY_KINDS, TagSpec

OVERPASS_ENDPOINTS: Tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OVERPASS_TIMEOUT_SECONDS = 45
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_FEATURES = 150_000
MAX_BBOX_AREA_KM2 = 100.0
MAX_SELECTORS = 32

_KEY_RE = re.compile(r"^[A-Za-z0-9_:.~-]{1,80}$")
_UNSAFE_VALUE_RE = re.compile(r"[\x00-\x1f\x7f\"\\;\[\]\(\){}]")


class QueryError(ValueError):
    """A bounded, user-actionable query or response error."""


def normalize_tag(key: object, value: object = "") -> Tuple[str, str]:
    key_text = str(key or "").strip()
    value_text = str(value or "").strip()
    if not _KEY_RE.fullmatch(key_text):
        raise QueryError("The OSM key contains unsupported characters.")
    if value_text == "*":
        value_text = ""
    if len(value_text) > 120 or _UNSAFE_VALUE_RE.search(value_text):
        raise QueryError("The OSM value contains unsupported query characters.")
    return key_text, value_text


def validate_bbox(
    south: object, west: object, north: object, east: object
) -> Tuple[float, float, float, float]:
    try:
        values = tuple(float(item) for item in (south, west, north, east))
    except (TypeError, ValueError) as exc:
        raise QueryError("The selected extent is not valid.") from exc
    south_f, west_f, north_f, east_f = values
    if not all(math.isfinite(item) for item in values):
        raise QueryError("The selected extent is not finite.")
    if not (
        -90 <= south_f < north_f <= 90
        and -180 <= west_f < east_f <= 180
    ):
        raise QueryError("The selected extent is outside WGS84 bounds.")
    mean_lat = math.radians((south_f + north_f) / 2)
    area = (
        (north_f - south_f)
        * 111.32
        * (east_f - west_f)
        * 111.32
        * max(0.01, abs(math.cos(mean_lat)))
    )
    if area <= 0 or area > MAX_BBOX_AREA_KM2:
        raise QueryError(
            f"The selected extent is {area:,.1f} km²; zoom below "
            f"{MAX_BBOX_AREA_KM2:,.0f} km²."
        )
    return values


def normalized_specs(specs: Iterable[TagSpec]) -> Tuple[TagSpec, ...]:
    unique = []
    seen = set()
    for spec in specs:
        if spec.geometry not in GEOMETRY_KINDS:
            raise QueryError("An unsupported geometry type was requested.")
        key, value = normalize_tag(spec.key, spec.value)
        row = TagSpec(key, value, spec.geometry)
        marker = (row.key, row.value, row.geometry)
        if marker not in seen:
            seen.add(marker)
            unique.append(row)
    if not unique:
        raise QueryError("At least one OSM tag selector is required.")
    if len(unique) > MAX_SELECTORS:
        raise QueryError("The preset contains too many OSM selectors.")
    return tuple(unique)


def build_query(
    specs: Iterable[TagSpec],
    bbox: Tuple[object, object, object, object],
) -> str:
    safe_specs = normalized_specs(specs)
    south, west, north, east = validate_bbox(*bbox)
    box = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    selectors = []
    for spec in safe_specs:
        tag = (
            f'["{spec.key}"="{spec.value}"]'
            if spec.value
            else f'["{spec.key}"]'
        )
        if spec.geometry == "point":
            selectors.append(f"  node{tag}({box});")
        elif spec.geometry == "line":
            selectors.append(f"  way{tag}({box});")
        else:
            selectors.append(f"  way{tag}({box});")
            selectors.append(f"  relation{tag}({box});")
    return (
        f"[out:json][timeout:{OVERPASS_TIMEOUT_SECONDS}];\n(\n"
        + "\n".join(dict.fromkeys(selectors))
        + "\n);\nout body geom;"
    )


def validate_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise QueryError("The OSM server returned an invalid response.")
    if str(payload.get("remark") or "").strip():
        raise QueryError("The OSM server timed out; zoom in and retry.")
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise QueryError("The OSM response has no elements list.")
    if len(elements) > MAX_FEATURES:
        raise QueryError("The OSM response is too large; zoom in and retry.")
    if any(not isinstance(item, dict) for item in elements):
        raise QueryError("The OSM response contains an invalid element.")
    return payload


def compact_tags(tags: object) -> str:
    clean = tags if isinstance(tags, dict) else {}
    text = json.dumps(
        clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(text) <= 16_000:
        return text
    bounded = {"_truncated": True}
    for key in sorted(clean):
        bounded[str(key)] = clean[key]
        candidate = json.dumps(
            bounded,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(candidate) > 15_900:
            bounded.pop(str(key), None)
            break
    return json.dumps(
        bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def matching_specs(
    tags: object, specs: Iterable[TagSpec], geometry: str
) -> Tuple[TagSpec, ...]:
    data = tags if isinstance(tags, dict) else {}
    return tuple(
        spec
        for spec in specs
        if spec.geometry == geometry
        and spec.key in data
        and (not spec.value or str(data.get(spec.key)) == spec.value)
    )
