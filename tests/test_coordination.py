import unittest
from unittest import mock

from msgboard.hooks import is_board_command

from .helpers import BoardTestCase


def context(result):
    return result["hookSpecificOutput"]["additionalContext"]


class QuietWhenAloneTest(BoardTestCase):
    def tool(self, session, cwd=None):
        payload = {"tool_name": "Read", "tool_input": {"file_path": str(self.repo / "README.md")}}
        if cwd is not None:
            payload["cwd"] = str(cwd)
        return self.hook("post-tool-use", payload, session=session)

    def test_an_agent_alone_in_its_repository_hears_nothing(self):
        self.assertIsNone(self.hook("session-start", {"source": "startup"}, session="A"))
        self.assertIsNone(self.tool("A"))
        self.assertIsNone(self.hook("user-prompt-submit", {"prompt": "go"}, session="A"))

    def test_agents_elsewhere_dont_count(self):
        other = self.tmp / "other-repo"
        (other / ".git").mkdir(parents=True)
        self.hook("session-start", {"source": "startup", "cwd": str(other)}, session="B")
        self.assertIsNone(self.hook("session-start", {"source": "startup"}, session="A"))

    def test_both_are_briefed_once_a_second_agent_works_in_the_repository(self):
        self.assertIsNone(self.hook("session-start", {"source": "startup"}, session="A"))
        wt = self.linked_worktree()
        b = self.hook("session-start", {"source": "startup", "cwd": str(wt)}, session="B")
        self.assertIn("## Message board: you are @", context(b))  # A is active in the main checkout
        self.assertIn("## Message board: you are @", context(self.tool("A")))  # A's next tool call
        self.assertIsNone(self.tool("A"))  # once

    def test_messages_addressed_to_a_lone_agent_still_reach_it(self):
        self.hook("session-start", {"source": "startup"}, session="A")
        a = self.name_of("A")
        self.ok("post", ".", f"@{a} please also update the changelog", session=None)
        text = context(self.tool("A"))
        self.assertIn("You are @", text)  # a one-line introduction, not the whole briefing
        self.assertIn("update the changelog", text)
        self.assertNotIn("Board conventions", text)
        self.ok("post", ".", "a general announcement", session=None)
        self.assertIsNone(self.tool("A"))  # not addressed to it: waits for its briefing

    def test_compacting_while_alone_means_a_fresh_briefing_later(self):
        self.ok("status", "around", session="B")
        self.assertIsNotNone(self.hook("session-start", {"source": "startup"}, session="A"))
        self.con().execute("UPDATE agents SET last_seen = last_seen - 3600 WHERE session_id = 'B'")
        self.assertIsNone(self.hook("session-start", {"source": "compact"}, session="A"))
        self.ok("status", "back", session="B")
        self.assertIn("## Message board: you are @", context(self.tool("A")))


class OtherWorktreesTest(BoardTestCase):
    def edit(self, session, path, cwd, event):
        payload = {"tool_name": "Edit", "cwd": str(cwd), "tool_input": {"file_path": str(cwd / path)}}
        return self.hook(event, payload, session=session)

    def test_a_claim_in_another_worktree_is_a_heads_up_never_a_stop(self):
        wt = self.linked_worktree()
        a = self.name_of("A")
        self.ok("claim", "src/auth/**", "--note", "splitting the service", session="A")
        self.assertIsNone(self.edit("B", "src/auth/login.py", wt, "pre-tool-use"))  # never stopped
        note = context(self.edit("B", "src/auth/login.py", wt, "post-tool-use"))
        self.assertIn(f"@{a} has claimed src/auth/**", note)
        self.assertIn("splitting the service", note)
        self.assertIn("merge conflict", note)
        self.assertIsNone(self.edit("B", "src/auth/login.py", wt, "post-tool-use"))  # once
        self.assertIn("in another worktree", self.ok("inbox", session="A"))
        self.assertIn("different worktrees", self.ok("events", session=None, typed=None))
        self.assertIsNone(self.edit("B", "src/other.py", wt, "post-tool-use"))

    def test_the_same_file_edited_in_two_worktrees(self):
        wt = self.linked_worktree()
        self.edit("A", "src/app.py", self.repo, "post-tool-use")
        a = self.name_of("A")
        note = context(self.edit("B", "src/app.py", wt, "post-tool-use"))
        self.assertIn(f"@{a} also edited src/app.py", note)
        self.assertIn("in another worktree", self.ok("inbox", session="A"))

    def test_claiming_an_area_claimed_in_another_worktree_says_so(self):
        wt = self.linked_worktree()
        self.ok("claim", "src/**", session="A")
        out = self.ok("claim", "src/auth/**", session="B", cwd=wt)  # not refused
        self.assertIn("Heads-up: in other worktrees", out)
        self.assertIn("src/**", out)

    def test_the_same_checkout_still_stops_once(self):
        self.ok("claim", "src/**", session="A")
        self.assertIsNotNone(self.edit("B", "src/x.py", self.repo, "pre-tool-use"))


class BroadcastTest(BoardTestCase):
    def test_at_all_reaches_the_repository_not_the_whole_machine(self):
        other = self.tmp / "other-repo"
        (other / ".git").mkdir(parents=True)
        wt = self.linked_worktree()
        self.name_of("B")
        self.ok("whoami", session="W", cwd=wt)
        self.ok("whoami", session="X", cwd=other)
        out = self.ok("post", ".", "@all about to rebase", session="A")
        self.assertIn(self.name_of("B"), out)
        self.assertIn(self.name_of("W"), out)  # another worktree, same repository
        self.assertNotIn(self.name_of("X"), out)
        self.assertIn("Nothing new", self.ok("inbox", session="X", cwd=other))

    def test_at_all_outside_any_repository_reaches_everyone(self):
        other = self.tmp / "other-repo"
        (other / ".git").mkdir(parents=True)
        self.ok("whoami", session="X", cwd=other)
        out = self.ok("post", "/", "--title", "machine-wide", "@all heads up", session="A")
        self.assertIn(self.name_of("X"), out)


class PausedAgentCommandsTest(unittest.TestCase):
    def test_messages_with_shell_characters_are_still_board_commands(self):
        for command in (
            'board post . "tests & lint pass; all good | ready"',
            "board post 4 - <<'EOF'\nline one & two; three\n$(not run)\nEOF",
            "board post 4 - <<EOF\nplain multi-line\nmessage\nEOF",
            'board post . "first line\nsecond line"',
            "board post . '$(literal) and `ticks`'",
            "python3 -S /x/message-board/board.py who",
        ):
            self.assertTrue(is_board_command(command), command)

    def test_windows_paths(self):
        with mock.patch("msgboard.hooks.os.name", "nt"):
            self.assertTrue(is_board_command(r'"C:\Program Files\Python\python.exe" C:\tools\message-board\board.py who'))
            self.assertTrue(is_board_command(r'& "C:\Users\me\bin\board.exe" post . "done"'))
            self.assertFalse(is_board_command(r"C:\Python\python.exe C:\evil.py board.py"))

    def test_anything_else_the_shell_would_run_is_not(self):
        for command in (
            'board post . "$(rm -rf ~)"',
            'board post . "`id`"',
            "board post 4 - <<EOF\n$(rm -rf ~)\nEOF",
            "board post 4 - <<'EOF'\nfine\nEOF\nrm -rf x",
            "board help > src/main.py",
            "board post . --file <(rm -rf x)",
            "board who && rm -rf x",
            "python3 evil.py board.py",
            'python3 -c "import os" board.py',
            'board post . "unterminated',
        ):
            self.assertFalse(is_board_command(command), command)


if __name__ == "__main__":
    unittest.main()
