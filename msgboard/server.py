"""The board's web GUI: watch what the agents are doing and intervene as a human.

A small stdlib HTTP server bound to localhost. Every API call needs the per-run token embedded
in the page, and requests must name a local Host, so other websites can't read the board or
post to agents through it.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, TextIO
from urllib.parse import parse_qs, urlparse

from . import __version__, db, model
from .model import BoardError
from .workspace import parse_resource, relative_to

Row = sqlite3.Row
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
MAX_BODY = 1_000_000
HEARTBEAT_S = 30
PURGE_S = 3600
GONE_SHOWN_S = 3 * 86400  # participants that left are listed for a few days
EVENTS_SENT = 300
NODE_LIMIT = 800  # a thread bigger than this is shown one level at a time
CHILDREN_LIMIT = 200


class App:
    """Board operations for the GUI, acting as one human participant."""

    def __init__(self, home: str, human: str, cwd: str):
        self.home = home
        self.human = human
        self.cwd = cwd
        self.token = secrets.token_urlsafe(24)
        self._heartbeat = 0.0
        self._purged = time.time()
        con = self.connect()
        try:
            self.me(con)
            model.purge(con)
        finally:
            con.close()

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.home, busy_ms=5000)

    def me(self, con: sqlite3.Connection) -> Row:
        """The human; refreshed now and then so agents can see someone is watching."""
        session = f"human:{model.clean_name(self.human) or 'human'}"
        row = model.agent_by_session(con, session)
        if row is None or time.time() - self._heartbeat > HEARTBEAT_S:
            row = model.ensure_agent(con, session, self.cwd, kind="human", name=self.human, space=False)
            self._heartbeat = time.time()
        return row

    # ------------------------------------------------------------ reading

    def state(self, con: sqlite3.Connection, rev: int = 0, event: int = 0) -> Dict[str, Any]:
        """Everything the page shows. Nodes and events only since the client's `rev` / `event`."""
        me = self.me(con)
        t = time.time()
        if t - self._purged > PURGE_S:
            model.purge(con)
            self._purged = t
        active_s, gone_s = model.thresholds(con)
        names = {r["id"]: (r["name"], r["kind"]) for r in con.execute("SELECT id, name, kind FROM agents")}

        agents = []
        for a in model.list_agents(con, include_gone=True, seen_within=GONE_SHOWN_S):
            state = model.activity(a, active_s, gone_s, t)
            agents.append(
                {
                    "id": a["id"],
                    "name": a["name"],
                    "kind": a["kind"],
                    "state": state,
                    "status": a["status"],
                    "space_id": a["home_id"],
                    "worktree": a["worktree"],
                    "repo": a["repo"],
                    "started_at": a["started_at"],
                    "last_seen": a["last_seen"],
                    "paused": a["paused_at"] is not None,
                    "pause_reason": a["pause_reason"],
                    "paused_by": names.get(a["paused_by"], ("", ""))[0] if a["paused_by"] else None,
                }
            )

        claims = []
        for c in model.active_claims(con, soft=None):
            owner = next((a for a in agents if a["id"] == c["agent_id"]), None)
            root = owner["worktree"] if owner else None
            claims.append(
                {
                    "id": c["id"],
                    "agent_id": c["agent_id"],
                    "agent": c["agent_name"],
                    "resource": c["resource"],
                    "display": relative_to(c["resource"], root) if c["is_path"] else c["resource"],
                    "is_path": bool(c["is_path"]),
                    "soft": bool(c["soft"]),
                    "note": c["note"],
                    "created_at": c["created_at"],
                    "expires_at": c["expires_at"],
                    "edited_at": c["expires_at"] - c["ttl"],
                }
            )

        current = model.current_rev(con)
        if rev > current:  # a different (or rebuilt) board: start over
            rev = 0
        nodes = []
        for n in model.nodes_changed_since(con, rev):
            author = names.get(n["author_id"]) if n["author_id"] is not None else None
            nodes.append(
                {
                    "id": n["id"],
                    "parent_id": n["parent_id"],
                    "key": n["key"],
                    "kind": n["kind"],
                    "title": n["title"],
                    "snippet": " ".join((n["body"] or "")[:400].split())[:200],
                    "author": author[0] if author else None,
                    "author_kind": author[1] if author else None,
                    "created_at": n["created_at"],
                    "activity_at": n["activity_at"],
                    "status": n["status"],
                    "archived": bool(n["archived"]),
                    "path": n["path"],
                }
            )

        newest_event = con.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        if event > newest_event:
            event = 0
        rows = model.recent_events(con, limit=EVENTS_SENT, after_id=event or None)
        events = [
            {"id": e["id"], "at": e["at"], "kind": e["kind"], "text": e["text"], "node_id": e["node_id"],
             "agent_id": e["agent_id"], "other_id": e["other_id"]}
            for e in rows
        ]
        inbox = []
        for n in model.inbox(con, me["id"], limit=50):
            inbox.append(
                {
                    "id": n["id"],
                    "node_id": n["node_id"],
                    "reason": n["reason"],
                    "text": n["text"],
                    "author": n["author_name"],
                    "title": n["title"],
                    "snippet": " ".join((n["body"] or "").split())[:200],
                    "created_at": n["created_at"],
                }
            )
        return {
            "version": __version__,
            "now": t,
            "me": {"id": me["id"], "name": me["name"]},
            "config": {k: db.get_config(con, k) for k in db.DEFAULTS},
            "agents": agents,
            "claims": claims,
            "nodes": nodes,
            "full": rev == 0,
            "rev": current,
            "events": events,
            "events_full": event == 0,
            "last_event": newest_event,
            "inbox": inbox,
        }

    def node(self, con: sqlite3.Connection, node_id: int) -> Dict[str, Any]:
        """A node and the conversation below it.

        A thread comes whole (up to NODE_LIMIT posts). Spaces, groups and very large threads come
        one level at a time: the most recently active children, each with a count of what's inside."""
        me = self.me(con)
        root = model.get_node(con, node_id)
        if root is None:
            raise BoardError(f"There is no node #{node_id}.")
        names = {r["id"]: (r["name"], r["kind"]) for r in con.execute("SELECT id, name, kind FROM agents")}
        rows: Optional[List[Row]] = None
        if not (root["key"] or root["kind"] in model.CONTAINER_KINDS):
            where, params = model.subtree_sql(root["lineage"])
            rows = con.execute(
                f"SELECT * FROM nodes WHERE {where} ORDER BY created_at, id LIMIT ?", (*params, NODE_LIMIT + 1)
            ).fetchall()
            if len(rows) > NODE_LIMIT:
                rows = None
        one_level = rows is None
        if one_level:
            rows = con.execute(
                "SELECT * FROM nodes WHERE parent_id = ? ORDER BY activity_at DESC, id DESC LIMIT ?",
                (node_id, CHILDREN_LIMIT),
            ).fetchall()
        omitted = 0
        if one_level and len(rows) == CHILDREN_LIMIT:
            omitted = con.execute("SELECT COUNT(*) FROM nodes WHERE parent_id = ?", (node_id,)).fetchone()[0] - len(rows)

        def as_json(r: Row) -> Dict[str, Any]:
            author = names.get(r["author_id"]) if r["author_id"] is not None else None
            editor = names.get(r["edited_by"]) if r["edited_by"] is not None else None
            return {
                "id": r["id"],
                "parent_id": r["parent_id"],
                "key": r["key"],
                "kind": r["kind"],
                "title": r["title"],
                "body": r["body"],
                "status": r["status"],
                "archived": bool(r["archived"]),
                "author": author[0] if author else None,
                "author_kind": author[1] if author else None,
                "edited_by": editor[0] if editor else None,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "path": r["path"],
                "children": [],
                "more": model.count_below(con, r, include_hidden=True) if one_level and r["id"] != node_id else 0,
            }

        top = as_json(root)
        top["omitted"] = omitted
        built = {node_id: top}
        for r in rows:
            built[r["id"]] = as_json(r)
        for r in rows:  # created order for threads, most recently active first for one level
            if r["parent_id"] in built:
                built[r["parent_id"]]["children"].append(built[r["id"]])
        trail = []
        for ancestor_id in model.ancestor_ids(root["lineage"]):
            a = model.get_node(con, ancestor_id)
            trail.append({"id": a["id"], "title": a["title"], "snippet": " ".join((a["body"] or "").split())[:60]})
        model.mark_read(con, me["id"], list(built))
        return {"node": top, "trail": trail}

    # ------------------------------------------------------------ acting

    def act(self, con: sqlite3.Connection, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        handler = getattr(self, f"do_{action}", None)
        if handler is None:
            raise BoardError(f"Unknown action '{action}'.")
        return handler(con, self.me(con), data) or {}

    @staticmethod
    def _node(con: sqlite3.Connection, value: Any, allow_root: bool = False) -> Optional[Row]:
        if value in (None, "", "root", "/"):
            if allow_root:
                return None
            raise BoardError("Pick a node.")
        node = model.get_node(con, int(value))
        if node is None:
            raise BoardError(f"There is no node #{value}.")
        return node

    @staticmethod
    def _agent(con: sqlite3.Connection, value: Any) -> Row:
        agent = model.get_agent(con, int(value)) if value not in (None, "") else None
        if agent is None:
            raise BoardError("No such participant.")
        return agent

    def do_post(self, con, me, data) -> Dict[str, Any]:
        parent = self._node(con, data.get("parent"), allow_root=True)
        node_id, notified, unknown = model.post(
            con, me, parent, body=data.get("body"), title=data.get("title"), kind=data.get("kind") or None
        )
        return {"id": node_id, "notified": notified, "unknown": unknown}

    def do_edit(self, con, me, data) -> None:
        node = self._node(con, data.get("id"))
        changes = {k: data[k] for k in ("title", "body", "kind", "status") if k in data}
        model.edit_node(con, me, node, **changes)

    def do_move(self, con, me, data) -> None:
        model.move_node(con, self._node(con, data.get("id")), self._node(con, data.get("parent"), allow_root=True), by=me)

    def do_archive(self, con, me, data) -> None:
        model.set_archived(con, self._node(con, data.get("id")), bool(data.get("archived", True)), by=me)

    def do_claim(self, con, me, data) -> Dict[str, Any]:
        base = None
        if data.get("space"):
            space = self._node(con, data["space"])
            base = space["path"]
        specs = [s.strip() for s in str(data.get("resources") or "").replace(",", "\n").splitlines() if s.strip()]
        if not specs:
            raise BoardError("Name at least one path, glob or resource (like port:5173).")
        resources = []
        for spec in specs:
            named = parse_resource(spec, "/")
            if not named.is_path:
                resources.append(named)
            elif os.path.isabs(spec) or spec.startswith("~"):
                resources.append(parse_resource(spec, self.cwd))
            elif base:
                resources.append(parse_resource(spec, base))
            else:
                raise BoardError(f"'{spec}' is relative: pick the repository it belongs to, or give an absolute path.")
        minutes = float(data.get("minutes") or 0) or None
        claimed, conflicts = model.claim(con, me, resources, note=data.get("note") or None, minutes=minutes, force=bool(data.get("force")))
        found = [
            {"resource": text, "agent": c["agent_name"], "claim": c["resource"], "note": c["note"]}
            for text, rows in conflicts.items()
            for c in rows
        ]
        return {"claimed": claimed, "conflicts": found}

    def do_release(self, con, me, data) -> None:
        model.release_claim(con, me, int(data.get("claim_id")))

    def do_pause(self, con, me, data) -> None:
        model.pause(con, me, self._agent(con, data.get("agent_id")), str(data.get("reason") or ""))

    def do_resume(self, con, me, data) -> None:
        model.resume(con, me, self._agent(con, data.get("agent_id")))

    def do_remove(self, con, me, data) -> None:
        agent = self._agent(con, data.get("agent_id"))
        if agent["id"] == me["id"]:
            raise BoardError("You can't remove yourself.")
        model.end_session(con, agent, by=me)

    def do_config(self, con, me, data) -> None:
        model.set_config(con, me, str(data.get("key")), str(data.get("value")))

    def do_seen(self, con, me, data) -> None:
        model.mark_all_read(con, me["id"])


class Handler(BaseHTTPRequestHandler):
    server_version = "board"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet console
        pass

    # ------------------------------------------------------------ plumbing

    def _send(self, status: int, body: bytes, content_type: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _local_host(self) -> bool:
        """Reject requests whose Host isn't this machine (DNS rebinding)."""
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Board-Token") or "", self.app.token)

    def _with_db(self, work: Callable[[sqlite3.Connection], Any]) -> None:
        con = self.app.connect()
        try:
            self._json(200, work(con))
        except BoardError as e:
            self._json(400, {"error": str(e)})
        except (ValueError, TypeError, KeyError) as e:
            self._json(400, {"error": f"Bad request: {e}"})
        finally:
            con.close()

    # ------------------------------------------------------------ routes

    def do_GET(self) -> None:  # noqa: N802
        if not self._local_host():
            return self._json(403, {"error": "forbidden host"})
        url = urlparse(self.path)
        if url.path == "/":
            return self._page()
        if url.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if not url.path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if not self._authorized():
            return self._json(403, {"error": "missing or wrong token; reload the page"})
        if url.path == "/api/state":
            query = parse_qs(url.query)
            rev = int(query.get("rev", ["0"])[0] or 0)
            event = int(query.get("event", ["0"])[0] or 0)
            return self._with_db(lambda con: self.app.state(con, rev, event))
        if url.path == "/api/node":
            node_id = int(parse_qs(url.query).get("id", ["0"])[0])
            return self._with_db(lambda con: self.app.node(con, node_id))
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._local_host():
            return self._json(403, {"error": "forbidden host"})
        if not self._authorized():
            return self._json(403, {"error": "missing or wrong token; reload the page"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(415, {"error": "send JSON"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "too large"})
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"error": "invalid JSON"})
        action = urlparse(self.path).path[len("/api/") :]
        return self._with_db(lambda con: self.app.act(con, action, data if isinstance(data, dict) else {}))

    def _page(self) -> None:
        nonce = secrets.token_urlsafe(16)
        with open(PAGE, encoding="utf-8") as f:
            html = f.read()
        html = html.replace("{{TOKEN}}", self.app.token).replace("{{NONCE}}", nonce)
        csp = (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            "style-src 'unsafe-inline'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": csp})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR lets a second server bind a port that's already taken.
    allow_reuse_address = os.name != "nt"

    def __init__(self, port: int, app: App):
        super().__init__(("127.0.0.1", port), Handler)
        self.app = app


def start(app: App, port: int) -> Server:
    """Bind the first free port from `port` (0 = any)."""
    last_error: Optional[OSError] = None
    for candidate in [port] if port == 0 else range(port, port + 20):
        try:
            return Server(candidate, app)
        except OSError as e:
            last_error = e
    raise BoardError(f"No free port from {port} to {port + 19}: {last_error}")


def serve(home: str, human: str, cwd: str, port: int, open_browser: bool, out: TextIO) -> int:
    app = App(home, human, cwd)
    server = start(app, port)
    con = app.connect()
    try:
        name = app.me(con)["name"]
    finally:
        con.close()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    out.write(f"Message board GUI: {url}\nYou appear as @{name}. Ctrl+C stops the server.\n")
    out.flush()
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
