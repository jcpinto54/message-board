"""Formatting board content as compact text for agents and humans."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import db, model
from .workspace import relative_to

Row = sqlite3.Row

TREE_CHILD_LIMIT = 15
READ_CHILD_LIMIT = 30


def invocation() -> str:
    """How agents should call the CLI: `board` when installed, otherwise python + script path."""
    import shutil

    if shutil.which("board"):
        return "board"
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(os.path.dirname(here), "board.py").replace("\\", "/")
    return f'python "{script}"' if " " in script else f"python {script}"


def ago(ts: float, t: Optional[float] = None) -> str:
    d = max(0.0, (t or time.time()) - ts)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 48 * 3600:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def until(ts: float, t: Optional[float] = None) -> str:
    d = max(0.0, ts - (t or time.time()))
    if d < 3600:
        return f"{max(1, int(d // 60))}m"
    return f"{int(d // 3600)}h{int(d % 3600 // 60):02d}m"


def clock(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M") if dt.date() == datetime.now().date() else dt.strftime("%b %d %H:%M")


def snippet(text: Optional[str], width: int = 90) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= width else one[: width - 3].rstrip() + "..."


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def label(node: Optional[Row], width: int = 60) -> str:
    if node is None:
        return "/"
    if node["title"]:
        return node["title"]
    return '"' + snippet(node["body"], width) + '"'


class Names:
    """Caches agent names for one rendering pass."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self._cache: Dict[int, Tuple[str, str]] = {}

    def __call__(self, agent_id: Optional[int]) -> str:
        if agent_id is None:
            return "(board)"
        if agent_id not in self._cache:
            a = model.get_agent(self.con, agent_id)
            self._cache[agent_id] = (a["name"], a["kind"]) if a else ("unknown", "")
        name, kind = self._cache[agent_id]
        return f"@{name}" + (" [human]" if kind == "human" else "")


def path_text(con: sqlite3.Connection, node: Optional[Row], width: int = 30) -> str:
    """Titles from the top level down to node, e.g. 'message-board > Auth plan'."""
    if node is None:
        return "/"
    chain = [model.get_node(con, i) for i in model.ancestor_ids(node["lineage"])]
    return " > ".join(label(n, width) for n in chain + [node])


# ---------------------------------------------------------------- tree


class Snapshot:
    """The slice of the tree being shown: the nodes below `root` (None = the top level), at most
    `levels` levels deep (None = all of them). Sizes beyond the slice are counted in the database."""

    def __init__(
        self,
        con: sqlite3.Connection,
        root: Optional[Row] = None,
        levels: Optional[int] = None,
        include_archived: bool = False,
    ):
        self.con = con
        self.levels = levels
        self.include_archived = include_archived
        query, params = "SELECT * FROM nodes WHERE 1", []
        base = -1
        if root is not None:
            where, extra = model.subtree_sql(root["lineage"])
            query += " AND " + where
            params += extra
            base = root["depth"]
        if levels is not None:
            query += " AND depth <= ?"
            params.append(base + levels)
        if not include_archived:
            query += " AND hidden = 0"
        rows = con.execute(query + " ORDER BY created_at, id", params).fetchall()
        self.nodes: Dict[int, Row] = {r["id"]: r for r in rows}
        self.kids: Dict[Optional[int], List[int]] = {}
        for r in rows:
            self.kids.setdefault(r["parent_id"], []).append(r["id"])
        self._size: Dict[int, int] = {}

    def size(self, node_id: int) -> int:
        """Number of (visible) nodes below node_id."""
        if node_id not in self._size:
            if self.levels is None:
                self._size[node_id] = sum(1 + self.size(k) for k in self.kids.get(node_id, []))
            else:
                self._size[node_id] = model.count_below(self.con, self.nodes[node_id], self.include_archived)
        return self._size[node_id]


def tree(
    con: sqlite3.Connection,
    root: Optional[Row],
    depth: int,
    include_archived: bool = False,
    home_id: Optional[int] = None,
    child_limit: Optional[int] = TREE_CHILD_LIMIT,
) -> List[str]:
    snap = Snapshot(con, root, depth, include_archived)
    names = Names(con)
    t = time.time()
    lines: List[str] = []
    if root is None:
        lines.append("/ (top level)")
    else:
        lines.append(f"#{root['id']} {label(root)}  ({path_text(con, root)})")

    def describe(n: Row) -> str:
        meta = []
        if n["kind"] != "message":
            meta.append(n["kind"])
        if n["status"]:
            meta.append(n["status"])
        if n["author_id"] is not None:
            meta.append(names(n["author_id"]))
        size = snap.size(n["id"])
        if size:
            meta.append(plural(size, "post"))
        meta.append(ago(n["activity_at"], t))
        if n["archived"]:
            meta.append("archived")
        if n["id"] == home_id:
            meta.append("your space")
        return f"#{n['id']} {label(n)}  ({', '.join(meta)})"

    def walk(parent_id: Optional[int], level: int) -> None:
        ids = snap.kids.get(parent_id, [])
        if parent_id is None:
            # Top level: the conventions first, then the most recently active.
            ids = sorted(ids, key=lambda i: (snap.nodes[i]["key"] != "conventions", -snap.nodes[i]["activity_at"]))
        hidden = 0
        if child_limit is not None and len(ids) > child_limit:
            keep = set(sorted(ids, key=lambda i: -snap.nodes[i]["activity_at"])[:child_limit])
            hidden = len(ids) - len(keep)
            ids = [i for i in ids if i in keep]
        for i in ids:
            lines.append("  " * level + describe(snap.nodes[i]))
            if level < depth:
                walk(i, level + 1)
        if hidden:
            where = f"board tree #{parent_id}" if parent_id is not None else "board tree"
            lines.append("  " * level + f"... {hidden} less recently active not shown ({where} --all)")

    walk(root["id"] if root is not None else None, 1)
    if len(lines) == 1:
        lines.append("  (empty)")
    return lines


# ---------------------------------------------------------------- read


def read(
    con: sqlite3.Connection, node: Row, depth: int = 4, show_all: bool = False
) -> Tuple[List[str], Set[int]]:
    """A node and the conversation below it. Returns (lines, ids shown)."""
    snap = Snapshot(con, node, None if show_all else depth, include_archived=True)
    names = Names(con)
    t = time.time()
    shown: Set[int] = {node["id"]}

    header = f"#{node['id']} "
    if node["title"]:
        header += f"{node['title']} "
    meta = [node["kind"]]
    if node["status"]:
        meta.append(node["status"])
    if node["author_id"] is not None:
        meta.append(names(node["author_id"]))
    meta.append(f"{clock(node['created_at'])} ({ago(node['created_at'], t)})")
    if node["edited_by"] is not None:
        meta.append(f"edited by {names(node['edited_by'])} {ago(node['updated_at'], t)}")
    if node["archived"]:
        meta.append("archived")
    lines = [header + "[" + ", ".join(meta) + "]"]
    parent = model.get_node(con, node["parent_id"]) if node["parent_id"] else None
    lines.append(f"in: {path_text(con, parent)}")
    if node["body"]:
        lines.extend(node["body"].splitlines())

    def walk(parent_id: int, level: int) -> None:
        ids = snap.kids.get(parent_id, [])
        if not show_all and len(ids) > READ_CHILD_LIMIT:
            skipped = len(ids) - READ_CHILD_LIMIT
            lines.append("  " * level + f"({skipped} earlier not shown: board read #{parent_id} --all)")
            ids = ids[-READ_CHILD_LIMIT:]
        for i in ids:
            n = snap.nodes[i]
            pad = "  " * level
            head = f"{pad}#{n['id']} {names(n['author_id'])} {clock(n['created_at'])} ({ago(n['created_at'], t)})"
            extra = [] if n["kind"] == "message" else [n["kind"]]
            if n["status"]:
                extra.append(n["status"])
            if n["archived"]:
                extra.append("archived")
            if extra:
                head += " [" + ", ".join(extra) + "]"
            if n["title"]:
                head += f" {n['title']}"
            lines.append(head)
            shown.add(i)
            for body_line in (n["body"] or "").splitlines():
                lines.append(pad + "  " + body_line)
            below = snap.size(i)
            if below and (level >= depth and not show_all):
                lines.append(pad + f"  (+{plural(below, 'more post')} below: board read #{i})")
            elif below:
                walk(i, level + 1)

    walk(node["id"], 1)
    return lines, shown


# ---------------------------------------------------------------- feeds


def log_line(con: sqlite3.Connection, row: Row, names: Optional[Names] = None) -> str:
    names = names or Names(con)
    parent = model.get_node(con, row["parent_id"]) if row["parent_id"] else None
    text = f"{clock(row['created_at'])} #{row['id']} {names(row['author_id'])} in {path_text(con, parent, 24)}: "
    if row["title"]:
        text += f"[{row['kind']}] {row['title']}"
        if row["body"]:
            text += " - " + snippet(row["body"], 80)
    else:
        text += snippet(row["body"], 110)
    return text


REASONS = {
    "mention": "mentioned you",
    "reply": "replied to you",
    "subscribed": "posted",
}


def notification_line(con: sqlite3.Connection, n: Row) -> str:
    if n["node_id"] is None:
        return f"- {n['text']} ({ago(n['created_at'])})"
    node = model.get_node(con, n["node_id"])
    if node is None:
        return f"- #{n['node_id']} (deleted)"
    if n["text"]:
        return f"- #{node['id']} {n['text']}: {label(node, 70)}"
    parent = model.get_node(con, node["parent_id"]) if node["parent_id"] else None
    who = "@" + (n["author_name"] or "unknown")
    what = REASONS.get(n["reason"], n["reason"])
    text = f"- #{node['id']} {who} {what} in {path_text(con, parent, 24)}: "
    if node["title"]:
        text += f"[{node['kind']}] {node['title']}" + (" - " + snippet(node["body"], 70) if node["body"] else "")
    else:
        text += snippet(node["body"], 100)
    return text


def delivery(con: sqlite3.Connection, me: Row, command: str = "board") -> Optional[str]:
    """New notifications for an agent, marked as delivered. None when there's nothing new."""
    rows = model.undelivered(con, me["id"])
    if not rows:
        return None
    limit = max(1, int(db.config_number(con, "notify_max")))
    model.mark_delivered(con, [r["id"] for r in rows])
    lines = [f"[message board] {plural(len(rows), 'new notification')} for @{me['name']}:"]
    lines += [notification_line(con, r) for r in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"- ...and {len(rows) - limit} more: `{command} inbox`")
    if any(r["node_id"] is not None for r in rows):
        lines.append(f"Read with `{command} read <id>`; answer on the board if it concerns you.")
    return "\n".join(lines)


CHANGES_MAX = 6


def changes(con: sqlite3.Connection, me: Row, command: str = "board") -> Optional[str]:
    """What changed around the agent (statuses, claims, joins, pauses...) since it last looked.

    Moves the agent's cursor, so each change is reported once. None when nothing relevant changed."""
    rows, newest, more = model.unseen_changes(con, me)
    if newest > (me["seen_event_id"] or 0):
        model.mark_events_seen(con, me["id"], newest)
    if not rows:
        return None
    latest_status = {e["agent_id"]: e["id"] for e in rows if e["kind"] == "status"}
    rows = [e for e in rows if e["kind"] != "status" or latest_status[e["agent_id"]] == e["id"]]
    lines = ["[message board] Changes since you last looked:"]
    skipped = len(rows) - CHANGES_MAX
    if more or skipped > 0:
        lines.append(f"- (more changed than fits here: `{command} who` shows the current state)")
    lines += [f"- {e['text']}" for e in rows[-CHANGES_MAX:]]
    return "\n".join(lines)


# ---------------------------------------------------------------- agents and claims


def claim_text(c: Row, root: Optional[str] = None, t: Optional[float] = None) -> str:
    text = relative_to(c["resource"], root) if c["is_path"] else c["resource"]
    if c["soft"]:
        return f"{text} (edited {ago(c['expires_at'] - c['ttl'], t)})"
    if c["note"]:
        text += f' "{c["note"]}"'
    return text + f" (expires in {until(c['expires_at'], t)} unless renewed)"


def agent_line(
    con: sqlite3.Connection, a: Row, me: Optional[Row], t: float, active_s: float, gone_s: float
) -> str:
    state = model.activity(a, active_s, gone_s, t)
    home = model.get_node(con, a["home_id"]) if a["home_id"] else None
    you = me is not None and a["id"] == me["id"]
    parts = [f"@{a['name']}" + (" (you)" if you else "") + (" [human]" if a["kind"] == "human" else "")]
    if home is not None:
        place = home["title"] or f"#{home['id']}"
        if a["worktree"] and a["repo"] and a["worktree"] != a["repo"]:
            place += f" (worktree {a['worktree']})"
        parts.append(place)
    if state == "active" and t - a["last_seen"] < 60:
        parts.append("active now")
    else:
        parts.append(f"{state}, last seen {ago(a['last_seen'], t)}")
    if a["status"]:
        parts.append(f'"{a["status"]}"')
    return " | ".join(parts)


def who(con: sqlite3.Connection, me: Optional[Row], include_gone: bool = False) -> List[str]:
    active_s, gone_s = model.thresholds(con)
    t = time.time()
    agents = model.list_agents(con, include_gone)
    if not agents:
        return ["Nobody has used the board yet."]
    claims = model.active_claims(con, soft=None)
    lines: List[str] = []
    for a in agents:
        lines.append(agent_line(con, a, me, t, active_s, gone_s))
        mine = [c for c in claims if c["agent_id"] == a["id"]]
        hard = [claim_text(c, a["worktree"], t) for c in mine if not c["soft"]]
        soft = [relative_to(c["resource"], a["worktree"]) for c in mine if c["soft"]]
        if hard:
            lines.append("    claims: " + "; ".join(hard))
        if soft:
            lines.append("    recently edited: " + ", ".join(soft[:8]) + (" ..." if len(soft) > 8 else ""))
    return lines


def claims_list(con: sqlite3.Connection, rows: Sequence[Row], root: Optional[str]) -> List[str]:
    if not rows:
        return ["No active claims."]
    t = time.time()
    return [f"@{c['agent_name']}: {claim_text(c, root, t)}" for c in rows]


# ---------------------------------------------------------------- coordination checks


def paused_notice(con: sqlite3.Connection, me: Row, command: str) -> str:
    by = model.get_agent(con, me["paused_by"]) if me["paused_by"] else None
    who = f"@{by['name']}" if by else "A human"
    return (
        f'[message board] {who} has PAUSED you: "{me["pause_reason"]}". Don\'t change anything until you '
        "are resumed. Edits, shell commands, subagents and MCP tools are refused, except "
        f"`{command}` commands (e.g. `{command} inbox`, `{command} reply <id> \"...\"`). "
        "Tell your user that you're paused and why, and wait."
    )


def _owner_state(c: Row, t: float) -> str:
    text = f"@{c['agent_name']} was last active {ago(c['agent_last_seen'], t)}"
    if c["agent_status"]:
        text += f' (status: "{c["agent_status"]}")'
    return text


def claim_block(c: Row, shown: str, resource: str, strict: bool, window_s: float, command: str) -> str:
    t = time.time()
    note = f' ("{c["note"]}")' if c["note"] else ""
    human = c["agent_kind"] == "human"
    lines = [
        f"[message board] Coordination check: {shown} is inside {'the human ' if human else ''}@{c['agent_name']}'s "
        f"claim on {resource}{note}. {_owner_state(c, t)}. They have been told you want to edit it.",
        f'Coordinate before you edit: ask them (`{command} post . "@{c["agent_name"]} ..."` or reply in their '
        f"thread), check `{command} who`, or work on something else first.",
    ]
    if human:
        lines.append(
            f"A human's claim holds: edits inside it stay blocked until @{c['agent_name']} releases it. "
            "Don't work around it (for example through shell commands); ask them or your user."
        )
    elif strict:
        lines.append(
            f"Edits inside this claim stay blocked until @{c['agent_name']} releases it or it expires "
            f"(in {until(c['expires_at'], t)} without activity). If it can't wait, ask the user."
        )
    else:
        lines.append(
            "This is not a permission error. If you've coordinated, or the edit can't wait, retry it: "
            f"retries in the next {int(window_s // 60)} minutes go through, and @{c['agent_name']} is told."
        )
    return "\n".join(lines)


def git_block(
    sub: str, why: str, worktree: str, others: Sequence[Row], claims: Sequence[Row], strict: bool, window_s: float, command: str
) -> str:
    t = time.time()
    lines = [f"[message board] Coordination check: `git {sub}` {why}. Worktree: {worktree}."]
    for c in claims:
        note = f' ("{c["note"]}")' if c["note"] else ""
        lines.append(f"@{c['agent_name']} has claimed git operations here{note}. {_owner_state(c, t)}.")
    if others:
        listed = []
        for a in others:
            item = f"@{a['name']} (last active {ago(a['last_seen'], t)}"
            item += f', "{a["status"]}")' if a["status"] else ")"
            listed.append(item)
        lines.append("Other agents active in this worktree: " + ", ".join(listed) + ".")
    lines.append(
        f'Coordinate first: announce it (`{command} post . "@all about to git {sub} because ..."`) and give them '
        "a chance to object, or use a narrower command limited to your own files (e.g. `git add <your files>`, "
        "`git stash push -- <your files>`)."
    )
    if strict:
        lines.append("This stays blocked while the claim on .git is held. If it can't wait, ask the user.")
    else:
        lines.append(
            f"If it's safe (or you've coordinated), retry the same command: retries in the next "
            f"{int(window_s // 60)} minutes go through, and the others are told."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- briefing


def briefing(con: sqlite3.Connection, me: Row, command: str = "board") -> str:
    """What an agent needs to know when it joins (or resumes, or compacts)."""
    active_s, gone_s = model.thresholds(con)
    t = time.time()
    home = model.get_node(con, me["home_id"]) if me["home_id"] else None
    lines = [
        f"## Message board: you are @{me['name']}",
        "Other AI agents (and the human) may be working on this machine right now, possibly in this "
        "same repository and worktree. The shared message board is how you coordinate with them: split "
        "the work, announce changes that affect others, ask and answer questions, share findings, and "
        "claim files before you edit them.",
        f"Run it as a shell command: `{command} <command>`. `{command} help` prints the full manual.",
    ]
    if command != "board":
        lines.append(f"(Wherever the conventions or the manual say `board`, run `{command}`.)")
    if home is not None:
        lines.append(f'Your space: #{home["id"]} "{home["title"]}" (address it as `.`). Worktree: {me["worktree"]}')

    if me["paused_at"] is not None:
        lines.append(paused_notice(con, me, command))

    everyone = [a for a in model.list_agents(con) if a["id"] != me["id"]]
    humans = [a for a in everyone if a["kind"] == "human"]
    others = [a for a in everyone if a["kind"] != "human"]
    here = [a for a in others if a["home_id"] == me["home_id"]]
    elsewhere = [a for a in others if a["home_id"] != me["home_id"]]
    if humans:
        listed = []
        for h in humans[:HUMANS_MAX]:
            state = "watching now" if model.activity(h, active_s, gone_s, t) == "active" else f"seen {ago(h['last_seen'], t)}"
            listed.append(f"@{h['name']} ({state})")
        lines.append(
            "Humans on the board: " + ", ".join(listed) + ". @mention them when you need a decision "
            "or an agent is stuck; they can also pause agents and release claims."
        )

    # The working agreements come before the live state, so that if the briefing is ever cut
    # short, it loses old activity and not the rules.
    lines += _conventions(con, home)
    lines.append("")
    if me["status"]:
        lines.append("Carry on with your task, keep your status current, and follow the conventions.")
    else:
        lines.append(
            f'First steps: when you know your task, set your status (`{command} status "..."`). '
            "Then check whether anyone below is working on something related, and claim the files "
            "you're about to change."
        )

    lines.append("")
    lines.append(
        f"--- Current state at {clock(t)}. This is a snapshot: after it, you hear about changes after your "
        f"tool calls, and `{command} who` / `{command} inbox` show the latest. ---"
    )
    claims = model.active_claims(con)
    mine = [claim_text(c, me["worktree"], t) for c in claims if c["agent_id"] == me["id"]]
    if me["status"]:
        lines.append(f'Your status: "{me["status"]}"')
    if mine:
        lines.append("Your claims: " + "; ".join(mine))
    if here:
        lines.append(f"Agents in this repository ({len(here)}):")
        for a in here[:HERE_MAX]:
            lines.append("  " + agent_line(con, a, me, t, active_s, gone_s))
            theirs = [claim_text(c, me["worktree"], t) for c in claims if c["agent_id"] == a["id"]]
            if theirs:
                extra = f"; (+{len(theirs) - CLAIMS_SHOWN} more)" if len(theirs) > CLAIMS_SHOWN else ""
                lines.append("    claims: " + "; ".join(theirs[:CLAIMS_SHOWN]) + extra)
        if len(here) > HERE_MAX:
            lines.append(f"  ...and {len(here) - HERE_MAX} more (least recently active): `{command} who`")
    else:
        lines.append("No other agents are working in this repository right now.")
    if elsewhere:
        brief = []
        for a in elsewhere[:ELSEWHERE_MAX]:
            space = model.get_node(con, a["home_id"]) if a["home_id"] else None
            brief.append(f"@{a['name']} ({space['title'] if space else '?'})")
        more = f" and {len(elsewhere) - ELSEWHERE_MAX} more" if len(elsewhere) > ELSEWHERE_MAX else ""
        lines.append("Elsewhere: " + ", ".join(brief) + more)

    pending = model.inbox(con, me["id"])
    if pending:
        lines.append(f"Unread for you ({len(pending)}{'+' if len(pending) >= 50 else ''}):")
        lines += ["  " + notification_line(con, n) for n in pending[-5:]]
        if len(pending) > 5:
            lines.append(f"  ...see `{command} inbox` for all")
        model.mark_delivered(con, [n["id"] for n in pending])

    if home is not None:
        recent = model.recent(con, within=home["id"], limit=5)
        if recent:
            names = Names(con)
            lines.append(f"Recent in {home['title']}:")
            lines += ["  " + log_line(con, r, names) for r in recent]

    model.mark_events_seen(con, me["id"])  # the briefing already shows the current state
    return "\n".join(lines)


HUMANS_MAX = 4
HERE_MAX = 10
ELSEWHERE_MAX = 8
CLAIMS_SHOWN = 3
CONVENTIONS_MAX = 3500
REPO_CONVENTIONS_MAX = 1500


def _clip(body: str, limit: int, more: str) -> List[str]:
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + f"\n[...cut here: {more} shows the rest]"
    return ["  " + line for line in body.splitlines()]


def _conventions(con: sqlite3.Connection, home: Optional[Row]) -> List[str]:
    lines: List[str] = []
    conventions = model.node_by_key(con, "conventions")
    if conventions is not None and conventions["body"]:
        lines += ["", f"Board conventions (#{conventions['id']}, shared and editable):"]
        lines += _clip(conventions["body"], CONVENTIONS_MAX, "`board conventions`")
    if home is not None:
        local = model.child_titled(con, home["id"], "conventions")
        if local is not None and local["body"]:
            lines.append(f"Conventions for {home['title']} (#{local['id']}):")
            lines += _clip(local["body"], REPO_CONVENTIONS_MAX, f"`board read {local['id']}`")
    return lines
