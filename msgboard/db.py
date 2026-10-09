"""SQLite storage: connection setup, schema and settings."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator, Mapping, Optional

SCHEMA_VERSION = 3

DEFAULTS = {
    "enforce": "warn",
    "active_minutes": "15",
    "gone_hours": "12",
    "claim_minutes": "60",
    "touch_minutes": "30",
    "gate_minutes": "15",
    "notify_max": "6",
}

CONFIG_HELP = {
    "enforce": "warn: block a conflicting edit or git command once and let a retry through; "
    "block: always block; off: never block",
    "active_minutes": "an agent seen within this many minutes counts as active",
    "gone_hours": "an agent unseen for this many hours is treated as gone",
    "claim_minutes": "claims expire this many minutes after their owner's last activity",
    "touch_minutes": "a file an agent edited stays marked as recently edited by it for this many minutes",
    "gate_minutes": "after a coordination block, a retry within this many minutes goes through (warn mode)",
    "notify_max": "maximum number of notifications shown to an agent at once",
}

SCHEMA_V1 = """
CREATE TABLE agents (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL DEFAULT 'claude',
    session_id TEXT UNIQUE,
    cwd TEXT,
    worktree TEXT,
    worktree_key TEXT,
    repo TEXT,
    home_id INTEGER,
    status TEXT,
    status_at REAL,
    started_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    ended_at REAL,
    briefed_at REAL
);
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES nodes(id),
    key TEXT UNIQUE,
    kind TEXT NOT NULL,
    title TEXT,
    body TEXT,
    status TEXT,
    author_id INTEGER REFERENCES agents(id),
    edited_by INTEGER REFERENCES agents(id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    activity_at REAL NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX nodes_parent ON nodes(parent_id);
CREATE INDEX nodes_created ON nodes(created_at);
CREATE TABLE subscriptions (
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    node_id INTEGER NOT NULL REFERENCES nodes(id),
    deep INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (agent_id, node_id)
);
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    node_id INTEGER REFERENCES nodes(id),
    reason TEXT NOT NULL,
    text TEXT,
    created_at REAL NOT NULL,
    delivered_at REAL,
    read_at REAL
);
CREATE INDEX notifications_agent ON notifications(agent_id, read_at);
CREATE TABLE claims (
    id INTEGER PRIMARY KEY,
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    resource TEXT NOT NULL,
    match_key TEXT NOT NULL,
    is_path INTEGER NOT NULL,
    soft INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    ttl REAL NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX claims_expiry ON claims(expires_at);
CREATE TABLE gates (
    agent_id INTEGER NOT NULL REFERENCES agents(id),
    key TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (agent_id, key)
);
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

# Pausing agents, the repository path of each space, and an activity log for the GUI.
SCHEMA_V2 = """
ALTER TABLE agents ADD COLUMN paused_at REAL;
ALTER TABLE agents ADD COLUMN pause_reason TEXT;
ALTER TABLE agents ADD COLUMN paused_by INTEGER;
ALTER TABLE nodes ADD COLUMN path TEXT;
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    agent_id INTEGER,
    other_id INTEGER,
    node_id INTEGER,
    text TEXT NOT NULL
);
CREATE INDEX events_at ON events(at)
"""

# Scaling: each node stores its ancestor path ("lineage", e.g. "/2/7/12/") so subtrees are an
# indexed range, whether it's hidden under an archived node, and a revision for change tracking.
# Agents remember how far they've read the activity log.
SCHEMA_V3 = """
ALTER TABLE nodes ADD COLUMN lineage TEXT;
ALTER TABLE nodes ADD COLUMN depth INTEGER NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN rev INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN seen_event_id INTEGER;
CREATE INDEX nodes_lineage ON nodes(lineage);
CREATE INDEX nodes_rev ON nodes(rev);
CREATE INDEX agents_last_seen ON agents(last_seen);
CREATE INDEX notifications_pending ON notifications(agent_id) WHERE delivered_at IS NULL;
CREATE INDEX notifications_created ON notifications(created_at)
"""

MIGRATIONS = {1: SCHEMA_V1, 2: SCHEMA_V2, 3: SCHEMA_V3}


def board_home(env: Optional[Mapping[str, str]] = None) -> str:
    """Directory holding the board database (MESSAGE_BOARD_HOME or ~/.message-board)."""
    env = os.environ if env is None else env
    custom = env.get("MESSAGE_BOARD_HOME")
    if custom:
        return os.path.expanduser(custom)
    return os.path.join(os.path.expanduser("~"), ".message-board")


def connect(home: "os.PathLike[str] | str", busy_ms: int = 15000) -> sqlite3.Connection:
    os.makedirs(home, exist_ok=True)
    con = sqlite3.connect(
        os.path.join(home, "board.db"), timeout=busy_ms / 1000, isolation_level=None
    )
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA foreign_keys = ON")
    _migrate(con)
    return con


@contextmanager
def tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Write transaction. Nested use joins the outer transaction."""
    if con.in_transaction:
        yield con
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _migrate(con: sqlite3.Connection) -> None:
    if con.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    from .text import DEFAULT_CONVENTIONS

    with tx(con):
        # Another process may have migrated while we waited for the write lock.
        version = con.execute("PRAGMA user_version").fetchone()[0]
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[target].split(";"):
                if statement.strip():
                    con.execute(statement)
            if target == 1:
                t = time.time()
                con.execute(
                    "INSERT INTO nodes(key, kind, title, body, created_at, updated_at, activity_at)"
                    " VALUES ('conventions', 'doc', 'conventions', ?, ?, ?, ?)",
                    (DEFAULT_CONVENTIONS, t, t, t),
                )
            if target == 3:
                _backfill_lineage(con)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _backfill_lineage(con: sqlite3.Connection) -> None:
    """Compute lineage, depth and hidden for nodes created before schema 3."""
    rows = con.execute("SELECT id, parent_id, archived FROM nodes ORDER BY id").fetchall()
    kids: dict = {}
    archived = {}
    for r in rows:
        kids.setdefault(r[1], []).append(r[0])
        archived[r[0]] = r[2]
    updates = []
    stack = [(i, "/", -1, False) for i in kids.get(None, [])]
    while stack:
        node_id, prefix, depth, hidden = stack.pop()
        lineage = f"{prefix}{node_id}/"
        hidden = hidden or bool(archived[node_id])
        updates.append((lineage, depth + 1, int(hidden), node_id, node_id))
        stack.extend((k, lineage, depth + 1, hidden) for k in kids.get(node_id, []))
    con.executemany("UPDATE nodes SET lineage = ?, depth = ?, hidden = ?, rev = ? WHERE id = ?", updates)
    con.execute("UPDATE agents SET seen_event_id = (SELECT COALESCE(MAX(id), 0) FROM events)")


def get_config(con: sqlite3.Connection, key: str) -> str:
    row = con.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row[0] if row else DEFAULTS[key]


def config_number(con: sqlite3.Connection, key: str) -> float:
    try:
        return float(get_config(con, key))
    except ValueError:
        return float(DEFAULTS[key])


def set_config(con: sqlite3.Connection, key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting {key!r}; known: {', '.join(DEFAULTS)}")
    if key == "enforce":
        if value not in ("warn", "block", "off"):
            raise ValueError("enforce must be warn, block or off")
    else:
        try:
            if float(value) <= 0:
                raise ValueError
        except ValueError:
            raise ValueError(f"{key} must be a positive number") from None
    with tx(con):
        con.execute("INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)", (key, value))
