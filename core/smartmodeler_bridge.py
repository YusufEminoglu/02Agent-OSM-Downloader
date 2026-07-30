"""Defensive, secret-safe bridge to SmartModeler GIS.

The downloader never reads or stores AI credentials.  It only asks the loaded
SmartModeler plugin for display metadata and public UI entry points.  When the
plugin package is installed but disabled, the shared Connections dialog can
still be opened directly so the button never becomes a silent dead end.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class BridgeResult:
    ok: bool
    message: str = ""


def loaded_plugin() -> Optional[Any]:
    """Return a compatible loaded SmartModeler instance, if available."""
    try:
        from qgis.utils import plugins
    except Exception:  # noqa: BLE001 - optional plugin boundary
        return None
    plugin = plugins.get("planx_smartmodeler")
    if plugin is None:
        return None
    required = (
        "agent_connection_info",
        "open_ai_connections",
        "open_agent_workspace",
    )
    compatible = all(
        callable(getattr(plugin, name, None))
        for name in required
    )
    return plugin if compatible else None


def connection_info() -> Dict[str, object]:
    """Return bounded display-only state and never propagate bridge failures."""
    plugin = loaded_plugin()
    if plugin is None:
        return {
            "available": False,
            "profile_name": "",
            "provider_name": "",
            "model": "",
            "agent_chat_enabled": False,
        }
    try:
        raw = plugin.agent_connection_info()
    except Exception as error:  # noqa: BLE001 - optional plugin boundary
        return {
            "available": False,
            "error": _safe_error(error),
            "profile_name": "",
            "provider_name": "",
            "model": "",
            "agent_chat_enabled": False,
        }
    info = raw if isinstance(raw, dict) else {}
    return {
        "available": True,
        "profile_name": _bounded(info.get("profile_name"), 80),
        "provider_name": _bounded(info.get("provider_name"), 80),
        "model": _bounded(info.get("model"), 120),
        "agent_chat_enabled": bool(info.get("agent_chat_enabled")),
    }


def open_connections(parent=None) -> BridgeResult:
    """Open SmartModeler's shared connection editor through the safest path."""
    plugin = loaded_plugin()
    if plugin is not None:
        try:
            opened = plugin.open_ai_connections()
        except Exception as error:  # noqa: BLE001 - optional plugin boundary
            return BridgeResult(
                False,
                "SmartModeler could not open Connections: "
                f"{_safe_error(error)}",
            )
        if opened is not False:
            return BridgeResult(True)

    # The package can be installed but currently disabled.  Opening its
    # secret-safe settings dialog directly still writes only to SmartModeler's
    # QGIS settings/vault namespace; no key enters this plugin.
    try:
        from planx_smartmodeler.gui.ai_settings_dialog import AiSettingsDialog
        from planx_smartmodeler.gui.theme import STUDIO_STYLE
    except Exception:  # noqa: BLE001 - optional plugin boundary
        return BridgeResult(
            False,
            "SmartModeler GIS is required for AI connections. Open Plugins > "
            "Manage and Install Plugins, search for “SmartModeler GIS”, "
            "install it, then enable it and reopen this tab.",
        )
    try:
        dialog = AiSettingsDialog(parent)
        dialog.setStyleSheet(STUDIO_STYLE)
        dialog.exec()
    except Exception as error:  # noqa: BLE001 - optional plugin boundary
        return BridgeResult(
            False,
            f"AI Connections could not open: {_safe_error(error)}",
        )
    return BridgeResult(True)


def open_workspace() -> BridgeResult:
    """Open Agent Workspace when the companion plugin is loaded."""
    plugin = loaded_plugin()
    if plugin is None:
        return BridgeResult(
            False,
            "Enable SmartModeler GIS to use its supervised Agent Workspace.",
        )
    try:
        opened = plugin.open_agent_workspace()
    except Exception as error:  # noqa: BLE001 - optional plugin boundary
        return BridgeResult(
            False,
            f"Agent Workspace could not open: {_safe_error(error)}",
        )
    if opened is False:
        return BridgeResult(
            False,
            "SmartModeler Agent Workspace is not available yet.",
        )
    return BridgeResult(True)


def _bounded(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _safe_error(error: Exception) -> str:
    text = " ".join(str(error).split())
    bounded = text[:180] or type(error).__name__
    return bounded.replace("<", "").replace(">", "")
