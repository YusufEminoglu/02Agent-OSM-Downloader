"""QGIS plugin entry point."""


def classFactory(iface):
    from .main_plugin import AgentOsmDownloaderPlugin

    return AgentOsmDownloaderPlugin(iface)
