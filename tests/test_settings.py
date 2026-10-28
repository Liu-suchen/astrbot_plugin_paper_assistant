import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
config = importlib.import_module(ROOT.name + ".settings")
models = importlib.import_module(ROOT.name + ".models")
Store = importlib.import_module(ROOT.name + ".store").Store
Arxiv = importlib.import_module(ROOT.name + ".resolver").Arxiv
Zotero = importlib.import_module(ROOT.name + ".zotero").Zotero
net = importlib.import_module(ROOT.name + ".net")


class SettingsTests(unittest.TestCase):
    def test_defaults_match_ui_and_do_not_bind_a_machine(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text())
        settings = config.Settings()
        for key, definition in schema.items():
            self.assertEqual(settings.get(key), definition["default"], key)
        self.assertEqual(settings.get("file_callback_base"), "")
        self.assertEqual(settings.get("allowed_users"), [])


    def test_configured_choice_expiry_and_delivery_limit_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3", {
                "choice_ttl": 60, "delivery_attempts": 1,
            })
            try:
                choice = store.choice("user", "find", [{"title": "Example"}])
                expires = store.db.execute("SELECT expires FROM choices WHERE id=?", (choice,)).fetchone()[0]
                self.assertGreater(expires, time.time() + 55)
                self.assertLess(expires, time.time() + 65)
                job, _ = store.enqueue("user", "session", "find", {"title": "Example"})
                store.state(job, "done", {"text": "done"})
                self.assertEqual(len(store.undelivered()), 1)
                store.delivery(job, False)
                self.assertEqual(store.undelivered(), [])
            finally:
                store.close()

    def test_configuration_is_read_from_the_source(self):
        source = {"job_timeout": 120}
        settings = config.Settings(source)
        self.assertEqual(settings.get("job_timeout"), 120)
        source["job_timeout"] = 180
        self.assertEqual(settings.get("job_timeout"), 180)

    def test_invalid_values_fail_clearly(self):
        for key, value in [
            ("http_timeout", 0), ("candidate_limit", 100), ("max_pdf_mb", "bad"),
            ("choice_ttl", True), ("allowed_users", "12345"), ("private_only", "false"),
            ("job_timeout", 60.5), ("file_transfer_mode", "unknown"),
        ]:
            with self.subTest(key=key):
                with self.assertRaises(models.PaperError):
                    config.Settings({key: value}).get(key)

    def test_url_validation_does_not_disclose_credentials(self):
        for value in ["http://example.org", "https://user:secret@example.org",
                      "https://example.org?token=secret"]:
            with self.assertRaises(models.PaperError) as error:
                config.Settings({"zotero_api_base": value}).get("zotero_api_base")
            self.assertNotIn("secret", str(error.exception))

    def test_auto_file_delivery_uses_no_machine_default(self):
        self.assertEqual(config.file_transport({}), ("local", ""))
        self.assertEqual(config.file_transport({}, "https://files.example.org/"),
                         ("callback", "https://files.example.org"))

    def test_callback_override_and_local_mode(self):
        self.assertEqual(config.file_transport(
            {"file_callback_base": "http://bot.example.org:6185"}, "https://global.example.org"),
            ("callback", "http://bot.example.org:6185"))
        self.assertEqual(config.file_transport(
            {"file_transfer_mode": "local"}, "https://global.example.org"), ("local", ""))
        with self.assertRaises(models.PaperError):
            config.file_transport({"file_transfer_mode": "callback"})

    def test_library_scope_and_summary_configuration_fingerprint(self):
        first = config.Settings({"zotero_library_id": "100"})
        second = config.Settings({"zotero_library_id": "200"})
        self.assertNotEqual(first.library_scope(), second.library_scope())
        keys = ["summary_language", "summary_instruction"]
        self.assertNotEqual(first.fingerprint(keys),
                            config.Settings({"summary_language": "English"}).fingerprint(keys))

    def test_runtime_source_has_no_personal_deployment_address(self):
        for path in ROOT.glob("*.py"):
            text = path.read_text()
            for marker in ["/Users/", "host.docker.internal"]:
                self.assertNotIn(marker, text, str(path))

    def test_prompt_overrides_are_used_without_formatting_user_braces(self):
        source = {"summary_instruction": '自定义结构 {"method": "..."}', "summary_language": "English"}
        cfg = config.Settings(source)
        self.assertIn('{"method"', cfg.get("summary_instruction"))
        self.assertEqual(cfg.get("summary_language"), "English")


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_arxiv_endpoint_and_candidate_limit(self):
        cfg = {"arxiv_api_url": "https://arxiv-proxy.example.org/api/query",
               "candidate_limit": 2}
        http = AsyncMock()
        http.request.return_value = net.Response(
            200, b'<feed xmlns="http://www.w3.org/2005/Atom"/>', {})
        resolver = Arxiv(http, cfg)
        await resolver.resolve("Example title")
        url = http.request.call_args.args[1]
        self.assertTrue(url.startswith(cfg["arxiv_api_url"]))
        self.assertIn("max_results=2", url)

    async def test_zotero_key_only_goes_to_configured_api(self):
        http = AsyncMock()
        http.request.return_value = net.Response(200, b"[]", {})
        zotero = Zotero(http, {"zotero_library_id": "100", "zotero_api_key": "test-key",
                               "zotero_api_base": "https://zotero-proxy.example.org/api"}, None)
        await zotero.search("1706.03762")
        args = http.request.call_args
        self.assertTrue(args.args[1].startswith("https://zotero-proxy.example.org/api/users/100/"))
        self.assertEqual(args.kwargs["headers"]["Zotero-API-Key"], "test-key")

    async def test_configured_paper_links(self):
        paper = models.Paper("Example", arxiv="1706.03762")
        text = models.card(paper, {"arxiv_web_base": "https://papers.example.org"})
        self.assertIn("https://papers.example.org/pdf/1706.03762", text)


if __name__ == "__main__":
    unittest.main()
