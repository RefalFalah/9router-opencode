import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_9router as setup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.path = self.folder / "opencode.json"
        self.key = self.folder / setup.KEY_FILENAME
        self.state = self.folder / setup.STATE_FILENAME
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def install(self, initial=None, model="combo"):
        if initial is not None:
            self.path.write_text(json.dumps(initial), encoding="utf-8")
        current = self.read_config() if self.path.exists() else {}
        desired = setup.make_config(current, "https://router.test/v1", model, self.key)
        setup.save_config(self.path, desired, "fake-managed-test-key", expected=current)
        return desired

    def read_config(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write_config(self, config):
        self.path.write_text(json.dumps(config), encoding="utf-8")

    def files(self):
        return {file.name: file.read_bytes() for file in self.folder.iterdir() if file.is_file()}

    def test_restore_default_preserve_new_unrelated_changes_and_keep_backups(self):
        self.install({"model": "other/original", "agents": {"review": {"description": "Keep"}}})
        edited = self.read_config()
        edited["mcp"] = {"servers": {"new": {"type": "local", "command": ["demo"]}}}
        edited["providers"]["manual"] = {"name": "Unrelated provider"}
        self.write_config(edited)
        setup.cleanup(self.path, yes=True)
        result = self.read_config()
        self.assertEqual(result["model"], "other/original")
        self.assertEqual(result["agents"], edited["agents"])
        self.assertEqual(result["mcp"], edited["mcp"])
        self.assertEqual(result["providers"], {"manual": {"name": "Unrelated provider"}})
        self.assertFalse(self.key.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(len(list(self.folder.glob("*.bak-9router-*"))), 2)

    def test_restore_preexisting_9router_provider_without_exposing_old_key(self):
        initial = {
            "model": {"providerID": "other", "model": "original"},
            "providers": {"9router": {"settings": {"apiKey": "old-fake-test-key"}, "models": {"old": {}}}},
        }
        self.install(initial)
        setup.cleanup(self.path, yes=True)
        result = self.read_config()
        self.assertEqual(result["providers"], initial["providers"])
        self.assertEqual(result["model"], initial["model"])
        self.assertNotIn("old-fake-test-key", self.output.getvalue())
        self.assertNotIn("fake-managed-test-key", self.output.getvalue())

    def test_no_original_default_and_idempotent_cleanup(self):
        self.install()
        setup.cleanup(self.path, yes=True)
        self.assertNotIn("providers", self.read_config())
        self.assertNotIn("model", self.read_config())
        snapshot = self.files()
        self.assertEqual(setup.cleanup(self.path, yes=True), 0)
        self.assertEqual(self.files(), snapshot)

    def test_user_default_is_not_overwritten(self):
        self.install({"model": "other/original"})
        edited = self.read_config()
        edited["model"] = "manual/new-default"
        self.write_config(edited)
        setup.cleanup(self.path, yes=True)
        self.assertEqual(self.read_config()["model"], "manual/new-default")

    def test_repeated_setup_keeps_original_restore_point(self):
        self.install({"model": "other/original"})
        self.install(model="second-combo")
        setup.cleanup(self.path, yes=True)
        self.assertEqual(self.read_config()["model"], "other/original")
        self.assertNotIn("providers", self.read_config())

    def test_new_user_default_before_reinstall_becomes_restore_point(self):
        self.install({"model": "other/original"})
        edited = self.read_config()
        edited["model"] = "other/new-user-choice"
        self.write_config(edited)
        self.install(model="second-combo")
        setup.cleanup(self.path, yes=True)
        self.assertEqual(self.read_config()["model"], "other/new-user-choice")

    def test_preview_and_declined_confirmation_do_not_write(self):
        self.install()
        snapshot = self.files()
        setup.cleanup(self.path, dry_run=True)
        self.assertEqual(self.files(), snapshot)
        with patch("builtins.input", return_value="no"):
            setup.cleanup(self.path)
        self.assertEqual(self.files(), snapshot)

    def test_manual_provider_changes_block_cleanup_and_reinstall(self):
        self.install()
        edited = self.read_config()
        edited["providers"]["9router"]["models"]["manual-model"] = {"name": "Keep manual"}
        self.write_config(edited)
        snapshot = self.files()
        with self.assertRaises(setup.SetupError):
            setup.cleanup(self.path, yes=True)
        with self.assertRaises(setup.SetupError):
            self.install(model="third-combo")
        self.assertEqual(self.files(), snapshot)

    def test_manual_key_change_is_not_deleted(self):
        self.install()
        self.key.write_text("replacement-manual-test-key", encoding="utf-8")
        snapshot = self.files()
        with self.assertRaises(setup.SetupError):
            setup.cleanup(self.path, yes=True)
        self.assertEqual(self.files(), snapshot)

    def test_keep_shared_key_then_finish_cleanup_later(self):
        self.install()
        edited = self.read_config()
        edited["providers"]["manual"] = {"settings": {"apiKey": "{file:9router-api-key}"}}
        self.write_config(edited)
        setup.cleanup(self.path, yes=True)
        self.assertTrue(self.key.exists())
        self.assertTrue(self.state.exists())
        self.assertNotIn("9router", self.read_config()["providers"])
        edited = self.read_config()
        del edited["providers"]["manual"]
        self.write_config(edited)
        setup.cleanup(self.path, yes=True)
        self.assertFalse(self.key.exists())
        self.assertFalse(self.state.exists())

    def test_cleanup_without_ownership_does_not_touch_legacy_files(self):
        self.path.write_text('{"providers":{"9router":{"name":"Legacy"}}}', encoding="utf-8")
        self.key.write_text("legacy-fake-test-key", encoding="utf-8")
        snapshot = self.files()
        setup.cleanup(self.path, yes=True)
        self.assertEqual(self.files(), snapshot)

    def test_cleanup_config_write_failure_keeps_key_and_state(self):
        self.install()
        original = self.path.read_bytes()
        original_state = self.state.read_bytes()
        real_write = setup.atomic_write

        def fail_config(path, content):
            if path == self.path:
                raise OSError("simulated write failure")
            return real_write(path, content)

        with patch.object(setup, "atomic_write", side_effect=fail_config), self.assertRaises(OSError):
            setup.cleanup(self.path, yes=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.state.read_bytes(), original_state)
        self.assertTrue(self.key.exists())

    def test_changes_during_confirmation_are_not_overwritten(self):
        self.install()

        def edit_while_confirming(_):
            edited = self.read_config()
            edited["username"] = "Changed during confirmation"
            self.write_config(edited)
            return "CLEANUP"

        with patch("builtins.input", side_effect=edit_while_confirming), self.assertRaises(setup.SetupError):
            setup.cleanup(self.path)
        self.assertEqual(self.read_config()["username"], "Changed during confirmation")
        self.assertTrue(self.key.exists())

    def test_cli_cleanup_is_offline_and_does_not_prompt_for_key(self):
        self.install()
        with patch.object(setup, "fetch_models") as fetch, patch.object(setup.getpass, "getpass") as getpass:
            status = setup.main(["--config-dir", str(self.folder), "--cleanup", "--dry-run"])
        self.assertEqual(status, 0)
        fetch.assert_not_called()
        getpass.assert_not_called()

    def test_unmanaged_key_is_not_overwritten_on_install(self):
        self.key.write_text("unmanaged-fake-test-key", encoding="utf-8")
        snapshot = self.files()
        with self.assertRaises(setup.SetupError):
            self.install()
        self.assertEqual(self.files(), snapshot)

    def test_config_changed_during_setup_is_not_overwritten(self):
        installed = self.install()
        edited = self.read_config()
        edited["username"] = "New edit"
        self.write_config(edited)
        snapshot = self.files()
        with self.assertRaises(setup.SetupError):
            setup.save_config(self.path, installed, "another-fake-test-key", expected=installed)
        self.assertEqual(self.files(), snapshot)

    def test_tampered_or_renamed_state_is_rejected(self):
        self.install()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        state["config_name"] = "other.json"
        self.state.write_text(json.dumps(state), encoding="utf-8")
        snapshot = self.files()
        with self.assertRaises(setup.SetupError):
            setup.cleanup(self.path, yes=True)
        self.assertEqual(self.files(), snapshot)


if __name__ == "__main__":
    unittest.main()
