"""Small, explicit OpenStreetMap XYZ basemap helper."""
from __future__ import annotations

from qgis.core import QgsProject, QgsRasterLayer


OSM_BASEMAP_NAME = "OpenStreetMap Standard"
OSM_BASEMAP_PROPERTY = "zero2agent/osm_basemap"
OSM_BASEMAP_URI = (
    "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    "&zmin=0&zmax=19&crs=EPSG3857"
)
OSM_ATTRIBUTION = "© OpenStreetMap contributors"


def add_osm_basemap(project=None) -> tuple[QgsRasterLayer, bool]:
    """Add or reveal the standard OSM XYZ layer.

    Returns the layer and a boolean indicating whether a new layer was added.
    The layer is deliberately marked so repeated button presses do not create
    duplicate tile layers.
    """
    target = project or QgsProject.instance()
    for layer in target.mapLayers().values():
        if layer.customProperty(OSM_BASEMAP_PROPERTY, False):
            layer_tree = target.layerTreeRoot().findLayer(layer.id())
            if layer_tree is not None:
                layer_tree.setItemVisibilityChecked(True)
            return layer, False

    layer = QgsRasterLayer(OSM_BASEMAP_URI, OSM_BASEMAP_NAME, "wms")
    if not layer.isValid():
        raise ValueError("QGIS could not create the OpenStreetMap basemap.")
    layer.setCustomProperty(OSM_BASEMAP_PROPERTY, True)
    layer.setCustomProperty("zero2agent/attribution", OSM_ATTRIBUTION)
    target.addMapLayer(layer, False)
    target.layerTreeRoot().insertLayer(0, layer)
    return layer, True
