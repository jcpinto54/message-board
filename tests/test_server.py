import http.client
import json
import re
import threading
import urllib.request

from msgboard import model, server
from msgboard.workspace import parse_resource

from .helpers import BoardTestCase


class ServerTest(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.app = server.App(str(self.home), "Tester", str(self.repo))
        self.httpd = server.start(self.app, 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.agent = model.ensure_agent(self.con(), "A", str(self.repo))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def request(self, method, path, body=None, token=True, host=None, content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            headers["X-Board-Token"] = self.app.token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = content_type
        conn.request(method, path, body=data, headers=headers)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        return res.status, dict(res.getheaders()), raw

    def api(self, action, body=None):
        status, _, raw = self.request("POST" if body is not None else "GET", "/api/" + action, body)
        data = json.loads(raw)
        if status != 200:
            raise AssertionError(f"{action}: {status} {data}")
        return data

    def state(self):
        return self.api("state")

    # ------------------------------------------------------------ security

    def test_api_needs_the_token_and_a_local_host(self):
        self.assertEqual(self.request("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/state", host="evil.example:80")[0], 403)
        self.assertEqual(self.request("POST", "/api/post", {"parent": "", "body": "x"}, token=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/post", {"body": "x"}, content_type="text/plain")[0], 415)
        self.assertEqual(self.request("GET", "/api/state")[0], 200)
        self.assertEqual(self.request("GET", "/", host=f"localhost:{self.port}")[0], 200)

    def test_page_embeds_the_token_and_a_strict_csp(self):
        status, headers, raw = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        html = raw.decode("utf-8")
        self.assertIn(self.app.token, html)
        csp = headers["Content-Security-Policy"]
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        self.assertIn(f'<script nonce="{nonce}">', html)
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("{{", html)

    def test_errors_come_back_as_json(self):
        status, _, raw = self.request("POST", "/api/nonsense", {})
        self.assertEqual(status, 400)
        self.assertIn("Unknown action", json.loads(raw)["error"])
        status, _, raw = self.request("POST", "/api/post", {"parent": 999, "body": "x"})
        self.assertEqual(status, 400)

    # ------------------------------------------------------------ watching

    def test_state_shows_agents_claims_nodes_and_events(self):
        con = self.con()
        model.set_status(con, self.agent, "refactoring")
        model.claim(con, self.agent, [parse_resource("src/**", str(self.repo))], note="mine")
        s = self.state()
        self.assertEqual(s["me"]["name"], "tester")
        names = {a["name"]: a for a in s["agents"]}
        self.assertEqual(names[self.agent["name"]]["status"], "refactoring")
        self.assertEqual(names["tester"]["kind"], "human")
        self.assertEqual(s["claims"][0]["display"], "src/**")
        self.assertTrue(any(n["key"] == "conventions" for n in s["nodes"]))
        kinds = [e["kind"] for e in s["events"]]
        self.assertIn("join", kinds)
        self.assertIn("claim", kinds)

    def test_human_does_not_get_a_space_for_its_folder(self):
        spaces = [n for n in self.state()["nodes"] if n["parent_id"] is None and n["path"]]
        self.assertEqual(len(spaces), 1)  # only the agent's repository

    def test_node_detail_and_read_marks(self):
        space = self.agent["home_id"]
        thread = self.api("post", {"parent": space, "title": "Plan", "body": "do it"})["id"]
        reply, _, _ = model.post(self.con(), self.agent, model.get_node(self.con(), thread), body="@tester ok?")
        self.assertEqual(len(self.state()["inbox"]), 1)
        detail = self.api(f"node?id={thread}")
        self.assertEqual(detail["node"]["title"], "Plan")
        self.assertEqual(detail["node"]["children"][0]["id"], reply)
        self.assertEqual(self.state()["inbox"], [])  # reading marks it read
        space_view = self.api(f"node?id={space}")
        self.assertEqual(space_view["node"]["children"][0]["more"], 1)  # spaces show one level
        self.assertEqual(space_view["node"]["children"][0]["children"], [])

    # ------------------------------------------------------------ intervening

    def test_posting_as_the_human_notifies_agents(self):
        r = self.api("post", {"parent": self.agent["home_id"], "body": f"@{self.agent['name']} stop please"})
        self.assertEqual(r["notified"], [self.agent["name"]])
        inbox = model.inbox(self.con(), self.agent["id"])
        self.assertEqual(inbox[0]["reason"], "mention")
        self.assertEqual(inbox[0]["author_name"], "tester")

    def test_pause_and_resume(self):
        self.api("pause", {"agent_id": self.agent["id"], "reason": "wait for me"})
        a = model.get_agent(self.con(), self.agent["id"])
        self.assertIsNotNone(a["paused_at"])
        self.assertEqual(a["pause_reason"], "wait for me")
        self.assertTrue(next(x for x in self.state()["agents"] if x["id"] == a["id"])["paused"])
        self.api("resume", {"agent_id": self.agent["id"]})
        self.assertIsNone(model.get_agent(self.con(), self.agent["id"])["paused_at"])
        self.assertIn("resumed you", model.inbox(self.con(), self.agent["id"])[-1]["text"])

    def test_release_any_claim_and_claim_as_human(self):
        con = self.con()
        model.claim(con, self.agent, [parse_resource("src/**", str(self.repo))])
        claim_id = model.active_claims(con)[0]["id"]
        self.api("release", {"claim_id": claim_id})
        self.assertEqual(model.active_claims(con), [])
        self.assertIn("released your claim", model.inbox(con, self.agent["id"])[-1]["text"])

        r = self.api("claim", {"resources": "lib/**\nport:9000", "space": self.agent["home_id"], "note": "mine", "minutes": "60"})
        self.assertTrue(r["claimed"])
        model.claim(con, self.agent, [parse_resource("lib/x.py", str(self.repo))], force=True)
        r = self.api("claim", {"resources": "lib/x.py", "space": self.agent["home_id"]})
        self.assertFalse(r["claimed"])
        self.assertEqual(r["conflicts"][0]["agent"], self.agent["name"])
        self.assertTrue(self.api("claim", {"resources": "lib/x.py", "space": self.agent["home_id"], "force": True})["claimed"])

    def test_relative_claims_need_a_repository(self):
        status, _, raw = self.request("POST", "/api/claim", {"resources": "src/**"})
        self.assertEqual(status, 400)
        self.assertIn("relative", json.loads(raw)["error"])

    def test_config_edit_move_archive_remove_and_seen(self):
        self.api("config", {"key": "enforce", "value": "block"})
        self.assertEqual(self.state()["config"]["enforce"], "block")
        status, _, _ = self.request("POST", "/api/config", {"key": "enforce", "value": "maybe"})
        self.assertEqual(status, 400)
        group = self.api("post", {"parent": "", "title": "infra", "kind": "group"})["id"]
        post = self.api("post", {"parent": self.agent["home_id"], "body": "hello"})["id"]
        self.api("edit", {"id": post, "status": "done", "kind": "task"})
        self.api("move", {"id": post, "parent": group})
        self.api("archive", {"id": post, "archived": True})
        node = model.get_node(self.con(), post)
        self.assertEqual((node["status"], node["kind"], node["parent_id"], node["archived"]), ("done", "task", group, 1))
        self.api("remove", {"agent_id": self.agent["id"]})
        self.assertIsNotNone(model.get_agent(self.con(), self.agent["id"])["ended_at"])
        self.api("seen", {})
        self.assertEqual(self.state()["inbox"], [])


class AuthorizationTest(BoardTestCase):
    def test_agents_cannot_pause_or_force_release(self):
        con = self.con()
        a = model.ensure_agent(con, "A", str(self.repo))
        b = model.ensure_agent(con, "B", str(self.repo))
        with self.assertRaises(model.BoardError):
            model.pause(con, a, b, "no")
        model.claim(con, b, [parse_resource("x", str(self.repo))])
        with self.assertRaises(model.BoardError):
            model.release_claim(con, a, model.active_claims(con)[0]["id"])

    def test_urllib_client_works_too(self):
        app = server.App(str(self.home), "Tester", str(self.repo))
        httpd = server.start(app, 0)
        t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_address[1]}/api/state", headers={"X-Board-Token": app.token}
            )
            with urllib.request.urlopen(req, timeout=10) as res:
                self.assertEqual(json.loads(res.read())["me"]["name"], "tester")
        finally:
            httpd.shutdown()
            httpd.server_close()
