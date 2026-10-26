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
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

MAX_ENTRIES = 15
_ALGORITHM_PREFIX = "zero2agentosm:"
_OUTPUT_KEYS = {"OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POLYGONS"}
_SCALARS = (str, int, float, bool)


def _json_safe(value: Any) -> bool:
    if value is None or isinstance(value, _SCALARS):
        return True
    if isinstance(value, list):
        return all(isinstance(item, _SCALARS) for item in value)
    return False


def make_entry(label: str, algorithm_id: str, parameters: Dict[str, Any],
               extent: Dict[str, Any], when: str, summary: str = "",
               style_scope: str = "") -> Dict[str, Any]:
    """A history entry; raises ValueError for anything it cannot store."""
    if not str(algorithm_id).startswith(_ALGORITHM_PREFIX):
        raise ValueError("only this plugin's own algorithms are remembered")
    stored = {}
    for key, value in parameters.items():
        if key in _OUTPUT_KEYS or key == "EXTENT":
            continue
        if not _json_safe(value):
            raise ValueError(f"parameter {key} cannot be stored")
        stored[str(key)] = list(value) if isinstance(value, list) else value
    box = {k: float(extent[k]) for k in ("xmin", "ymin", "xmax", "ymax")}
    if not (box["xmin"] < box["xmax"] and box["ymin"] < box["ymax"]):
        raise ValueError("the extent is empty")
    box["crs"] = str(extent.get("crs") or "EPSG:4326")
    return {
        "label": str(label)[:160],
        "algorithm": str(algorithm_id),
        "parameters": stored,
        "extent": box,
        "when": str(when),
        "summary": str(summary)[:160],
        "style_scope": str(style_scope),
    }


def _valid(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    try:
        return make_entry(entry["label"], entry["algorithm"], entry["parameters"],
                          entry["extent"], entry["when"], entry.get("summary", ""),
                          entry.get("style_scope", ""))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


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
            and a["extent"] == b["extent"])


def add(entries: List[Dict[str, Any]], entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Newest first; an identical earlier request moves to the top, not twice."""
    kept = [old for old in entries if not _same_request(old, entry)]
    return ([entry] + kept)[:MAX_ENTRIES]
