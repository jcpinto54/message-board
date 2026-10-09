import json
import sqlite3
import time

from msgboard import db, hooks, model, render, server
from msgboard.workspace import parse_resource

from .helpers import BoardTestCase


class LineageTest(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.c = self.con()
        self.a = model.ensure_agent(self.c, "A", str(self.repo))
        self.space = model.get_node(self.c, self.a["home_id"])

    def post(self, parent, body="x", title=None):
        node_id, _, _ = model.post(self.c, self.a, parent, body=body, title=title)
        return model.get_node(self.c, node_id)

    def test_lineage_and_depth(self):
        thread = self.post(self.space, title="T")
        reply = self.post(thread)
        self.assertEqual(reply["lineage"], f"/{self.space['id']}/{thread['id']}/{reply['id']}/")
        self.assertEqual(reply["depth"], 2)
        self.assertEqual([i for i, _ in model.ancestors(self.c, reply["id"])], [thread["id"], self.space["id"]])
        self.assertEqual(sorted(i for i, _ in model.descendants(self.c, self.space["id"])), [thread["id"], reply["id"]])
        self.assertEqual(model.count_below(self.c, self.space), 2)

    def test_ids_that_share_a_prefix_stay_separate(self):
        # Node 1's subtree must not include node 10, 11...: the range ends at "/1/" -> "/10".
        root = model.get_node(self.c, 1)
        for _ in range(12):
            self.post(None, title="t")
        self.assertEqual(model.count_below(self.c, root), 0)

    def test_move_rewrites_the_subtree_and_refuses_cycles(self):
        group = self.post(None, title="group")
        thread = self.post(self.space, title="T")
        reply = self.post(thread)
        model.move_node(self.c, thread, group)
        reply = model.get_node(self.c, reply["id"])
        self.assertEqual(reply["lineage"], f"/{group['id']}/{thread['id']}/{reply['id']}/")
        self.assertEqual(reply["depth"], 2)
        with self.assertRaises(model.BoardError):
            model.move_node(self.c, model.get_node(self.c, group["id"]), model.get_node(self.c, reply["id"]))

    def test_archiving_hides_the_subtree(self):
        thread = self.post(self.space, title="T", body="needle one")
        inner = self.post(thread, body="needle two")
        nested = self.post(inner, body="needle three")
        model.set_archived(self.c, model.get_node(self.c, inner["id"]), True)  # archived inside
        model.set_archived(self.c, model.get_node(self.c, thread["id"]), True)
        hidden = lambda i: model.get_node(self.c, i)["hidden"]  # noqa: E731
        self.assertEqual([hidden(thread["id"]), hidden(inner["id"]), hidden(nested["id"])], [1, 1, 1])
        self.assertEqual(model.search(self.c, "needle"), [])
        self.assertEqual(model.recent(self.c), [])
        model.set_archived(self.c, model.get_node(self.c, thread["id"]), False)
        # The thread is back; the part archived on its own stays hidden.
        self.assertEqual([hidden(thread["id"]), hidden(inner["id"]), hidden(nested["id"])], [0, 1, 1])
        self.assertEqual([r["id"] for r in model.search(self.c, "needle")], [thread["id"]])
        moved = self.post(None, title="elsewhere")
        model.set_archived(self.c, model.get_node(self.c, moved["id"]), True)
        model.move_node(self.c, model.get_node(self.c, thread["id"]), model.get_node(self.c, moved["id"]))
        self.assertEqual(hidden(thread["id"]), 1)  # moved under something archived

    def test_new_children_of_hidden_nodes_are_hidden(self):
        thread = self.post(self.space, title="T")
        model.set_archived(self.c, thread, True)
        self.assertEqual(self.post(model.get_node(self.c, thread["id"]))["hidden"], 1)


class MigrationTest(BoardTestCase):
    def test_version_2_boards_are_upgraded_in_place(self):
        self.home.mkdir(parents=True)
        raw = sqlite3.connect(str(self.home / "board.db"))
        for script in (db.SCHEMA_V1, db.SCHEMA_V2):
            for statement in script.split(";"):
                if statement.strip():
                    raw.execute(statement)
        t = time.time()
        rows = [(1, None, 0), (2, None, 0), (3, 2, 1), (4, 3, 0), (5, 2, 0)]
        for node_id, parent, archived in rows:
            raw.execute(
                "INSERT INTO nodes(id, parent_id, kind, title, created_at, updated_at, activity_at, archived)"
                " VALUES (?, ?, 'message', 't', ?, ?, ?, ?)",
                (node_id, parent, t, t, t, archived),
            )
        raw.execute("INSERT INTO events(at, kind, text) VALUES (?, 'join', 'old news')", (t,))
        raw.execute("INSERT INTO agents(name, session_id, started_at, last_seen) VALUES ('old', 'S', ?, ?)", (t, t))
        raw.execute("PRAGMA user_version = 2")
        raw.commit()
        raw.close()

        c = self.con()
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        got = {r["id"]: (r["lineage"], r["depth"], r["hidden"]) for r in c.execute("SELECT * FROM nodes")}
        self.assertEqual(got[4], ("/2/3/4/", 2, 1))  # below the archived #3
        self.assertEqual(got[5], ("/2/5/", 1, 0))
        self.assertEqual(model.agent_by_session(c, "S")["seen_event_id"], 1)  # no replay of old history


class BriefingTest(BoardTestCase):
    def test_conventions_come_before_the_live_state_and_survive_truncation(self):
        c = self.con()
        me = model.ensure_agent(c, "ME", str(self.repo))
        for i in range(14):
            other = model.ensure_agent(c, f"O{i}", str(self.repo))
            model.set_status(c, other, "x" * 150)
            model.claim(c, other, [parse_resource(f"area{i}/{j}/**", str(self.repo)) for j in range(6)])
        text = render.briefing(c, model.get_agent(c, me["id"]))
        self.assertLess(text.index("Board conventions"), text.index("Current state"))
        self.assertIn("...and 4 more (least recently active)", text)
        self.assertIn("(+3 more)", text)
        clipped = hooks._context("SessionStart", text + "\n" + "filler " * 3000)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Board conventions", clipped)
        self.assertIn("8. Be brief", clipped)

    def test_long_conventions_are_clipped(self):
        c = self.con()
        me = model.ensure_agent(c, "ME", str(self.repo))
        model.edit_node(c, me, model.node_by_key(c, "conventions"), body="rule\n" * 2000)
        local, _, _ = model.post(c, me, model.get_node(c, me["home_id"]), title="Conventions", body="local rule")
        text = render.briefing(c, model.get_agent(c, me["id"]))
        self.assertIn("`board conventions` shows the rest", text)
        self.assertIn(f"Conventions for repo (#{local})", text)
        self.assertLess(len(text), 9000)


class ChangesTest(BoardTestCase):
    def changes_for(self, session):
        c = self.con()
        return render.changes(c, model.agent_by_session(c, session))

    def test_agents_hear_what_changed_around_them_once(self):
        c = self.con()
        a = model.ensure_agent(c, "A", str(self.repo))
        render.briefing(c, a)
        self.assertIsNone(self.changes_for("A"))
        b = model.ensure_agent(c, "B", str(self.repo))
        model.set_status(c, b, "first")
        model.set_status(c, b, "second")
        model.claim(c, b, [parse_resource("src/**", str(self.repo))], note="refactor")
        other_repo = self.tmp / "other"
        (other_repo / ".git").mkdir(parents=True)
        far = model.ensure_agent(c, "FAR", str(other_repo))
        model.set_status(c, far, "elsewhere")
        model.set_status(c, a, "my own")
        text = self.changes_for("A")
        self.assertIn(f"@{b['name']} joined", text)
        self.assertIn('"second"', text)
        self.assertNotIn('"first"', text)  # only the latest status
        self.assertIn(f"@{b['name']} claimed src/**", text)
        self.assertNotIn("elsewhere", text)  # another repository
        self.assertNotIn("my own", text)
        self.assertIsNone(self.changes_for("A"))  # reported once

    def test_humans_and_settings_are_always_relevant(self):
        c = self.con()
        a = model.ensure_agent(c, "A", str(self.repo))
        human = model.ensure_agent(c, "human:t", str(self.tmp), kind="human", name="t", space=False)
        model.claim(c, human, [parse_resource("payments/**", str(self.repo))], note="hands off")
        model.set_config(c, human, "enforce", "block")
        text = self.changes_for("A")
        self.assertIn("payments", text)
        self.assertIn("enforce = block", text)
        self.assertEqual(model.get_agent(c, a["id"])["seen_event_id"], c.execute("SELECT MAX(id) FROM events").fetchone()[0])

    def test_delivered_by_the_hooks_and_cleared_by_who(self):
        a = self.name_of("A")
        self.hook("session-start", {"source": "startup"}, session="A")
        self.ok("status", "testing", session="B")
        result = self.hook("post-tool-use", {"tool_name": "Read", "tool_input": {}}, session="A")
        self.assertIn("Changes since you last looked", result["hookSpecificOutput"]["additionalContext"])
        self.ok("status", "still testing", session="B")
        self.ok("who", session="A")
        self.assertIsNone(self.hook("post-tool-use", {"tool_name": "Read", "tool_input": {}}, session="A"))
        self.assertTrue(a)

    def test_a_far_behind_agent_skips_to_the_present(self):
        c = self.con()
        a = model.ensure_agent(c, "A", str(self.repo))
        b = model.ensure_agent(c, "B", str(self.repo))
        for i in range(210):
            model.set_status(c, b, f"s{i}")
        text = self.changes_for("A")
        self.assertIn("more changed than fits here", text)
        self.assertIn('"s209"', text)
        self.assertIsNone(self.changes_for("A"))
        self.assertTrue(a)


class PurgeTest(BoardTestCase):
    def test_old_notifications_are_cleaned_up(self):
        c = self.con()
        a = model.ensure_agent(c, "A", str(self.repo))
        old = time.time() - 10 * 86400
        ancient = time.time() - 40 * 86400
        with db.tx(c):
            for created, read in ((old, old), (old, None), (ancient, None), (time.time(), time.time())):
                c.execute(
                    "INSERT INTO notifications(agent_id, reason, text, created_at, read_at) VALUES (?, 'x', 't', ?, ?)",
                    (a["id"], created, read),
                )
        model.purge(c)
        left = [(r["created_at"] == old, r["read_at"]) for r in c.execute("SELECT * FROM notifications")]
        self.assertEqual(len(left), 2)  # the unread 10-day-old one and the fresh read one


class IncrementalStateTest(BoardTestCase):
    def test_the_gui_gets_only_what_changed(self):
        c = self.con()
        app = server.App(str(self.home), "Tester", str(self.repo))
        a = model.ensure_agent(c, "A", str(self.repo))
        first = app.state(c)
        self.assertTrue(first["full"])
        self.assertTrue(first["events_full"])
        quiet = app.state(c, first["rev"], first["last_event"])
        self.assertEqual(quiet["nodes"], [])
        self.assertEqual(quiet["events"], [])
        node_id, _, _ = model.post(c, a, model.get_node(c, a["home_id"]), body="hi")
        model.set_status(c, a, "busy")
        delta = app.state(c, first["rev"], first["last_event"])
        self.assertFalse(delta["full"])
        self.assertEqual(sorted(n["id"] for n in delta["nodes"]), sorted([node_id, a["home_id"]]))  # new + bumped space
        self.assertEqual([e["kind"] for e in delta["events"]], ["status"])
        reset = app.state(c, delta["rev"] + 100, delta["last_event"] + 100)  # a different board
        self.assertTrue(reset["full"])
        self.assertTrue(reset["events_full"])

    def test_huge_threads_come_one_level_at_a_time(self):
        c = self.con()
        app = server.App(str(self.home), "Tester", str(self.repo))
        a = model.ensure_agent(c, "A", str(self.repo))
        thread, _, _ = model.post(c, a, model.get_node(c, a["home_id"]), title="big", body="b")
        replies = []
        for i in range(12):
            r, _, _ = model.post(c, a, model.get_node(c, thread), body=f"r{i}")
            replies.append(r)
        model.post(c, a, model.get_node(c, replies[0]), body="nested")
        old = (server.NODE_LIMIT, server.CHILDREN_LIMIT)
        server.NODE_LIMIT, server.CHILDREN_LIMIT = 10, 5
        try:
            detail = app.node(c, thread)["node"]
        finally:
            server.NODE_LIMIT, server.CHILDREN_LIMIT = old
        self.assertEqual(len(detail["children"]), 5)
        self.assertEqual(detail["omitted"], 7)
        self.assertEqual(detail["children"][0]["id"], replies[0])  # most recently active first
        self.assertEqual(detail["children"][0]["more"], 1)
        whole = app.node(c, thread)["node"]
        self.assertEqual(len(whole["children"]), 12)
        self.assertEqual(whole["children"][0]["children"][0]["body"], "nested")
        json.dumps(whole)
