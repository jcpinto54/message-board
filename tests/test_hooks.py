import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from .helpers import BoardTestCase

ROOT = Path(__file__).resolve().parent.parent


def context(result):
    return result["hookSpecificOutput"]["additionalContext"]


class HookTest(BoardTestCase):
    def edit(self, session, path, event="pre-tool-use"):
        return self.hook(
            event,
            {"tool_name": "Edit", "tool_input": {"file_path": str(self.repo / path), "old_string": "a", "new_string": "b"}},
            session=session,
        )

    def bash(self, session, command, event="pre-tool-use"):
        return self.hook(event, {"tool_name": "Bash", "tool_input": {"command": command}}, session=session)

    def test_session_start_briefing(self):
        self.hook("session-start", {"source": "startup"}, session="B")
        result = self.hook("session-start", {"source": "startup"}, session="A")
        out = result["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn("you are @", out["additionalContext"])
        self.assertIn("Agents in this repository", out["additionalContext"])
        self.assertIn("Board conventions", out["additionalContext"])

    def test_claimed_file_is_denied_once_then_allowed(self):
        owner = self.name_of("A")
        self.ok("claim", "src/**", "--note", "parser rewrite", session="A")
        denied = self.edit("B", "src/parser.py")
        out = denied["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(f"@{owner}", out["permissionDecisionReason"])
        self.assertIn("parser rewrite", out["permissionDecisionReason"])
        self.assertIn("wants to edit", self.ok("inbox", session="A"))
        self.assertIsNone(self.edit("B", "src/parser.py"))  # the retry goes through
        self.assertIsNone(self.edit("B", "README.md"))  # unclaimed
        self.assertIsNone(self.edit("A", "src/parser.py"))  # own claim

    def test_block_mode_keeps_denying(self):
        self.ok("claim", "src/**", session="A")
        self.ok("config", "enforce", "block", session="A")
        self.assertIsNotNone(self.edit("B", "src/x.py"))
        self.assertIsNotNone(self.edit("B", "src/x.py"))
        self.ok("release", session="A")
        self.assertIsNone(self.edit("B", "src/x.py"))
        self.assertIn("released the claim", self.ok("inbox", session="B"))

    def test_enforce_off(self):
        self.ok("claim", "src/**", session="A")
        self.ok("config", "enforce", "off", session="A")
        self.assertIsNone(self.edit("B", "src/x.py"))

    def test_risky_git_only_blocked_when_others_are_here(self):
        self.assertIsNone(self.bash("A", "git stash"))
        self.name_of("B")
        denied = self.bash("A", "git stash")
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Other agents active in this worktree", denied["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.bash("A", "git stash"))
        self.assertIsNone(self.bash("A", "git status"))
        self.bash("A", "git stash", event="post-tool-use")
        self.assertIn("ran `git stash`", self.ok("inbox", session="B"))

    def test_powershell_tool_is_checked_too(self):
        self.name_of("B")
        result = self.hook(
            "pre-tool-use", {"tool_name": "PowerShell", "tool_input": {"command": "git checkout main"}}, session="A"
        )
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_overlapping_edits_give_both_sides_a_heads_up(self):
        self.edit("A", "src/shared.py", event="post-tool-use")
        result = self.edit("B", "src/shared.py", event="post-tool-use")
        self.assertIn("also edited src/shared.py", context(result))
        self.assertIn("just edited src/shared.py", context(self.bash("A", "ls", event="post-tool-use")))
        # Only once per pair and file.
        self.assertIsNone(self.edit("B", "src/shared.py", event="post-tool-use"))

    def test_notifications_are_delivered_once(self):
        b = self.name_of("B")
        self.ok("post", ".", f"@{b} ping", session="A")
        first = self.hook("user-prompt-submit", {"prompt": "hi"}, session="B")
        self.assertEqual(first["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("ping", context(first))
        self.assertIsNone(self.bash("B", "ls", event="post-tool-use"))
        self.assertIn("ping", self.ok("inbox", session="B"))  # still unread until read

    def test_sessions_running_before_install_get_the_briefing_once(self):
        first = self.bash("A", "ls", event="post-tool-use")
        self.assertIn("## Message board: you are @", context(first))
        self.assertIsNone(self.bash("A", "ls", event="post-tool-use"))

    def test_subagents_leave_notifications_to_the_main_agent(self):
        self.hook("session-start", {"source": "startup"}, session="B")
        b = self.name_of("B")
        self.ok("post", ".", f"@{b} ping", session="A")
        sub = self.hook(
            "post-tool-use", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "agent_id": "sub-1"}, session="B"
        )
        self.assertIsNone(sub)
        self.assertIn("ping", context(self.bash("B", "ls", event="post-tool-use")))

    def test_paused_agents_can_only_use_the_board(self):
        self.name_of("A")
        self.ok("pause", self.name_of("A"), "stop", "now", session=None)
        denied = self.edit("A", "src/x.py")
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn('PAUSED you: "stop now"', denied["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNotNone(self.bash("A", "npm test"))
        self.assertIsNone(self.bash("A", "board inbox"))
        self.assertIsNone(self.bash("A", 'python C:/x/board.py reply 3 "ok"'))
        self.assertIsNotNone(self.bash("A", "board inbox; rm -rf src"))
        self.assertIsNotNone(self.hook("pre-tool-use", {"tool_name": "mcp__gitlab__create_issue", "tool_input": {}}))
        self.assertIn("PAUSED", context(self.bash("A", "ls", event="post-tool-use")))
        self.ok("resume", self.name_of("A"), session=None)
        self.assertIsNone(self.edit("A", "src/x.py"))

    def test_agents_cannot_pause_each_other(self):
        b = self.name_of("B")
        code, _, err = self.run_cli("pause", b, "go away", session="A")
        self.assertEqual(code, 1)
        self.assertIn("Only humans", err)

    def test_human_claims_hold_even_in_warn_mode(self):
        self.name_of("A")
        self.ok("claim", "src/**", "--note", "hands off", session=None)
        first = self.edit("A", "src/x.py")
        self.assertIn("A human's claim holds", first["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNotNone(self.edit("A", "src/x.py"))  # a retry doesn't get through
        inbox = self.ok("inbox", session=None)
        self.assertEqual(inbox.count("wants to edit"), 1)  # and the human is told once
        self.ok("release", session=None)
        self.assertIsNone(self.edit("A", "src/x.py"))

    def test_events_and_force_release_from_the_cli(self):
        a = self.name_of("A")
        self.ok("claim", "src/**", session="A")
        code, _, err = self.run_cli("release", "--of", a, session="B")
        self.assertEqual(code, 1)
        self.assertIn("Only humans", err)
        self.assertIn(f"Released @{a}'s claims", self.ok("release", "--of", a, session=None))
        events = self.ok("events", session=None)
        self.assertIn(f"@{a} claimed src/**", events)
        self.assertIn(f"released @{a}'s claim on src/**", events)

    def test_session_end_releases_claims(self):
        a = self.name_of("A")
        self.ok("claim", "src/**", session="A")
        self.hook("session-end", {"reason": "prompt_input_exit"}, session="A")
        self.assertIn("No active claims", self.ok("claims", session="B"))
        self.assertNotIn(f"@{a} |", self.ok("who", session="B"))

    def test_broken_input_fails_open(self):
        code, out, _ = self.run_cli("hook", "pre-tool-use", stdin=b"{not json")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("JSONDecodeError", (self.home / "hook-errors.log").read_text(encoding="utf-8"))


class SubprocessHookTest(BoardTestCase):
    """Runs the real entry point the way Claude Code does: python board.py hook <event>."""

    def test_entry_point_writes_ascii_json(self):
        env = {**os.environ, **self.env, "CLAUDE_CODE_SESSION_ID": "S"}
        payload = {"session_id": "S", "cwd": str(self.repo), "source": "startup"}
        proc = subprocess.run(
            [sys.executable, str(ROOT / "board.py"), "hook", "session-start"],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            env=env,
            cwd=str(self.repo),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc.stdout.decode("ascii")  # pure ASCII regardless of the console code page
        data = json.loads(proc.stdout)
        self.assertIn("Message board", data["hookSpecificOutput"]["additionalContext"])

    def test_cli_output_is_utf8(self):
        env = {**os.environ, **self.env, "CLAUDE_CODE_SESSION_ID": "S"}
        proc = subprocess.run(
            [sys.executable, str(ROOT / "board.py"), "post", ".", "café ✓"],
            capture_output=True,
            env=env,
            cwd=str(self.repo),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "board.py"), "log"], capture_output=True, env=env, cwd=str(self.repo), timeout=60
        )
        self.assertIn("café ✓", proc.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
