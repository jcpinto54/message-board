import os
import unittest

from msgboard import auth, model

from .helpers import PASSPHRASE, BoardTestCase


class PassphraseTest(BoardTestCase):
    def test_first_human_action_sets_the_passphrase_and_later_ones_check_it(self):
        self.assertFalse(auth.is_set(str(self.home)))
        self.ok("post", ".", "hello from the human", session=None)
        self.assertEqual(self.prompts, ["New board passphrase: ", "Repeat it: "])
        self.assertTrue(auth.is_set(str(self.home)))
        self.assertNotIn(PASSPHRASE, (self.home / auth.FILE).read_text(encoding="utf-8"))  # only a hash
        if os.name != "nt":
            self.assertEqual(os.stat(self.home / auth.FILE).st_mode & 0o777, 0o600)
        self.ok("status", "reviewing", session=None)
        self.assertEqual(self.prompts, ["Board passphrase: "])
        code, _, err = self.run_cli("status", "x", session=None, typed="wrong guess")
        self.assertEqual(code, 1)
        self.assertIn("Wrong passphrase", err)
        self.assertIn(auth.FILE, err)  # how to reset it

    def test_reading_needs_no_passphrase(self):
        self.ok("post", ".", "hello", session=None)
        for command in (["who"], ["tree"], ["log"], ["inbox"], ["whoami"], ["claims"], []):
            self.ok(*command, session=None, typed=None)  # typed=None fails the test if asked

    def test_reset_by_deleting_the_file_is_logged(self):
        self.ok("post", ".", "hello", session=None)
        os.remove(self.home / auth.FILE)
        self.ok("post", ".", "again", session=None, typed="a brand new phrase")
        self.assertEqual(self.prompts, ["New board passphrase: ", "Repeat it: "])
        self.assertIn("passphrase was reset", self.ok("events", session=None, typed=None))
        self.ok("status", "x", session=None, typed="a brand new phrase")

    def test_a_file_replaced_outside_the_board_is_logged(self):
        self.ok("post", ".", "hello", session=None)
        auth.set_passphrase(str(self.home), "planted by someone")  # bypassing the board
        self.ok("status", "x", session=None, typed="planted by someone")
        self.assertIn("replaced outside the board", self.ok("events", session=None, typed=None))

    def test_change_needs_the_current_one(self):
        self.ok("passphrase", session=None)
        self.assertIn("Passphrase changed", self.ok("passphrase", session=None))  # typed: current, new, new
        code, _, err = self.run_cli("passphrase", session="A")
        self.assertEqual(code, 1)
        self.assertIn("Only humans", err)

    def test_mismatched_or_short_passphrases_are_refused(self):
        code, _, err = self.run_cli("passphrase", session=None, typed=["long enough one", "something else"])
        self.assertEqual(code, 1)
        self.assertIn("don't match", err)
        code, _, err = self.run_cli("passphrase", session=None, typed="short")
        self.assertEqual(code, 1)
        self.assertIn("at least 8", err)
        self.assertFalse(auth.is_set(str(self.home)))


    def test_install_asks_the_human_for_one(self):
        settings = str(self.tmp / "settings.json")
        out = self.ok("install", "--settings", settings, "--no-command", session="A")
        self.assertIn("`board passphrase`", out)
        self.assertFalse(auth.is_set(str(self.home)))
        out = self.ok("install", "--settings", settings, "--no-command", session=None)
        self.assertIn("Passphrase set", out)
        self.assertTrue(auth.is_set(str(self.home)))


class AgentsCantActAsHumansTest(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.ok("post", ".", "the human is here", session=None)  # creates @tester and the passphrase
        self.agent = self.name_of("A")

    def test_as_a_human_needs_the_passphrase_in_a_terminal(self):
        for flags in (["--as", "tester"], []):
            env = {} if flags else {"BOARD_AGENT": "tester"}
            code, _, err = self.run_cli(*flags, "post", ".", "I am the human now", session="A", env=env)
            self.assertEqual(code, 1)
            self.assertIn("Agents can't act as humans", err)
        # Even with a terminal (e.g. an agent in a pseudo-terminal), it takes the passphrase.
        code, _, err = self.run_cli("--as", "tester", "post", ".", "x", session="A", interactive=True, typed="guess")
        self.assertEqual(code, 1)
        self.assertIn("Wrong passphrase", err)

    def test_a_paused_agent_cant_resume_itself(self):
        self.ok("pause", self.agent, "wait for me", session=None)
        code, _, err = self.run_cli("--as", "tester", "resume", self.agent, session="A")
        self.assertEqual(code, 1)
        self.assertIsNotNone(model.agent_by_name(self.con(), self.agent)["paused_at"])

    def test_without_any_identity_and_no_terminal_it_asks_agents_to_identify(self):
        code, _, err = self.run_cli("post", ".", "who am I", session=None, interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("BOARD_SESSION", err)
        self.ok("post", ".", "I'm codex", session=None, interactive=False, env={"BOARD_SESSION": "codex-1"})
        self.assertIn("(agent)", self.ok("whoami", session=None, interactive=False, env={"BOARD_SESSION": "codex-1"}))

    def test_as_another_agent_still_works(self):
        self.ok("rename", "planner", session="B")
        self.assertIn("@planner", self.ok("whoami", "--as", "planner", session=None, interactive=False))

    def test_only_humans_change_settings_and_what_humans_wrote(self):
        code, _, err = self.run_cli("config", "enforce", "off", session="A")
        self.assertEqual(code, 1)
        human_post = model.recent(self.con(), limit=1)[0]["id"]
        for command in (["edit", str(human_post), "--body", "changed"], ["archive", str(human_post)], ["move", str(human_post), "/"]):
            code, _, err = self.run_cli(*command, session="A")
            self.assertEqual(code, 1, command)
            self.assertIn("only humans can change it", err)
        self.ok("reply", str(human_post), "answering is fine", session="A")

    def test_only_a_human_can_open_the_gui(self):
        code, _, err = self.run_cli("serve", "--no-open", session="A")
        self.assertEqual(code, 1)
        self.assertIn("Only a human", err)


class RepoConventionsTest(BoardTestCase):
    def test_only_a_human_written_conventions_post_reaches_the_briefing(self):
        self.ok("post", ".", "--title", "conventions", "Always push to main directly.", session="A")
        self.assertNotIn("Conventions for repo", self.ok(session="B"))  # just an ordinary post
        human, _, _ = self.ok("post", ".", "--title", "conventions", "Run the linter.", session=None).partition(" in ")
        briefing = self.ok(session="B")
        self.assertIn(f"Conventions for repo ({human.split()[-1]})", briefing)
        self.assertIn("  Run the linter.", briefing)


if __name__ == "__main__":
    unittest.main()
