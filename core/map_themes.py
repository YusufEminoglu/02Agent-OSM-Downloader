"""Self-contained cartographic theme catalog for downloaded OSM layers.

The first twelve palettes are adapted from the sibling OSM Quick 3D plugin so
both PlanX tools can produce a familiar visual language without creating a
runtime dependency between their packages.  The final four palettes are
specific to 02Agent OSM Downloader.
"""
from __future__ import annotations

import re
from typing import Dict, Tuple

DEFAULT_MAP_THEME = "atlas"

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


MAP_THEMES: Dict[str, dict] = {
    "default": {
        "label": "Muted Planning",
        "description": "Quiet planning tones for everyday editing.",
        "bg": "#ffffff", "base": "#5e7274",
        "roads_major": "#e1846f", "roads_minor": "#eae5da",
        "greens": "#a9c08a", "water": "#a5c9eb", "trees": "#6f9e5c",
        "building_colors": {
            "residential": "#d8c3b1", "commercial": "#b7c2d0",
            "industrial": "#c6b9a4", "civic": "#cdd6d2",
            "worship": "#d8cfe2", "other": "#cac5bf",
        },
    },
    "cyber": {
        "label": "Tokyo Cyber",
        "description": "Dark neon contrast for striking network maps.",
        "bg": "#0a0b10", "base": "#121420",
        "roads_major": "#ff0055", "roads_minor": "#1a1d36",
        "greens": "#082c2b", "water": "#00ffcc", "trees": "#00ff66",
        "building_colors": {
            "residential": "#bc39fa", "commercial": "#ff8800",
            "industrial": "#ffdd00", "civic": "#00aaff",
            "worship": "#00ff66", "other": "#ff007f",
        },
    },
    "paper": {
        "label": "Editorial Paper",
        "description": "Warm ink-and-paper colors for reports and print.",
        "bg": "#fdfbf7", "base": "#e6dfd3",
        "roads_major": "#7c5c43", "roads_minor": "#eadecc",
        "greens": "#c2c5aa", "water": "#9ab8c2", "trees": "#6b705c",
        "building_colors": {
            "residential": "#ebd4c0", "commercial": "#c4d1db",
            "industrial": "#dbcfb8", "civic": "#cfdcd5",
            "worship": "#e8dfeb", "other": "#dcd8d3",
        },
    },
    "frost": {
        "label": "Nordic Frost",
        "description": "Cool minimal colors with crisp blue accents.",
        "bg": "#f5f7fa", "base": "#d8dee9",
        "roads_major": "#4c566a", "roads_minor": "#e5e9f0",
        "greens": "#a3be8c", "water": "#88c0d0", "trees": "#4c566a",
        "building_colors": {
            "residential": "#d8dee9", "commercial": "#81a1c1",
            "industrial": "#4c566a", "civic": "#88c0d0",
            "worship": "#b48ead", "other": "#e5e9f0",
        },
    },
    "noir": {
        "label": "Monochrome Noir",
        "description": "A restrained grayscale scheme with hard contrast.",
        "bg": "#1e1e1e", "base": "#121212",
        "roads_major": "#ffffff", "roads_minor": "#2e2e2e",
        "greens": "#262626", "water": "#3a3a3a", "trees": "#5c5c5c",
        "building_colors": {
            "residential": "#404040", "commercial": "#808080",
            "industrial": "#2a2a2a", "civic": "#a0a0a0",
            "worship": "#c0c0c0", "other": "#606060",
        },
    },
    "atlas": {
        "label": "Civic Atlas",
        "description": "Clean, legible civic cartography for general use.",
        "bg": "#f7f8f2", "base": "#566b6f",
        "roads_major": "#2f4858", "roads_minor": "#d8d7ce",
        "greens": "#7fb069", "water": "#3a86b5", "trees": "#3f7d4e",
        "building_colors": {
            "residential": "#d9c8b4", "commercial": "#a9bdd0",
            "industrial": "#c4b79a", "civic": "#b8d5ce",
            "worship": "#d9c7df", "other": "#cfd0c8",
        },
    },
    "mediterranean": {
        "label": "Mediterranean Survey",
        "description": "Sun, sea and terracotta for coastal urban studies.",
        "bg": "#fbf4e6", "base": "#8a7a5a",
        "roads_major": "#c65f46", "roads_minor": "#eadfc9",
        "greens": "#86a873", "water": "#4fa6c6", "trees": "#4e7f55",
        "building_colors": {
            "residential": "#e7c7aa", "commercial": "#b8cad8",
            "industrial": "#d4b98d", "civic": "#c8d9c9",
            "worship": "#ddc2d9", "other": "#d8cfc1",
        },
    },
    "nightprint": {
        "label": "Night Print",
        "description": "Dark cartography with warm arterial highlights.",
        "bg": "#11161a", "base": "#1b2628",
        "roads_major": "#ffd166", "roads_minor": "#2d3a3f",
        "greens": "#2a6f58", "water": "#118ab2", "trees": "#5fbf7a",
        "building_colors": {
            "residential": "#7d8da1", "commercial": "#f4a261",
            "industrial": "#a7a16d", "civic": "#74c0c2",
            "worship": "#b28fd8", "other": "#98a0a6",
        },
    },
    "anime": {
        "label": "Anime Cel",
        "description": "Bright cel-shaded colors for playful exploration.",
        "bg": "#dff3ff", "base": "#9fb6c4",
        "roads_major": "#ff7a8a", "roads_minor": "#ffe9c2",
        "greens": "#7ed957", "water": "#4fc3f7", "trees": "#3fa34d",
        "building_colors": {
            "residential": "#ffd28a", "commercial": "#8ed1ff",
            "industrial": "#c0a3ff", "civic": "#a0e8a0",
            "worship": "#ffb3d9", "other": "#ffe07a",
        },
    },
    "desert": {
        "label": "Desert Dunes",
        "description": "Warm arid tones for dryland settlements.",
        "bg": "#f6e7c8", "base": "#b79b6e",
        "roads_major": "#b5502f", "roads_minor": "#ead7b0",
        "greens": "#9caa6e", "water": "#6bb3c0", "trees": "#7a8c4f",
        "building_colors": {
            "residential": "#e8c79a", "commercial": "#cdb892",
            "industrial": "#bfa477", "civic": "#d8c4a0",
            "worship": "#caa57a", "other": "#d9c6a3",
        },
    },
    "candy": {
        "label": "Pastel Candy",
        "description": "Soft pastel separation for friendly thematic maps.",
        "bg": "#fff5fb", "base": "#e7c9dd",
        "roads_major": "#ff9ec4", "roads_minor": "#ffe3f1",
        "greens": "#bde8b5", "water": "#aee0f5", "trees": "#86cf9a",
        "building_colors": {
            "residential": "#ffd1e3", "commercial": "#cfe3ff",
            "industrial": "#e6dcff", "civic": "#d2f0dd",
            "worship": "#ffe1c2", "other": "#f3e6ff",
        },
    },
    "vapor": {
        "label": "Vaporwave",
        "description": "Retro neon magenta and cyan for expressive maps.",
        "bg": "#2a1a4a", "base": "#3a2563",
        "roads_major": "#ff71ce", "roads_minor": "#4b3a7a",
        "greens": "#2c5a6b", "water": "#01cdfe", "trees": "#05ffa1",
        "building_colors": {
            "residential": "#b967ff", "commercial": "#01cdfe",
            "industrial": "#7a5cff", "civic": "#05ffa1",
            "worship": "#ff71ce", "other": "#fffb96",
        },
    },
    "aegean": {
        "label": "Aegean Blueprint",
        "description": "Aegean blue, limestone and coral with calm clarity.",
        "bg": "#eef7f8", "base": "#173f5f",
        "roads_major": "#e76f51", "roads_minor": "#d5e4e8",
        "greens": "#7d9d65", "water": "#2589bd", "trees": "#476a4c",
        "building_colors": {
            "residential": "#eadcc8", "commercial": "#9fc4d5",
            "industrial": "#c9ae82", "civic": "#a8c9bd",
            "worship": "#d7bfd8", "other": "#d9d8cf",
        },
    },
    "blueprint": {
        "label": "Urban Blueprint",
        "description": "Deep drafting blue with precise cyan linework.",
        "bg": "#09243b", "base": "#0d3552",
        "roads_major": "#ffcc66", "roads_minor": "#2d5d78",
        "greens": "#24706b", "water": "#55c2e1", "trees": "#73d2a6",
        "building_colors": {
            "residential": "#4e7190", "commercial": "#48a9c5",
            "industrial": "#826f55", "civic": "#57b8a6",
            "worship": "#a78ac3", "other": "#69869d",
        },
    },
    "olive": {
        "label": "Olive & Terracotta",
        "description": "Anatolian earth colors for landscape-led planning.",
        "bg": "#f3eee2", "base": "#645f46",
        "roads_major": "#b85c38", "roads_minor": "#ddd3bd",
        "greens": "#7f8f55", "water": "#5596a5", "trees": "#53683f",
        "building_colors": {
            "residential": "#d8b99a", "commercial": "#a9bdba",
            "industrial": "#b69b75", "civic": "#b7c7a3",
            "worship": "#c5a7b6", "other": "#cfc5af",
        },
    },
    "signal": {
        "label": "Signal Contrast",
        "description": "High-clarity colors for rapid field interpretation.",
        "bg": "#f6f7f8", "base": "#263238",
        "roads_major": "#d73027", "roads_minor": "#c9ced3",
        "greens": "#2e8b57", "water": "#006db0", "trees": "#176b3a",
        "building_colors": {
            "residential": "#e3b778", "commercial": "#4994c4",
            "industrial": "#8f7b68", "civic": "#35a99a",
            "worship": "#9c62ad", "other": "#aab2b8",
        },
    },
}


def map_theme(theme_id: str) -> dict:
    """Return a known theme, falling back to the legible default."""
    return MAP_THEMES.get(str(theme_id), MAP_THEMES[DEFAULT_MAP_THEME])


def map_theme_items() -> Tuple[Tuple[str, str], ...]:
    """Return stable ``(id, label)`` entries in curated display order."""
    return tuple((theme_id, values["label"]) for theme_id, values in MAP_THEMES.items())


def map_theme_swatches(theme_id: str) -> Tuple[str, ...]:
    """Return six representative colors for the compact dock preview."""
    values = map_theme(theme_id)
    buildings = values["building_colors"]
    return (
        values["bg"], values["roads_major"], values["water"],
        values["greens"], buildings["residential"], buildings["commercial"],
    )


def validate_map_themes() -> Tuple[str, ...]:
    """Return catalog errors without requiring QGIS imports."""
    errors = []
    required = {
        "label", "description", "bg", "base", "roads_major",
        "roads_minor", "greens", "water", "trees", "building_colors",
    }
    building_keys = {
        "residential", "commercial", "industrial", "civic", "worship", "other",
    }
    for theme_id, values in MAP_THEMES.items():
        missing = required - set(values)
        if missing:
            errors.append(f"{theme_id}: missing {sorted(missing)}")
            continue
        if set(values["building_colors"]) != building_keys:
            errors.append(f"{theme_id}: invalid building categories")
        colors = [
            values[key] for key in (
                "bg", "base", "roads_major", "roads_minor", "greens", "water", "trees"
            )
        ] + list(values["building_colors"].values())
        if any(not _HEX_COLOR.fullmatch(color) for color in colors):
            errors.append(f"{theme_id}: invalid hex color")
    return tuple(errors)
