"""Pure-Python checks for public metadata and Agent Protocol v1."""
from __future__ import annotations

import configparser
import json
from pathlib import Path
import unittest


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
                "zero2agentosm:download_custom_tag",
            },
        )

    def test_manifest_keeps_network_inputs_bounded(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / "agent_protocol.json").read_text(encoding="utf-8")
        )
        safety = manifest["safety"]
        self.assertEqual(safety["maximum_extent_area_km2"], 100)
        self.assertFalse(safety["raw_query_input"])
        self.assertFalse(safety["arbitrary_url_input"])
        self.assertFalse(safety["file_path_input"])
        self.assertFalse(safety["api_key_input"])

    def test_hub_metadata_has_rich_discovery_fields(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(PLUGIN_ROOT / "metadata.txt", encoding="utf-8")
        metadata = parser["general"]
        self.assertGreaterEqual(len(metadata["description"]), 100)
        self.assertIn("Agent Protocol v1", metadata["about"])
        self.assertIn("26 curated presets", metadata["about"])
        tags = {item.strip() for item in metadata["tags"].split(",")}
        self.assertGreaterEqual(len(tags), 15)
        self.assertTrue(
            {"qgis", "openstreetmap", "overpass", "agent", "smartmodeler"}
            <= tags
        )
        self.assertEqual(metadata["hasprocessingprovider"], "yes")


if __name__ == "__main__":
    unittest.main()
