"""Keep deployment packaging independent from credentials and operator state.

Architecture: isolated release fixture -> real public_config/build_archive ->
inspect ZIP members and bytes. Deliberate credential markers prove the release
boundary, without reading actual user secrets or creating Foundry resources.
Main callers: unittest discovery and deployment readiness checks.
"""

from contextlib import redirect_stdout
import importlib.util
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import posixpath
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
SPEC = importlib.util.spec_from_file_location("backprop_deployment", ROOT / "deploy/deploy.py")
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)

ASSETS = {"index.html", "styles.css", "workspace.css", "app.mjs", "model.mjs", "artifact-lineage.mjs", "smoothing.mjs", "charts.mjs", "interaction-overlays.mjs",
          "workspace.mjs", "run-analysis.mjs", "run-filter-editor.mjs", "workspace-layout.mjs", "saved-views.mjs", "panel-editor.mjs", "media-view.mjs", "mask-pixels.mjs", "graph-view.mjs", "graph-json.mjs", "console-view.mjs", "table-explorer.mjs", "table-join-key.mjs", "public-table.mjs", "numpy-file.mjs", "plotly-view.mjs", "plotly-strict-4.1.1.min.js", "plotly-LICENSE.txt", "run-control.mjs", "api.js", "auth.js", "network.mjs", "favicon.svg", "sha256.mjs", "tensorboard-replay.mjs", "plotly-reset.mjs"}
ASSETS.update({"vega-view.mjs", "vega-6.2.0.min.js", "vega-lite-6.4.1.min.js",
               "vega-interpreter-2.2.1.min.js", "vega-LICENSE.txt", "vega-lite-LICENSE.txt", "vega-interpreter-LICENSE.txt", "vega-provenance.json"})
ASSETS.update({"plotly-resources.mjs", "plotly-strict-3.6.0.min.js", "plotly-map-sat.json",
               "plotly-map-hybrid.json", "plotly-map-LICENSE.txt", "plotly-topology-provenance.json"})
ASSETS.update({"history-completeness.mjs", "view-state.mjs"})
ASSETS.update({"experience.mjs", "chart-series.mjs", "workspace-store.mjs", "workspace-session.mjs",
               "panel-catalog.mjs", "panel-bank.mjs", "project-pages.mjs", "run-comparer.mjs", "chart-defaults.mjs"})
ASSETS.update({'artifact-collections.mjs', 'tensorboard-groups.mjs', 'tensorboard-summary.mjs'})
ASSETS.update(f"{scope}_{resolution}m.json"
              for scope in ("africa", "antarctica", "asia", "europe", "north-america", "oceania",
                            "south-america", "usa", "world") for resolution in (110, 50))
PUBLIC_FIELDS = {"host", "client_id", "app_url", "stream_dataset_rid", "media_dataset_rid", "stream_view_rid", "branch", "map_resource_origins"}
SECRET_MARKER = "operator-credential-must-never-ship"
STATE = {"client_id": "public-client-identifier", "app_url": "https://backprop.example.com/",
         "application_rid": "ri.third-party-applications.main.application.test",
         "token": SECRET_MARKER, "nested": {"client_secret": SECRET_MARKER}}
RESOURCES = {"host": "https://customer-foundry.example.com", "branch": "master",
             "stream_dataset_rid": "ri.foundry.main.dataset.events",
             "media_dataset_rid": "ri.foundry.main.dataset.media",
             "stream_view_rid": "ri.foundry-streaming.main.view.events",
             "project_rid": "ri.compass.main.folder.project",
             "token": SECRET_MARKER, "refresh_token": SECRET_MARKER,
             "operator_settings": {"password": SECRET_MARKER}}


class DeploymentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Path(self.temporary.name)
        web = self.fixture / "web"
        web.mkdir()
        (self.fixture / "THIRD_PARTY_NOTICES.md").write_text("Synthetic notices")
        for name in ASSETS:
            (web / name).write_text(f"public asset {name}\n")
        (web / "index.html").write_text("<!doctype html><html><head><title>Backprop</title></head><body>Public app</body></html>")
        for directory in ("deploy", "backend", "sdk", ".backprop", ".git"):
            path = self.fixture / directory
            path.mkdir()
            (path / "state.json").write_text(SECRET_MARKER)
        (web / ".env").write_text(SECRET_MARKER)
        (web / ".hidden.js").write_text(SECRET_MARKER)
        (web / "operator-state.json").write_text(SECRET_MARKER)
        (web / "config.json").write_text(SECRET_MARKER)
        (web / "credentials.py").write_text(SECRET_MARKER)
        (web / "assets").mkdir()
        (web / "assets" / "private.js").write_text(SECRET_MARKER)

    def tearDown(self):
        self.temporary.cleanup()

    def build(self):
        with patch.object(DEPLOY, "ROOT", self.fixture):
            return DEPLOY.build_archive(STATE, RESOURCES)

    def test_aggregate_size_and_file_count_include_generated_files(self):
        with patch.object(DEPLOY, "MAX_WEBSITE_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "aggregate"):
                self.build()
        with patch.object(DEPLOY, "MAX_WEBSITE_FILES", len(ASSETS) + 1):
            with self.assertRaisesRegex(ValueError, "file limit"):
                self.build()

    def test_upload_can_use_a_longer_explicit_deadline_without_changing_normal_reads(self):
        from unittest.mock import MagicMock
        from foundry_api import FoundryApi
        api=FoundryApi.__new__(FoundryApi);api.host=RESOURCES['host'];api.token='fixture-token';api.opener=MagicMock()
        response=api.opener.open.return_value.__enter__.return_value
        response.read.return_value=b'{}';response.headers={'Content-Type':'application/json'}
        self.assertEqual(api.request('/fixture'),{})
        self.assertEqual(api.opener.open.call_args.kwargs['timeout'],30)
        self.assertEqual(api.request('/fixture','POST',b'archive','application/octet-stream',timeout=180),{})
        self.assertEqual(api.opener.open.call_args.kwargs['timeout'],180)

    def test_public_config_uses_exact_key_allowlist(self):
        config = DEPLOY.public_config(STATE, RESOURCES)
        self.assertEqual(set(config), PUBLIC_FIELDS)
        self.assertEqual(config["host"], RESOURCES["host"])
        self.assertEqual(config["stream_dataset_rid"], RESOURCES["stream_dataset_rid"])
        self.assertEqual(config["media_dataset_rid"], RESOURCES["media_dataset_rid"])
        self.assertEqual(config["client_id"], STATE["client_id"])
        self.assertNotIn(SECRET_MARKER, json.dumps(config))

    def test_archive_contains_only_public_assets_and_generated_config(self):
        archive = self.build()
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            self.assertEqual(set(bundle.namelist()), ASSETS | {"config.json", "THIRD_PARTY_NOTICES.md"})
            for member in bundle.namelist():
                self.assertNotIn(SECRET_MARKER.encode(), bundle.read(member))
                self.assertNotIn("/", member)
            generated = json.loads(bundle.read("config.json"))
        self.assertEqual(generated, DEPLOY.public_config(STATE, RESOURCES))

    def test_build_only_uses_configured_state_paths_without_credentials(self):
        """Exercise the operator entry point without any credential provider.

        Main callers: portable deployment checks. The ZIP must depend only on
        the selected public state, even when no cloud CLI or token is available.
        """
        deployment = self.fixture / "operator" / "application.json"
        resources = self.fixture / "operator" / "resources.json"
        deployment.parent.mkdir()
        deployment.write_text(json.dumps({**STATE, "host": RESOURCES["host"],
                                          "project_rid": RESOURCES["project_rid"]}))
        resources.write_text(json.dumps(RESOURCES))
        config = deployment.parent / "config.json"
        config.write_text(json.dumps({"host": RESOURCES["host"],
                                      "deployment_state": deployment.name,
                                      "resource_state": resources.name}))
        output = self.fixture / "website.zip"
        with patch.dict(os.environ, {"BACKPROP_DEPLOY_CONFIG": str(config)}, clear=True), \
             patch.object(DEPLOY, "ROOT", self.fixture), \
             patch.object(DEPLOY, "FoundryApi", side_effect=AssertionError("Build requested credentials")), \
             patch("foundry_api.subprocess.run", side_effect=AssertionError("Build invoked gcloud")), \
             patch.object(sys, "argv", ["deploy.py", "--version", "1.2.3", "--build-only", str(output)]), \
             redirect_stdout(io.StringIO()):
            DEPLOY.main()
        with zipfile.ZipFile(output) as bundle:
            self.assertEqual(json.loads(bundle.read("config.json"))["host"], RESOURCES["host"])
            self.assertIn(RESOURCES["host"].encode(), bundle.read("index.html"))
            self.assertTrue(all(SECRET_MARKER.encode() not in bundle.read(name) for name in bundle.namelist()))

    def test_unknown_executable_file_is_not_an_implicit_public_asset(self):
        (self.fixture / "web" / "operator-state.js").write_text(SECRET_MARKER)
        with zipfile.ZipFile(io.BytesIO(self.build())) as bundle:
            self.assertNotIn("operator-state.js", bundle.namelist())

    def test_real_website_dependencies_are_present_in_archive(self):
        """Follow actual entry points so an outdated allowlist fails before release.

        Main callers: deployment preflight. Fixture-only packaging tests protect
        the secret boundary, but cannot detect a newly imported source module.
        This check inspects the real HTML and every local JS import in the ZIP.
        """
        with zipfile.ZipFile(io.BytesIO(DEPLOY.build_archive(STATE, RESOURCES))) as bundle:
            sources = {name: bundle.read(name).decode("utf-8") for name in bundle.namelist()}
        references = []
        class DependencyParser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == "script" and attributes.get("src"):
                    references.append(("index.html", attributes["src"]))
                if tag == "link" and attributes.get("href"):
                    references.append(("index.html", attributes["href"]))
        DependencyParser().feed(sources["index.html"])
        for name, source in sources.items():
            if name.endswith((".js", ".mjs")):
                imports = re.findall(r"^\s*import\s+(?:.*?\s+from\s+)?[\"']([^\"']+)[\"']", source, re.MULTILINE)
                imports += re.findall(r"\bimport\s*\(\s*[\"']([^\"']+)[\"']", source)
                references.extend((name, dependency) for dependency in imports)
        self.assertGreater(len(references), 10, "Dependency inspection must discover the actual application graph")
        for importer, dependency in references:
            with self.subTest(importer=importer, dependency=dependency):
                self.assertTrue(dependency.startswith("./"), "Published code must use explicit local dependencies")
                target = posixpath.normpath(posixpath.join(posixpath.dirname(importer), dependency))
                self.assertIn(target, sources, f"Release omits {dependency} required by {importer}")

    def test_pinned_plotly_lazy_asset_and_license_ship_unchanged(self):
        with zipfile.ZipFile(io.BytesIO(DEPLOY.build_archive(STATE, RESOURCES))) as bundle:
            module = bundle.read("plotly-view.mjs").decode()
            source = re.search(r"PLOTLY_SOURCE = ['\"]\./([^'\"]+)", module).group(1)
            self.assertEqual(source, "plotly-strict-4.1.1.min.js")
            self.assertEqual(hashlib.sha256(bundle.read(source)).hexdigest(),
                             "4ac841837b31c1bfd2ab140e3a248ddcc6c2106978fbf2c1c18a7aada930a37a")
            self.assertIn("Permission is hereby granted", bundle.read("plotly-LICENSE.txt").decode())
            self.assertEqual(hashlib.sha256(bundle.read("plotly-strict-3.6.0.min.js")).hexdigest(),
                             "8f094d412acc077a1112bef047fdd49efaee28b5d3036bf7b12267d8e03da916")
            provenance = json.loads(bundle.read("plotly-topology-provenance.json"))
            self.assertEqual(provenance["version"], "4.1.1")
            topology = [item for item in provenance["assets"] if re.fullmatch(r"[a-z-]+_(110|50)m.json", item["file"])]
            self.assertEqual(len(topology), 18)
            for item in topology:
                with self.subTest(asset=item["file"]):
                    content = bundle.read(item["file"])
                    self.assertEqual(len(content), item["size"])
                    self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
                    self.assertEqual(json.loads(content)["type"], "Topology")
            self.assertIn("Permission is hereby granted", bundle.read("plotly-map-LICENSE.txt").decode())

    def test_symlink_cannot_import_operator_state(self):
        link = self.fixture / "web" / "credentials.js"
        link.symlink_to(self.fixture / "backend" / "state.json")
        with zipfile.ZipFile(io.BytesIO(self.build())) as bundle:
            self.assertNotIn("credentials.js", bundle.namelist())
            self.assertTrue(all(SECRET_MARKER.encode() not in bundle.read(member) for member in bundle.namelist()))

    def test_archive_bytes_do_not_depend_on_wall_clock(self):
        with patch("zipfile.time.localtime", return_value=(2026, 1, 1, 1, 1, 0, 3, 1, -1)):
            first = self.build()
        with patch("zipfile.time.localtime", return_value=(2026, 10, 3, 10, 20, 30, 5, 276, -1)):
            second = self.build()
        self.assertEqual(first, second, "All ZIP members, including generated config, need fixed timestamps")

    def test_missing_required_asset_prevents_broken_release(self):
        (self.fixture / "web" / "app.mjs").unlink()
        with self.assertRaisesRegex(ValueError, "Required public asset.*app.mjs"):
            self.build()

    def test_required_asset_symlink_fails_closed(self):
        path = self.fixture / "web" / "app.mjs"
        path.unlink()
        path.symlink_to(self.fixture / "backend" / "state.json")
        with self.assertRaisesRegex(ValueError, "Required public asset.*app.mjs"):
            self.build()

    def test_missing_required_resource_prevents_broken_configuration(self):
        resources = {key: value for key, value in RESOURCES.items() if key != "stream_dataset_rid"}
        with self.assertRaisesRegex(ValueError, "stream_dataset_rid"):
            DEPLOY.public_config(STATE, resources)

    def test_placeholders_cannot_enter_a_release(self):
        required = ("stream_dataset_rid", "media_dataset_rid", "branch", "client_id", "app_url")
        for key in required:
            for placeholder in ("YOUR_VALUE", "REPLACE_ME", "<example-value>"):
                state = {**STATE, key: placeholder} if key in STATE else STATE
                resources = {**RESOURCES, key: placeholder} if key in RESOURCES else RESOURCES
                with self.subTest(key=key, placeholder=placeholder), self.assertRaises(ValueError):
                    DEPLOY.public_config(state, resources)

    def test_conflicting_project_or_application_url_fails_before_packaging(self):
        for key, value in (("project_rid", "ri.compass.main.folder.other"),
                           ("app_url", "https://different-app.example.com/")):
            state = {**STATE, "project_rid": RESOURCES["project_rid"]}
            resources = {**RESOURCES, key: value}
            with self.subTest(key=key), patch.object(Path, "read_bytes", side_effect=AssertionError("Read public assets")):
                with self.assertRaisesRegex(ValueError, key):
                    DEPLOY.build_archive(state, resources)

    def test_app_url_cannot_embed_credentials_or_callback_parameters(self):
        for url in ("http://backprop.example.com/", "https://user:password@example.com/",
                    "https://backprop.example.com/?access_token=example"):
            with self.assertRaisesRegex(ValueError, "HTTPS website origin"):
                DEPLOY.public_config({**STATE, "app_url": url}, RESOURCES)

    def test_release_injects_escaped_policy_before_other_head_content(self):
        with zipfile.ZipFile(io.BytesIO(self.build())) as bundle:
            source = bundle.read("index.html").decode()
        policies = []
        class MetaParser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == "meta" and attributes.get("http-equiv") == "Content-Security-Policy":
                    policies.append(attributes["content"])
        MetaParser().feed(source)
        self.assertEqual(len(policies), 1)
        self.assertLess(source.index("Content-Security-Policy"), source.index("<title>"))
        self.assertIn("&#x27;self&#x27;", source)
        providers = " ".join([*DEPLOY.map_resource_origins(STATE), DEPLOY.MAKI_ICON_SOURCE])
        self.assertEqual(set(policies[0].split("; ")), {
            "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'",
            f"connect-src 'self' blob: {RESOURCES['host']} {providers}", f"img-src 'self' blob: data: {providers}",
            "media-src 'self' blob:", "frame-src 'self' blob:", "object-src 'none'",
            "worker-src 'self' blob:", "base-uri 'self'", "form-action 'none'",
        })
        self.assertIn(RESOURCES["host"], source)

    def test_map_permissions_are_explicit_normalized_and_do_not_read_backend_overrides(self):
        defaults = DEPLOY.public_config(STATE, {**RESOURCES, "map_resource_origins": ["https://untrusted.example"]})
        self.assertEqual(len(defaults["map_resource_origins"]), 16)
        self.assertNotIn("https://untrusted.example", defaults["map_resource_origins"])
        selected = ["https://Tiles.Example.com:443", "https://tiles.example.com", "https://custom.example:8443"]
        config = DEPLOY.public_config({**STATE, "map_resource_origins": selected}, RESOURCES)
        self.assertEqual(config["map_resource_origins"], ["https://tiles.example.com", "https://custom.example:8443"])
        source = DEPLOY.apply_content_security_policy(b"<html><head></head></html>", config).decode()
        self.assertIn("https://tiles.example.com https://custom.example:8443", source)
        self.assertNotIn("https://tile.openstreetmap.org", source)

    def test_empty_map_permissions_keep_only_pinned_icon_directory(self):
        config = DEPLOY.public_config({**STATE, "map_resource_origins": []}, RESOURCES)
        self.assertEqual(config["map_resource_origins"], [])
        source = DEPLOY.apply_content_security_policy(b"<html><head></head></html>", config).decode()
        self.assertNotIn("https://tile.openstreetmap.org", source)
        self.assertIn(DEPLOY.MAKI_ICON_SOURCE, source)
        self.assertNotIn("https://cdn.jsdelivr.net ", source)
        self.assertNotIn("events.mapbox.com", source)
        self.assertNotIn("unsafe-eval", source)

    def test_invalid_map_permissions_fail_before_archive_or_csp_creation(self):
        malformed = [None, {}, "https://tiles.example", 3, [None], [False],
                     ["http://tiles.example"], ["https://user:password@tiles.example"],
                     ["https://tiles.example/"], ["https://tiles.example/path"],
                     ["https://tiles.example?"], ["https://tiles.example#"],
                     ["https://tiles.example?key=secret"], ["https://*.example"],
                     ["https://tiles.example; script-src *"], ["https://tiles.example\n"],
                     ["https://tiles.example:65536"], ["https://tiles.example:"],
                     ["https://tiles.example:0"], ["https://-tiles.example"],
                     ["https://tiles..example"], ["https://tiles.example\\path"]]
        for values in malformed:
            with self.subTest(value=values):
                with self.assertRaisesRegex(ValueError, "map_resource_origins"):
                    DEPLOY.public_config({**STATE, "map_resource_origins": values}, RESOURCES)
                with self.assertRaisesRegex(ValueError, "map_resource_origins"):
                    DEPLOY.apply_content_security_policy(b"<head></head>", {"host": RESOURCES["host"], "map_resource_origins": values})

    def test_invalid_foundry_origin_cannot_enter_security_policy(self):
        for host in ("https://foundry.example.com; connect-src *", 'https://host.example.com" onload="x',
                     "http://foundry.example.com", "https://foundry.example.com/path"):
            with self.subTest(host=host), self.assertRaisesRegex(ValueError, "HTTPS origin"):
                DEPLOY.build_archive(STATE, {**RESOURCES, "host": host})

    def test_headless_html_cannot_ship_without_policy(self):
        (self.fixture / "web/index.html").write_text("<html><body>No head</body></html>")
        with self.assertRaisesRegex(ValueError, "requires a head element"):
            self.build()

    def test_oversized_asset_fails_before_upload(self):
        read_bytes = Path.read_bytes
        def contents(path):
            return b"a" * 20_000_001 if path.name == "app.mjs" else read_bytes(path)
        with patch.object(Path, "read_bytes", contents):
            with self.assertRaisesRegex(ValueError, "20 MB"):
                self.build()

    def test_committed_manifests_contain_no_credential_fields(self):
        forbidden = {"token", "access_token", "refresh_token", "password", "client_secret", "api_key", "secret"}
        def visit(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(key.lower() for key in value))
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)
        for relative in ("deploy/state.example.json", "backend/state.example.json"):
            path = ROOT / relative
            self.assertTrue(path.is_file(), f"Portable checkout requires {relative}")
            visit(json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
