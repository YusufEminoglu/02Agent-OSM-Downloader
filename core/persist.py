# -*- coding: utf-8 -*-
"""Keep downloads: GeoPackage output with embedded styles, and data provenance.

A download used to exist only as temporary memory layers, gone when QGIS
closed. With a GeoPackage target every result layer is written as its own
table into one file, its current style is stored inside that file
(``layer_styles``, as the default style) so the file opens styled anywhere,
and the layers added to the project are the file-backed ones.

Provenance travels with every result layer either way: the OpenStreetMap
attribution ODbL requires, and the date of the OSM database the server
answered from, in the layer metadata and as a custom property.
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Iterable, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsLayerMetadata,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"
TIMESTAMP_PROPERTY = "zero2agent/osm_timestamp"
_TABLE_RE = re.compile(r"[^A-Za-z0-9_]+")


def table_name(label: str, suffix: str, used: Iterable[str] = ()) -> str:
    """A GeoPackage-safe, unique table name built from a download label."""
    base = _TABLE_RE.sub("_", f"{label}_{suffix}".casefold()).strip("_") or "osm"
    base = base[:56].strip("_") or "osm"
    taken = {name.casefold() for name in used}
    name, index = base, 2
    while name.casefold() in taken:
        name = f"{base[:52]}_{index}"
        index += 1
    return name


def apply_provenance(layer: QgsVectorLayer, timestamp: str, request: str) -> None:
    """Attribution and data date in the layer's metadata and properties."""
    metadata = layer.metadata() if isinstance(layer.metadata(), QgsLayerMetadata) else QgsLayerMetadata()
    rights = [right for right in metadata.rights() if "OpenStreetMap" not in right]
    metadata.setRights(rights + [ATTRIBUTION])
    metadata.setLicenses(sorted(set(metadata.licenses()) | {"ODbL-1.0"}))
    when = f"OSM data as of {timestamp} (UTC)" if timestamp else "OSM data date not reported by the server"
    metadata.setAbstract(f"{request}\n{when}. Downloaded with 02Agent OSM Downloader.")
    history = [line for line in metadata.history() if not line.startswith("OSM data")]
    metadata.setHistory(history + [when])
    layer.setMetadata(metadata)
    layer.setCustomProperty(TIMESTAMP_PROPERTY, timestamp)
    # The server attribution moved onto serverProperties() in QGIS 3.22 and
    # the layer-level setter is deprecated; use whichever this build has.
    server = layer.serverProperties() if hasattr(layer, "serverProperties") else None
    if server is not None and hasattr(server, "setAttribution"):
        server.setAttribution(ATTRIBUTION)
    elif hasattr(layer, "setAttribution"):
        layer.setAttribution(ATTRIBUTION)


def _writer_options(table: str, append: bool) -> QgsVectorFileWriter.SaveVectorOptions:
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = table
    options.fileEncoding = "UTF-8"
    # Scoped enum access is not available on every supported QGIS 3 build.
    action = getattr(QgsVectorFileWriter, "ActionOnExistingFile", QgsVectorFileWriter)
    options.actionOnExistingFile = (
        action.CreateOrOverwriteLayer if append else action.CreateOrOverwriteFile
    )
    return options


def write_geopackage(
    layers: Sequence[Tuple[QgsVectorLayer, str]],
    path: str,
    label: str,
    project: Optional[QgsProject] = None,
) -> List[QgsVectorLayer]:
    """Write ``(layer, suffix)`` pairs into one GeoPackage; return file layers.

    An existing file is kept and only same-named tables are replaced, so one
    project GeoPackage can collect many downloads. Each file layer receives
    the in-memory layer's style, saved into the GeoPackage as its default.
    Raises RuntimeError naming the layer that could not be written.
    """
    if not path:
        raise RuntimeError("Choose a GeoPackage file to save the download into.")
    if not path.lower().endswith(".gpkg"):
        path = f"{path}.gpkg"
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    project = project or QgsProject.instance()
    context = project.transformContext()
    existing: List[str] = []
    if os.path.exists(path):
        # Read the GeoPackage's own catalogue instead of opening an OGR layer:
        # a second OGR connection on a file other layers still hold makes
        # GDAL report "unable to open database file" on its feature count.
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
                existing = [row[0] for row in connection.execute(
                    "SELECT table_name FROM gpkg_contents")]
        except sqlite3.Error as exc:
            raise RuntimeError(f"{path} is not a readable GeoPackage: {exc}") from exc
    written: List[QgsVectorLayer] = []
    used = list(existing)
    for layer, suffix in layers:
        table = table_name(label, suffix, used)
        used.append(table)
        append = os.path.exists(path)
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, context, _writer_options(table, append)
        )
        error_code = result[0]
        no_error = getattr(QgsVectorFileWriter, "WriterError", QgsVectorFileWriter).NoError
        if error_code != no_error:
            message = result[1] if len(result) > 1 else ""
            raise RuntimeError(
                f"Could not write '{layer.name()}' to {path}: {message or error_code}"
            )
        file_layer = QgsVectorLayer(f"{path}|layername={table}", layer.name(), "ogr")
        if not file_layer.isValid():
            raise RuntimeError(f"The saved table '{table}' could not be opened again.")
        style_doc_error = ""
        from qgis.PyQt.QtXml import QDomDocument
        document = QDomDocument("qgis")
        layer.exportNamedStyle(document)
        file_layer.importNamedStyle(document)
        for key in layer.customPropertyKeys():
            file_layer.setCustomProperty(key, layer.customProperty(key))
        file_layer.setMetadata(layer.metadata())
        # saveStyleToDatabaseV2 (QGIS 3.40+) replaces the deprecated call; both
        # hand back the error text, alone or after a result flag.
        # Count first. Measured on QGIS 3.44 and 4.2: saving the style before
        # the layer's first feature count makes GDAL log "unable to open
        # database file" for that count; counting beforehand removes it.
        file_layer.featureCount()
        save = getattr(file_layer, "saveStyleToDatabaseV2", None) or file_layer.saveStyleToDatabase
        saved = save(table, "Downloaded style", True, "")
        if isinstance(saved, tuple):
            style_doc_error = str(saved[-1] or "")
        elif isinstance(saved, str):
            style_doc_error = saved
        if style_doc_error:
            raise RuntimeError(f"The style of '{table}' could not be stored: {style_doc_error}")
        written.append(file_layer)
    return written
