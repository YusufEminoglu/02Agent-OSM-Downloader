"""Pure-Python checks for public metadata and Agent Protocol v1."""
from __future__ import annotations

import configparser
import json
from pathlib import Path
import sys
import types
import unittest
from urllib.parse import urlsplit

from zero2agent_osm_downloader.core import geocoding, query, smartmodeler_bridge


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class AgentProtocolTests(unittest.TestCase):
    def test_manifest_exposes_stable_processing_endpoints(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / "agent_protocol.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["protocol_version"], "1.0")
        self.assertEqual(manifest["package_name"], "zero2agent_osm_downloader")
        self.assertEqual(manifest["provider_id"], "zero2agentosm")
        self.assertEqual(manifest["transport"], "qgis_processing_registry")
        self.assertTrue(
            manifest["execution"]["explicit_user_approval_required"]
        )
        self.assertTrue(manifest["execution"]["assistant_text_required"])
        self.assertTrue(
            manifest["execution"]["fresh_context_token_required"]
        )
        self.assertEqual(
            {item["id"] for item in manifest["algorithms"]},
            {
                "zero2agentosm:download_preset",
                "zero2agentosm:download_place",
                "zero2agentosm:download_custom_tag",
                "zero2agentosm:download_advanced",
            },
        )

    def test_manifest_keeps_network_inputs_bounded(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / "agent_protocol.json").read_text(encoding="utf-8")
        )
        safety = manifest["safety"]
        # The manifest is what an agent reads before proposing a run, so it
        # must state the limits the code actually enforces.
        self.assertEqual(
            safety["maximum_extent_area_km2"], query.MAX_BBOX_AREA_KM2
        )
        self.assertEqual(
            safety["maximum_single_request_area_km2"],
            query.MAX_TILE_AREA_KM2,
        )
        self.assertEqual(
            safety["maximum_requests_per_download"], query.MAX_TILES
        )
        self.assertEqual(
            safety["maximum_merged_features"], query.MAX_TOTAL_FEATURES
        )
        self.assertEqual(
            safety["maximum_advanced_tag_filters"],
            query.MAX_ADVANCED_FILTERS,
        )
        self.assertEqual(safety["advanced_match_modes"], ["any", "all"])
        self.assertFalse(safety["raw_query_input"])
        self.assertFalse(safety["arbitrary_url_input"])
        self.assertFalse(safety["file_path_input"])
        self.assertFalse(safety["api_key_input"])

    def test_manifest_lists_every_host_the_plugin_contacts(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / "agent_protocol.json").read_text(encoding="utf-8")
        )
        hosts = set(manifest["safety"]["network_hosts"])
        overpass_hosts = {
            urlsplit(item).hostname for item in query.OVERPASS_ENDPOINTS
        }
        self.assertTrue(overpass_hosts <= hosts)
        self.assertIn(geocoding.NOMINATIM_HOST, hosts)
        # Nothing may contact a host the manifest does not declare.
        self.assertEqual(hosts, overpass_hosts | {geocoding.NOMINATIM_HOST})

    def test_place_endpoint_documents_the_boundary_parameters(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / "agent_protocol.json").read_text(encoding="utf-8")
        )
        place = next(
            item for item in manifest["algorithms"]
            if item["id"] == "zero2agentosm:download_place"
        )
        self.assertIn("CLIP", place["parameters"])
        self.assertIn("AREA_ID", place["parameters"])
        self.assertIn("Nominatim", place["notes"])

    def test_hub_metadata_has_rich_discovery_fields(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(PLUGIN_ROOT / "metadata.txt", encoding="utf-8")
        metadata = parser["general"]
        self.assertGreaterEqual(len(metadata["description"]), 100)
        self.assertIn("Agent Protocol v1", metadata["about"])
        self.assertIn("30 curated presets", metadata["about"])
        tags = {item.strip() for item in metadata["tags"].split(",")}
        self.assertGreaterEqual(len(tags), 15)
        self.assertTrue(
            {"qgis", "openstreetmap", "overpass", "agent", "smartmodeler"}
            <= tags
        )
        self.assertEqual(metadata["hasprocessingprovider"], "yes")
        self.assertEqual(metadata["experimental"].casefold(), "false")
        self.assertNotIn("alpha", metadata["description"].casefold())
        self.assertNotIn("alpha", metadata["about"].casefold())

    def test_connections_falls_back_to_installed_smartmodeler_dialog(
        self,
    ) -> None:
        module_names = (
            "zero2smartmodeler",
            "zero2smartmodeler.gui",
            "zero2smartmodeler.gui.ai_settings_dialog",
            "zero2smartmodeler.gui.theme",
        )
        previous = {
            name: sys.modules.get(name)
            for name in module_names
        }
        opened = []

        class FakeDialog:
            def __init__(self, parent=None) -> None:
                self.parent = parent

            def setStyleSheet(self, _style: str) -> None:
                return None

            def exec(self) -> None:
                opened.append(True)

        package = types.ModuleType("zero2smartmodeler")
        package.__path__ = []
        gui = types.ModuleType("zero2smartmodeler.gui")
        gui.__path__ = []
        dialog_module = types.ModuleType(
            "zero2smartmodeler.gui.ai_settings_dialog"
        )
        dialog_module.AiSettingsDialog = FakeDialog
        theme_module = types.ModuleType("zero2smartmodeler.gui.theme")
        theme_module.STUDIO_STYLE = ""

        original_loader = smartmodeler_bridge.loaded_plugin
        try:
            sys.modules[module_names[0]] = package
            sys.modules[module_names[1]] = gui
            sys.modules[module_names[2]] = dialog_module
            sys.modules[module_names[3]] = theme_module
            smartmodeler_bridge.loaded_plugin = lambda: None
            result = smartmodeler_bridge.open_connections()
        finally:
            smartmodeler_bridge.loaded_plugin = original_loader
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        self.assertTrue(result.ok)
        self.assertEqual(opened, [True])

    def test_missing_smartmodeler_message_explains_installation(self) -> None:
        module_name = "zero2smartmodeler.gui.ai_settings_dialog"
        previous = sys.modules.get(module_name)
        original_loader = smartmodeler_bridge.loaded_plugin
        try:
            sys.modules[module_name] = None
            smartmodeler_bridge.loaded_plugin = lambda: None
            result = smartmodeler_bridge.open_connections()
        finally:
            smartmodeler_bridge.loaded_plugin = original_loader
            if previous is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous
        self.assertFalse(result.ok)
        self.assertIn("Manage and Install Plugins", result.message)
        self.assertIn("search for “02Agent Smart Modeler”", result.message)
        self.assertIn("install it", result.message)
        self.assertIn("enable it", result.message)


if __name__ == "__main__":
    unittest.main()
