# -*- coding: utf-8 -*-
"""Recent downloads: what was asked, where, and when - so it can be asked again.

A download is remembered as the Processing request that produced it (the
algorithm id and its parameters, outputs excluded), the area it covered and
the time it ran. Re-running an entry sends the identical request again, so
the answer is today's OSM data for last month's question.

The store is a JSON list kept in one QGIS setting; this module owns the
format and never touches QGIS, so it is testable on its own. Anything that
does not parse as an entry is dropped on load rather than raised - a
corrupted setting must cost the history, never the plugin.

Datasets are stored by preset id, never by position: the Processing PRESET
parameter is an index into the catalogue, and an index saved before a preset
was added would quietly run a different dataset. Entries written by 1.5.x
stored those indexes; they are read through the catalogue order of that time
and rewritten with ids.

A download that finished with parts missing is stored with
``"incomplete": true``. Re-running it sends the same request, so the parts
that arrived come from the cache and only the missing ones are fetched; a
complete re-run replaces the entry, because both are the same request.
Entries without the key, including every one written before 1.6, are
complete.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_ENTRIES = 15
_ALGORITHM_PREFIX = "zero2agentosm:"
_OUTPUT_KEYS = {"OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POLYGONS"}
_SCALARS = (str, int, float, bool)
#: Algorithms whose PRESET parameter selects catalogue datasets.
PRESET_ALGORITHMS = {
    "zero2agentosm:download_preset",
    "zero2agentosm:download_place",
}
#: The catalogue order of 1.5.0 and 1.5.1, the versions that stored PRESET
#: indexes: index i of such an entry meant LEGACY_PRESET_ORDER[i].
LEGACY_PRESET_ORDER: Tuple[str, ...] = (
    "urban_context", "urban_transit", "urban_amenities", "buildings",
    "land_use", "urban_form", "multimodal_network", "rail_network",
    "road_network", "admin_level_2", "admin_level_3", "admin_level_4",
    "admin_level_5", "admin_level_6", "admin_level_7", "admin_level_8",
    "admin_level_9", "admin_level_10", "place_centres",
    "osm_as_is_full_visible_stack", "osm_as_is_mobility",
    "osm_as_is_overview", "osm_as_is_urban_detail", "bike_facilities",
    "cycle_network", "car_services", "parking", "education", "emergency",
    "blue_network", "green_spaces", "green_blue_all", "healthcare",
    "public_transport_all", "bus_transit", "rail_transit", "worship",
    "religious_buildings", "sport", "heritage", "tourism", "traffic_calming",
    "traffic_controls",
)


def _json_safe(value: Any) -> bool:
    if value is None or isinstance(value, _SCALARS):
        return True
    if isinstance(value, list):
        return all(isinstance(item, _SCALARS) for item in value)
    return False


def _indexes(value: Any) -> List[int]:
    items = value if isinstance(value, list) else [value]
    if not items or any(isinstance(item, bool) or not isinstance(item, int) for item in items):
        raise ValueError("PRESET is not a list of indexes")
    return list(items)


def make_entry(label: str, algorithm_id: str, parameters: Dict[str, Any],
               extent: Dict[str, Any], when: str, summary: str = "",
               style_scope: str = "", presets: Sequence[str] = (),
               where: str = "", incomplete: bool = False) -> Dict[str, Any]:
    """A history entry; raises ValueError for anything it cannot store.

    For the preset algorithms ``presets`` names the datasets by id and any
    PRESET indexes in ``parameters`` are left out; ``where`` is a short
    place name or position that tells two areas apart in the Recent list;
    ``incomplete`` marks a download that finished with parts missing.
    """
    if not str(algorithm_id).startswith(_ALGORITHM_PREFIX):
        raise ValueError("only this plugin's own algorithms are remembered")
    preset_ids = [str(item) for item in presets if str(item)]
    if str(algorithm_id) in PRESET_ALGORITHMS and not preset_ids:
        raise ValueError("a preset download needs its preset ids")
    stored = {}
    for key, value in parameters.items():
        if key in _OUTPUT_KEYS or key == "EXTENT":
            continue
        if key == "PRESET" and preset_ids:
            continue
        if not _json_safe(value):
            raise ValueError(f"parameter {key} cannot be stored")
        stored[str(key)] = list(value) if isinstance(value, list) else value
    box = {k: float(extent[k]) for k in ("xmin", "ymin", "xmax", "ymax")}
    if not (box["xmin"] < box["xmax"] and box["ymin"] < box["ymax"]):
        raise ValueError("the extent is empty")
    box["crs"] = str(extent.get("crs") or "EPSG:4326")
    entry = {
        "label": str(label)[:160],
        "algorithm": str(algorithm_id),
        "parameters": stored,
        "extent": box,
        "when": str(when),
        "summary": str(summary)[:160],
        "style_scope": str(style_scope),
        "where": str(where or "")[:80],
    }
    if preset_ids:
        entry["presets"] = preset_ids
    if incomplete:
        entry["incomplete"] = True
    return entry


def _valid(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    try:
        presets = entry.get("presets") or ()
        if not isinstance(presets, (list, tuple)):
            return None
        if not presets and entry["algorithm"] in PRESET_ALGORITHMS:
            # Written by 1.5.x: PRESET held indexes into that catalogue.
            presets = [
                LEGACY_PRESET_ORDER[index]
                for index in _indexes(entry["parameters"]["PRESET"])
            ]
        # Only a stored true marks an entry incomplete: "false", 1 or any
        # other value in a damaged setting reads as complete.
        return make_entry(entry["label"], entry["algorithm"], entry["parameters"],
                          entry["extent"], entry["when"], entry.get("summary", ""),
                          entry.get("style_scope", ""), presets,
                          entry.get("where", ""), entry.get("incomplete") is True)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None


def request_parameters(entry: Dict[str, Any], preset_order: Sequence[str]) -> Dict[str, Any]:
    """The Processing parameters that repeat ``entry`` with today's catalogue.

    ``preset_order`` is the current catalogue's preset ids in PRESET order.
    Raises ValueError when a stored dataset no longer exists.
    """
    parameters = dict(entry["parameters"])
    presets = entry.get("presets") or ()
    if presets:
        order = list(preset_order)
        missing = [preset_id for preset_id in presets if preset_id not in order]
        if missing:
            raise ValueError(
                f"dataset {', '.join(missing)} is no longer in the catalogue"
            )
        parameters["PRESET"] = [order.index(preset_id) for preset_id in presets]
    return parameters


def loads(text: object) -> List[Dict[str, Any]]:
    """Entries from the stored JSON; malformed content yields what is valid."""
    try:
        data = json.loads(str(text or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    entries = [entry for entry in (_valid(item) for item in data) if entry]
    return entries[:MAX_ENTRIES]


def dumps(entries: List[Dict[str, Any]]) -> str:
    return json.dumps(entries[:MAX_ENTRIES], ensure_ascii=False, sort_keys=True)


def _same_request(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return (a["algorithm"] == b["algorithm"] and a["parameters"] == b["parameters"]
            and a.get("presets") == b.get("presets") and a["extent"] == b["extent"])


def add(entries: List[Dict[str, Any]], entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Newest first; an identical earlier request moves to the top, not twice."""
    kept = [old for old in entries if not _same_request(old, entry)]
    return ([entry] + kept)[:MAX_ENTRIES]
