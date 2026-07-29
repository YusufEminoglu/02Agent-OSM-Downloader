"""Curated OSM thematic presets and a small offline intent router."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

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
        "road_network", "network", "Network", "Road network",
        "All OSM highway ways for network analysis.",
        _tags(("highway", "", "line")),
        ("road network", "street network", "yol ağı", "yollar", "network"),
    ),
    Preset(
        "rail_network", "network", "Network", "Rail network",
        "Rail, tram, subway and light-rail ways.",
        _tags(
            ("railway", "rail", "line"), ("railway", "tram", "line"),
            ("railway", "subway", "line"), ("railway", "light_rail", "line"),
        ),
        ("rail network", "railway", "demiryolu", "tram", "metro ağı"),
    ),
    Preset(
        "multimodal_network", "network", "Network", "Multimodal network",
        "Road, rail, ferry-route ways and transport stops.",
        _tags(
            ("highway", "", "line"), ("railway", "", "line"),
            ("route", "ferry", "line"), ("highway", "bus_stop", "point"),
            ("railway", "station", "point"),
        ),
        ("multimodal", "complete network", "tüm ağ", "ulaşım ağı"),
    ),
    Preset(
        "buildings", "morphology", "Morphology", "Buildings",
        "Building footprints for urban morphology.",
        _tags(("building", "", "polygon")),
        ("building", "buildings", "bina", "binalar", "footprint"),
    ),
    Preset(
        "land_use", "morphology", "Morphology", "Land use",
        "OSM land-use polygons.",
        _tags(("landuse", "", "polygon")),
        ("land use", "landuse", "arazi kullanımı", "kullanım"),
    ),
    Preset(
        "urban_form", "morphology", "Morphology", "Urban form",
        "Buildings, land use, barriers and place centres.",
        _tags(
            ("building", "", "polygon"), ("landuse", "", "polygon"),
            ("barrier", "", "line"), ("place", "", "point"),
        ),
        ("urban form", "morphology", "morfoloji", "kent formu", "urban fabric"),
    ),
    Preset(
        "green_spaces", "green_blue", "Green & Blue", "Green spaces",
        "Parks, forests, woods, gardens and grass areas.",
        _tags(
            ("leisure", "park", "polygon"), ("landuse", "forest", "polygon"),
            ("natural", "wood", "polygon"), ("leisure", "garden", "polygon"),
            ("landuse", "grass", "polygon"),
        ),
        ("green", "green space", "park", "forest", "yeşil", "orman"),
    ),
    Preset(
        "blue_network", "green_blue", "Green & Blue", "Blue network",
        "Water bodies, coastlines and waterways.",
        _tags(
            ("natural", "water", "polygon"), ("water", "", "polygon"),
            ("waterway", "", "line"), ("natural", "coastline", "line"),
        ),
        ("blue", "water", "waterway", "mavi", "su", "dere", "nehir"),
    ),
    Preset(
        "green_blue_all", "green_blue", "Green & Blue", "Green-blue system",
        "Combined green spaces and blue infrastructure.",
        _tags(
            ("leisure", "park", "polygon"), ("landuse", "forest", "polygon"),
            ("natural", "wood", "polygon"), ("natural", "water", "polygon"),
            ("waterway", "", "line"), ("natural", "coastline", "line"),
        ),
        ("green blue", "green-blue", "yeşil mavi", "ekolojik ağ"),
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
        ("bus", "bus stop", "otobüs", "durak", "bus transit"),
    ),
    Preset(
        "rail_transit", "public_transport", "Public Transport", "Rail transit",
        "Stations, tram stops and subway entrances.",
        _tags(
            ("railway", "station", "point"), ("railway", "halt", "point"),
            ("railway", "tram_stop", "point"),
            ("railway", "subway_entrance", "point"),
        ),
        ("rail transit", "station", "tram stop", "metro", "istasyon"),
    ),
    Preset(
        "public_transport_all", "public_transport", "Public Transport",
        "All public transport",
        "Combined bus and rail passenger facilities.",
        _tags(
            ("highway", "bus_stop", "point"),
            ("public_transport", "platform", "point"),
            ("public_transport", "platform", "polygon"),
            ("public_transport", "station", "point"),
            ("public_transport", "station", "polygon"),
            ("railway", "station", "point"), ("railway", "tram_stop", "point"),
            ("railway", "subway_entrance", "point"),
        ),
        ("public transport", "toplu taşıma", "transit", "ulaşım durakları"),
    ),
    Preset(
        "worship", "religious", "Religious", "Places of worship",
        "Places of worship as points and footprints.",
        _tags(
            ("amenity", "place_of_worship", "point"),
            ("amenity", "place_of_worship", "polygon"),
        ),
        ("worship", "religious", "cami", "kilise", "ibadet", "dini"),
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
        ("religious building", "dini yapı", "mosque", "church", "temple"),
    ),
    Preset(
        "tourism", "tourism", "Tourism", "Tourism facilities",
        "OSM tourism features as points and areas.",
        _tags(("tourism", "", "point"), ("tourism", "", "polygon")),
        ("tourism", "tourist", "turizm", "otel", "museum", "müze"),
    ),
    Preset(
        "heritage", "tourism", "Tourism", "Historic heritage",
        "Historic and archaeological features.",
        _tags(("historic", "", "point"), ("historic", "", "polygon")),
        ("heritage", "historic", "archaeology", "tarihi", "arkeoloji"),
    ),
    Preset(
        "sport", "sport", "Sport", "Sports facilities",
        "Pitches, stadiums, sports centres and sport-tagged features.",
        _tags(
            ("leisure", "pitch", "polygon"), ("leisure", "stadium", "polygon"),
            ("leisure", "sports_centre", "polygon"),
            ("sport", "", "point"), ("sport", "", "polygon"),
        ),
        ("sport", "sports", "stadium", "pitch", "spor", "stadyum"),
    ),
    Preset(
        "cycle_network", "bike", "Bike", "Cycle network",
        "Dedicated cycleways and cycleway-tagged roads.",
        _tags(("highway", "cycleway", "line"), ("cycleway", "", "line")),
        ("cycle network", "cycleway", "bike network", "bisiklet ağı"),
    ),
    Preset(
        "bike_facilities", "bike", "Bike", "Bike facilities",
        "Bicycle parking, rental and repair facilities.",
        _tags(
            ("amenity", "bicycle_parking", "point"),
            ("amenity", "bicycle_rental", "point"),
            ("amenity", "bicycle_repair_station", "point"),
        ),
        ("bike facility", "bicycle parking", "bisiklet park", "bisiklet"),
    ),
    Preset(
        "parking", "car", "Car", "Parking",
        "Parking areas, garages and parking points.",
        _tags(
            ("amenity", "parking", "point"),
            ("amenity", "parking", "polygon"),
            ("amenity", "parking_entrance", "point"),
        ),
        ("parking", "car park", "otopark", "park yeri"),
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
        ("car service", "fuel", "charging", "benzin", "şarj", "araç"),
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
        ("traffic signal", "crossing", "trafik ışığı", "sinyal", "yaya geçidi"),
    ),
    Preset(
        "traffic_calming", "traffic", "Traffic", "Traffic calming",
        "Traffic-calming devices and speed cameras.",
        _tags(
            ("traffic_calming", "", "point"),
            ("highway", "speed_camera", "point"),
        ),
        ("traffic calming", "speed camera", "kasis", "hız kamerası"),
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
        ("health", "hospital", "clinic", "sağlık", "hastane", "eczane"),
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
        ("education", "school", "university", "eğitim", "okul", "üniversite"),
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
        ("emergency", "fire", "police", "acil", "itfaiye", "polis", "toplanma"),
    ),
)

PRESETS_BY_ID: Dict[str, Preset] = {item.preset_id: item for item in PRESETS}
GROUPS: Tuple[Tuple[str, str], ...] = tuple(
    dict.fromkeys((item.group_id, item.group_title) for item in PRESETS)
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
    confidence: float = 0.0


def presets_for_group(group_id: str) -> Tuple[Preset, ...]:
    return tuple(item for item in PRESETS if item.group_id == group_id)


def get_preset(preset_id: str) -> Preset:
    try:
        return PRESETS_BY_ID[preset_id]
    except KeyError as exc:
        raise ValueError(f"Unknown preset: {preset_id}") from exc


def _normalized(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return " ".join(
        "".join(char for char in folded if not unicodedata.combining(char)).split()
    )


def _geometry_hint(text: str, key: str) -> str:
    words = _normalized(text)
    if any(item in words for item in ("point", "nokta", "poi", "durak")):
        return "point"
    if any(item in words for item in ("line", "çizgi", "hat", "yol", "network")):
        return "line"
    if any(item in words for item in ("polygon", "poligon", "alan", "bina")):
        return "polygon"
    if key in {"building", "landuse", "leisure"}:
        return "polygon"
    if key in {"highway", "railway", "waterway"}:
        return "line"
    return "point"


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

    normalized = _normalized(raw)
    scored = []
    for preset in PRESETS:
        terms: Iterable[str] = preset.keywords + (preset.title, preset.group_title)
        score = sum(
            max(1, len(_normalized(term).split()))
            for term in terms
            if _normalized(term) in normalized
        )
        if score:
            scored.append((score, preset.preset_id))
    if not scored:
        return PromptIntent("none")
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, preset_id = scored[0]
    confidence = min(1.0, 0.45 + best_score * 0.12)
    return PromptIntent("preset", preset_id=preset_id, confidence=confidence)
