"""Curated OSM thematic presets and a small offline intent router."""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

GEOMETRY_KINDS = ("point", "line", "polygon")


@dataclass(frozen=True)
class TagSpec:
    key: str
    value: str
    geometry: str


@dataclass(frozen=True)
class Preset:
    preset_id: str
    group_id: str
    group_title: str
    title: str
    description: str
    tags: Tuple[TagSpec, ...]
    keywords: Tuple[str, ...] = ()

    @property
    def processing_label(self) -> str:
        return f"{self.group_title} — {self.title}"


def _tags(*rows: Tuple[str, str, str]) -> Tuple[TagSpec, ...]:
    return tuple(TagSpec(*row) for row in rows)


PRESETS: Tuple[Preset, ...] = (
    Preset(
        "urban_context",
        "urban_context",
        "Urban Context",
        "Street, built form & nature",
        "Road network, building footprints, individual trees and tree rows.",
        _tags(
            ("highway", "", "line"),
            ("building", "", "polygon"),
            ("natural", "tree", "point"),
            ("natural", "tree_row", "line"),
        ),
        (
            "urban context",
            "base map",
            "roads buildings trees",
            "road building tree",
            "city context",
        ),
    ),
    Preset(
        "urban_transit",
        "urban_context",
        "Urban Context",
        "Public transport context",
        "Bus stops, platforms, stations, entrances and mapped transit routes.",
        _tags(
            ("highway", "bus_stop", "point"),
            ("public_transport", "stop_position", "point"),
            ("public_transport", "platform", "point"),
            ("public_transport", "platform", "polygon"),
            ("public_transport", "station", "point"),
            ("public_transport", "station", "polygon"),
            ("amenity", "bus_station", "point"),
            ("amenity", "bus_station", "polygon"),
            ("railway", "station", "point"),
            ("railway", "tram_stop", "point"),
            ("railway", "subway_entrance", "point"),
            ("route", "bus", "line"),
            ("route", "tram", "line"),
        ),
        (
            "public transport context", "bus stops", "transit stops",
            "stations", "transit routes",
        ),
    ),
    Preset(
        "urban_amenities",
        "urban_context",
        "Urban Context",
        "Street amenities & public realm",
        "Crossings, signals, lighting, benches, drinking water and everyday services.",
        _tags(
            ("highway", "crossing", "point"),
            ("highway", "traffic_signals", "point"),
            ("highway", "street_lamp", "point"),
            ("amenity", "bench", "point"),
            ("amenity", "bicycle_parking", "point"),
            ("amenity", "drinking_water", "point"),
            ("amenity", "waste_basket", "point"),
            ("amenity", "toilets", "point"),
            ("amenity", "cafe", "point"),
            ("amenity", "restaurant", "point"),
        ),
        (
            "street amenities", "public realm", "pedestrian services",
            "crossings", "street furniture",
        ),
    ),
    Preset(
        "road_network", "network", "Network", "Road network",
        "All OSM highway ways for network analysis.",
        _tags(("highway", "", "line")),
        ("road network", "street network", "network"),
    ),
    Preset(
        "rail_network", "network", "Network", "Rail network",
        "Rail, tram, subway and light-rail ways.",
        _tags(
            ("railway", "rail", "line"), ("railway", "tram", "line"),
            ("railway", "subway", "line"), ("railway", "light_rail", "line"),
        ),
        ("rail network", "railway", "tram", "metro"),
    ),
    Preset(
        "multimodal_network", "network", "Network", "Multimodal network",
        "Road, rail, ferry-route ways and transport stops.",
        _tags(
            ("highway", "", "line"), ("railway", "", "line"),
            ("route", "ferry", "line"), ("highway", "bus_stop", "point"),
            ("railway", "station", "point"),
        ),
        ("multimodal", "complete network"),
    ),
    Preset(
        "buildings", "morphology", "Morphology", "Buildings",
        "Building footprints for urban morphology.",
        _tags(("building", "", "polygon")),
        ("building", "buildings", "footprint"),
    ),
    Preset(
        "land_use", "morphology", "Morphology", "Land use",
        "OSM land-use polygons.",
        _tags(("landuse", "", "polygon")),
        ("land use", "landuse", "zoning"),
    ),
    Preset(
        "urban_form", "morphology", "Morphology", "Urban form",
        "Buildings, land use, barriers and place centres.",
        _tags(
            ("building", "", "polygon"), ("landuse", "", "polygon"),
            ("barrier", "", "line"), ("place", "", "point"),
        ),
        ("urban form", "morphology", "urban fabric"),
    ),
    Preset(
        "green_spaces", "green_blue", "Green & Blue", "Green spaces",
        "Parks, forests, woods, gardens and grass areas.",
        _tags(
            ("leisure", "park", "polygon"), ("landuse", "forest", "polygon"),
            ("natural", "wood", "polygon"), ("leisure", "garden", "polygon"),
            ("landuse", "grass", "polygon"),
        ),
        ("green", "green space", "park", "parks", "forest", "forests"),
    ),
    Preset(
        "blue_network", "green_blue", "Green & Blue", "Blue network",
        "Water bodies, coastlines and waterways.",
        _tags(
            ("natural", "water", "polygon"), ("water", "", "polygon"),
            ("waterway", "", "line"), ("natural", "coastline", "line"),
        ),
        ("blue", "water", "waterway", "river"),
    ),
    Preset(
        "green_blue_all", "green_blue", "Green & Blue", "Green-blue system",
        "Combined green spaces and blue infrastructure.",
        _tags(
            ("leisure", "park", "polygon"), ("landuse", "forest", "polygon"),
            ("natural", "wood", "polygon"), ("natural", "water", "polygon"),
            ("waterway", "", "line"), ("natural", "coastline", "line"),
        ),
        ("green blue", "green-blue", "ecological network"),
    ),
    Preset(
        "bus_transit", "public_transport", "Public Transport", "Bus transit",
        "Bus stops, platforms and bus stations.",
        _tags(
            ("highway", "bus_stop", "point"),
            ("public_transport", "platform", "point"),
            ("public_transport", "platform", "polygon"),
            ("amenity", "bus_station", "point"),
            ("amenity", "bus_station", "polygon"),
        ),
        ("bus", "bus stop", "bus transit"),
    ),
    Preset(
        "rail_transit", "public_transport", "Public Transport", "Rail transit",
        "Stations, tram stops and subway entrances.",
        _tags(
            ("railway", "station", "point"), ("railway", "halt", "point"),
            ("railway", "tram_stop", "point"),
            ("railway", "subway_entrance", "point"),
        ),
        ("rail transit", "station", "tram stop", "metro station"),
    ),
    Preset(
        "public_transport_all", "public_transport", "Public Transport",
        "All public transport",
        "Combined bus, rail and mapped passenger-route features.",
        _tags(
            ("highway", "bus_stop", "point"),
            ("public_transport", "platform", "point"),
            ("public_transport", "platform", "polygon"),
            ("public_transport", "station", "point"),
            ("public_transport", "station", "polygon"),
            ("railway", "station", "point"), ("railway", "tram_stop", "point"),
            ("railway", "subway_entrance", "point"),
            ("route", "bus", "line"), ("route", "tram", "line"),
        ),
        ("public transport", "transit", "transit stops"),
    ),
    Preset(
        "worship", "religious", "Religious", "Places of worship",
        "Places of worship as points and footprints.",
        _tags(
            ("amenity", "place_of_worship", "point"),
            ("amenity", "place_of_worship", "polygon"),
        ),
        ("worship", "religious", "mosque", "church", "place of worship"),
    ),
    Preset(
        "religious_buildings", "religious", "Religious", "Religious buildings",
        "Mosques, churches, temples, synagogues, chapels and cathedrals.",
        _tags(
            ("building", "mosque", "polygon"),
            ("building", "church", "polygon"),
            ("building", "temple", "polygon"),
            ("building", "synagogue", "polygon"),
            ("building", "chapel", "polygon"),
            ("building", "cathedral", "polygon"),
        ),
        ("religious building", "mosque", "church", "temple"),
    ),
    Preset(
        "tourism", "tourism", "Tourism", "Tourism facilities",
        "OSM tourism features as points and areas.",
        _tags(("tourism", "", "point"), ("tourism", "", "polygon")),
        ("tourism", "tourist", "hotel", "museum"),
    ),
    Preset(
        "heritage", "tourism", "Tourism", "Historic heritage",
        "Historic and archaeological features.",
        _tags(("historic", "", "point"), ("historic", "", "polygon")),
        ("heritage", "historic", "archaeology"),
    ),
    Preset(
        "sport", "sport", "Sport", "Sports facilities",
        "Pitches, stadiums, sports centres and sport-tagged features.",
        _tags(
            ("leisure", "pitch", "polygon"), ("leisure", "stadium", "polygon"),
            ("leisure", "sports_centre", "polygon"),
            ("sport", "", "point"), ("sport", "", "polygon"),
        ),
        ("sport", "sports", "stadium", "pitch"),
    ),
    Preset(
        "cycle_network", "bike", "Bike", "Cycle network",
        "Dedicated cycleways and cycleway-tagged roads.",
        _tags(("highway", "cycleway", "line"), ("cycleway", "", "line")),
        ("cycle network", "cycleway", "bike network"),
    ),
    Preset(
        "bike_facilities", "bike", "Bike", "Bike facilities",
        "Bicycle parking, rental and repair facilities.",
        _tags(
            ("amenity", "bicycle_parking", "point"),
            ("amenity", "bicycle_rental", "point"),
            ("amenity", "bicycle_repair_station", "point"),
        ),
        ("bike facility", "bicycle parking", "bike parking"),
    ),
    Preset(
        "parking", "car", "Car", "Parking",
        "Parking areas, garages and parking points.",
        _tags(
            ("amenity", "parking", "point"),
            ("amenity", "parking", "polygon"),
            ("amenity", "parking_entrance", "point"),
        ),
        ("parking", "car park", "parking area"),
    ),
    Preset(
        "car_services", "car", "Car", "Car services",
        "Fuel, charging, car rental, car wash and service roads.",
        _tags(
            ("amenity", "fuel", "point"),
            ("amenity", "charging_station", "point"),
            ("amenity", "car_rental", "point"),
            ("amenity", "car_wash", "point"),
            ("highway", "service", "line"),
        ),
        ("car service", "fuel", "charging", "vehicle"),
    ),
    Preset(
        "traffic_controls", "traffic", "Traffic", "Traffic controls",
        "Signals, crossings, stop and give-way controls.",
        _tags(
            ("highway", "traffic_signals", "point"),
            ("highway", "crossing", "point"),
            ("highway", "stop", "point"),
            ("highway", "give_way", "point"),
        ),
        ("traffic signal", "crossing", "traffic control"),
    ),
    Preset(
        "traffic_calming", "traffic", "Traffic", "Traffic calming",
        "Traffic-calming devices and speed cameras.",
        _tags(
            ("traffic_calming", "", "point"),
            ("highway", "speed_camera", "point"),
        ),
        ("traffic calming", "speed camera"),
    ),
    Preset(
        "healthcare", "health", "Health", "Healthcare",
        "Hospitals, clinics, doctors, pharmacies and health facilities.",
        _tags(
            ("amenity", "hospital", "point"), ("amenity", "hospital", "polygon"),
            ("amenity", "clinic", "point"), ("amenity", "clinic", "polygon"),
            ("amenity", "doctors", "point"), ("amenity", "pharmacy", "point"),
            ("healthcare", "", "point"), ("healthcare", "", "polygon"),
        ),
        ("health", "hospital", "clinic", "pharmacy"),
    ),
    Preset(
        "education", "education", "Education", "Education",
        "Schools, universities, colleges, kindergartens and libraries.",
        _tags(
            ("amenity", "school", "point"), ("amenity", "school", "polygon"),
            ("amenity", "university", "point"),
            ("amenity", "university", "polygon"),
            ("amenity", "college", "point"), ("amenity", "college", "polygon"),
            ("amenity", "kindergarten", "point"),
            ("amenity", "kindergarten", "polygon"),
            ("amenity", "library", "point"), ("amenity", "library", "polygon"),
        ),
        ("education", "school", "university", "college"),
    ),
    Preset(
        "emergency", "emergency", "Emergency", "Emergency services",
        "Fire, police, ambulance, shelters and emergency facilities.",
        _tags(
            ("amenity", "fire_station", "point"),
            ("amenity", "fire_station", "polygon"),
            ("amenity", "police", "point"), ("amenity", "police", "polygon"),
            ("emergency", "ambulance_station", "point"),
            ("emergency", "assembly_point", "point"),
            ("amenity", "shelter", "point"), ("amenity", "shelter", "polygon"),
        ),
        ("emergency", "fire", "police", "assembly point"),
    ),
    Preset(
        "administrative_places", "places", "Places", "Administrative places",
        "Administrative boundaries and named place centres resolved from a command.",
        _tags(
            ("boundary", "administrative", "line"),
            ("boundary", "administrative", "polygon"),
            ("place", "", "point"),
        ),
        (
            "place", "places", "location", "locations", "city", "town",
            "district", "neighbourhood", "city", "county", "state",
            "location", "administrative boundary",
        ),
    ),
)

PRESETS_BY_ID: Dict[str, Preset] = {item.preset_id: item for item in PRESETS}
GROUPS: Tuple[Tuple[str, str], ...] = tuple(
    dict.fromkeys((item.group_id, item.group_title) for item in PRESETS)
)

GROUP_CONTEXTS: Dict[str, Tuple[str, str]] = {
    "urban_context": (
        "Start with the street-scale city context: movement, built form, nature, "
        "transit and everyday public-realm features.",
        "Related: Network, Green & Blue, Public Transport",
    ),
    "network": (
        "Explore movement systems as connected infrastructure: roads, rail, "
        "ferries and key interchange points.",
        "Related: Urban Context, Public Transport, Bike",
    ),
    "morphology": (
        "Study the physical structure of the city through buildings, land use, "
        "barriers and named place centres.",
        "Related: Urban Context, Places, Green & Blue",
    ),
    "green_blue": (
        "Read the ecological structure of a place through parks, vegetation, "
        "water bodies, coastlines and waterways.",
        "Related: Urban Context, Morphology",
    ),
    "public_transport": (
        "Inspect passenger movement from stops and platforms to stations, "
        "entrances and mapped bus or tram routes.",
        "Related: Urban Context, Network, Bike",
    ),
    "religious": (
        "Map places of worship and the building footprints associated with them.",
        "Related: Tourism, Heritage, Urban Context",
    ),
    "tourism": (
        "Combine visitor facilities with historic, archaeological and heritage "
        "features.",
        "Related: Religious, Sport, Urban Context",
    ),
    "sport": (
        "Locate pitches, stadiums, sports centres and sport-tagged places.",
        "Related: Green & Blue, Urban Context",
    ),
    "bike": (
        "Follow cycling infrastructure and the facilities that support everyday "
        "bike journeys.",
        "Related: Network, Public Transport, Urban Context",
    ),
    "car": (
        "Find parking, charging, fuel, rental, washing and service-road features.",
        "Related: Network, Traffic, Urban Context",
    ),
    "traffic": (
        "Inspect safety and movement-control features such as crossings, signals, "
        "stops, calming devices and cameras.",
        "Related: Network, Urban Context, Public Transport",
    ),
    "health": (
        "Map healthcare buildings, facilities, clinics, doctors and pharmacies.",
        "Related: Emergency, Urban Context, Places",
    ),
    "education": (
        "Map schools, universities, colleges, kindergartens and libraries.",
        "Related: Urban Context, Places, Public Transport",
    ),
    "emergency": (
        "Locate emergency response, police, fire, ambulance, shelter and assembly "
        "facilities.",
        "Related: Health, Urban Context, Places",
    ),
    "places": (
        "Resolve named administrative boundaries and place centres when a command "
        "needs a real-world location.",
        "Related: Urban Context, Morphology",
    ),
}


def group_context(group_id: str) -> Tuple[str, str]:
    """Return a short focus and relationship hint for a thematic group."""
    return GROUP_CONTEXTS.get(
        str(group_id or ""),
        ("Choose related OSM datasets for a bounded download.", ""),
    )

_TAG_RE = re.compile(
    r"\b([A-Za-z0-9_:.~-]{1,80})\s*=\s*(\*|[A-Za-z0-9_:.~-]{0,120})"
)


@dataclass(frozen=True)
class PromptIntent:
    mode: str
    preset_id: str = ""
    key: str = ""
    value: str = ""
    geometry: str = ""
    place_name: str = ""
    confidence: float = 0.0
    extra_preset_ids: Tuple[str, ...] = ()
    exclude_key: str = ""
    exclude_value: str = ""


def presets_for_group(group_id: str) -> Tuple[Preset, ...]:
    return tuple(item for item in PRESETS if item.group_id == group_id)


def get_preset(preset_id: str) -> Preset:
    try:
        return PRESETS_BY_ID[preset_id]
    except KeyError as exc:
        raise ValueError(f"Unknown preset: {preset_id}") from exc


def _normalized(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(
        char for char in folded if not unicodedata.combining(char)
    )
    return " ".join(re.sub(r"[^a-z0-9_:.~]+", " ", without_marks).split())


def _contains_phrase(text: str, phrase: str) -> bool:
    normalized_phrase = _normalized(phrase)
    return bool(
        normalized_phrase
        and f" {normalized_phrase} " in f" {text} "
    )


def _geometry_hint(text: str, key: str) -> str:
    words = _normalized(text)
    if any(
        _contains_phrase(words, item)
        for item in ("point", "poi", "stop")
    ):
        return "point"
    if any(
        _contains_phrase(words, item)
        for item in ("line", "network", "route")
    ):
        return "line"
    if any(
        _contains_phrase(words, item)
        for item in ("polygon", "area", "building")
    ):
        return "polygon"
    if key in {"building", "landuse", "leisure"}:
        return "polygon"
    if key in {"highway", "railway", "waterway"}:
        return "line"
    return "point"


_COMMAND_WORDS = {
    "download", "get", "fetch", "load", "data", "osm", "from", "for",
    "in", "at", "near", "within", "around", "of",
}


def _scored_presets(normalized: str) -> list:
    scored = []
    for preset in PRESETS:
        terms: Iterable[str] = preset.keywords + (
            preset.title, preset.group_title
        )
        score = sum(
            max(1, len(_normalized(term).split()))
            for term in terms
            if _contains_phrase(normalized, term)
        )
        if score:
            scored.append((score, preset.preset_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored


# Single-word keywords, titles and group titles (4+ characters, to avoid
# noisy short-word collisions) mapped back to every preset that uses them,
# built once so a typo-tolerant fallback never has to rescan the catalog per
# command. A *multi*-word term such as "roads buildings trees" or "Road
# network" is deliberately skipped here: exact matching already checks it as
# one phrase, but decomposed into individual words for fuzzy matching, a
# single word out of a longer phrase ("buildings" out of "roads buildings
# trees") is too weak a signal and would out-vote the dedicated single-word
# preset it is really a typo for.
_vocab_builder: Dict[str, list] = {}
for _preset in PRESETS:
    for _term in _preset.keywords + (_preset.title, _preset.group_title):
        _term_words = _normalized(_term).split()
        if len(_term_words) != 1:
            continue
        _word = _term_words[0]
        if len(_word) < 4:
            continue
        _bucket = _vocab_builder.setdefault(_word, [])
        if _preset.preset_id not in _bucket:
            _bucket.append(_preset.preset_id)
_KEYWORD_VOCAB: Dict[str, Tuple[str, ...]] = {
    word: tuple(ids) for word, ids in _vocab_builder.items()
}
del _vocab_builder, _preset, _term, _term_words, _word, _bucket

_FUZZY_CUTOFF = 0.82


def _fuzzy_preset(normalized: str) -> Tuple[str, float]:
    """A typo-tolerant fallback used only when no exact phrase matched.

    Offline commands have no spellchecker behind them, so a single
    misspelled word (`buildngs`, `helthcare`) would otherwise fall through
    to "no confident match" even though the intent is obvious to a human.
    Each matched vocabulary word contributes its similarity ratio split
    across every preset that uses it, so a word owned by a single preset
    outweighs one shared by several unrelated presets (otherwise a common
    word like "road" — shared by Road network and Urban Context — could tip
    a close call towards whichever preset id sorts last). Confidence is
    capped well below an exact match so the UI can still show this as a
    lower-certainty guess.
    """
    words = [word for word in normalized.split() if len(word) >= 4]
    if not words:
        return "", 0.0
    vocabulary = tuple(_KEYWORD_VOCAB)
    weights: Dict[str, float] = {}
    for word in words:
        for candidate in difflib.get_close_matches(
            word, vocabulary, n=3, cutoff=_FUZZY_CUTOFF
        ):
            ratio = difflib.SequenceMatcher(None, word, candidate).ratio()
            bucket = _KEYWORD_VOCAB[candidate]
            share = ratio / len(bucket)
            for preset_id in bucket:
                weights[preset_id] = weights.get(preset_id, 0.0) + share
    if not weights:
        return "", 0.0
    best_id = max(weights, key=lambda preset_id: (weights[preset_id], preset_id))
    return best_id, min(0.6, 0.4 + 0.1 * weights[best_id])


def _best_preset(normalized: str) -> Tuple[str, float]:
    scored = _scored_presets(normalized)
    if not scored:
        return _fuzzy_preset(normalized)
    score, preset_id = scored[0]
    return preset_id, min(1.0, 0.45 + score * 0.12)


def _top_presets(
    normalized: str, primary_id: str, limit: int = 2
) -> Tuple[str, ...]:
    """Return other presets in the same theme group the command also names.

    Restricted to the primary match's own group: the Presets tab only shows
    one theme group's checkboxes at a time, so a cross-group suggestion here
    could never actually be checked in the dock. Within one group, e.g.
    "download parks and water" naming both Green spaces and Blue network is
    directly representable as two checked datasets.
    """
    if not primary_id or primary_id not in PRESETS_BY_ID:
        return ()
    group_id = PRESETS_BY_ID[primary_id].group_id
    extras = [
        preset_id
        for score, preset_id in _scored_presets(normalized)
        if preset_id != primary_id
        and PRESETS_BY_ID[preset_id].group_id == group_id
    ]
    return tuple(extras[:limit])


_NEGATION_RE = re.compile(
    r"\b(?:without|excluding|except|exclude|no)\b\s+"
    r"([^,;]+?)"
    r"(?=\s+\b(?:in|at|near|within|around|for|of|and)\b|[,;]|$)",
    re.IGNORECASE,
)


def _extract_exclusion(raw: str) -> Tuple[str, str, str]:
    """Pull a "without X" clause out of a command before it is interpreted.

    Returns `(remaining_text, exclude_key, exclude_value)`. The negated
    clause is removed from the text handed back so a place or preset search
    over the rest of the command never sees the negated words. Only the
    negated phrase itself is removed — anything after a following place
    connector ("in Berlin") is preserved for the normal place extraction
    that runs afterwards.
    """
    match = _NEGATION_RE.search(raw)
    if not match:
        return raw, "", ""
    clause = match.group(1).strip(" ,;:-")
    remaining = (raw[: match.start()] + " " + raw[match.end():]).strip()
    if not clause:
        return remaining, "", ""
    tag_match = _TAG_RE.search(clause)
    if tag_match:
        return remaining, tag_match.group(1), tag_match.group(2).strip()
    # Exact catalog matches only: an exclusion is applied silently to the
    # request, unlike a positive match the user still reviews on the
    # Download tab, so a fuzzy guess here is more likely to hide data than
    # to help.
    scored = _scored_presets(_normalized(clause))
    if not scored:
        return remaining, "", ""
    _, preset_id = scored[0]
    representative = get_preset(preset_id).tags[0]
    return remaining, representative.key, representative.value


def _extract_place(raw: str, preset_id: str) -> str:
    """Extract a human place phrase without pretending to geocode it locally."""
    text = " ".join(str(raw or "").split()).strip(" ,;:-")
    if not text:
        return ""
    # Explicit location connectors allow arbitrary administrative names,
    # including names not shipped in a fixed list.
    connector = re.search(
        r"\b(?:in|at|near|within|around|for|of)\b\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if connector:
        candidate = connector.group(1).strip(" ,;:-")
        candidate = re.split(
            r"\b(?:download|get|fetch|load|with|where)\b",
            candidate,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" ,;:-")
        return candidate[:120]

    if preset_id:
        return ""

    candidate = re.sub(
        r"^(?:download|get|fetch|load|osm|data)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip(" ,;:-")
    words = _normalized(candidate).split()
    if (
        1 <= len(words) <= 6
        and (len(words) == 1 or any(char.isupper() for char in candidate))
        and not any(
            word in _COMMAND_WORDS for word in words
        )
    ):
        return candidate[:120]
    return ""


def interpret_prompt(text: object) -> PromptIntent:
    raw = str(text or "").strip()
    if not raw:
        return PromptIntent("none")
    tag_match = _TAG_RE.search(raw)
    if tag_match:
        key = tag_match.group(1)
        value = tag_match.group(2).strip()
        return PromptIntent(
            "custom", key=key, value=value,
            geometry=_geometry_hint(raw, key), confidence=1.0,
        )

    raw, exclude_key, exclude_value = _extract_exclusion(raw)

    normalized = _normalized(raw)
    words = tuple(normalized.split())
    has_road = any(
        word.startswith(("road", "street")) for word in words
    )
    has_building = any(
        word.startswith("building") for word in words
    )
    has_tree = any(
        word.startswith("tree") for word in words
    )
    preset_id, confidence = _best_preset(normalized)
    if has_road and (has_building or has_tree):
        preset_id, confidence = "urban_context", 1.0
    place_name = _extract_place(raw, preset_id)
    extra_preset_ids = _top_presets(normalized, preset_id)
    if place_name:
        return PromptIntent(
            "place",
            preset_id=preset_id or "administrative_places",
            place_name=place_name,
            confidence=max(confidence, 0.72),
            extra_preset_ids=extra_preset_ids,
            exclude_key=exclude_key,
            exclude_value=exclude_value,
        )
    if not preset_id:
        return PromptIntent("none")
    return PromptIntent(
        "preset",
        preset_id=preset_id,
        confidence=confidence,
        extra_preset_ids=extra_preset_ids,
        exclude_key=exclude_key,
        exclude_value=exclude_value,
    )
