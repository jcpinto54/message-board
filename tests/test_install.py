import json
import shutil
import tempfile
import unittest
from pathlib import Path

from msgboard import install
from msgboard.model import BoardError


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msgboard-install-"))
        self.settings = self.tmp / "settings.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_merge_keeps_other_settings_and_is_idempotent(self):
        theirs = {"type": "command", "command": "echo mine"}
        original = {
            "model": "opus",
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [theirs]}]},
        }
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        install.install(self.settings, command=False)
        install.install(self.settings, command=False)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "opus")
        pre = data["hooks"]["PreToolUse"]
        self.assertIn(theirs, pre[0]["hooks"])
        ours = [h for g in pre for h in g["hooks"] if install.is_ours(h)]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["args"][-2:], ["hook", "pre-tool-use"])
        for event, *_ in install.HOOKS:
            self.assertIn(event, data["hooks"])
        self.assertTrue(list(self.tmp.glob("settings.json.bak-*")))
        self.assertIn("installed", install.describe(self.settings)[0])

        install.uninstall(self.settings, command=False)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data, original)

    def test_missing_and_invalid_settings(self):
        self.assertEqual(install.load_settings(self.settings), {})
        self.assertIn("not installed", install.describe(self.settings)[0])
        self.settings.write_text("{oops", encoding="utf-8")
        with self.assertRaises(BoardError):
            install.install(self.settings, command=False)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{oops")

    def test_dry_run_writes_nothing(self):
        lines = install.install(self.settings, dry_run=True, command=False)
        self.assertFalse(self.settings.exists())
        self.assertIn("SessionStart", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
