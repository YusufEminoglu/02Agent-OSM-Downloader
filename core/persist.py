# -*- coding: utf-8 -*-
"""Keep downloads: GeoPackage output with embedded styles, and data provenance.

A download used to exist only as temporary memory layers, gone when QGIS
closed. With a GeoPackage target every result layer is written as its own
table into one file, its current style is stored inside that file
(``layer_styles``, as the default style) so the file opens styled anywhere,
and the layers added to the project are the file-backed ones.

Provenance travels with every result layer either way: the OpenStreetMap
attribution ODbL requires, and the date of the OSM database the server
answered from, in the layer metadata and as a custom property. In a
GeoPackage the metadata is stored in the file (``gpkg_metadata``), so it is
still there when the file is opened later or by someone else, and the
attribution and date are repeated in ``gpkg_contents.description`` for
programs that do not read QGIS metadata.
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from qgis.core import (
    QgsLayerMetadata,
    QgsProject,
    QgsProviderRegistry,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"
TIMESTAMP_PROPERTY = "zero2agent/osm_timestamp"
#: How the history line naming the servers that delivered the data begins.
SOURCE_HISTORY_PREFIX = "Data source: "
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


def apply_provenance(
    layer: QgsVectorLayer, timestamp: str, request: str, source: str = ""
) -> None:
    """Attribution, data date and data source in the layer's metadata and properties.

    `source` names the servers that delivered the data, as the download's
    DATA_SOURCE output does ("overpass.private.coffee, postpass.geofabrik.de",
    or "local cache"); it goes into the abstract and the history, so a saved
    GeoPackage says where its data came from.
    """
    metadata = layer.metadata() if isinstance(layer.metadata(), QgsLayerMetadata) else QgsLayerMetadata()
    rights = [right for right in metadata.rights() if "OpenStreetMap" not in right]
    metadata.setRights(rights + [ATTRIBUTION])
    metadata.setLicenses(sorted(set(metadata.licenses()) | {"ODbL-1.0"}))
    when = f"OSM data as of {timestamp} (UTC)" if timestamp else "OSM data date not reported by the server"
    servers = " ".join(str(source or "").split())
    origin = ""
    if servers:
        origin = " from the local cache" if servers == "local cache" else f" from {servers}"
    # The abstract is what other programs show as a GeoPackage table's
    # description (see _contents_description), so it names the source too.
    metadata.setAbstract(
        f"{request}\n{when}. Downloaded{origin} with 02Agent OSM Downloader.\n{ATTRIBUTION}"
    )
    history = [
        line for line in metadata.history()
        if not line.startswith(("OSM data", SOURCE_HISTORY_PREFIX))
    ]
    metadata.setHistory(
        history + [when] + ([f"{SOURCE_HISTORY_PREFIX}{servers}"] if servers else [])
    )
    layer.setMetadata(metadata)
    layer.setCustomProperty(TIMESTAMP_PROPERTY, timestamp)
    # The server attribution moved onto serverProperties() in QGIS 3.22 and
    # the layer-level setter is deprecated; use whichever this build has.
    server = layer.serverProperties() if hasattr(layer, "serverProperties") else None
    if server is not None and hasattr(server, "setAttribution"):
        server.setAttribution(ATTRIBUTION)
    elif hasattr(layer, "setAttribution"):
        layer.setAttribution(ATTRIBUTION)


def _contents_description(layer: QgsVectorLayer) -> str:
    """The ``gpkg_contents.description`` of an OSM table: its abstract.

    That column is what programs that know nothing of QGIS metadata show for
    a table. QGIS 3.44 and 4.2 copy the abstract into it when they save the
    metadata; writing the same text when the table is created keeps it filled
    on builds that do not. Empty for a layer that carries no OSM attribution,
    such as the As-Is ground polygon.
    """
    metadata = layer.metadata()
    if not any("OpenStreetMap" in right for right in metadata.rights()):
        return ""
    return metadata.abstract()


def _read_only_uri(path: str) -> str:
    """A read-only SQLite URI that names ``path`` exactly.

    SQLite parses ``file:{path}`` as a URI, so a ``#`` ended the path and
    ``%41`` turned into ``A``: the second save into a folder such as
    ``Maps #2`` called the file unreadable. Everything but the separators is
    percent-encoded, and the authority stays empty so that a UNC path
    (``//server/share``) is not read as a host name.
    """
    absolute = os.path.abspath(path).replace(os.sep, "/")
    if not absolute.startswith("/"):
        absolute = f"/{absolute}"
    return f"file://{quote(absolute, safe='/:')}?mode=ro"


def _table_uri(path: str, table: str) -> str:
    """The OGR source of one GeoPackage table, as the provider builds it."""
    uri = QgsProviderRegistry.instance().encodeUri(
        "ogr", {"path": path, "layerName": table}
    )
    return uri or f"{path}|layername={table}"


def _store_metadata(file_layer: QgsVectorLayer, table: str) -> None:
    """Write the file layer's metadata into its own GeoPackage.

    ``setMetadata`` changes the layer object only. Opened again -- later,
    or by whoever the file was given to -- every table came back with no
    rights, no licence and no data date. ``saveDefaultMetadata`` has the
    OGR provider store the metadata in ``gpkg_metadata`` for this table.
    """
    message, saved = "this QGIS cannot store layer metadata", False
    save = getattr(file_layer, "saveDefaultMetadata", None)
    if save is not None:
        result = save()
        # (message, success) on every supported build.
        if isinstance(result, tuple) and len(result) == 2:
            message, saved = str(result[0] or ""), bool(result[1])
    if not saved:
        raise RuntimeError(
            f"The attribution of '{table}' could not be stored: {message}"
        )


def _writer_options(
    table: str, append: bool, description: str = ""
) -> QgsVectorFileWriter.SaveVectorOptions:
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = table
    options.fileEncoding = "UTF-8"
    # Scoped enum access is not available on every supported QGIS 3 build.
    action = getattr(QgsVectorFileWriter, "ActionOnExistingFile", QgsVectorFileWriter)
    options.actionOnExistingFile = (
        action.CreateOrOverwriteLayer if append else action.CreateOrOverwriteFile
    )
    if description:
        # GDAL writes this into gpkg_contents.description, which programs
        # that know nothing of QGIS metadata still show.
        options.layerOptions = [f"DESCRIPTION={description}"]
    return options


def write_geopackage(
    layers: Sequence[Tuple[QgsVectorLayer, str]],
    path: str,
    label: str,
    project: Optional[QgsProject] = None,
) -> List[QgsVectorLayer]:
    """Write ``(layer, suffix)`` pairs into one GeoPackage; return file layers.

    An existing file is kept and nothing in it is replaced: a table name
    that is already taken is written as ``<name>_2``, ``<name>_3`` and so
    on, so one project GeoPackage can collect many downloads, repeats
    included. Each file layer receives the in-memory layer's style, saved
    into the GeoPackage as its default, and its metadata, saved into the
    file as well. Raises RuntimeError naming the layer that could not be
    written.
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
            with sqlite3.connect(_read_only_uri(path), uri=True) as connection:
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
            layer, path, context,
            _writer_options(table, append, _contents_description(layer)),
        )
        error_code = result[0]
        no_error = getattr(QgsVectorFileWriter, "WriterError", QgsVectorFileWriter).NoError
        if error_code != no_error:
            message = result[1] if len(result) > 1 else ""
            raise RuntimeError(
                f"Could not write '{layer.name()}' to {path}: {message or error_code}"
            )
        file_layer = QgsVectorLayer(_table_uri(path, table), layer.name(), "ogr")
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
        # Count first. Measured on QGIS 3.44 and 4.2: saving the style before
        # the layer's first feature count makes GDAL log "unable to open
        # database file" for that count; counting beforehand removes it.
        file_layer.featureCount()
        _store_metadata(file_layer, table)
        # saveStyleToDatabaseV2 (QGIS 3.40+) replaces the deprecated call; both
        # hand back the error text, alone or after a result flag.
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
