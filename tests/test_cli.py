import unittest

from msgboard import model

from .helpers import BoardTestCase


class IdentityTest(BoardTestCase):
    def test_each_session_gets_a_stable_unique_name(self):
        a1, a2, b = self.name_of("A"), self.name_of("A"), self.name_of("B")
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b)

    def test_inside_claude_code_without_session_is_an_error(self):
        code, _, err = self.run_cli("whoami", session=None, env={"CLAUDECODE": "1"})
        self.assertEqual(code, 1)
        self.assertIn("--as", err)

    def test_human_outside_claude_code(self):
        out = self.ok("whoami", session=None)
        self.assertIn("@tester (human)", out)

    def test_as_and_rename(self):
        a = self.name_of("A")
        self.ok("rename", "planner", session="A")
        self.assertEqual(self.name_of("A"), "planner")
        out = self.ok("whoami", "--as", "planner", session="B")
        self.assertIn("@planner", out)
        code, _, err = self.run_cli("rename", "planner", session="B")
        self.assertEqual(code, 1)
        self.assertIn("taken", err)
        self.assertNotEqual(a, "planner")


class ConversationTest(BoardTestCase):
    def test_post_reply_read_and_inbox(self):
        a, b = self.name_of("A"), self.name_of("B")
        out = self.ok("post", ".", "--title", "Plan", f"@{b} please review the parser", session="A")
        self.assertIn("Notified: @" + b, out)
        thread = int(out.split("#", 1)[1].split(" ", 1)[0])

        inbox = self.ok("inbox", session="B")
        self.assertIn("mentioned you", inbox)
        self.assertIn("Plan", inbox)

        self.ok("reply", str(thread), "Looks good", session="B")
        self.assertIn("replied to you", self.ok("inbox", session="A"))

        read = self.ok("read", str(thread), session="A")
        self.assertIn("Looks good", read)
        self.assertIn("Nothing new", self.ok("inbox", session="A"))

    def test_shallow_home_subscription_skips_deep_replies(self):
        self.name_of("A"), self.name_of("B"), self.name_of("C")
        out = self.ok("post", ".", "--title", "Topic", "start", session="A")
        thread = out.split("#", 1)[1].split(" ", 1)[0]
        # C hears about the new thread (shallow subscription to the repo space)...
        self.assertIn("Topic", self.ok("inbox", session="C"))
        self.ok("seen", "all", session="C")
        # ...but not about replies deep in it, which only participants hear.
        self.ok("reply", thread, "a reply", session="B")
        self.assertIn("Nothing new", self.ok("inbox", session="C"))
        self.assertIn("a reply", self.ok("inbox", session="A"))

    def test_mention_all_and_unknown(self):
        self.name_of("B")
        out = self.ok("post", ".", "hello @all and @nobody", session="A")
        self.assertIn("nobody is called @nobody", out)
        self.assertIn("hello", self.ok("inbox", session="B"))

    def test_stdin_and_file_bodies(self):
        body = "line one\nline two é✓"
        self.ok("post", ".", "-", session="A", stdin=body.encode("utf-8"))
        (self.repo / "msg.md").write_text("from a file", encoding="utf-8")
        self.ok("post", ".", "--file", "msg.md", session="A")
        log = self.ok("log", session="A")
        self.assertIn("line one line two é✓", log)
        self.assertIn("from a file", log)

    def test_tree_paths_edit_move_archive(self):
        self.ok("post", "/", "--title", "infra", "--kind", "group", session="A")
        self.ok("post", "/infra", "--title", "CI", "pipelines are red", session="A")
        tree = self.ok("tree", session="A")
        self.assertIn("infra", tree)
        self.assertIn("CI", tree)
        node = model.resolve(self.con(), "/infra/ci", None)
        self.assertEqual(node["title"], "CI")
        self.ok("edit", "/infra/CI", "--status", "done", "--kind", "task", session="B")
        self.assertIn("task, done", self.ok("read", "/infra/CI", session="A"))
        self.ok("move", "/infra/CI", ".", session="A")
        self.assertIsNotNone(model.resolve(self.con(), "CI", model.agent_by_session(self.con(), "A")["home_id"]))
        self.ok("archive", "CI", session="A")
        self.assertNotIn("CI", self.ok("tree", session="A"))
        self.assertIn("CI", self.ok("tree", "--all", session="A"))

    def test_conventions_are_shown_and_editable(self):
        out = self.ok(session="A")
        self.assertIn("Board conventions", out)
        self.name_of("B")
        self.ok("edit", "conventions", "--body", "1. Be kind.", session="A")
        self.assertIn("updated the board conventions", self.ok("inbox", session="B"))
        self.assertIn("1. Be kind.", self.ok("conventions", session="B"))
        self.assertIn("Nothing new", self.ok("inbox", session="B"))

    def test_search_and_help(self):
        self.ok("post", ".", "the flaky test is test_login", session="A")
        self.assertIn("test_login", self.ok("search", "flaky", session="B"))
        self.assertIn("CLAIMS", self.ok("help", "claim", session="A"))

    def test_errors_are_reported_not_raised(self):
        code, _, err = self.run_cli("read", "999")
        self.assertEqual(code, 1)
        self.assertIn("no node #999", err.lower())
        code, _, err = self.run_cli("post", ".")
        self.assertEqual(code, 1)


class ClaimTest(BoardTestCase):
    def test_conflicts_force_release_and_waiters(self):
        a, b = self.name_of("A"), self.name_of("B")
        self.ok("claim", "src/**", "--note", "refactor", session="A")
        code, out, _ = self.run_cli("claim", "src/a.py", session="B")
        self.assertEqual(code, 1)
        self.assertIn("refactor", out)
        self.assertIn(f"@{a}", out)
        self.ok("claim", "src/a.py", "--force", session="B")
        self.assertIn("force-claimed", self.ok("inbox", session="A"))
        claims = self.ok("claims", session="A")
        self.assertIn("src/**", claims)
        self.ok("release", "src", session="A")
        self.assertNotIn(f"@{a}", self.ok("claims", session="A"))
        self.ok("release", session="B")
        self.assertIn("No active claims", self.ok("claims", session="A"))

    def test_named_and_git_resources(self):
        self.ok("claim", "port:5173", session="A")
        code, _, _ = self.run_cli("claim", "port:*", session="B")
        self.assertEqual(code, 1)
        self.ok("claim", ".git", "--note", "rebasing", cwd=self.repo / "src", session="A")
        claims = model.active_claims(self.con())
        self.assertTrue(any(c["resource"].endswith("/repo/.git") for c in claims))

    def test_claims_lapse_while_inactive(self):
        self.ok("claim", "src/x.py", session="A")
        con = self.con()
        con.execute("UPDATE claims SET expires_at = 0")
        self.ok("whoami", session="A")
        self.assertIn("expired while you were inactive", self.ok("inbox", "--all", session="A"))
        self.assertIn("No active claims", self.ok("claims", session="A"))

    def test_config(self):
        self.ok("config", "enforce", "block", session="A")
        self.assertIn("enforce = block", self.ok("config", session="A"))
        code, _, err = self.run_cli("config", "enforce", "maybe", session="A")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
