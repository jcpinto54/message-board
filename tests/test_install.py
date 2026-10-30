import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from msgboard import install, render
from msgboard.model import BoardError


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msgboard-install-"))
        self.settings = self.tmp / "settings.json"
        self.home = str(self.tmp / "board-home")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_merge_keeps_other_settings_and_is_idempotent(self):
        theirs = {"type": "command", "command": "echo mine"}
        original = {
            "model": "opus",
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [theirs]}]},
        }
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        install.install(self.settings, self.home, command=False)
        install.install(self.settings, self.home, command=False)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "opus")
        pre = data["hooks"]["PreToolUse"]
        self.assertIn(theirs, pre[0]["hooks"])
        ours = [h for g in pre for h in g["hooks"] if install.is_ours(h)]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["args"][-2:], ["hook", "pre-tool-use"])
        for event, *_ in install.HOOKS:
            self.assertIn(event, data["hooks"])
        # Agents use the board without permission prompts, and can write to it when sandboxed.
        self.assertEqual(data["permissions"]["allow"].count("Bash(board:*)"), 1)
        self.assertEqual(data["sandbox"]["filesystem"]["allowWrite"], [self.home])
        self.assertTrue(list(self.tmp.glob("settings.json.bak-*")))
        self.assertIn("installed", install.describe(self.settings)[0])

        install.uninstall(self.settings, self.home, command=False)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(data, original)

    def test_uninstall_keeps_the_users_own_permissions(self):
        original = {"permissions": {"allow": ["Bash(npm test:*)"]}, "sandbox": {"enabled": True}}
        self.settings.write_text(json.dumps(original), encoding="utf-8")
        install.install(self.settings, self.home, command=False)
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertIn("Bash(npm test:*)", data["permissions"]["allow"])
        self.assertTrue(data["sandbox"]["enabled"])
        install.uninstall(self.settings, self.home, command=False)
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")), original)

    def test_missing_and_invalid_settings(self):
        self.assertEqual(install.load_settings(self.settings), {})
        self.assertIn("not installed", install.describe(self.settings)[0])
        self.settings.write_text("{oops", encoding="utf-8")
        with self.assertRaises(BoardError):
            install.install(self.settings, self.home, command=False)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{oops")

    def test_dry_run_writes_nothing(self):
        lines = install.install(self.settings, self.home, dry_run=True, command=False)
        self.assertFalse(self.settings.exists())
        self.assertIn("SessionStart", "\n".join(lines))



class CodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msgboard-codex-"))
        self.codex = self.tmp / ".codex"
        (self.codex / "rules").mkdir(parents=True)
        self.agents = self.codex / "AGENTS.md"
        self.rules = self.codex / "rules" / "default.rules"
        self.original_agents = "## Working Style\n\n- Write tests first.\n"
        self.original_rules = 'prefix_rule(pattern=["pnpm", "dev"], decision="allow")\n'
        self.agents.write_text(self.original_agents, encoding="utf-8")
        self.rules.write_text(self.original_rules, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_adds_a_section_and_a_rule_and_uninstall_takes_them_out(self):
        lines = install.install_codex(self.codex)
        text = self.agents.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(self.original_agents))  # the user's own instructions stay first
        self.assertIn("run `board`", text)
        self.assertIn(install.CODEX_RULE, self.rules.read_text(encoding="utf-8").splitlines())
        self.assertEqual(len(lines), 2)
        self.assertIn("set up in", install.describe_codex(self.codex)[0])

        self.assertEqual(install.install_codex(self.codex), [f"Codex: already set up ({self.agents})."])
        self.assertEqual(self.agents.read_text(encoding="utf-8"), text)  # idempotent
        self.assertEqual(self.rules.read_text(encoding="utf-8").count(install.CODEX_RULE), 1)

        install.uninstall_codex(self.codex)
        self.assertEqual(self.agents.read_text(encoding="utf-8"), self.original_agents)
        self.assertEqual(self.rules.read_text(encoding="utf-8"), self.original_rules)
        self.assertTrue(list(self.codex.glob("AGENTS.md.bak-*")))

    def test_an_updated_section_replaces_the_old_one(self):
        stale = f"{self.original_agents}\n{install.CODEX_BEGIN}\nold board text\n{install.CODEX_END}\n\n## Mine, after\n"
        self.agents.write_text(stale, encoding="utf-8")
        install.install_codex(self.codex)
        text = self.agents.read_text(encoding="utf-8")
        self.assertNotIn("old board text", text)
        self.assertEqual(text.count(install.CODEX_BEGIN), 1)
        self.assertIn("## Mine, after", text)

    def test_nothing_happens_without_codex(self):
        self.assertEqual(install.install_codex(self.tmp / "no-codex-here"), [])
        self.assertEqual(install.describe_codex(self.tmp / "no-codex-here"), [])
        self.assertFalse((self.tmp / "no-codex-here").exists())


@unittest.skipIf(os.name == "nt", "pyenv is POSIX-only")
class CommandOnPathTest(unittest.TestCase):
    """pyenv puts the Python's own folder first on PATH for programs it starts; shells use its shims."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msgboard-path-"))
        self.root = self.tmp / "pyenv"
        self.version_bin = self.root / "versions" / "3.13.0" / "bin"
        self.shims = self.root / "shims"
        for d in (self.version_bin, self.shims):
            d.mkdir(parents=True)
        self.executable(self.version_bin / "board")  # where pip puts it
        self.env = {"PYENV_ROOT": str(self.root), "PATH": os.pathsep.join([str(self.version_bin), str(self.shims)])}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def executable(path: Path) -> None:
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)

    def test_the_pythons_own_folder_doesnt_count(self):
        with mock.patch.dict(os.environ, self.env):
            self.assertIsNone(render.on_path("board"))
            self.executable(self.shims / "board")
            self.assertEqual(render.on_path("board"), str(self.shims / "board"))

    def test_install_rehashes_pyenv_and_checks_the_command_is_reachable(self):
        calls = []

        def run(args, **kw):
            calls.append(args[1:3] if args[0] != "pyenv" else args)
            if args == ["pyenv", "rehash"]:
                self.executable(self.shims / "board")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.dict(os.environ, self.env), mock.patch.object(install.subprocess, "run", run):
            lines = install.install_command()
        self.assertEqual(calls, [["-m", "pip"], ["pyenv", "rehash"]])
        self.assertIn(f"installed with pip ({self.shims / 'board'}", lines[0])

    def test_install_says_so_when_shells_cant_find_it(self):
        ok = lambda args, **kw: subprocess.CompletedProcess(args, 0, "", "")  # noqa: E731
        with mock.patch.dict(os.environ, self.env), mock.patch.object(install.subprocess, "run", ok), \
                mock.patch.object(install, "_shim_folder", lambda: None):
            lines = install.install_command()
        self.assertIn("your shells don't look there", lines[0])
        self.assertIn("agents will be told to run `python", lines[1])


if __name__ == "__main__":
    unittest.main()
