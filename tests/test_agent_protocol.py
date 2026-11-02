"""Pure-Python checks for public metadata and Agent Protocol v1.

What the manifest, metadata.txt and the public documents claim is compared
with the code: hosts with the constants the network code sends requests to,
limits with the constants it enforces, and every declared parameter and
output with what the four algorithms declare when the provider loads them.
"""
from __future__ import annotations

import ast
import configparser
import contextlib
import importlib
import json
from pathlib import Path
import re
import sys
import types
import unittest
from urllib.parse import parse_qsl, urlsplit

from zero2agent_osm_downloader.core import (
    cache,
    catalog,
    geocoding,
    map_themes,
    postpass,
    query,
    smartmodeler_bridge,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "zero2agent_osm_downloader"
ALGORITHM_IDS = {
    "zero2agentosm:download_preset",
    "zero2agentosm:download_place",
    "zero2agentosm:download_custom_tag",
    "zero2agentosm:download_advanced",
}
#: Hosts the shipped code names only as text or as links a person opens in
#: a browser: the manual, the issue form, the Referer value, the Processing
#: help page, the OSM tag reference and the OSM copyright page.  Nothing in
#: the plugin sends a request to them.
LINK_ONLY_HOSTS = frozenset({
    "geophilo.com",
    "github.com",
    "wiki.openstreetmap.org",
    "www.openstreetmap.org",
})
_URL_HOST_RE = re.compile(r"https?://([A-Za-z0-9.-]+)")
#: "KEY_1..KEY_6" in the manifest stands for KEY_1 to KEY_6.
_NAME_RANGE_RE = re.compile(r"^([A-Z]+)_(\d+)\.\.\1_(\d+)$")


def _text(name: str) -> str:
    return (PLUGIN_ROOT / name).read_text(encoding="utf-8")


def _manifest() -> dict:
    return json.loads(_text("agent_protocol.json"))


def _metadata() -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(PLUGIN_ROOT / "metadata.txt", encoding="utf-8")
    return parser["general"]


def _string_constant(relative_path: str, name: str) -> str:
    """A module-level string constant, read from source without importing.

    core/basemap.py imports qgis.core, which a pure test cannot load.
    """
    tree = ast.parse(_text(relative_path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise LookupError(f"{relative_path} defines no {name}.")


def _basemap_host() -> str:
    """The host of the XYZ basemap the dock's Add OSM basemap button adds."""
    uri = _string_constant("core/basemap.py", "OSM_BASEMAP_URI")
    return urlsplit(dict(parse_qsl(uri))["url"]).hostname


def _contacted_hosts() -> set:
    """Every host a download or a place search can send a request to."""
    return {urlsplit(url).hostname for url in query.OVERPASS_ENDPOINTS} | {
        postpass.POSTPASS_HOST,
        geocoding.NOMINATIM_HOST,
    }


def _hosts_named_in_shipped_code() -> dict:
    """host -> files, for every http(s) URL in a string of the shipped code.

    Docstrings are skipped.  A URL built around a constant, such as
    f"https://{NOMINATIM_HOST}/search", names no literal host here; the
    constant itself is checked through _contacted_hosts().
    """
    found: dict = {}
    for path in sorted(PLUGIN_ROOT.rglob("*.py")):
        relative = path.relative_to(PLUGIN_ROOT)
        if relative.parts[0] in ("tests", "docs") or "__pycache__" in relative.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                for host in _URL_HOST_RE.findall(node.value):
                    found.setdefault(host.lower(), set()).add(relative.as_posix())
    return found


def _declared_names(names) -> set:
    expanded = set()
    for name in names:
        match = _NAME_RANGE_RE.match(name)
        if match is None:
            expanded.add(name)
            continue
        stem, first, last = match.group(1), int(match.group(2)), int(match.group(3))
        expanded.update(f"{stem}_{index}" for index in range(first, last + 1))
    return expanded


# ------------------------------------------------- the provider, without QGIS
# The Processing modules import qgis.core and Qt at the top.  Stand-in
# modules let the real provider load the real algorithm classes, whose real
# initAlgorithm() then declares its parameters and outputs to a recorder.
class _StandInType(type):
    """Every attribute of a stand-in class is another stand-in class, so
    enum paths such as QgsProcessingParameterNumber.Type.Double resolve."""

    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _stand_in(name)


class _StandIn(metaclass=_StandInType):
    """A QGIS or Qt object: it keeps the arguments it was built with.

    Any other method, such as flags() or setFlags(), answers with another
    stand-in, and flags combine with `|`, so a declaration that sets flags
    on a parameter still reaches addParameter().
    """

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs

    def name(self) -> str:
        return str(self.args[0]) if self.args else ""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _StandIn

    def __or__(self, _other):
        return self

    __ror__ = __and__ = __rand__ = __or__


def _stand_in(name: str) -> type:
    return _StandInType(name, (_StandIn,), {})


class _AlgorithmBase(_StandIn):
    """QgsProcessingAlgorithm: records what initAlgorithm declares."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.declared_parameters: dict = {}
        self.declared_outputs: dict = {}

    def addParameter(self, definition, *_args) -> bool:
        self.declared_parameters[definition.name()] = definition
        return True

    def addOutput(self, definition) -> bool:
        self.declared_outputs[definition.name()] = definition
        return True

    def parameterDefinition(self, name):
        return self.declared_parameters.get(name)


class _ProviderBase(_StandIn):
    """QgsProcessingProvider: records the algorithms it loads."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.loaded: list = []

    def addAlgorithm(self, algorithm) -> bool:
        self.loaded.append(algorithm)
        return True


_BASES = {
    "QgsProcessingAlgorithm": _AlgorithmBase,
    "QgsProcessingProvider": _ProviderBase,
}
_QGIS_MODULES = (
    "qgis",
    "qgis.core",
    "qgis.PyQt",
    "qgis.PyQt.QtCore",
    "qgis.PyQt.QtGui",
    "qgis.PyQt.QtNetwork",
)


def _stand_in_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []

    def lookup(attribute: str):
        if attribute.startswith("__"):
            raise AttributeError(attribute)
        value = _BASES.get(attribute) or _stand_in(attribute)
        setattr(module, attribute, value)
        return value

    module.__getattr__ = lookup
    return module


def _default_value(definition):
    if "defaultValue" in definition.kwargs:
        return definition.kwargs["defaultValue"]
    # QgsProcessingParameterBoolean(name, description, defaultValue, ...)
    if type(definition).__name__ == "QgsProcessingParameterBoolean":
        return definition.args[2] if len(definition.args) > 2 else None
    return None


def _described(definitions: dict) -> dict:
    return {
        name: (type(item).__name__, _default_value(item))
        for name, item in definitions.items()
    }


@contextlib.contextmanager
def _processing_modules_without_qgis():
    """The plugin's Processing modules, imported over stand-in QGIS modules.

    sys.modules and the package attribute are restored afterwards, so no
    other test sees the stand-ins or a module built on them.
    """
    package = sys.modules[PACKAGE]
    prefix = f"{PACKAGE}.processing"

    def replaced(name: str) -> bool:
        return name in _QGIS_MODULES or name == prefix or name.startswith(prefix + ".")

    saved = {name: module for name, module in sys.modules.items() if replaced(name)}
    attributes = vars(package)
    had_attribute = "processing" in attributes
    previous_attribute = attributes.get("processing")
    for name in saved:
        del sys.modules[name]
    try:
        for name in _QGIS_MODULES:
            sys.modules[name] = _stand_in_module(name)
        yield (
            importlib.import_module(f"{prefix}.provider"),
            importlib.import_module(f"{prefix}.osm_algorithms"),
        )
    finally:
        for name in [name for name in sys.modules if replaced(name)]:
            del sys.modules[name]
        sys.modules.update(saved)
        if had_attribute:
            package.processing = previous_attribute
        else:
            attributes.pop("processing", None)


def _live_processing() -> tuple:
    """(signatures, cache time to live) of the real Processing code.

    signatures maps each algorithm id to (parameters, outputs), each a dict
    name -> (QGIS class name, default value).
    """
    signatures = {}
    with _processing_modules_without_qgis() as (provider_module, algorithms):
        provider = provider_module.AgentOsmProvider()
        provider.loadAlgorithms()
        for algorithm in provider.loaded:
            algorithm.initAlgorithm()
            signatures[f"{provider.id()}:{algorithm.name()}"] = (
                _described(algorithm.declared_parameters),
                _described(algorithm.declared_outputs),
            )
        ttl = algorithms._CACHE_TTL_SECONDS
    return signatures, ttl


def _plugin_manager_finds(term: str, metadata) -> bool:
    """QGIS Plugin Manager's search, as src/app/pluginmanager/
    qgspluginsortfilterproxymodel.cpp runs it: the typed text is one
    case-insensitive regular expression, matched against the name, the
    description, the author and the tags.  `about` is not searched."""
    pattern = re.compile(term, re.IGNORECASE)
    return any(
        pattern.search(metadata[field])
        for field in ("name", "description", "author", "tags")
    )


class AgentProtocolTests(unittest.TestCase):
    def test_manifest_exposes_stable_processing_endpoints(self) -> None:
        manifest = _manifest()
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
            {item["id"] for item in manifest["algorithms"]}, ALGORITHM_IDS
        )

    def test_manifest_keeps_network_inputs_bounded(self) -> None:
        safety = _manifest()["safety"]
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
            safety["maximum_planned_parts_per_download"], query.MAX_TILES
        )
        # Split parts and retries included.
        self.assertEqual(
            safety["maximum_requests_per_download"], query.MAX_JOB_REQUESTS
        )
        self.assertEqual(
            safety["maximum_merged_features"], query.MAX_TOTAL_FEATURES
        )
        self.assertEqual(
            safety["maximum_advanced_tag_filters"],
            query.MAX_ADVANCED_FILTERS,
        )
        # MAX_SELECTORS counts tag selectors, not datasets: one dataset can
        # bring several.  The old name said datasets.
        self.assertEqual(
            safety["maximum_tag_selectors_per_request"], query.MAX_SELECTORS
        )
        self.assertNotIn("maximum_datasets_per_request", safety)
        self.assertEqual(safety["advanced_match_modes"], list(query.MATCH_MODES))
        self.assertFalse(safety["raw_query_input"])
        self.assertFalse(safety["arbitrary_url_input"])
        self.assertFalse(safety["file_path_input"])
        self.assertFalse(safety["api_key_input"])

    def test_manifest_lists_every_host_the_plugin_contacts(self) -> None:
        safety = _manifest()["safety"]
        self.assertEqual(
            urlsplit(postpass.POSTPASS_ENDPOINT).hostname, postpass.POSTPASS_HOST
        )
        self.assertEqual(
            urlsplit(geocoding.NOMINATIM_SEARCH_URL).hostname,
            geocoding.NOMINATIM_HOST,
        )
        # Exactly the hosts the code sends requests to, in the code's order
        # for the Overpass hosts.
        self.assertEqual(set(safety["network_hosts"]), _contacted_hosts())
        self.assertEqual(len(safety["network_hosts"]), len(_contacted_hosts()))
        self.assertEqual(
            safety["network_hosts"][: len(query.OVERPASS_ENDPOINTS)],
            [urlsplit(url).hostname for url in query.OVERPASS_ENDPOINTS],
        )
        self.assertEqual(
            set(safety["network_host_roles"]), set(safety["network_hosts"])
        )
        # The basemap host is declared apart: only the dock's button loads
        # tiles, never a Processing endpoint.
        self.assertEqual(safety["optional_basemap_tile_hosts"], [_basemap_host()])
        self.assertNotIn(_basemap_host(), safety["network_hosts"])
        self.assertIn("Add OSM basemap", safety["optional_basemap_note"])

    def test_shipped_code_names_no_undeclared_host(self) -> None:
        safety = _manifest()["safety"]
        declared = set(safety["network_hosts"]) | set(
            safety["optional_basemap_tile_hosts"]
        )
        named = _hosts_named_in_shipped_code()
        undeclared = {
            host: sorted(files)
            for host, files in named.items()
            if host not in declared and host not in LINK_ONLY_HOSTS
        }
        self.assertEqual(undeclared, {})
        # The scan reads what the code really holds.
        self.assertIn(_basemap_host(), named)
        self.assertIn(postpass.POSTPASS_HOST, named)
        self.assertTrue(
            {urlsplit(url).hostname for url in query.OVERPASS_ENDPOINTS} <= set(named)
        )

    def test_manifest_matches_the_live_algorithms(self) -> None:
        def loaded() -> set:
            return {
                name for name in sys.modules
                if name.split(".")[0] == "qgis"
                or name.startswith(f"{PACKAGE}.processing")
            }

        before = loaded()
        signatures, _ttl = _live_processing()
        # The stand-ins and the modules built on them are gone again.
        self.assertEqual(loaded(), before)
        self.assertEqual(set(signatures), ALGORITHM_IDS)
        for item in _manifest()["algorithms"]:
            parameters, outputs = signatures[item["id"]]
            with self.subTest(algorithm=item["id"]):
                self.assertEqual(_declared_names(item["parameters"]), set(parameters))
                self.assertEqual(set(item["outputs"]), set(outputs))

    def test_shared_parameters_and_outputs_have_their_declared_kinds(self) -> None:
        signatures, _ttl = _live_processing()
        expected_parameters = {
            "AROUND_X": "QgsProcessingParameterNumber",
            "AROUND_Y": "QgsProcessingParameterNumber",
            "AROUND_METERS": "QgsProcessingParameterNumber",
            "ALLOW_FALLBACK": "QgsProcessingParameterBoolean",
        }
        expected_outputs = {
            "OSM_TIMESTAMP": "QgsProcessingOutputString",
            "TILES_TOTAL": "QgsProcessingOutputNumber",
            "TILES_MISSING": "QgsProcessingOutputNumber",
            "DATA_SOURCE": "QgsProcessingOutputString",
            "FEATURES_DROPPED": "QgsProcessingOutputNumber",
            "FEATURES_OUTSIDE": "QgsProcessingOutputNumber",
            "NOTICE": "QgsProcessingOutputString",
        }
        for algorithm_id, (parameters, outputs) in sorted(signatures.items()):
            with self.subTest(algorithm=algorithm_id):
                self.assertEqual(
                    {name: parameters.get(name, ("missing",))[0] for name in expected_parameters},
                    expected_parameters,
                )
                self.assertEqual(
                    {name: outputs.get(name, ("missing",))[0] for name in expected_outputs},
                    expected_outputs,
                )
                # "boolean_default_false": the fallback is off unless turned on.
                self.assertIs(parameters["ALLOW_FALLBACK"][1], False)
                self.assertEqual(parameters["AROUND_METERS"][1], 0.0)

    def test_place_endpoint_documents_the_boundary_parameters(self) -> None:
        place = next(
            item for item in _manifest()["algorithms"]
            if item["id"] == "zero2agentosm:download_place"
        )
        self.assertIn("CLIP", place["parameters"])
        self.assertIn("AREA_ID", place["parameters"])
        self.assertIn("Nominatim", place["notes"])
        self.assertIn("does not run this endpoint", place["notes"])

    def test_hub_metadata_has_rich_discovery_fields(self) -> None:
        metadata = _metadata()
        description = metadata["description"]
        self.assertNotIn("\n", description)
        self.assertGreaterEqual(len(description), 100)
        # The words people type: Hub and Plugin Manager search the
        # description and the tags, never `about`.
        for phrase in (
            "Download OpenStreetMap (OSM) data", "extract", "roads",
            "buildings", "street network", "schools, hospitals", "GeoPackage",
        ):
            self.assertIn(phrase, description)
        for word in ("best", "ultimate", "elite", "powerful", "seamless"):
            self.assertNotIn(word, description.casefold())
        tags = {item.strip() for item in metadata["tags"].split(",")}
        self.assertTrue(
            {
                "openstreetmap", "osm", "osm data", "osm downloader",
                "download osm", "extract", "street network", "schools",
                "hospitals", "geopackage", "overpass", "geophilo",
                "smartmodeler",
            }
            <= tags
        )
        # Tags that matched nothing anyone searches for, and a format the
        # plugin does not write.
        self.assertFalse(
            {"qgis", "advanced-query", "query-builder", "data-downloader", "shapefile"}
            & tags
        )
        about = metadata["about"]
        self.assertIn("Agent Protocol v1", about)
        self.assertIn(
            "four Processing endpoints, three of which 02SmartModeler runs", about
        )
        self.assertIn(
            f"{len(catalog.PRESETS)} ready-made datasets in "
            f"{len(catalog.GROUPS)} topics",
            about,
        )
        self.assertIn(f"{len(map_themes.map_theme_items())} map styles", about)
        for stale in ("merged seamlessly", "today's data", "mirror"):
            self.assertNotIn(stale, about)
        self.assertEqual(metadata["hasprocessingprovider"], "yes")
        self.assertEqual(metadata["experimental"].casefold(), "false")
        self.assertNotIn("alpha", description.casefold())
        self.assertNotIn("alpha", about.casefold())
        # The "02" start sorts the plugin first in Plugin Manager.
        self.assertTrue(metadata["name"].startswith("02"))

    def test_plugin_manager_search_finds_the_words_people_type(self) -> None:
        metadata = _metadata()
        # The five terms the audit measured as not found, then the ones that
        # already worked and must keep working.
        for term in (
            "download osm", "extract", "street network", "school", "hospital",
            "osm download", "osm data", "openstreetmap", "overpass", "roads",
            "buildings", "geopackage", "poi",
        ):
            with self.subTest(term=term):
                self.assertTrue(_plugin_manager_finds(term, metadata))

    def test_metadata_has_no_percent_sign(self) -> None:
        # configparser interpolation turns a literal % into a broken upload
        # (docs/TRAPS.md 1.5).
        self.assertNotIn(b"%", (PLUGIN_ROOT / "metadata.txt").read_bytes())

    def test_tracker_is_the_public_issue_list(self) -> None:
        metadata = _metadata()
        self.assertEqual(
            metadata["tracker"],
            "https://github.com/YusufEminoglu/02Agent-OSM-Downloader/issues",
        )

    def test_version_strings_agree(self) -> None:
        metadata = _metadata()
        version = metadata["version"]
        citation = _text("CITATION.cff")
        heading = re.search(
            r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})$",
            _text("CHANGELOG.md"),
            re.MULTILINE,
        )
        self.assertIsNotNone(heading)
        self.assertEqual(heading.group(1), version)
        self.assertIn(f'version: "{version}"', citation)
        self.assertIn(f'date-released: "{heading.group(2)}"', citation)
        self.assertTrue(
            metadata["changelog"].strip().startswith(f"{version} - ")
        )

    def test_public_documents_name_every_host(self) -> None:
        documents = {
            "metadata.txt about": _metadata()["about"],
            "README.md": _text("README.md"),
            "AGENT_PROTOCOL.md": _text("AGENT_PROTOCOL.md"),
            "THIRD_PARTY_NOTICES.md": _text("THIRD_PARTY_NOTICES.md"),
        }
        hosts = sorted(_contacted_hosts() | {_basemap_host()})
        for name, text in documents.items():
            with self.subTest(document=name):
                self.assertEqual([host for host in hosts if host not in text], [])
                # An alias of overpass.private.coffee, removed in 1.6.0.
                self.assertNotIn("kumi", text)

    def test_public_documents_state_the_live_numbers(self) -> None:
        signatures, ttl = _live_processing()
        self.assertTrue(signatures)
        readme = _text("README.md")
        styles = len(map_themes.map_theme_items())
        self.assertIn(f"{styles} map styles", readme)
        self.assertIn(f"{len(catalog.PRESETS)} ready-made datasets", readme)
        self.assertIn(f"{len(catalog.GROUPS)} topics", readme)
        self.assertIn("English and Turkish", readme)
        self.assertIn(f"{query.MAX_TILE_AREA_KM2:,.0f} km²", readme)
        self.assertIn(f"{query.MAX_BBOX_AREA_KM2:,.0f} km²", readme)
        self.assertIn(f"{query.MAX_JOB_REQUESTS} requests", readme)
        # The copy says one hour and 24 hours; the code must agree.
        self.assertEqual(ttl, 60 * 60)
        self.assertEqual(cache.UNFINISHED_JOB_TTL_SECONDS, 24 * 60 * 60)
        self.assertIn("one hour", readme)
        self.assertIn("24 hours", readme)
        for stale in ("15-minute session cache", "16 map styles", "four fixed hosts"):
            self.assertNotIn(stale, readme)

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
        # The name Plugin Manager finds: the Hub lists the companion as
        # "02SmartModeler - Visual Workflow Builder".
        self.assertIn("search for “02SmartModeler”", result.message)
        self.assertNotIn("02Agent Smart Modeler", result.message)
        self.assertIn("install it", result.message)
        self.assertIn("enable it", result.message)


if __name__ == "__main__":
    unittest.main()
