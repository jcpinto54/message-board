import re
import threading
import time
import unittest

from msgboard import cli, model, render
from msgboard.hooks import is_board_command

from .helpers import BoardTestCase


class WaitTest(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.saved = cli.WAIT_POLL_S, cli.WAIT_HEARTBEAT_S
        cli.WAIT_POLL_S = 0.02

    def tearDown(self):
        cli.WAIT_POLL_S, cli.WAIT_HEARTBEAT_S = self.saved
        super().tearDown()

    def start_waiting(self, *args, session="A"):
        """Run `board wait ...` in another thread, returning once it's actually waiting."""
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("run", self.run_cli("wait", *args, session=session)))
        thread.start()
        con = self.con()
        deadline = time.time() + 5
        while not (model.agent_by_session(con, session) or {"waiting": None})["waiting"]:
            self.assertLess(time.time(), deadline, "the wait never started")
            self.assertTrue(thread.is_alive(), result)
            time.sleep(0.01)
        return thread, result

    def finish(self, thread, result) -> str:
        thread.join(10)
        self.assertFalse(thread.is_alive(), "still waiting")
        code, out, err = result["run"]
        self.assertEqual(code, 0, err)
        return out

    def ask(self, text, session="A") -> int:
        out = self.ok("post", ".", "--title", "Question", text, session=session)
        return int(re.search(r"Posted #(\d+)", out).group(1))

    def test_returns_with_the_reply_in_full(self):
        b = self.name_of("B")
        out = self.ok("post", ".", "--title", "Ports", f"@{b} which port is free?", session="A")
        question = int(re.search(r"Posted #(\d+)", out).group(1))
        self.assertIn(f"wait {question}", out)  # the hint, right where it's needed
        thread, result = self.start_waiting(str(question), "--timeout", "10s")
        self.assertIn(f"waiting on #{question}", self.ok("who", session="B"))
        long_answer = "Use 5174.\n" + "The dev server keeps 5173 for the whole migration. " * 5
        self.ok("reply", str(question), long_answer, session="B")
        out = self.finish(thread, result)
        self.assertIn(f"@{b}", out)
        self.assertIn(long_answer.splitlines()[1].strip(), out)  # the whole body, not a snippet
        self.assertIn("Nothing new", self.ok("inbox", session="A"))  # it's been read
        self.assertIsNone(model.agent_by_session(self.con(), "A")["waiting"])
        self.assertNotIn("waiting on", self.ok("who", session="B"))

    def test_a_reply_that_came_before_the_wait_counts_and_my_own_posts_dont(self):
        question = self.ask(f"@{self.name_of('B')} ok to rename utils.py?")
        self.ok("reply", str(question), "yes, go ahead", session="B")
        self.assertIn("yes, go ahead", self.ok("wait", str(question), "--timeout", "5s", session="A"))
        self.ok("reply", str(question), "thanks, doing it", session="A")
        out = self.ok("wait", str(question), "--timeout", "0.2s", session="A")
        self.assertIn(f"Nothing yet after 0.2s: still waiting on #{question}", out)

    def test_replies_deep_in_the_thread_count(self):
        question = self.ask(f"@{self.name_of('B')} thoughts?")
        first = self.ok("reply", str(question), "one question back: which branch?", session="B")
        first_id = re.search(r"Posted #(\d+)", first).group(1)
        self.ok("reply", first_id, "main", session="A")
        thread, result = self.start_waiting(str(question), "--timeout", "10s")
        self.ok("reply", first_id, "then fine by me", session="B")
        self.assertIn("then fine by me", self.finish(thread, result))

    def test_waits_until_a_claimed_area_is_free(self):
        self.ok("claim", "src/**", "--note", "big refactor", session="B")
        thread, result = self.start_waiting("src/auth/**", "--timeout", "10s")
        self.assertIn("waiting on src/auth/** to be free", self.ok("who", session="B"))
        self.ok("release", session="B")
        out = self.finish(thread, result)
        self.assertIn("Nobody else holds a claim on src/auth/** now", out)
        self.assertIn("claim src/auth/**", out)
        self.assertIn("Nobody else holds", self.ok("wait", "port:5173", "--timeout", "5s", session="A"))  # free already

    def test_without_a_target_it_waits_for_what_is_addressed_to_me(self):
        a = self.name_of("A")
        thread, result = self.start_waiting("--timeout", "10s")
        self.ok("post", ".", "general news for everyone", session=None)  # a post in my space: not for me
        time.sleep(0.1)
        self.assertTrue(thread.is_alive())
        self.ok("post", ".", f"@{a} can you review #3 when you get a chance?", session="B")
        out = self.finish(thread, result)
        self.assertIn("mentioned you", out)
        self.assertIn("can you review #3 when you get a chance?", out)
        self.assertNotIn("general news", out)

    def test_something_the_hooks_already_delivered_still_ends_the_wait(self):
        # Running in the background, the agent keeps working, and its hooks may show it the
        # message first. The wait must still return.
        con = self.con()
        a = model.ensure_agent(con, "A", str(self.repo))
        b = model.ensure_agent(con, "B", str(self.repo))
        before = model.last_notification_id(con, a["id"])
        model.post(con, b, model.get_node(con, a["home_id"]), body=f"@{a['name']} done with the API")
        model.mark_delivered(con, [r["id"] for r in model.undelivered(con, a["id"])])
        rows = model.addressed_to(con, a["id"], before, render.BACKGROUND_REASONS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(model.addressed_to(con, a["id"], rows[0]["id"], render.BACKGROUND_REASONS), [])

    def test_a_paused_agent_waits_to_be_resumed(self):
        a = self.name_of("A")
        self.ok("pause", a, "hold on", session=None)
        self.assertTrue(is_board_command("board wait --timeout 30m"))
        thread, result = self.start_waiting("--timeout", "10s")
        self.ok("resume", a, session=None)
        self.assertIn("resumed you", self.finish(thread, result))

    def test_timing_out_is_not_an_error(self):
        out = self.ok("wait", "--timeout", "0.2s", session="A")
        self.assertIn(f"Nothing yet after 0.2s: still waiting on messages for @{self.name_of('A')}", out)
        self.assertIn(" wait` again to keep waiting, or carry on.", out)

    def test_a_waiting_agent_stays_active_and_keeps_its_claims(self):
        cli.WAIT_HEARTBEAT_S = 0.05
        self.ok("claim", "docs/**", session="A")
        thread, result = self.start_waiting("--timeout", "0.6s")
        con = self.con()
        con.execute("UPDATE agents SET last_seen = last_seen - 1000 WHERE session_id = 'A'")
        con.execute("UPDATE claims SET expires_at = expires_at - 3000")
        self.finish(thread, result)
        a = model.agent_by_session(con, "A")
        self.assertLess(time.time() - a["last_seen"], 5)
        self.assertEqual(len(model.active_claims(con, agent_id=a["id"])), 1)

    def test_a_wait_that_was_killed_goes_stale(self):
        con = self.con()
        a = model.ensure_agent(con, "A", str(self.repo))
        model.set_waiting(con, a["id"], "#3", time.time() + 3600)
        self.assertEqual(model.waiting_on(model.get_agent(con, a["id"])), "#3")
        con.execute("UPDATE agents SET last_seen = last_seen - ? WHERE id = ?", (model.WAIT_FRESH_S + 1, a["id"]))
        self.assertIsNone(model.waiting_on(model.get_agent(con, a["id"])))


if __name__ == "__main__":
    unittest.main()
