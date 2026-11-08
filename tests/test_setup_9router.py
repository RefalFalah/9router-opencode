import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_9router as setup


class JsoncTests(unittest.TestCase):
    def test_comments_trailing_commas_and_string_values(self):
        parsed = setup.parse_jsonc(r'''
        {
          // Keep the URL and string content.
          "url": "https://example.com/v1",
          "literal": "// /* x */ ,} ,] \\\"",
          /* multi
             line */
          "nested": {"items": [1, 2,],},
        }
        ''')
        self.assertEqual(parsed["url"], "https://example.com/v1")
        self.assertEqual(parsed["nested"], {"items": [1, 2]})
        self.assertTrue(parsed["literal"].startswith("// /* x */ ,} ,]"))

    def test_reject_invalid_or_ambiguous_config(self):
        for raw in ('[]', '{"bad":', '{"x": 1, "x": 2}', '{"x": NaN}'):
            with self.subTest(raw=raw), self.assertRaises(setup.SetupError):
                setup.parse_jsonc(raw)


class ConfigTests(unittest.TestCase):
    def test_preserve_unrelated_settings_and_existing_models(self):
        original = {
            "agents": {"review": {"model": "other/reviewer"}},
            "mcp": {"servers": {"demo": {"type": "local", "command": ["demo"]}}},
            "providers": {
                "other": {"name": "Do not change"},
                "9router": {
                    "settings": {"headerTimeout": 12345},
                    "models": {"old": {"name": "Old"}, "cx/coder": {"limit": {"context": 10000}}},
                },
            },
        }
        snapshot = json.loads(json.dumps(original))
        result = setup.make_config(original, "https://router.test/v1", "cx/coder", Path("C:/config/key"))
        self.assertEqual(original, snapshot)
        self.assertEqual(result["agents"], original["agents"])
        self.assertEqual(result["mcp"], original["mcp"])
        self.assertEqual(result["providers"]["other"], original["providers"]["other"])
        provider = result["providers"]["9router"]
        self.assertEqual(provider["package"], "@opencode/ai/providers/openai-compatible")
        self.assertEqual(provider["settings"]["headerTimeout"], 12345)
        self.assertTrue(provider["settings"]["apiKey"].startswith("{file:"))
        self.assertIn("old", provider["models"])
        self.assertEqual(provider["models"]["cx/coder"]["limit"]["context"], 10000)
        self.assertEqual(result["model"], "9router/cx/coder")
        self.assertNotIn("provider", result)

    def test_optional_limits_and_invalid_shape(self):
        result = setup.make_config({}, "https://router.test/v1", "combo", Path("key"), 32000, 4000)
        self.assertEqual(result["providers"]["9router"]["models"]["combo"]["limit"], {"context": 32000, "output": 4000})
        with self.assertRaises(setup.SetupError):
            setup.make_config({}, "https://router.test/v1", "combo", Path("key"), 1000, 4000)
        with self.assertRaises(setup.SetupError):
            setup.make_config({"providers": []}, "https://router.test/v1", "combo", Path("key"))

    def test_save_backup_and_secret_separation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "opencode.jsonc"
            old = b'{ // original comment\n "model": "other/old",\n}\n'
            path.write_bytes(old)
            result = setup.make_config(setup.parse_jsonc(old.decode()), "https://router.test/v1", "combo", path.parent / "9router-api-key")
            backup = setup.save_config(path, result, "fake-secret-for-test")
            self.assertEqual(backup.read_bytes(), old)
            self.assertEqual((path.parent / "9router-api-key").read_text(), "fake-secret-for-test")
            self.assertNotIn("fake-secret-for-test", path.read_text())
            self.assertEqual(json.loads(path.read_text())["model"], "9router/combo")
            if os.name != "nt":
                for file in (path, backup, path.parent / "9router-api-key"):
                    self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_restore_old_secret_when_config_write_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "opencode.json"
            key = path.parent / "9router-api-key"
            path.write_text('{"model":"old/model"}')
            installed = setup.make_config({"model": "old/model"}, "https://router.test/v1", "combo", key)
            setup.save_config(path, installed, "old-test-key")
            old_config = path.read_bytes()
            state_path = path.parent / setup.STATE_FILENAME
            old_state = state_path.read_bytes()
            updated = setup.make_config(installed, "https://router.test/v1", "new-combo", key)
            real_write = setup.atomic_write

            def fail_config(destination, content):
                if destination == path:
                    raise OSError("simulated failure")
                return real_write(destination, content)

            with patch.object(setup, "atomic_write", side_effect=fail_config):
                with self.assertRaises(OSError):
                    setup.save_config(path, updated, "new-test-key")
            self.assertEqual(key.read_text(), "old-test-key")
            self.assertEqual(path.read_bytes(), old_config)
            self.assertEqual(state_path.read_bytes(), old_state)

    def test_config_location_and_ambiguous_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.assertEqual(setup.config_location(folder), folder.resolve() / "opencode.json")
            (folder / "opencode.jsonc").write_text("{}")
            self.assertEqual(setup.config_location(folder), folder.resolve() / "opencode.jsonc")
            (folder / "opencode.json").write_text("{}")
            with self.assertRaises(setup.SetupError):
                setup.config_location(folder)


class URLTests(unittest.TestCase):
    def test_normalize_and_preserve_prefix(self):
        cases = {
            "https://router.test": "https://router.test/v1",
            "https://router.test/v1/": "https://router.test/v1",
            "https://router.test/ai/v1": "https://router.test/ai/v1",
            "http://127.0.0.1:20128": "http://127.0.0.1:20128/v1",
            "http://[::1]:20128": "http://[::1]:20128/v1",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(setup.normalize_url(url), expected)

    def test_reject_insecure_or_invalid_url(self):
        for url in (
            "http://router.test", "ftp://router.test", "router.test", "https://user:pass@router.test",
            "https://router.test?key=abc", "https://router.test/#abc", "https://router.test/dashboard",
            "https://router.test/v1/chat/completions", "https://router.test:bad", "https://router.test/\nx",
        ):
            with self.subTest(url=url), self.assertRaises(setup.SetupError):
                setup.normalize_url(url)
        self.assertEqual(setup.normalize_url("http://router.test", True), "http://router.test/v1")


class NetworkAndCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/v1/models":
                    self.send_response(200)
                    body = json.dumps({"data": [{"id": "cx/coder"}, {"id": "combo"}, {"id": "combo"}]}).encode()
                elif self.path == "/redirect/v1/models":
                    self.send_response(302)
                    self.send_header("Location", "/should-not-receive-key")
                    body = b""
                elif self.path == "/html/v1/models":
                    self.send_response(200)
                    body = b"<html>Login page</html>"
                else:
                    self.send_response(401)
                    body = b"Do not print server error bodies: fake-test-key"
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_fetch_sorted_models_and_bearer_auth(self):
        self.assertEqual(setup.fetch_models(self.url + "/v1", "fake-test-key"), ["combo", "cx/coder"])
        self.assertEqual(self.seen[-1], ("/v1/models", "Bearer fake-test-key"))

    def test_no_redirects_and_no_error_body_leaks(self):
        for prefix, message in (("redirect", "redirect"), ("unauthorized", "401"), ("html", "bukan JSON")):
            with self.subTest(prefix=prefix), self.assertRaises(setup.SetupError) as captured:
                setup.fetch_models(self.url + "/" + prefix + "/v1", "fake-test-key")
            self.assertIn(message, str(captured.exception))
            self.assertNotIn("fake-test-key", str(captured.exception))
        self.assertFalse(any(path == "/should-not-receive-key" for path, _ in self.seen))

    def test_interactive_cli_flow_writes_only_test_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with patch.dict(os.environ, {"NINEROUTER_API_KEY": ""}), patch("builtins.input", side_effect=[self.url, "cx/coder"]), patch.object(setup.getpass, "getpass", return_value="fake-test-key"), contextlib.redirect_stdout(output):
                status = setup.main(["--config-dir", temporary])
            self.assertEqual(status, 0)
            self.assertNotIn("fake-test-key", output.getvalue())
            config = json.loads((Path(temporary) / "opencode.json").read_text())
            self.assertEqual(config["model"], "9router/cx/coder")

    def test_list_models_and_unknown_model_do_not_write(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"NINEROUTER_API_KEY": "fake-test-key"}), contextlib.redirect_stdout(io.StringIO()):
            setup.main(["--config-dir", temporary, "--url", self.url, "--list-models"])
            self.assertEqual(list(Path(temporary).iterdir()), [])
            with self.assertRaises(setup.SetupError):
                setup.main(["--config-dir", temporary, "--url", self.url, "--model", "not-listed"])
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_unsafe_key_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"NINEROUTER_API_KEY": "bad\nkey"}), patch.object(setup, "fetch_models") as fetch, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(setup.SetupError):
                setup.main(["--config-dir", temporary, "--url", self.url, "--model", "combo"])
            fetch.assert_not_called()
            self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
