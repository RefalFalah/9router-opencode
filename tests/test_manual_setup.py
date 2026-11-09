import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_9router as setup


class ManualSetupTests(unittest.TestCase):
    def test_default_interactive_setup_never_contacts_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with patch.dict(os.environ, {"NINEROUTER_API_KEY": ""}), patch("builtins.input", side_effect=["https://router.test/v1", "cx/my-manual-model"]), patch.object(setup.getpass, "getpass", return_value="fake-test-key"), patch.object(setup, "fetch_models") as fetch, patch.object(setup, "build_opener") as opener, patch.object(setup, "diagnose_connection") as diagnosis, contextlib.redirect_stdout(output):
                result = setup.main(["--config-dir", temporary])
            self.assertEqual(result, 0)
            fetch.assert_not_called()
            opener.assert_not_called()
            diagnosis.assert_not_called()
            self.assertIn("Mode manual", output.getvalue())
            self.assertIn("belum diuji", output.getvalue())
            self.assertNotIn("Koneksi OK", output.getvalue())
            self.assertNotIn("fake-test-key", output.getvalue())
            config = json.loads((Path(temporary) / "opencode.json").read_text(encoding="utf-8"))
            self.assertEqual(config["model"], "9router/cx/my-manual-model")
            provider = config["providers"]["9router"]
            self.assertEqual(provider["models"]["cx/my-manual-model"]["modelID"], "cx/my-manual-model")
            self.assertEqual(provider["headers"]["User-Agent"], setup.USER_AGENT)
            self.assertTrue((Path(temporary) / setup.STATE_FILENAME).exists())

    def test_argument_model_and_legacy_skip_check_are_offline(self):
        for flags in ([], ["--skip-check"]):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"NINEROUTER_API_KEY": "fake-test-key"}), patch("builtins.input") as prompt, patch.object(setup, "fetch_models") as fetch, contextlib.redirect_stdout(io.StringIO()):
                result = setup.main(["--config-dir", temporary, "--url", "https://router.test/v1", "--model", "manual-combo"] + flags)
                self.assertEqual(result, 0)
                prompt.assert_not_called()
                fetch.assert_not_called()

    def test_optional_catalog_check_still_checks_and_passes_timeout(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"NINEROUTER_API_KEY": "fake-test-key"}), patch.object(setup, "fetch_models", return_value=["checked-model"]) as fetch, contextlib.redirect_stdout(io.StringIO()):
            result = setup.main(["--config-dir", temporary, "--url", "https://router.test/v1", "--model", "checked-model", "--check-model", "--timeout", "90"])
            self.assertEqual(result, 0)
            fetch.assert_called_once_with("https://router.test/v1", "fake-test-key", timeout=90)
            self.assertTrue((Path(temporary) / "opencode.json").exists())

    def test_invalid_manual_model_does_not_fetch_or_write(self):
        for model in ("bad model", "model#variant"):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"NINEROUTER_API_KEY": "fake-test-key"}), patch.object(setup, "fetch_models") as fetch, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(setup.SetupError):
                    setup.main(["--config-dir", temporary, "--url", "https://router.test/v1", "--model", model])
                fetch.assert_not_called()
                self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_conflicting_check_options_are_rejected(self):
        for flags in (
            ["--skip-check", "--check-model"],
            ["--skip-check", "--list-models"],
            ["--diagnose", "--check-model"],
            ["--cleanup", "--check-model"],
        ):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as captured:
                setup.main(flags)
            self.assertEqual(captured.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
