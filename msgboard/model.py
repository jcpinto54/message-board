"""Board operations: agents, the node tree, notifications, subscriptions and claims."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import db
from .workspace import Resource, Workspace, locate, overlaps, path_key, path_matches, relative_to

Row = sqlite3.Row


class BoardError(Exception):
    """A problem to report to the user, without a traceback."""


def now() -> float:
    return time.time()


ADJECTIVES = """
amber bold brave brisk calm clever cosmic crisp curious daring deft eager fair fleet gentle glad
grand happy hardy jolly keen kind lively lucid lunar mellow merry mighty nimble noble patient
plucky polar proud quick quiet rapid ready rustic sage serene sharp silent sleek smart snowy
solar spry steady stellar stoic sunny swift tidy vivid wise witty zesty
""".split()

ANIMALS = """
albatross badger beaver bison bobcat camel caribou cheetah condor cougar coyote crane dingo
dolphin eagle falcon ferret finch fox gazelle gecko gibbon heron ibex jackal jaguar kestrel
koala lemur leopard lynx magpie marten meerkat mink moose narwhal ocelot orca osprey otter owl
panda panther pelican penguin puffin quail raven seal shrike sparrow stork tapir tiger toucan
walrus weasel wolf wombat yak zebra
""".split()

CONTAINER_KINDS = {"group", "repo", "folder", "doc"}
BROADCAST_NAMES = {"all", "everyone", "here"}
MENTION_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9][A-Za-z0-9_-]*)")
_ID_RE = re.compile(r"^#?(\d+)$")


def thresholds(con: sqlite3.Connection) -> Tuple[float, float]:
    """(active, gone) windows in seconds."""
    return (
        db.config_number(con, "active_minutes") * 60,
        db.config_number(con, "gone_hours") * 3600,
    )


def _in(ids: Sequence[int]) -> str:
    return ",".join("?" * len(ids))


# ---------------------------------------------------------------- agents


def get_agent(con: sqlite3.Connection, agent_id: int) -> Optional[Row]:
    return con.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()


def agent_by_name(con: sqlite3.Connection, name: str) -> Optional[Row]:
    return con.execute("SELECT * FROM agents WHERE name = ?", (name.lstrip("@"),)).fetchone()


def agent_by_session(con: sqlite3.Connection, session_id: str) -> Optional[Row]:
    return con.execute("SELECT * FROM agents WHERE session_id = ?", (session_id,)).fetchone()


def clean_name(raw: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-_")[:32]


def _generated_name(con: sqlite3.Connection) -> str:
    import random

    rng = random.SystemRandom()
    for _ in range(200):
        name = f"{rng.choice(ADJECTIVES)}-{rng.choice(ANIMALS)}"
        if agent_by_name(con, name) is None:
            return name
    while True:
        name = f"agent-{rng.randrange(1_000_000):06d}"
        if agent_by_name(con, name) is None:
            return name


def _unique_name(con: sqlite3.Connection, wanted: str) -> str:
    base = clean_name(wanted) or "human"
    name, i = base, 2
    while agent_by_name(con, name) is not None or name in BROADCAST_NAMES:
        name, i = f"{base}-{i}", i + 1
    return name


def ensure_agent(
    con: sqlite3.Connection,
    session_id: str,
    cwd: str,
    *,
    kind: str = "claude",
    name: Optional[str] = None,
    follow_cwd: bool = False,
    space: bool = True,
) -> Row:
    """The participant for a session, created on first contact. Every call counts as activity.

    With space=False (a human in the GUI), the working directory doesn't get a space."""
    t = now()
    with db.tx(con):
        row = agent_by_session(con, session_id)
        if row is None:
            ws = locate(cwd)
            if space:
                home_id: Optional[int] = ensure_space(con, ws)
            else:
                existing = node_by_key(con, ws.key)
                home_id = existing["id"] if existing is not None else None
            agent_name = _unique_name(con, name) if name else _generated_name(con)
            cur = con.execute(
                "INSERT INTO agents(name, kind, session_id, cwd, worktree, worktree_key, repo,"
                " home_id, started_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_name, kind, session_id, cwd, ws.root, path_key(ws.root), ws.repo, home_id, t, t),
            )
            agent_id = cur.lastrowid
            if space:
                subscribe(con, agent_id, home_id, deep=False)
            where = f" in {get_node(con, home_id)['title']}" if home_id is not None else ""
            event(con, "join", f"@{agent_name} joined{where}", agent_id=agent_id)
            # A newcomer gets the current state in its briefing, not the whole history.
            con.execute(
                "UPDATE agents SET seen_event_id = (SELECT MAX(id) FROM events) WHERE id = ?", (agent_id,)
            )
        else:
            agent_id = row["id"]
            if follow_cwd and (row["cwd"] is None or path_key(cwd) != path_key(row["cwd"])):
                ws = locate(cwd)
                home_id = ensure_space(con, ws)
                con.execute(
                    "UPDATE agents SET worktree = ?, worktree_key = ?, repo = ?, home_id = ?"
                    " WHERE id = ?",
                    (ws.root, path_key(ws.root), ws.repo, home_id, agent_id),
                )
                subscribe(con, agent_id, home_id, deep=False)
            if row["ended_at"] is not None:
                event(con, "join", f"@{row['name']} is back", agent_id=agent_id)
            con.execute(
                "UPDATE agents SET last_seen = ?, ended_at = NULL, cwd = ? WHERE id = ?",
                (t, cwd, agent_id),
            )
            _renew_claims(con, agent_id, t)
    return get_agent(con, agent_id)


def _renew_claims(con: sqlite3.Connection, agent_id: int, t: float) -> None:
    """Keep an active agent's claims alive; tell it about the ones that lapsed while it was away."""
    lapsed = con.execute(
        "SELECT resource FROM claims WHERE agent_id = ? AND soft = 0 AND expires_at <= ?",
        (agent_id, t),
    ).fetchall()
    if lapsed:
        con.execute(
            "DELETE FROM claims WHERE agent_id = ? AND soft = 0 AND expires_at <= ?", (agent_id, t)
        )
        names = ", ".join(r["resource"] for r in lapsed)
        notify(
            con,
            agent_id,
            "claim",
            text=f"Your claim on {names} expired while you were inactive. Claim it again if you still need it.",
        )
        agent = get_agent(con, agent_id)
        shown = ", ".join(relative_to(r["resource"], agent["worktree"]) for r in lapsed)
        event(con, "expire", f"@{agent['name']}'s claim on {shown} expired while it was inactive", agent_id=agent_id)
    con.execute(
        "UPDATE claims SET expires_at = ? + ttl WHERE agent_id = ? AND soft = 0", (t, agent_id)
    )


def end_session(con: sqlite3.Connection, agent: Row, by: Optional[Row] = None) -> None:
    """Mark a participant as gone and release its claims (by: a human removing it)."""
    with db.tx(con):
        con.execute(
            "UPDATE agents SET ended_at = ?, paused_at = NULL WHERE id = ?", (now(), agent["id"])
        )
        released = con.execute(
            "SELECT * FROM claims WHERE agent_id = ? AND soft = 0", (agent["id"],)
        ).fetchall()
        con.execute("DELETE FROM claims WHERE agent_id = ?", (agent["id"],))
        con.execute("DELETE FROM gates WHERE agent_id = ?", (agent["id"],))
        _tell_waiters(con, agent["name"], released, "ended their session, releasing")
        what = f"@{by['name']} removed @{agent['name']} from the board" if by else f"@{agent['name']} ended its session"
        if released:
            what += " (released " + ", ".join(relative_to(c["resource"], agent["worktree"]) for c in released) + ")"
        event(con, "end", what, agent_id=agent["id"], other_id=by["id"] if by else None)


def activity(row: Row, active_s: float, gone_s: float, t: Optional[float] = None) -> str:
    """'active', 'idle' or 'gone'."""
    if row["ended_at"] is not None:
        return "gone"
    idle = (t or now()) - row["last_seen"]
    if idle <= active_s:
        return "active"
    return "idle" if idle <= gone_s else "gone"


def list_agents(con: sqlite3.Connection, include_gone: bool = False, seen_within: Optional[float] = None) -> List[Row]:
    """Participants, most recently seen first. Without include_gone, only active and idle ones."""
    active_s, gone_s = thresholds(con)
    if include_gone:
        since = now() - seen_within if seen_within else 0.0
        return con.execute(
            "SELECT * FROM agents WHERE last_seen >= ? ORDER BY last_seen DESC", (since,)
        ).fetchall()
    return con.execute(
        "SELECT * FROM agents WHERE ended_at IS NULL AND last_seen >= ? ORDER BY last_seen DESC",
        (now() - gone_s,),
    ).fetchall()


def active_in_worktree(con: sqlite3.Connection, worktree_key: str, exclude_id: int) -> List[Row]:
    """Agents (not humans: the hooks don't see their edits) active in a worktree."""
    active_s, gone_s = thresholds(con)
    rows = con.execute(
        "SELECT * FROM agents WHERE worktree_key = ? AND id != ? AND kind != 'human'"
        " ORDER BY last_seen DESC",
        (worktree_key, exclude_id),
    ).fetchall()
    return [r for r in rows if activity(r, active_s, gone_s) == "active"]


def mark_briefed(con: sqlite3.Connection, agent: Row) -> None:
    with db.tx(con):
        con.execute("UPDATE agents SET briefed_at = ? WHERE id = ?", (now(), agent["id"]))


def set_status(con: sqlite3.Connection, agent: Row, text: Optional[str]) -> None:
    with db.tx(con):
        con.execute(
            "UPDATE agents SET status = ?, status_at = ? WHERE id = ?",
            (text or None, now(), agent["id"]),
        )
        what = f'@{agent["name"]}: "{text}"' if text else f"@{agent['name']} cleared its status"
        event(con, "status", what, agent_id=agent["id"])


def rename(con: sqlite3.Connection, agent: Row, new_name: str) -> str:
    name = clean_name(new_name)
    if len(name) < 2 or name in BROADCAST_NAMES:
        raise BoardError("Names need 2-32 characters from a-z, 0-9, '-' and '_' (and can't be all/everyone/here).")
    with db.tx(con):
        other = agent_by_name(con, name)
        if other is not None and other["id"] != agent["id"]:
            raise BoardError(f"@{name} is already taken.")
        con.execute("UPDATE agents SET name = ? WHERE id = ?", (name, agent["id"]))
        event(con, "status", f"@{agent['name']} is now @{name}", agent_id=agent["id"])
    return name


def pause(con: sqlite3.Connection, by: Row, agent: Row, reason: str) -> None:
    """Stop an agent: its edits, shell commands, subagents and MCP tools are refused until resumed."""
    if by["kind"] != "human":
        raise BoardError("Only humans can pause agents.")
    if agent["kind"] == "human":
        raise BoardError("Humans can't be paused.")
    reason = (reason or "").strip() or "no reason given"
    with db.tx(con):
        con.execute(
            "UPDATE agents SET paused_at = ?, pause_reason = ?, paused_by = ? WHERE id = ?",
            (now(), reason, by["id"], agent["id"]),
        )
        event(
            con, "pause", f'@{by["name"]} paused @{agent["name"]}: "{reason}"', agent_id=agent["id"], other_id=by["id"]
        )


def resume(con: sqlite3.Connection, by: Row, agent: Row) -> None:
    if by["kind"] != "human":
        raise BoardError("Only humans can resume agents.")
    with db.tx(con):
        con.execute(
            "UPDATE agents SET paused_at = NULL, pause_reason = NULL, paused_by = NULL WHERE id = ?",
            (agent["id"],),
        )
        notify(con, agent["id"], "resume", text=f"@{by['name']} resumed you. You can carry on.")
        event(con, "resume", f"@{by['name']} resumed @{agent['name']}", agent_id=agent["id"], other_id=by["id"])


def subscribe(con: sqlite3.Connection, agent_id: int, node_id: Optional[int], deep: bool = True) -> None:
    if node_id is None:
        return
    con.execute(
        "INSERT INTO subscriptions(agent_id, node_id, deep) VALUES (?, ?, ?)"
        " ON CONFLICT(agent_id, node_id) DO UPDATE SET deep = MAX(deep, excluded.deep)",
        (agent_id, node_id, int(deep)),
    )


def unsubscribe(con: sqlite3.Connection, agent_id: int, node_id: int) -> bool:
    with db.tx(con):
        cur = con.execute(
            "DELETE FROM subscriptions WHERE agent_id = ? AND node_id = ?", (agent_id, node_id)
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------- nodes


def get_node(con: sqlite3.Connection, node_id: int) -> Optional[Row]:
    return con.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()


def node_by_key(con: sqlite3.Connection, key: str) -> Optional[Row]:
    return con.execute("SELECT * FROM nodes WHERE key = ?", (key,)).fetchone()


# Each node's lineage is the path of ids from the top ("/2/7/12/"), so a subtree is the range
# [lineage, upper(lineage)) on an indexed column instead of a recursive walk.


def upper(lineage: str) -> str:
    """Exclusive upper bound of the lineages that start with `lineage` ('/' < '0')."""
    return lineage[:-1] + "0"


def subtree_sql(lineage: str, column: str = "lineage", include_self: bool = False) -> Tuple[str, Tuple[str, str]]:
    """WHERE fragment (and parameters) selecting the nodes below a lineage."""
    low = ">=" if include_self else ">"
    return f"{column} {low} ? AND {column} < ?", (lineage, upper(lineage))


def child_titled(con: sqlite3.Connection, parent_id: int, title: str) -> Optional[Row]:
    return con.execute(
        "SELECT * FROM nodes WHERE parent_id = ? AND lower(title) = lower(?) AND archived = 0"
        " ORDER BY created_at LIMIT 1",
        (parent_id, title),
    ).fetchone()


def ancestor_ids(lineage: str) -> List[int]:
    """Ids above a node, top level first."""
    return [int(x) for x in lineage.strip("/").split("/")[:-1]]


def ancestors(con: sqlite3.Connection, node_id: int) -> List[Tuple[int, int]]:
    """(id, distance) of every ancestor, parent first (distance 1)."""
    node = get_node(con, node_id)
    if node is None or not node["lineage"]:
        return []
    ids = ancestor_ids(node["lineage"])
    return [(i, len(ids) - pos) for pos, i in enumerate(ids)][::-1]


def descendants(con: sqlite3.Connection, node_id: int) -> List[Tuple[int, int]]:
    """(id, distance) of every node below node_id."""
    node = get_node(con, node_id)
    if node is None:
        return []
    where, params = subtree_sql(node["lineage"])
    rows = con.execute(f"SELECT id, depth FROM nodes WHERE {where}", params).fetchall()
    return [(r[0], r[1] - node["depth"]) for r in rows]


def count_below(con: sqlite3.Connection, node: Row, include_hidden: bool = False) -> int:
    where, params = subtree_sql(node["lineage"])
    if not include_hidden:
        where += " AND hidden = 0"
    return con.execute(f"SELECT COUNT(*) FROM nodes WHERE {where}", params).fetchone()[0]


def _next_rev(con: sqlite3.Connection) -> int:
    """A counter that grows with every change to a node, so readers can ask "what changed since N"."""
    return con.execute("SELECT COALESCE(MAX(rev), 0) + 1 FROM nodes").fetchone()[0]


def _bump(con: sqlite3.Connection, node_id: int, t: float) -> None:
    """Mark the ancestors of node_id as active now."""
    node = get_node(con, node_id)
    ids = ancestor_ids(node["lineage"]) if node is not None and node["lineage"] else []
    if ids:
        con.execute(
            f"UPDATE nodes SET activity_at = ?, rev = ? WHERE id IN ({_in(ids)})", (t, _next_rev(con), *ids)
        )


def _refresh_hidden(con: sqlite3.Connection, node: Row) -> None:
    """Recompute `hidden` (archived, or below something archived) for a node and its subtree."""
    above = ancestor_ids(node["lineage"])
    hidden = bool(above) and con.execute(
        f"SELECT 1 FROM nodes WHERE archived = 1 AND id IN ({_in(above)}) LIMIT 1", above
    ).fetchone() is not None
    where, params = subtree_sql(node["lineage"], include_self=True)
    con.execute(f"UPDATE nodes SET hidden = ? WHERE {where}", (int(hidden), *params))
    if not hidden:
        for (lineage,) in con.execute(
            f"SELECT lineage FROM nodes WHERE archived = 1 AND {where}", params
        ).fetchall():
            inner, inner_params = subtree_sql(lineage, include_self=True)
            con.execute(f"UPDATE nodes SET hidden = 1 WHERE {inner}", inner_params)


def create_node(
    con: sqlite3.Connection,
    parent_id: Optional[int],
    author_id: Optional[int],
    *,
    kind: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    key: Optional[str] = None,
) -> int:
    t = now()
    with db.tx(con):
        parent = get_node(con, parent_id) if parent_id is not None else None
        cur = con.execute(
            "INSERT INTO nodes(parent_id, key, kind, title, body, author_id, created_at, updated_at,"
            " activity_at, depth, hidden, rev) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                parent_id, key, kind, title, body, author_id, t, t, t,
                parent["depth"] + 1 if parent is not None else 0,
                parent["hidden"] if parent is not None else 0,
                _next_rev(con),
            ),
        )
        node_id = cur.lastrowid
        lineage = (parent["lineage"] if parent is not None else "/") + f"{node_id}/"
        con.execute("UPDATE nodes SET lineage = ? WHERE id = ?", (lineage, node_id))
        _bump(con, node_id, t)
    return node_id


def ensure_space(con: sqlite3.Connection, ws: Workspace) -> int:
    """The top-level node for a repository (or plain folder), created on first use."""
    row = node_by_key(con, ws.key)
    if row is not None:
        if row["path"] is None:
            con.execute("UPDATE nodes SET path = ? WHERE id = ?", (ws.repo, row["id"]))
        if row["archived"]:
            set_archived(con, row, False)
        return row["id"]
    taken = {(r[0] or "").lower() for r in con.execute("SELECT title FROM nodes WHERE parent_id IS NULL")}
    title = ws.name
    if title.lower() in taken:
        parent = os.path.basename(os.path.dirname(ws.repo.rstrip("/\\")))
        title = f"{title} ({parent})" if parent else title
    base, i = title, 2
    while title.lower() in taken:
        title, i = f"{base} {i}", i + 1
    what = "Repository" if ws.is_git else "Folder"
    body = f"{what} {ws.repo}. Agents working here coordinate in this space."
    with db.tx(con):
        node_id = create_node(
            con, None, None, kind="repo" if ws.is_git else "folder", title=title, body=body, key=ws.key
        )
        con.execute("UPDATE nodes SET path = ? WHERE id = ?", (ws.repo, node_id))
    return node_id


def clean_kind(kind: str) -> str:
    k = re.sub(r"[^a-z0-9_-]+", "-", kind.strip().lower()).strip("-")
    if not k:
        raise BoardError("The kind can't be empty.")
    return k[:24]


def mentioned_names(*texts: Optional[str]) -> List[str]:
    names: List[str] = []
    for text in texts:
        for raw in MENTION_RE.findall(text or ""):
            name = raw.rstrip("-_").lower()
            if name and name not in names:
                names.append(name)
    return names


def post(
    con: sqlite3.Connection,
    author: Row,
    parent: Optional[Row],
    *,
    body: Optional[str] = None,
    title: Optional[str] = None,
    kind: Optional[str] = None,
) -> Tuple[int, List[str], List[str]]:
    """Create a node under parent (None = top level). Returns (id, notified names, unknown @names)."""
    body = (body or "").strip() or None
    title = (title or "").strip() or None
    if not body and not title:
        raise BoardError("Nothing to post: give a message and/or --title.")
    kind = clean_kind(kind) if kind else ("message" if not title else ("thread" if body else "group"))
    parent_id = parent["id"] if parent is not None else None
    with db.tx(con):
        node_id = create_node(con, parent_id, author["id"], kind=kind, title=title, body=body)
        subscribe(con, author["id"], node_id, deep=True)
        if parent is not None and parent["key"] is None and parent["kind"] not in CONTAINER_KINDS:
            subscribe(con, author["id"], parent_id, deep=True)  # hear follow-ups in this conversation
        notified, unknown = _fan_out(con, node_id)
    return node_id, notified, unknown


def _fan_out(con: sqlite3.Connection, node_id: int) -> Tuple[List[str], List[str]]:
    node = get_node(con, node_id)
    author = node["author_id"]
    t = now()
    _, gone_s = thresholds(con)

    def alive(a: Row) -> bool:
        return a["ended_at"] is None and t - a["last_seen"] <= gone_s

    reasons: Dict[int, str] = {}
    unknown: List[str] = []
    for name in mentioned_names(node["title"], node["body"]):
        if name in BROADCAST_NAMES:
            for a in con.execute("SELECT * FROM agents"):
                if a["id"] != author and alive(a):
                    reasons.setdefault(a["id"], "mention")
            continue
        a = agent_by_name(con, name)
        if a is None:
            unknown.append(name)
        elif a["id"] != author:
            reasons[a["id"]] = "mention"
    if node["parent_id"] is not None:
        parent_author = get_node(con, node["parent_id"])["author_id"]
        if parent_author is not None and parent_author != author:
            a = get_agent(con, parent_author)
            if a is not None and alive(a):
                reasons.setdefault(parent_author, "reply")
    for ancestor_id, depth in ancestors(con, node_id):
        subs = con.execute(
            "SELECT s.agent_id, s.deep, a.ended_at, a.last_seen FROM subscriptions s"
            " JOIN agents a ON a.id = s.agent_id WHERE s.node_id = ?",
            (ancestor_id,),
        ).fetchall()
        for s in subs:
            if s["agent_id"] == author or s["agent_id"] in reasons:
                continue
            # A shallow subscription hears direct posts and new titled threads/groups, not every reply.
            if not (s["deep"] or depth == 1 or node["title"]):
                continue
            if alive(s):
                reasons[s["agent_id"]] = "subscribed"
    for agent_id, reason in reasons.items():
        con.execute(
            "INSERT INTO notifications(agent_id, node_id, reason, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, node_id, reason, t),
        )
    names = [get_agent(con, a)["name"] for a in reasons]
    return names, unknown


_UNSET = object()


def edit_node(
    con: sqlite3.Connection,
    editor: Row,
    node: Row,
    *,
    title: object = _UNSET,
    body: object = _UNSET,
    kind: object = _UNSET,
    status: object = _UNSET,
) -> None:
    sets: List[str] = []
    values: List[object] = []
    for column, value in (("title", title), ("body", body), ("kind", kind), ("status", status)):
        if value is _UNSET:
            continue
        if column == "kind":
            value = clean_kind(str(value))
        else:
            value = (str(value).strip() or None) if value is not None else None
        sets.append(f"{column} = ?")
        values.append(value)
    if not sets:
        raise BoardError("Nothing to change: use --title, --body/--file, --kind or --status.")
    t = now()
    with db.tx(con):
        con.execute(
            f"UPDATE nodes SET {', '.join(sets)}, updated_at = ?, activity_at = ?, edited_by = ?, rev = ?"
            " WHERE id = ?",
            (*values, t, t, editor["id"], _next_rev(con), node["id"]),
        )
        _bump(con, node["id"], t)
        if node["key"] == "conventions":
            _, gone_s = thresholds(con)
            for a in con.execute("SELECT * FROM agents WHERE id != ?", (editor["id"],)).fetchall():
                if a["ended_at"] is None and t - a["last_seen"] <= gone_s:
                    notify(
                        con,
                        a["id"],
                        "conventions",
                        node_id=node["id"],
                        text=f"@{editor['name']} updated the board conventions",
                    )
        elif node["author_id"] is not None and node["author_id"] != editor["id"]:
            notify(
                con,
                node["author_id"],
                "edited",
                node_id=node["id"],
                text=f"@{editor['name']} edited your post",
            )
        changed = ", ".join(s.split(" = ")[0] for s in sets)
        event(con, "edit", f"@{editor['name']} edited the {changed} of #{node['id']}", agent_id=editor["id"], node_id=node["id"])


def move_node(con: sqlite3.Connection, node: Row, new_parent: Optional[Row], by: Optional[Row] = None) -> None:
    new_parent_id = new_parent["id"] if new_parent is not None else None
    if new_parent is not None and new_parent["lineage"].startswith(node["lineage"]):
        raise BoardError("Can't move a node inside itself.")
    t = now()
    with db.tx(con):
        node = get_node(con, node["id"])
        old = node["lineage"]
        new = (new_parent["lineage"] if new_parent is not None else "/") + f"{node['id']}/"
        shift = (new_parent["depth"] + 1 if new_parent is not None else 0) - node["depth"]
        rev = _next_rev(con)
        where, params = subtree_sql(old, include_self=True)
        con.execute(
            f"UPDATE nodes SET lineage = ? || substr(lineage, ?), depth = depth + ?, rev = ? WHERE {where}",
            (new, len(old) + 1, shift, rev, *params),
        )
        con.execute(
            "UPDATE nodes SET parent_id = ?, activity_at = ? WHERE id = ?", (new_parent_id, t, node["id"])
        )
        _refresh_hidden(con, get_node(con, node["id"]))
        _bump(con, node["id"], t)
        if by is not None:
            where = f"#{new_parent_id}" if new_parent_id is not None else "the top level"
            event(con, "edit", f"@{by['name']} moved #{node['id']} under {where}", agent_id=by["id"], node_id=node["id"])


def set_archived(con: sqlite3.Connection, node: Row, archived: bool, by: Optional[Row] = None) -> None:
    with db.tx(con):
        con.execute(
            "UPDATE nodes SET archived = ?, rev = ? WHERE id = ?", (int(archived), _next_rev(con), node["id"])
        )
        _refresh_hidden(con, get_node(con, node["id"]))
        if by is not None:
            what = "archived" if archived else "restored"
            event(con, "edit", f"@{by['name']} {what} #{node['id']}", agent_id=by["id"], node_id=node["id"])


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def resolve(con: sqlite3.Connection, ref: str, home_id: Optional[int]) -> Optional[Row]:
    """Find a node by #id, '.', '/', or a path of titles. None means the top level."""
    ref = ref.strip()
    if ref in ("/", "root"):
        return None
    if ref in (".", "~", "here", "home"):
        if home_id is None:
            raise BoardError("You don't have a home space yet.")
        return get_node(con, home_id)
    m = _ID_RE.match(ref)
    if m:
        row = get_node(con, int(m.group(1)))
        if row is None:
            raise BoardError(f"There is no node #{m.group(1)}.")
        return row
    if ref.startswith("/"):
        starts: List[Optional[int]] = [None]
    elif ref.startswith("./"):
        starts = [home_id]
    else:
        starts = [home_id, None] if home_id is not None else [None]
    segments = [s for s in ref.split("/") if s and s != "."]
    misses: List[str] = []
    for start in starts:
        found = _walk(con, start, segments, misses)
        if found is not False:
            return found  # type: ignore[return-value]
    raise BoardError(f"No node matches '{ref}' ({'; '.join(misses)}). `board tree` shows the structure.")


def _walk(con: sqlite3.Connection, start: Optional[int], segments: List[str], misses: List[str]):
    current = start
    for seg in segments:
        m = _ID_RE.match(seg)
        if m:
            row = get_node(con, int(m.group(1)))
            if row is None:
                misses.append(f"no #{m.group(1)}")
                return False
            current = row["id"]
            continue
        where = "parent_id IS NULL" if current is None else "parent_id = ?"
        kids = con.execute(
            f"SELECT * FROM nodes WHERE {where} AND title IS NOT NULL ORDER BY created_at, id",
            () if current is None else (current,),
        ).fetchall()
        matches = [k for k in kids if (k["title"] or "").lower() == seg.lower()]
        if not matches:
            matches = [k for k in kids if k["title"] and slug(k["title"]) == slug(seg)]
        if not matches:
            where = "the top level" if current is None else f"#{current}"
            misses.append(f"no '{seg}' under {where}")
            return False
        live = [k for k in matches if not k["archived"]] or matches
        if len(live) > 1:
            ids = ", ".join(f"#{k['id']}" for k in live)
            raise BoardError(f"'{seg}' is ambiguous ({ids}); use an id.")
        current = live[0]["id"]
    return get_node(con, current) if current is not None else None


def _within(con: sqlite3.Connection, within: Optional[int], include_self: bool) -> Tuple[str, Tuple]:
    if within is None:
        return "", ()
    node = get_node(con, within)
    if node is None:
        return " AND 0", ()
    where, params = subtree_sql(node["lineage"], "n.lineage", include_self=include_self)
    return " AND " + where, params


def search(con: sqlite3.Connection, text: str, within: Optional[int] = None, limit: int = 30) -> List[Row]:
    pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    scope, scope_params = _within(con, within, include_self=True)
    return con.execute(
        "SELECT n.*, a.name AS author_name FROM nodes n LEFT JOIN agents a ON a.id = n.author_id"
        " WHERE n.hidden = 0 AND (n.title LIKE ? ESCAPE '\\' OR n.body LIKE ? ESCAPE '\\')"
        f"{scope} ORDER BY n.created_at DESC LIMIT ?",
        (pattern, pattern, *scope_params, limit),
    ).fetchall()


def recent(
    con: sqlite3.Connection,
    *,
    within: Optional[int] = None,
    since: Optional[float] = None,
    after_id: Optional[int] = None,
    limit: int = 20,
) -> List[Row]:
    """Recent posts by participants, oldest first."""
    scope, scope_params = _within(con, within, include_self=False)
    query = (
        "SELECT n.*, a.name AS author_name, a.kind AS author_kind FROM nodes n"
        " LEFT JOIN agents a ON a.id = n.author_id WHERE n.author_id IS NOT NULL AND n.hidden = 0" + scope
    )
    params: List[object] = list(scope_params)
    if since is not None:
        query += " AND n.created_at >= ?"
        params.append(since)
    if after_id is not None:
        query += " AND n.id > ?"
        params.append(after_id)
    rows = con.execute(query + " ORDER BY n.created_at DESC, n.id DESC LIMIT ?", (*params, limit)).fetchall()
    return rows[::-1]


def nodes_changed_since(con: sqlite3.Connection, rev: int) -> List[Row]:
    """Nodes created or changed after revision `rev` (all of them for 0)."""
    return con.execute("SELECT * FROM nodes WHERE rev > ? ORDER BY created_at, id", (rev,)).fetchall()


def current_rev(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COALESCE(MAX(rev), 0) FROM nodes").fetchone()[0]


# ---------------------------------------------------------------- notifications


def notify(
    con: sqlite3.Connection,
    agent_id: int,
    reason: str,
    *,
    node_id: Optional[int] = None,
    text: Optional[str] = None,
) -> None:
    con.execute(
        "INSERT INTO notifications(agent_id, node_id, reason, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (agent_id, node_id, reason, text, now()),
    )


_NOTIFICATION_SELECT = """
SELECT n.id, n.agent_id, n.node_id, n.reason, n.text, n.created_at, n.delivered_at, n.read_at,
       nd.title, nd.body, nd.kind, nd.parent_id, a.name AS author_name
FROM notifications n
LEFT JOIN nodes nd ON nd.id = n.node_id
LEFT JOIN agents a ON a.id = nd.author_id
"""
_REASON_ORDER = (
    "CASE n.reason WHEN 'mention' THEN 0 WHEN 'reply' THEN 1 WHEN 'claim' THEN 2 ELSE 3 END"
)


def undelivered(con: sqlite3.Connection, agent_id: int) -> List[Row]:
    return con.execute(
        _NOTIFICATION_SELECT + " WHERE n.agent_id = ? AND n.delivered_at IS NULL"
        f" AND n.read_at IS NULL ORDER BY {_REASON_ORDER}, n.id",
        (agent_id,),
    ).fetchall()


def mark_delivered(con: sqlite3.Connection, notification_ids: Sequence[int]) -> None:
    if not notification_ids:
        return
    t = now()
    with db.tx(con):
        con.execute(
            f"UPDATE notifications SET delivered_at = ? WHERE id IN ({_in(notification_ids)})",
            (t, *notification_ids),
        )
        # Notices that don't point at a post are done once shown.
        con.execute(
            f"UPDATE notifications SET read_at = ? WHERE node_id IS NULL"
            f" AND id IN ({_in(notification_ids)})",
            (t, *notification_ids),
        )


def inbox(con: sqlite3.Connection, agent_id: int, include_read: bool = False, limit: int = 50) -> List[Row]:
    condition = "" if include_read else " AND n.read_at IS NULL"
    rows = con.execute(
        _NOTIFICATION_SELECT + f" WHERE n.agent_id = ?{condition} ORDER BY n.id DESC LIMIT ?",
        (agent_id, limit),
    ).fetchall()
    return rows[::-1]


def unread_count(con: sqlite3.Connection, agent_id: int) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM notifications WHERE agent_id = ? AND read_at IS NULL", (agent_id,)
    ).fetchone()[0]


def mark_read(con: sqlite3.Connection, agent_id: int, node_ids: Iterable[int]) -> int:
    ids = list(node_ids)
    if not ids:
        return 0
    t = now()
    total = 0
    with db.tx(con):
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            cur = con.execute(
                "UPDATE notifications SET read_at = ?, delivered_at = COALESCE(delivered_at, ?)"
                f" WHERE agent_id = ? AND read_at IS NULL AND node_id IN ({_in(chunk)})",
                (t, t, agent_id, *chunk),
            )
            total += cur.rowcount
    return total


def mark_all_read(con: sqlite3.Connection, agent_id: int) -> int:
    t = now()
    with db.tx(con):
        cur = con.execute(
            "UPDATE notifications SET read_at = ?, delivered_at = COALESCE(delivered_at, ?)"
            " WHERE agent_id = ? AND read_at IS NULL",
            (t, t, agent_id),
        )
    return cur.rowcount


# ---------------------------------------------------------------- claims


_CLAIM_SELECT = (
    "SELECT c.*, a.name AS agent_name, a.status AS agent_status, a.last_seen AS agent_last_seen,"
    " a.kind AS agent_kind, a.worktree AS agent_worktree"
    " FROM claims c JOIN agents a ON a.id = c.agent_id WHERE c.expires_at > ?"
)


def active_claims(
    con: sqlite3.Connection,
    *,
    agent_id: Optional[int] = None,
    exclude_agent: Optional[int] = None,
    soft: Optional[bool] = False,
) -> List[Row]:
    query, params = _CLAIM_SELECT, [now()]
    if agent_id is not None:
        query += " AND c.agent_id = ?"
        params.append(agent_id)
    if exclude_agent is not None:
        query += " AND c.agent_id != ?"
        params.append(exclude_agent)
    if soft is not None:
        query += " AND c.soft = ?"
        params.append(int(soft))
    return con.execute(query + " ORDER BY c.created_at", params).fetchall()


def resource_of(row: Row) -> Resource:
    return Resource(row["resource"], row["match_key"], bool(row["is_path"]))


def conflicting_claims(con: sqlite3.Connection, res: Resource, agent_id: int) -> List[Row]:
    return [
        c
        for c in active_claims(con, exclude_agent=agent_id)
        if overlaps(resource_of(c), res)
    ]


def claims_covering(con: sqlite3.Connection, key: str, exclude_agent: int) -> List[Row]:
    """Other agents' claims and recent-edit markers that cover the path `key`."""
    return [
        c
        for c in active_claims(con, exclude_agent=exclude_agent, soft=None)
        if c["is_path"] and path_matches(c["match_key"], key)
    ]


def claim(
    con: sqlite3.Connection,
    me: Row,
    resources: Sequence[Resource],
    *,
    note: Optional[str] = None,
    minutes: Optional[float] = None,
    force: bool = False,
) -> Tuple[bool, Dict[str, List[Row]]]:
    """Claim every resource, or none of them if any conflicts (unless force).

    Returns (claimed, conflicts by resource text)."""
    ttl = (minutes or db.config_number(con, "claim_minutes")) * 60
    t = now()
    with db.tx(con):
        conflicts = {r.text: conflicting_claims(con, r, me["id"]) for r in resources}
        conflicts = {k: v for k, v in conflicts.items() if v}
        if conflicts and not force:
            return False, conflicts
        for r in resources:
            mine = con.execute(
                "SELECT id FROM claims WHERE agent_id = ? AND match_key = ? AND soft = 0",
                (me["id"], r.key),
            ).fetchone()
            if mine is not None:
                con.execute(
                    "UPDATE claims SET note = COALESCE(?, note), ttl = ?, expires_at = ? WHERE id = ?",
                    (note, ttl, t + ttl, mine["id"]),
                )
            else:
                con.execute(
                    "INSERT INTO claims(agent_id, resource, match_key, is_path, soft, note, ttl,"
                    " created_at, expires_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                    (me["id"], r.text, r.key, int(r.is_path), note, ttl, t, t + ttl),
                )
        for text, rows in conflicts.items():
            for owner in {row["agent_id"] for row in rows}:
                theirs = ", ".join(row["resource"] for row in rows if row["agent_id"] == owner)
                notify(
                    con,
                    owner,
                    "claim",
                    text=f"@{me['name']} force-claimed {text}, which overlaps your claim on {theirs}."
                    " Coordinate with them on the board.",
                )
                owner_name = rows[0]["agent_name"] if rows[0]["agent_id"] == owner else get_agent(con, owner)["name"]
                event(
                    con,
                    "force",
                    f"@{me['name']} force-claimed {text} over @{owner_name}'s claim on {theirs}",
                    agent_id=me["id"],
                    other_id=owner,
                )
        claimed = ", ".join(relative_to(r.text, me["worktree"]) if r.is_path else r.text for r in resources)
        event(con, "claim", f"@{me['name']} claimed {claimed}" + (f' ("{note}")' if note else ""), agent_id=me["id"])
    return True, conflicts


def release_claim(con: sqlite3.Connection, by: Row, claim_id: int) -> Row:
    """A human releases anyone's claim."""
    if by["kind"] != "human":
        raise BoardError("Only humans can release other participants' claims.")
    with db.tx(con):
        row = con.execute(
            "SELECT c.*, a.name AS agent_name, a.worktree AS agent_worktree FROM claims c"
            " JOIN agents a ON a.id = c.agent_id WHERE c.id = ?",
            (claim_id,),
        ).fetchone()
        if row is None:
            raise BoardError("That claim no longer exists.")
        con.execute("DELETE FROM claims WHERE id = ?", (claim_id,))
        shown = relative_to(row["resource"], row["agent_worktree"]) if row["is_path"] else row["resource"]
        if row["agent_id"] != by["id"]:
            notify(con, row["agent_id"], "claim", text=f"@{by['name']} released your claim on {row['resource']}.")
            event(
                con,
                "force",
                f"@{by['name']} released @{row['agent_name']}'s claim on {shown}",
                agent_id=by["id"],
                other_id=row["agent_id"],
            )
        else:
            event(con, "release", f"@{by['name']} released {shown}", agent_id=by["id"])
        _tell_waiters(con, row["agent_name"], [row], "lost")
    return row


def release(con: sqlite3.Connection, me: Row, resources: Optional[Sequence[Resource]] = None) -> List[Row]:
    """Release my claims (all of them when resources is None)."""
    with db.tx(con):
        mine = con.execute(
            "SELECT * FROM claims WHERE agent_id = ? AND soft = 0", (me["id"],)
        ).fetchall()
        if resources is None:
            chosen = list(mine)
        else:
            chosen = [
                c
                for c in mine
                if any(
                    c["match_key"] == r.key
                    or (r.is_path and c["is_path"] and path_matches(r.key, c["match_key"]))
                    for r in resources
                )
            ]
        if chosen:
            ids = [c["id"] for c in chosen]
            con.execute(f"DELETE FROM claims WHERE id IN ({_in(ids)})", ids)
            _tell_waiters(con, me["name"], chosen, "released")
            released = ", ".join(relative_to(c["resource"], me["worktree"]) for c in chosen)
            event(con, "release", f"@{me['name']} released {released}", agent_id=me["id"])
    return chosen


def _tell_waiters(con: sqlite3.Connection, owner_name: str, claims: Sequence[Row], what: str) -> None:
    """Notify agents that were held back by these claims."""
    for c in claims:
        key = f"claim:{c['id']}"
        for gate in con.execute("SELECT agent_id FROM gates WHERE key = ?", (key,)).fetchall():
            notify(
                con,
                gate["agent_id"],
                "claim",
                text=f"@{owner_name} {what} the claim on {c['resource']} that held you back.",
            )
        con.execute("DELETE FROM gates WHERE key = ?", (key,))


def touch_file(con: sqlite3.Connection, agent_id: int, path_text: str, key: str) -> None:
    """Mark a file as recently edited by an agent (an automatic, soft claim)."""
    ttl = db.config_number(con, "touch_minutes") * 60
    t = now()
    with db.tx(con):
        row = con.execute(
            "SELECT id FROM claims WHERE agent_id = ? AND match_key = ? AND soft = 1", (agent_id, key)
        ).fetchone()
        if row is not None:
            con.execute(
                "UPDATE claims SET expires_at = ?, ttl = ?, resource = ? WHERE id = ?",
                (t + ttl, ttl, path_text, row["id"]),
            )
        else:
            con.execute(
                "INSERT INTO claims(agent_id, resource, match_key, is_path, soft, ttl, created_at,"
                " expires_at) VALUES (?, ?, ?, 1, 1, ?, ?, ?)",
                (agent_id, path_text, key, ttl, t, t + ttl),
            )


# ---------------------------------------------------------------- gates


def gate_passed(con: sqlite3.Connection, agent_id: int, key: str, window_s: float) -> bool:
    """Whether the agent already hit this coordination check within the window."""
    row = con.execute(
        "SELECT at FROM gates WHERE agent_id = ? AND key = ?", (agent_id, key)
    ).fetchone()
    return row is not None and now() - row["at"] <= window_s


def set_gate(con: sqlite3.Connection, agent_id: int, key: str) -> None:
    with db.tx(con):
        con.execute(
            "INSERT OR REPLACE INTO gates(agent_id, key, at) VALUES (?, ?, ?)", (agent_id, key, now())
        )


READ_NOTIFICATION_DAYS = 7
NOTIFICATION_DAYS = 30
EVENT_DAYS = 30


def purge(con: sqlite3.Connection) -> None:
    """Drop long-expired claims, stale gates, old events and old notifications."""
    t = now()
    with db.tx(con):
        con.execute("DELETE FROM claims WHERE expires_at < ?", (t - 86400,))
        con.execute("DELETE FROM claims WHERE soft = 1 AND expires_at < ?", (t,))
        con.execute("DELETE FROM gates WHERE at < ?", (t - 86400,))
        con.execute("DELETE FROM events WHERE at < ?", (t - EVENT_DAYS * 86400,))
        con.execute(
            "DELETE FROM notifications WHERE read_at IS NOT NULL AND created_at < ?",
            (t - READ_NOTIFICATION_DAYS * 86400,),
        )
        con.execute("DELETE FROM notifications WHERE created_at < ?", (t - NOTIFICATION_DAYS * 86400,))


# ---------------------------------------------------------------- activity log


def event(
    con: sqlite3.Connection,
    kind: str,
    text: str,
    *,
    agent_id: Optional[int] = None,
    other_id: Optional[int] = None,
    node_id: Optional[int] = None,
) -> None:
    """Record something that happened, for the activity timeline."""
    con.execute(
        "INSERT INTO events(at, kind, agent_id, other_id, node_id, text) VALUES (?, ?, ?, ?, ?, ?)",
        (now(), kind, agent_id, other_id, node_id, text),
    )


def log_event(con: sqlite3.Connection, kind: str, text: str, **ids: Optional[int]) -> None:
    with db.tx(con):
        event(con, kind, text, **ids)


def recent_events(con: sqlite3.Connection, limit: int = 200, after_id: Optional[int] = None) -> List[Row]:
    """Newest last."""
    if after_id is not None:
        rows = con.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?", (after_id, limit)
        ).fetchall()
    else:
        rows = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return rows[::-1]


# What an agent hears about in "changes since you last looked": the state of the board,
# not the conversation (posts arrive as notifications).
CHANGE_KINDS = ("join", "end", "status", "claim", "release", "force", "expire", "pause", "resume", "config")


def unseen_changes(con: sqlite3.Connection, me: Row, limit: int = 200) -> Tuple[List[Row], int, bool]:
    """Changes by others in my space (plus humans' and setting changes) since my cursor.

    Returns (events, newest event id to move the cursor to, whether more were skipped)."""
    query = (
        f"SELECT e.*, a.home_id AS agent_home, a.kind AS agent_kind FROM events e"
        f" LEFT JOIN agents a ON a.id = e.agent_id WHERE e.id > ?"
        f" AND e.kind IN ({_in(CHANGE_KINDS)}) ORDER BY e.id {{order}} LIMIT ?"
    )
    params = (me["seen_event_id"] or 0, *CHANGE_KINDS, limit + 1)
    rows = con.execute(query.format(order="ASC"), params).fetchall()
    more = len(rows) > limit
    if more:  # far behind: skip to the present, keeping only the most recent changes
        rows = con.execute(query.format(order="DESC"), params).fetchall()[:limit][::-1]
    newest = rows[-1]["id"] if rows else (me["seen_event_id"] or 0)
    relevant = [
        e
        for e in rows
        if e["agent_id"] != me["id"]
        and e["other_id"] != me["id"]  # those reach me as notifications already
        and (e["kind"] == "config" or e["agent_kind"] == "human" or (me["home_id"] is not None and e["agent_home"] == me["home_id"]))
    ]
    return relevant, newest, more


def mark_events_seen(con: sqlite3.Connection, agent_id: int, event_id: Optional[int] = None) -> None:
    """Move an agent's cursor in the activity log (to the newest event by default)."""
    with db.tx(con):
        if event_id is None:
            event_id = con.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
        con.execute(
            "UPDATE agents SET seen_event_id = MAX(COALESCE(seen_event_id, 0), ?) WHERE id = ?", (event_id, agent_id)
        )


def set_config(con: sqlite3.Connection, by: Row, key: str, value: str) -> None:
    try:
        db.set_config(con, key, value)
    except ValueError as e:
        raise BoardError(str(e)) from None
    log_event(con, "config", f"@{by['name']} set {key} = {value}", agent_id=by["id"])
