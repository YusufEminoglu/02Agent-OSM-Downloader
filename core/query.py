"""Bounded Overpass query construction shared by UI and Processing."""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Pattern, Sequence, Tuple

from .catalog import GEOMETRY_KINDS, TagSpec
from .osm_areas import has_ring_members

# One entry per operator, asked in this order.  overpass.kumi.systems is gone:
# it is an alias of overpass.private.coffee (same machine, same certificate),
# so failing over to it only asked a dead host twice.  overpass-api.de comes
# last because its operators ask to be used only when the others cannot help.
OVERPASS_ENDPOINTS: Tuple[str, ...] = (
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)
#: `[timeout:N]` under QGIS's default 60 s network timeout: the transfer waits
#: T = 55 s and the server is given N = T - 15 = 40 s, leaving the 15 s an
#: accepted query can spend queued (core/transport.py derives both from the
#: user's actual QGIS setting).
OVERPASS_TIMEOUT_SECONDS = 40
#: The place-name search runs only when Nominatim failed; it claims little.
PLACE_SEARCH_TIMEOUT_SECONDS = 15
#: Memory one request asks the server to reserve: 128 MiB instead of the
#: 512 MiB Overpass assumes when a query declares nothing.  A dispatcher that
#: is nearly full admits the small claim and sheds the default one.
OVERPASS_MAXSIZE_BYTES = 134_217_728
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_FEATURES = 150_000
# One Overpass request stays small enough to be a polite, reliable request.
# Anything larger is split into a grid of these tiles rather than refused, so a
# whole district or city can be acquired without ever sending a heavy query.
MAX_TILE_AREA_KM2 = 100.0
#: A tile the server finds too heavy is split into four, at most twice:
#: 100 -> 25 -> 6.25 km².
MAX_TILE_DEPTH = 2
# Ceiling for a complete tiled job.  Past this the honest answer is that the
# user wants a regional extract, not an Overpass download.
MAX_BBOX_AREA_KM2 = 2_500.0
MAX_TILES = 40
#: Requests one download may send in total, split parts and retries included.
MAX_JOB_REQUESTS = 64
# Merged across every tile.  A single response is still capped at MAX_FEATURES.
MAX_TOTAL_FEATURES = 400_000
TOO_MANY_OBJECTS_MESSAGE = (
    f"The request returned more than {MAX_TOTAL_FEATURES:,} OSM objects. "
    "Reduce the extent or select fewer datasets."
)
MAX_SELECTORS = 32
MAX_ADVANCED_FILTERS = 6
MATCH_MODES = ("any", "all")
VALUE_MODES = ("exact", "regex")
MAX_REGEX_VALUE_LENGTH = 60

_KM_PER_DEGREE = 111.32

_KEY_RE = re.compile(r"^[A-Za-z0-9_:.~-]{1,80}$")
_UNSAFE_VALUE_RE = re.compile(r"[\x00-\x1f\x7f\"\\;\[\]\(\){}]")
# Regex-mode values still cannot break out of the Overpass QL `~"..."`
# string, so quotes, backslashes and statement separators stay banned; the
# regex metacharacters `()[]{}^$.*+?|` are allowed so patterns like
# `^(garage|shed)$` work. A slow pattern is bounded server-side by the
# query's own `[timeout:N]`, the same as any other selector; on this side
# `normalize_tag` refuses the nested repeats that could hang the client.
_UNSAFE_REGEX_VALUE_RE = re.compile(r'[\x00-\x1f\x7f"\\;]')


class QueryError(ValueError):
    """A bounded, user-actionable query or response error."""


#: The wildcard selector means "every tagged object here", minus the two
#: families the Places group owns.  Administrative boundaries are enormous
#: relations and place nodes are settlement labels; both swamp an as-is render
#: and are chosen deliberately from Places instead.  The Overpass filter and
#: `_value_matches` below have to agree, or features either arrive and get
#: dropped, or are asked for and never come.
WILDCARD_EXCLUDED_KEYS: Tuple[Tuple[str, str], ...] = (
    ("boundary", "administrative"),
    ("place", ""),
)
_WILDCARD_FILTER = "".join(
    f'["{key}"!="{value}"]' if value else f'[!"{key}"]'
    for key, value in WILDCARD_EXCLUDED_KEYS
)
#: The only relations the wildcard reads: areas.  Routes, route masters,
#: turn restrictions, stop areas, sites and collections are paths or groups,
#: often region-sized (in one 0.3 km² download 29 relations were 92% of the
#: bytes, one of them spanning the country), and drew as stray lines and
#: labelled points.
WILDCARD_RELATION_TYPES: Tuple[str, ...] = ("multipolygon", "boundary")
_WILDCARD_RELATION_FILTER = (
    '["type"~"^(' + "|".join(WILDCARD_RELATION_TYPES) + ')$"]'
)


def wildcard_excluded(tags: object) -> bool:
    """True when a wildcard request must skip this element.

    Compared exactly, as the server compares `["boundary"!="administrative"]`:
    a lower-cased match here refused what the server had sent.
    """
    if not isinstance(tags, dict):
        return False
    for key, value in WILDCARD_EXCLUDED_KEYS:
        actual = tags.get(key)
        if actual is None:
            continue
        if not value or str(actual) == value:
            return True
    return False


def wildcard_reads_relation(tags: object, members: object = None) -> bool:
    """True when the wildcard's relation selector covers this relation.

    Overpass is asked for `["type"~"^(multipolygon|boundary)$"]`.  Postpass
    strips `type` from every relation, so there an untyped relation counts
    when it has ring members ("outer"/"inner"), which only the relations it
    assembled as areas have.
    """
    data = tags if isinstance(tags, dict) else {}
    if "type" in data:
        return str(data["type"]) in WILDCARD_RELATION_TYPES
    return has_ring_members(members)


# ------------------------------------------------------------ regex mode
# A regex-mode value is matched twice: by the server, with POSIX extended
# regular expressions (glibc `regcomp`, C.UTF-8 locale), and again here, with
# Python's `re`, to tell which selector each returned object matched.  The
# two engines read some text differently, so a value is accepted only in the
# subset both read the same way, and its POSIX character classes are
# translated for the second reading.  Otherwise the server refuses the
# request (HTTP 400), or answers and this side drops everything it sent.
# Postpass, the fallback, matches with PostgreSQL's engine, which refuses a
# repeat count without its lower bound, so `{,n}` is sent as `{0,n}`.

#: The POSIX classes regex mode understands, as `[[:digit:]]` and so on.
POSIX_CLASSES: Tuple[str, ...] = (
    "alpha", "digit", "alnum", "upper", "lower", "space", "punct",
)
#: Each class as an exact ASCII part plus the Unicode categories it may take
#: beyond ASCII.  The categories cover what glibc's C.UTF-8 class holds (and
#: a little more), so a value the server matched is never refused here; the
#: surplus only matters when one object is checked against several patterns.
#: Measured against glibc 2.43 for every code point: nothing it puts in a
#: class is missing here, and ASCII is exact.  [:space:] is glibc's own list.
_SPACE_CLASS = "\\t-\\r \\u1680\\u2000-\\u2006\\u2008-\\u200a\\u2028\\u2029\\u205f\\u3000"
_CLASS_ASCII: Dict[str, str] = {
    "alpha": "A-Za-z",
    "digit": "0-9",
    "alnum": "0-9A-Za-z",
    "upper": "A-Z",
    "lower": "a-z",
    "punct": "!-/:-@\\[-`{-~",
}
_CLASS_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "alpha": ("Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Mc", "Me", "Nd", "Nl",
              "No", "So", "Cn"),
    "upper": ("Lu", "Lt", "Nl", "So", "Cn"),
    "lower": ("Ll", "Lt", "Lm", "Lo", "Mn", "Nl", "So", "Cn"),
    "punct": ("Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po", "Sm", "Sc", "Sk",
              "So", "Mn", "Mc", "Me", "No", "Cf", "Co", "Cn", "Zs"),
}
_CLASS_CATEGORIES["alnum"] = _CLASS_CATEGORIES["alpha"]
#: POSIX's guaranteed minimum for the largest repeat count (RE_DUP_MAX).
_MAX_REPEAT_COUNT = 255
_BOUND_RE = re.compile(r"\{(\d*)(,?)(\d*)\}")


@lru_cache(maxsize=1)
def _category_runs() -> Tuple[Tuple[int, int, str], ...]:
    """Every code point above ASCII as (first, last, Unicode category) runs."""
    runs: List[List[Any]] = []
    for code in range(0x80, 0x110000):
        category = unicodedata.category(chr(code))
        if runs and runs[-1][2] == category:
            runs[-1][1] = code
        else:
            runs.append([code, code, category])
    return tuple((first, last, category) for first, last, category in runs)


@lru_cache(maxsize=None)
def _class_text(name: str) -> str:
    """The body of a Python character class equal to or wider than [[:name:]]."""
    if name == "space":
        return _SPACE_CLASS
    categories = frozenset(_CLASS_CATEGORIES.get(name, ()))
    pieces = [_CLASS_ASCII[name]]
    start = end = -1
    for first, last, category in _category_runs():
        if category not in categories:
            continue
        if first == end + 1:
            end = last
            continue
        if start >= 0:
            pieces.append(_class_range(start, end))
        start, end = first, last
    if start >= 0:
        pieces.append(_class_range(start, end))
    return "".join(pieces)


def _class_range(first: int, last: int) -> str:
    def escape(code: int) -> str:
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"

    return escape(first) if first == last else f"{escape(first)}-{escape(last)}"


def _class_char(char: str) -> str:
    """One literal character inside a Python class, escaped where `re` reads it."""
    return "\\" + char if char in "\\]^-[&~|" else char


def _bracket(pattern: str, start: int) -> Tuple[int, str]:
    """Read the POSIX bracket expression at `start`; (end, Python class)."""
    index = start + 1
    negated = pattern.startswith("^", index)
    if negated:
        index += 1
    parts: List[str] = []
    first = True
    while True:
        if index >= len(pattern):
            raise QueryError("The regex value is not a valid pattern.")
        char = pattern[index]
        if char == "]" and not first:
            return index + 1, "[" + ("^" if negated else "") + "".join(parts) + "]"
        first = False
        kind = pattern[index + 1:index + 2]
        if char == "[" and kind in (":", "=", "."):
            close = pattern.find(kind + "]", index + 2)
            if close < 0:
                raise QueryError("The regex value is not a valid pattern.")
            name = pattern[index + 2:close]
            if kind != ":":
                raise QueryError(
                    f"The regex uses [{kind}{name}{kind}], which regex mode "
                    "does not support; write the characters themselves."
                )
            if name not in POSIX_CLASSES:
                raise QueryError(
                    f"The regex uses the unknown class [:{name}:]; regex mode "
                    "understands "
                    + ", ".join(f"[:{item}:]" for item in POSIX_CLASSES)
                    + "."
                )
            parts.append(_class_text(name))
            index = close + 2
            continue
        last = pattern[index + 2:index + 3]
        if kind == "-" and last not in ("", "]"):
            if last == "[" or ord(last) < ord(char):
                raise QueryError("The regex value is not a valid pattern.")
            parts.append(_class_char(char) + "-" + _class_char(last))
            index += 3
            continue
        parts.append(_class_char(char))
        index += 1


def _quantifier(pattern: str, index: int) -> Tuple[int, str]:
    """(end, "repeat" | "optional" | "") of the quantifier at `index`."""
    char = pattern[index:index + 1]
    if char in ("*", "+"):
        return index + 1, "repeat"
    if char == "?":
        return index + 1, "optional"
    if char != "{":
        return index, ""
    match = _BOUND_RE.match(pattern, index)
    if match is None or not (match.group(1) or match.group(3)):
        raise QueryError(
            f"The regex has a '{{' (character {index + 1}) that does not start "
            "a repeat count such as {2} or {1,3}; the OSM servers reject it. "
            "Put a literal brace in brackets: [{]."
        )
    low = int(match.group(1) or 0)
    high = int(match.group(3)) if match.group(3) else (None if match.group(2) else low)
    if max(low, high or 0) > _MAX_REPEAT_COUNT:
        raise QueryError(
            f"The regex repeats something more than {_MAX_REPEAT_COUNT} times."
        )
    if high is not None and high < low:
        raise QueryError("The regex value is not a valid pattern.")
    return match.end(), ("repeat" if high is None or high > 1 else "optional")


def _bound(text: str) -> str:
    """A quantifier as both servers read it: `{,n}` written as `{0,n}`.

    glibc takes a missing lower bound as 0; PostgreSQL, which runs the
    Postpass fallback, refuses it (HTTP 400).  Python reads both alike.
    """
    return "{0," + text[2:] if text.startswith("{,") else text


def _read_regex(pattern: str) -> Tuple[str, str]:
    """(Python pattern, pattern for the servers) of a regex-mode value.

    Raises QueryError naming the problem when the value uses what the OSM
    servers' POSIX engine rejects -- `(?...)` (inline flags, non-capturing
    groups, lookahead and lookbehind) or a `{` that is not a repeat count --
    or what Python would read differently: two repeat operators in a row
    (`a+?` is lazy in Python, `(a+)?` in POSIX).  A repeated group that
    itself repeats, such as `(a+)+`, `(a*)*` or `(a|b+)+`, is refused too:
    checking it against a long value can take minutes and cannot be
    canceled.  Backreferences need a backslash, which is never accepted.
    For this side, POSIX classes become Python classes and `$` becomes `\\Z`;
    everything else is copied as is, because outside brackets the accepted
    syntax means the same in both.  For the servers the value is kept as
    typed, except that a repeat count `{,n}` is written `{0,n}`.
    """
    out: List[str] = []
    server: List[str] = []
    #: Per open group: whether something inside it repeats.
    groups: List[bool] = []
    previous = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "[":
            start = index
            index, text = _bracket(pattern, index)
            out.append(text)
            server.append(pattern[start:index])
            previous = "atom"
            continue
        if char in "(|":
            if char == "(" and pattern.startswith("?", index + 1):
                raise QueryError(
                    "The regex uses '(?', which the OSM servers do not "
                    "support: no inline flags such as (?i), no (?:...) "
                    "groups, no lookahead or lookbehind. Use plain groups "
                    "such as ^(garage|shed)$."
                )
            if char == "(":
                groups.append(False)
            out.append(char)
            server.append(char)
            index += 1
            previous = ""
            continue
        if char == ")":
            inner = groups.pop() if groups else False
            index += 1
            end, kind = _quantifier(pattern, index)
            if kind == "repeat" and inner:
                raise QueryError(
                    "The regex repeats a group that already repeats, such as "
                    "(a+)+ or (a|b+)*; checking it can take minutes. Repeat "
                    "the inside or the group, not both."
                )
            if groups and (inner or kind):
                groups[-1] = True
            closed = ")" + _bound(pattern[index:end])
            out.append(closed)
            server.append(closed)
            index = end
            previous = "quantifier" if kind else "atom"
            continue
        if char in "*+?{":
            end, kind = _quantifier(pattern, index)
            if previous == "quantifier":
                raise QueryError(
                    "The regex has two repeat operators in a row (such as '+?' "
                    "or '*+'); the OSM servers and this plugin read that "
                    "differently. Use one."
                )
            if previous != "atom":
                raise QueryError("The regex value is not a valid pattern.")
            if groups:
                groups[-1] = True
            repeat = _bound(pattern[index:end])
            out.append(repeat)
            server.append(repeat)
            index = end
            previous = "quantifier"
            continue
        # POSIX `$` is the end of the value; Python's also matches before a
        # final newline, so it becomes \Z.
        out.append("\\Z" if char == "$" else char)
        server.append(char)
        index += 1
        previous = "anchor" if char in "^$" else "atom"
    return "".join(out), "".join(server)


def client_regex(pattern: str) -> str:
    """Check a regex-mode value and return the Python pattern for this side.

    See :func:`_read_regex` for what is refused and how it is translated.
    """
    return _read_regex(pattern)[0]


def server_regex(pattern: str) -> str:
    """Check a regex-mode value and return it as both servers read it.

    The value as typed, except `{,n}`, which becomes `{0,n}`: Overpass
    accepts both, and Postpass's PostgreSQL only the second.  Brackets are
    left alone, so `[{,]` stays a set of two characters.
    """
    return _read_regex(pattern)[1]


@lru_cache(maxsize=128)
def _compiled_client_regex(pattern: str) -> Pattern:
    # POSIX `.` matches a newline as well; DOTALL makes Python's agree.
    return re.compile(client_regex(pattern), re.DOTALL)


def normalize_tag(
    key: object, value: object = "", value_mode: str = "exact"
) -> Tuple[str, str]:
    mode = str(value_mode or "").strip().casefold()
    if mode not in VALUE_MODES:
        raise QueryError("The tag value mode is not valid.")
    key_text = str(key or "").strip()
    value_text = str(value or "").strip()
    if not _KEY_RE.fullmatch(key_text):
        raise QueryError("The OSM key contains unsupported characters.")
    if value_text == "*":
        # "Any value" in both modes, as a blank value is: a bare * is not a
        # pattern at all, and the selector becomes ["key"].
        value_text = ""
    if mode == "exact":
        if len(value_text) > 120 or _UNSAFE_VALUE_RE.search(value_text):
            raise QueryError(
                "The OSM value contains unsupported query characters."
            )
    elif value_text:
        if len(value_text) > MAX_REGEX_VALUE_LENGTH:
            raise QueryError(
                "The regex value is longer than "
                f"{MAX_REGEX_VALUE_LENGTH} characters."
            )
        if _UNSAFE_REGEX_VALUE_RE.search(value_text):
            raise QueryError(
                "The regex value contains unsupported query characters."
            )
        # Normalised here, so the Overpass query, the Postpass statement and
        # the matching on this side all read the same text.
        value_text = server_regex(value_text)
        try:
            _compiled_client_regex(value_text)
        except re.error as exc:
            raise QueryError("The regex value is not a valid pattern.") from exc
    return key_text, value_text


def excludes_tag(tags: object, key: object, value: object = "") -> bool:
    """Return True when a fetched element's tags match a client-side exclusion.

    Exclusion is applied after the OSM response arrives, not folded into the
    Overpass query text: subtracting a selector from a union in Overpass QL
    needs a second named set per request, which would multiply the already
    bounded query surface for no real benefit here. A blank value follows the
    same "any value" convention as every other tag field in this module.
    """
    key_text = str(key or "").strip()
    if not key_text:
        return False
    data = tags if isinstance(tags, dict) else {}
    if key_text not in data:
        return False
    value_text = str(value or "").strip()
    if not value_text or value_text == "*":
        return True
    return str(data.get(key_text)) == value_text


def _widest_longitude_scale(south: float, north: float) -> float:
    """Return the largest km-per-degree-of-longitude factor inside the box.

    Longitude degrees are widest nearest the equator, so the honest worst case
    for an area estimate is the latitude edge closest to it.  Using the mean
    latitude instead under-reports a tall box and lets an oversized tile
    through.
    """
    if south <= 0 <= north:
        return 1.0
    closest_to_equator = min(abs(south), abs(north))
    return max(0.01, math.cos(math.radians(closest_to_equator)))


def bbox_area_km2(
    south: object, west: object, north: object, east: object
) -> float:
    """Return the worst-case planar area of a WGS84 box in km²."""
    south_f, west_f, north_f, east_f = (
        float(item) for item in (south, west, north, east)
    )
    return (
        (north_f - south_f)
        * _KM_PER_DEGREE
        * (east_f - west_f)
        * _KM_PER_DEGREE
        * _widest_longitude_scale(south_f, north_f)
    )


def _checked_bbox(
    south: object, west: object, north: object, east: object
) -> Tuple[float, float, float, float]:
    """Validate WGS84 sanity only, without applying any area ceiling."""
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
    return values


def validate_bbox(
    south: object, west: object, north: object, east: object
) -> Tuple[float, float, float, float]:
    """Validate an extent that must fit in a single Overpass request."""
    values = _checked_bbox(south, west, north, east)
    area = bbox_area_km2(*values)
    if area <= 0 or area > MAX_TILE_AREA_KM2:
        raise QueryError(
            f"The selected extent is {area:,.1f} km²; a single OSM request is "
            f"limited to {MAX_TILE_AREA_KM2:,.0f} km²."
        )
    return values


def validate_job_bbox(
    south: object, west: object, north: object, east: object
) -> Tuple[float, float, float, float]:
    """Validate an extent that may be split across several tiled requests."""
    values = _checked_bbox(south, west, north, east)
    area = bbox_area_km2(*values)
    if area <= 0:
        raise QueryError("The selected extent has no area.")
    if area > MAX_BBOX_AREA_KM2:
        raise QueryError(
            f"The selected extent is {area:,.0f} km²; this downloader covers "
            f"up to {MAX_BBOX_AREA_KM2:,.0f} km². Zoom in, or download the "
            "area in parts."
        )
    return values


def tile_bbox(
    bbox: Tuple[object, object, object, object],
    max_tile_km2: float = MAX_TILE_AREA_KM2,
) -> Tuple[Tuple[float, float, float, float], ...]:
    """Split an extent into a grid of boxes that each fit one OSM request.

    Tiles are kept close to square so no single request degenerates into a
    long thin strip, which Overpass answers far more slowly than a compact
    box of the same area.
    """
    south, west, north, east = validate_job_bbox(*bbox)
    limit = max(1.0, float(max_tile_km2))
    area = bbox_area_km2(south, west, north, east)
    if area <= limit:
        return ((south, west, north, east),)

    scale = _widest_longitude_scale(south, north)
    height_km = (north - south) * _KM_PER_DEGREE
    width_km = (east - west) * _KM_PER_DEGREE * scale
    side_km = math.sqrt(limit)
    rows = max(1, math.ceil(height_km / side_km))
    columns = max(1, math.ceil(width_km / side_km))
    if rows * columns > MAX_TILES:
        raise QueryError(
            f"The selected extent needs {rows * columns} OSM requests; the "
            f"limit is {MAX_TILES}. Zoom in, or download the area in parts."
        )

    latitude_step = (north - south) / rows
    longitude_step = (east - west) / columns
    tiles = []
    for row in range(rows):
        tile_south = south + row * latitude_step
        tile_north = north if row == rows - 1 else tile_south + latitude_step
        for column in range(columns):
            tile_west = west + column * longitude_step
            tile_east = (
                east if column == columns - 1 else tile_west + longitude_step
            )
            tiles.append((tile_south, tile_west, tile_north, tile_east))
    return tuple(tiles)


def normalized_specs(
    specs: Iterable[TagSpec], value_mode: str = "exact"
) -> Tuple[TagSpec, ...]:
    unique = []
    seen = set()
    for spec in specs:
        if spec.geometry not in GEOMETRY_KINDS:
            raise QueryError("An unsupported geometry type was requested.")
        if spec.key == "*":
            key, value = "*", ""
        else:
            key, value = normalize_tag(spec.key, spec.value, value_mode)
        qualifiers = tuple(
            normalize_tag(qualifier_key, qualifier_value)
            for qualifier_key, qualifier_value in spec.qualifiers
        )
        row = TagSpec(key, value, spec.geometry, qualifiers)
        marker = (row.key, row.value, row.geometry, row.qualifiers)
        if marker not in seen:
            seen.add(marker)
            unique.append(row)
    if not unique:
        raise QueryError("At least one OSM tag selector is required.")
    if len(unique) > MAX_SELECTORS:
        raise QueryError("The preset contains too many OSM selectors.")
    return tuple(unique)


def is_admin_boundary_spec(spec: TagSpec) -> bool:
    """True for an exact `boundary=administrative` + `admin_level` request.

    An administrative area is a *relation*; the closed ways in it are the
    fragments its ring is built from.  Reading those fragments as areas turns
    one neighbourhood into a pile of part-polygons, so both the query and the
    feature reader have to agree that only relations carry admin polygons --
    which is why this predicate is shared rather than written twice.
    """
    return (
        spec.key == "boundary"
        and spec.value == "administrative"
        and any(key == "admin_level" and value for key, value in spec.qualifiers)
    )


def advanced_specs(
    filters: Iterable[Tuple[object, object]],
    geometries: Sequence[str],
    match_mode: str = "any",
    value_mode: str = "exact",
) -> Tuple[TagSpec, ...]:
    """Create bounded selectors for the structured advanced-query endpoint.

    Advanced requests deliberately accept tag fields rather than raw Overpass
    text.  Keeping this normalization in the shared core makes the dock,
    Processing provider and agent manifest use the same authority boundary.
    """
    mode = str(match_mode or "").strip().casefold()
    if mode not in MATCH_MODES:
        raise QueryError("The advanced match mode is not valid.")

    safe_filters = []
    seen_filters = set()
    for key, value in filters:
        normalized = normalize_tag(key, value, value_mode)
        if normalized not in seen_filters:
            seen_filters.add(normalized)
            safe_filters.append(normalized)
    if not safe_filters:
        raise QueryError("At least one advanced OSM tag is required.")
    if len(safe_filters) > MAX_ADVANCED_FILTERS:
        raise QueryError(
            f"Advanced queries accept at most {MAX_ADVANCED_FILTERS} tag filters."
        )
    if mode == "all":
        keys = [key for key, _value in safe_filters]
        if len(keys) != len(set(keys)):
            raise QueryError(
                "ALL matching cannot use the same OSM key more than once."
            )

    safe_geometries = tuple(dict.fromkeys(str(item) for item in geometries))
    if not safe_geometries or any(
        item not in GEOMETRY_KINDS for item in safe_geometries
    ):
        raise QueryError("Choose at least one supported geometry type.")
    return normalized_specs(
        (
            TagSpec(key, value, geometry)
            for geometry in safe_geometries
            for key, value in safe_filters
        ),
        value_mode,
    )


def overpass_header(timeout_s: int = OVERPASS_TIMEOUT_SECONDS) -> str:
    """The settings statement every request starts with.

    `[timeout:N]` is what the server may spend; `[maxsize]` is the memory it
    is asked to reserve.  Both are claims the dispatcher admits against, so
    they are kept as small as the request allows.
    """
    seconds = int(timeout_s)
    if seconds < 1:
        raise QueryError("The server time limit is not valid.")
    return f"[out:json][timeout:{seconds}][maxsize:{OVERPASS_MAXSIZE_BYTES}];"


_SETTINGS_RE = re.compile(r"\[out:json\](?:\[[a-z]+:[^\]\[]*\])*;")


def with_overpass_header(query_text: str, timeout_s: int) -> str:
    """Give an already rendered request this module's settings statement.

    The place-name search is rendered elsewhere with its own settings; this
    swaps them for `[timeout:N][maxsize]` without touching the selectors.
    """
    text = str(query_text or "")
    match = _SETTINGS_RE.match(text)
    if match is None:
        raise QueryError("The request has no Overpass settings statement.")
    return overpass_header(timeout_s) + text[match.end():]


def _render_query(
    safe_specs: Tuple[TagSpec, ...],
    scope: str,
    match_mode: str,
    preamble: str = "",
    value_mode: str = "exact",
    timeout_s: int = OVERPASS_TIMEOUT_SECONDS,
) -> str:
    """Render the bounded request.

    `scope` is the complete Overpass filter suffix appended to every selector,
    already parenthesised — one bounding box, or an area filter followed by a
    bounding box.
    """
    mode = str(match_mode or "").strip().casefold()
    if mode not in MATCH_MODES:
        raise QueryError("The query match mode is not valid.")
    values_mode = str(value_mode or "").strip().casefold()
    if values_mode not in VALUE_MODES:
        raise QueryError("The tag value mode is not valid.")
    operator = "~" if values_mode == "regex" else "="

    def _tag(spec: TagSpec) -> str:
        if spec.key == "*":
            primary = '[~"."~"."]' + _WILDCARD_FILTER
        else:
            primary = (
                f'["{spec.key}"{operator}"{spec.value}"]'
                if spec.value
                else f'["{spec.key}"]'
            )
        qualifiers = "".join(
            f'["{key}"="{value}"]' if value else f'["{key}"]'
            for key, value in spec.qualifiers
        )
        return primary + qualifiers

    def _relations(specs: Tuple[TagSpec, ...], tags: str) -> str:
        # The wildcard reads area relations only; core.postpass and
        # `_value_matches` apply the same rule.
        if any(spec.key == "*" for spec in specs):
            tags += _WILDCARD_RELATION_FILTER
        return f"  relation{tags}{scope};"

    selectors = []
    _is_admin_boundary = is_admin_boundary_spec

    if mode == "any":
        for spec in safe_specs:
            tag = _tag(spec)
            if spec.geometry == "point":
                if _is_admin_boundary(spec):
                    selectors.append(_relations((spec,), tag))
                else:
                    selectors.append(f"  node{tag}{scope};")
            elif spec.geometry == "line":
                selectors.append(f"  way{tag}{scope};")
                selectors.append(_relations((spec,), tag))
            else:
                if not _is_admin_boundary(spec):
                    selectors.append(f"  way{tag}{scope};")
                selectors.append(_relations((spec,), tag))
    else:
        for geometry in GEOMETRY_KINDS:
            geometry_specs = tuple(
                spec for spec in safe_specs if spec.geometry == geometry
            )
            if not geometry_specs:
                continue
            tags = "".join(_tag(spec) for spec in geometry_specs)
            admin_only = all(_is_admin_boundary(spec) for spec in geometry_specs)
            if geometry == "point":
                if admin_only:
                    selectors.append(_relations(geometry_specs, tags))
                else:
                    selectors.append(f"  node{tags}{scope};")
            elif geometry == "line":
                selectors.append(f"  way{tags}{scope};")
                selectors.append(_relations(geometry_specs, tags))
            else:
                if not admin_only:
                    selectors.append(f"  way{tags}{scope};")
                selectors.append(_relations(geometry_specs, tags))

    return (
        overpass_header(timeout_s)
        + "\n"
        + (f"{preamble}\n" if preamble else "")
        + "(\n"
        + "\n".join(dict.fromkeys(selectors))
        + "\n);\nout body geom;"
    )


def _box_scope(bbox: Tuple[float, float, float, float]) -> str:
    """Return a bounding-box filter, parenthesised and ready to append."""
    south, west, north, east = bbox
    return f"({south:.7f},{west:.7f},{north:.7f},{east:.7f})"


def _checked_mode(value: object, allowed: Tuple[str, ...], label: str) -> str:
    mode = str(value or "").strip().casefold()
    if mode not in allowed:
        raise QueryError(f"The {label} is not valid.")
    return mode


@dataclass(frozen=True)
class TileRequest:
    """One bounded Overpass request: which selectors, over which box.

    The fetch path works on these rather than on rendered text, so a part the
    server finds too heavy can be split, and another backend can render the
    same selection its own way.  Construction normalises and validates every
    field, so no unchecked text can reach a rendered query.
    """

    specs: Tuple[TagSpec, ...]
    bbox: Tuple[float, float, float, float]
    match_mode: str = "any"
    value_mode: str = "exact"
    area_id: int = 0
    depth: int = 0

    def __post_init__(self) -> None:
        value_mode = _checked_mode(self.value_mode, VALUE_MODES, "tag value mode")
        object.__setattr__(self, "value_mode", value_mode)
        object.__setattr__(
            self, "match_mode",
            _checked_mode(self.match_mode, MATCH_MODES, "query match mode"),
        )
        object.__setattr__(
            self, "specs", normalized_specs(self.specs, value_mode)
        )
        object.__setattr__(self, "bbox", _checked_bbox(*self.bbox))
        object.__setattr__(
            self, "area_id", validate_area_id(self.area_id) if self.area_id else 0
        )
        depth = int(self.depth)
        if not 0 <= depth <= MAX_TILE_DEPTH:
            raise QueryError("The request part depth is not valid.")
        object.__setattr__(self, "depth", depth)

    @property
    def area_km2(self) -> float:
        return bbox_area_km2(*self.bbox)

    @property
    def can_split(self) -> bool:
        return self.depth < MAX_TILE_DEPTH

    def render(self, timeout_s: int = OVERPASS_TIMEOUT_SECONDS) -> str:
        """The Overpass QL for this part, with `[timeout:N][maxsize]`."""
        box = _box_scope(self.bbox)
        if self.area_id:
            return _render_query(
                self.specs,
                f"(area.searchArea){box}",
                self.match_mode,
                preamble=f"area({self.area_id})->.searchArea;",
                value_mode=self.value_mode,
                timeout_s=timeout_s,
            )
        return _render_query(
            self.specs, box, self.match_mode,
            value_mode=self.value_mode, timeout_s=timeout_s,
        )

    def cache_key(self) -> str:
        """The text this part is cached under.

        Always rendered with the default time limit: a user whose QGIS
        network timeout is shorter sends a smaller `[timeout:N]`, but asks for
        the same data, so both share one cache entry.
        """
        return self.render()

    def split(self) -> Tuple["TileRequest", ...]:
        """Four quarter parts (south-west, south-east, north-west, north-east).

        The halves are computed once, so neighbouring parts share their edge
        exactly; an object on a seam comes back from both and the merge keeps
        one copy.
        """
        if not self.can_split:
            raise QueryError("This part is already the smallest size.")
        south, west, north, east = self.bbox
        middle_lat = (south + north) / 2.0
        middle_lon = (west + east) / 2.0
        quarters = (
            (south, west, middle_lat, middle_lon),
            (south, middle_lon, middle_lat, east),
            (middle_lat, west, north, middle_lon),
            (middle_lat, middle_lon, north, east),
        )
        return tuple(
            replace(self, bbox=quarter, depth=self.depth + 1)
            for quarter in quarters
        )


def plan_requests(
    specs: Iterable[TagSpec],
    bbox: Tuple[object, object, object, object],
    match_mode: str = "any",
    value_mode: str = "exact",
    area_id: object = 0,
    max_tile_km2: float = MAX_TILE_AREA_KM2,
) -> Tuple[TileRequest, ...]:
    """One bounded request per tile covering the whole extent.

    With an area id every part is also clipped to that mapped boundary; the
    extent still applies, so an area never makes a request unbounded.
    """
    safe_specs = normalized_specs(specs, value_mode)
    checked_area = validate_area_id(area_id) if area_id else 0
    return tuple(
        TileRequest(
            safe_specs, tile, match_mode, value_mode, checked_area
        )
        for tile in tile_bbox(bbox, max_tile_km2)
    )


def build_query(
    specs: Iterable[TagSpec],
    bbox: Tuple[object, object, object, object],
    match_mode: str = "any",
    value_mode: str = "exact",
) -> str:
    """Build the single bounded request for an extent that fits one tile."""
    return TileRequest(
        tuple(specs), validate_bbox(*bbox), match_mode, value_mode
    ).cache_key()


def build_queries(
    specs: Iterable[TagSpec],
    bbox: Tuple[object, object, object, object],
    match_mode: str = "any",
    max_tile_km2: float = MAX_TILE_AREA_KM2,
    value_mode: str = "exact",
) -> Tuple[str, ...]:
    """Build one bounded request per tile covering the whole extent.

    The texts are exactly the cache keys the download uses for these tiles.
    """
    return tuple(
        request.cache_key()
        for request in plan_requests(
            specs, bbox, match_mode, value_mode, max_tile_km2=max_tile_km2
        )
    )


def validate_area_id(value: object) -> int:
    """Validate an Overpass area identifier derived from an OSM object id.

    Overpass derives area ids from OSM ids: a relation becomes
    3600000000 + id and a way becomes 2400000000 + id.  Only those two ranges
    are accepted, so no other numeric text can reach the query.
    """
    try:
        area_id = int(value)
    except (TypeError, ValueError) as exc:
        raise QueryError("The place area identifier is not valid.") from exc
    relation_area = 3_600_000_000 <= area_id < 3_700_000_000
    way_area = 2_400_000_000 <= area_id < 2_500_000_000
    if not (relation_area or way_area):
        raise QueryError("The place area identifier is out of range.")
    return area_id


def build_area_query(
    specs: Iterable[TagSpec],
    area_id: object,
    bbox: Tuple[object, object, object, object],
    match_mode: str = "any",
    value_mode: str = "exact",
) -> str:
    """Build a request clipped to a real administrative boundary.

    An area filter follows the mapped boundary itself, so a district download
    stops at the district edge instead of at the corners of its bounding box.

    The extent is applied as well, and is not optional. This function renders
    exactly one request and therefore enforces the single-request ceiling; use
    :func:`build_area_queries` to tile a complete place download.
    """
    checked_id = validate_area_id(area_id)
    return TileRequest(
        tuple(specs), validate_bbox(*bbox), match_mode, value_mode, checked_id
    ).cache_key()


def build_area_queries(
    specs: Iterable[TagSpec],
    area_id: object,
    bbox: Tuple[object, object, object, object],
    match_mode: str = "any",
    max_tile_km2: float = MAX_TILE_AREA_KM2,
    value_mode: str = "exact",
) -> Tuple[str, ...]:
    """Build bounded, tiled requests clipped to one administrative area."""
    return tuple(
        request.cache_key()
        for request in plan_requests(
            specs, bbox, match_mode, value_mode,
            area_id=validate_area_id(area_id), max_tile_km2=max_tile_km2,
        )
    )


def preview_query(
    specs: Iterable[TagSpec],
    match_mode: str = "any",
    value_mode: str = "exact",
) -> str:
    """Return a display-only query with a non-executable extent placeholder."""
    return _render_query(
        normalized_specs(specs, value_mode),
        "(<selected extent>)",
        match_mode,
        value_mode=value_mode,
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


class ElementMerger:
    """Merge tiled Overpass responses as they arrive, one copy per object.

    Overpass returns any object whose geometry touches the requested box, so
    a road or building crossing a tile seam comes back from both tiles.
    Keeping the first copy of each (type, id) leaves exactly one feature.
    Counting while the tiles arrive lets a download stop at the object cap
    before it sends another request, instead of after the last one.
    """

    def __init__(self) -> None:
        self._elements: Dict[Tuple[str, str], Any] = {}
        self._stamps: List[str] = []

    def add(self, payload: Any) -> int:
        """Merge one validated response; return the objects held so far."""
        data = validate_payload(payload)
        stamp = osm_timestamp((data,))
        if stamp:
            self._stamps.append(stamp)
        merged = self._elements
        for element in data.get("elements", []):
            key = (str(element.get("type", "")), str(element.get("id", "")))
            if key not in merged:
                merged[key] = element
        return len(merged)

    @property
    def count(self) -> int:
        return len(self._elements)

    @property
    def over_limit(self) -> bool:
        return len(self._elements) > MAX_TOTAL_FEATURES

    def result(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"elements": list(self._elements.values())}
        if self._stamps:
            result["osm3s"] = {"timestamp_osm_base": min(self._stamps)}
        return result


def merge_elements(payloads: Iterable[Any]) -> Dict[str, Any]:
    """Merge tiled Overpass responses into one de-duplicated payload."""
    merger = ElementMerger()
    for payload in payloads:
        merger.add(payload)
        if merger.over_limit:
            raise QueryError(TOO_MANY_OBJECTS_MESSAGE)
    return merger.result()


def osm_timestamp(payloads: Iterable[Any]) -> str:
    """The data date of a response, as Overpass reports it (ISO 8601, UTC).

    ``osm3s.timestamp_osm_base`` is the moment of the OSM database the server
    answered from. Tiles can come from different mirrors at different minutes;
    the *oldest* is reported, because the merged result is only guaranteed to
    be at least that fresh. An absent or malformed field yields "".
    """
    stamps = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        meta = payload.get("osm3s")
        stamp = meta.get("timestamp_osm_base") if isinstance(meta, dict) else None
        if isinstance(stamp, str) and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", stamp):
            stamps.append(stamp)
    return min(stamps) if stamps else ""


#: Area above which a wildcard ("all tagged objects") request asks first. An
#: unfiltered pull carries every tagged object - typically ten to fifty times
#: the features of a filtered preset over the same ground.
HEAVY_WILDCARD_KM2 = 2.0


def is_heavy_wildcard(specs: Iterable[TagSpec], area_km2: float) -> bool:
    """True when ``specs`` include a wildcard and the area is above the limit."""
    return area_km2 > HEAVY_WILDCARD_KM2 and any(spec.key == "*" for spec in specs)


def compact_tags(tags: object) -> str:
    """The tags as compact JSON, at most 16,000 characters.

    Past the limit the value that does not fit is left out and the keys
    after it are still tried, so one oversized description cannot take every
    later tag with it; `_truncated` marks a shortened copy.
    """
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
    return json.dumps(
        bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _value_matches(
    data: Dict[str, Any],
    spec: TagSpec,
    value_mode: str,
    element_type: str = "",
    members: object = None,
) -> bool:
    if spec.key == "*":
        primary_matches = bool(data)
        if not primary_matches:
            return False
        if wildcard_excluded(data):
            return False
        if element_type == "relation" and not wildcard_reads_relation(
            data, members
        ):
            return False
        for key, value in spec.qualifiers:
            if key not in data:
                return False
            if value and str(data.get(key)) != value:
                return False
        return True
    if spec.key not in data:
        return False
    if not spec.value:
        primary_matches = True
    else:
        actual = str(data.get(spec.key))
        if value_mode == "regex":
            # Read as the server's POSIX engine reads it: its character
            # classes translated, its `.` matching any character.
            try:
                pattern = _compiled_client_regex(spec.value)
            except (QueryError, re.error):
                return False
            primary_matches = pattern.search(actual) is not None
        else:
            primary_matches = actual == spec.value
    if not primary_matches:
        return False
    for key, value in spec.qualifiers:
        if key not in data:
            return False
        if value and str(data.get(key)) != value:
            return False
    return True


def matching_specs(
    tags: object,
    specs: Iterable[TagSpec],
    geometry: str,
    match_mode: str = "any",
    value_mode: str = "exact",
    element_type: str = "",
    members: object = None,
) -> Tuple[TagSpec, ...]:
    """The specs of one geometry that an element's tags match.

    `element_type` and `members` describe the element itself: for a
    relation the wildcard counts only an area relation, as its Overpass
    selector does (:func:`wildcard_reads_relation`).
    """
    data = tags if isinstance(tags, dict) else {}
    candidates = tuple(spec for spec in specs if spec.geometry == geometry)
    matches = tuple(
        spec for spec in candidates
        if _value_matches(data, spec, value_mode, element_type, members)
    )
    mode = str(match_mode or "").strip().casefold()
    if mode not in MATCH_MODES:
        raise QueryError("The query match mode is not valid.")
    if mode == "all" and len(matches) != len(candidates):
        return ()
    return matches
