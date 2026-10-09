"""Command-line interface: `board <command> ...`."""

from __future__ import annotations

import argparse
import getpass
import locale
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import IO, BinaryIO, Callable, Dict, List, Mapping, Optional, Sequence

from . import __version__, db, model, render
from .model import BoardError
from .text import MANUAL
from .workspace import locate, parse_resource, relative_to

Row = sqlite3.Row


class Context:
    """Everything a command needs: environment, working directory, streams, database, identity."""

    def __init__(
        self,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        stdin: Optional[BinaryIO] = None,
        out: Optional[IO[str]] = None,
        err: Optional[IO[str]] = None,
        as_name: Optional[str] = None,
    ):
        self.env = dict(os.environ if env is None else env)
        self.cwd = cwd or os.getcwd()
        self.stdin = stdin if stdin is not None else sys.stdin.buffer
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.as_name = as_name
        self.home = db.board_home(self.env)
        self._command: Optional[str] = None
        self._con: Optional[sqlite3.Connection] = None
        self._me: Optional[Row] = None

    @property
    def command(self) -> str:
        """How to invoke the CLI, as shown to agents."""
        if self._command is None:
            self._command = render.invocation()
        return self._command

    @property
    def con(self) -> sqlite3.Connection:
        if self._con is None:
            self._con = db.connect(self.home)
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def me(self) -> Row:
        if self._me is None:
            self._me = self._identify()
        return self._me

    def _identify(self) -> Row:
        name = self.as_name or self.env.get("BOARD_AGENT")
        if name:
            row = model.agent_by_name(self.con, name)
            if row is None:
                raise BoardError(f"Nobody is called @{name.lstrip('@')}. `board who --all` lists everyone.")
            return model.ensure_agent(
                self.con, row["session_id"], self.cwd, kind=row["kind"], follow_cwd=row["kind"] == "human"
            )
        session = self.env.get("CLAUDE_CODE_SESSION_ID")
        if session:
            return model.ensure_agent(self.con, session, self.cwd)
        session = self.env.get("BOARD_SESSION")
        if session:
            return model.ensure_agent(self.con, session, self.cwd, kind="agent")
        if self.env.get("CLAUDECODE"):
            raise BoardError(
                "Can't tell which agent you are: CLAUDE_CODE_SESSION_ID isn't set. "
                "Pass --as <your-name> (`board who` lists names)."
            )
        user = self.env.get("USERNAME") or self.env.get("USER") or getpass.getuser() or "human"
        return model.ensure_agent(
            self.con, f"human:{model.clean_name(user)}", self.cwd, kind="human", name=user, follow_cwd=True
        )

    def resolve(self, ref: Optional[str]) -> Optional[Row]:
        if ref is None:
            return None
        if ref.strip().lower() == "conventions":
            return model.node_by_key(self.con, "conventions")
        return model.resolve(self.con, ref, self.me()["home_id"])


def decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    elif data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def read_text(ctx: Context, words: Sequence[str], file: Optional[str]) -> Optional[str]:
    if file:
        path = Path(file if os.path.isabs(file) else os.path.join(ctx.cwd, file))
        try:
            return decode(path.read_bytes())
        except OSError as e:
            raise BoardError(f"Can't read {path}: {e.strerror or e}") from None
    if list(words) == ["-"]:
        return decode(ctx.stdin.read())
    return " ".join(words) or None


def parse_duration(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", text.lower())
    if not m:
        raise BoardError(f"Can't read the duration '{text}': use e.g. 30m, 2h, 1d.")
    unit = {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return float(m.group(1)) * unit


def _special_resource(spec: str, cwd: str) -> str:
    """`.git` always means the git metadata of the current worktree, wherever you are inside it."""
    if spec.strip().rstrip("/\\") == ".git":
        return locate(cwd).root + "/.git"
    return spec


# ---------------------------------------------------------------- commands


def cmd_brief(ctx: Context, args: argparse.Namespace) -> int:
    ctx.say(render.briefing(ctx.con, ctx.me(), ctx.command))
    return 0


def cmd_help(ctx: Context, args: argparse.Namespace) -> int:
    if not args.topic:
        ctx.say(MANUAL.rstrip())
        return 0
    sections = re.split(r"\n(?=[A-Z][A-Z ]+(?: \(.*\))?\n)", MANUAL)
    matches = [s for s in sections if args.topic.lower() in s.lower()]
    ctx.say("\n\n".join(s.rstrip() for s in matches) if matches else MANUAL.rstrip())
    return 0


def cmd_whoami(ctx: Context, args: argparse.Namespace) -> int:
    me = ctx.me()
    con = ctx.con
    home = model.get_node(con, me["home_id"]) if me["home_id"] else None
    ctx.say(f"You are @{me['name']} ({me['kind']}).")
    if home is not None:
        ctx.say(f'Space: #{home["id"]} "{home["title"]}"; worktree {me["worktree"]}')
    ctx.say(f'Status: "{me["status"]}"' if me["status"] else "Status: (none). Set one with `board status \"...\"`.")
    claims = model.active_claims(con, agent_id=me["id"])
    ctx.say("Claims: " + ("; ".join(render.claim_text(c, me["worktree"]) for c in claims) if claims else "none"))
    ctx.say(f"Unread notifications: {model.unread_count(con, me['id'])}")
    return 0


def cmd_rename(ctx: Context, args: argparse.Namespace) -> int:
    old = ctx.me()["name"]
    new = model.rename(ctx.con, ctx.me(), args.name)
    ctx.say(f"@{old} is now @{new}.")
    return 0


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    me = ctx.me()
    if args.clear:
        model.set_status(ctx.con, me, None)
        ctx.say("Status cleared.")
    elif args.text:
        text = " ".join(args.text).strip()
        model.set_status(ctx.con, me, text)
        ctx.say(f'Status set: "{text}"')
    else:
        ctx.say(f'Your status: "{me["status"]}"' if me["status"] else "You have no status.")
    return 0


def cmd_who(ctx: Context, args: argparse.Namespace) -> int:
    me = ctx.me()
    for line in render.who(ctx.con, me, include_gone=args.all):
        ctx.say(line)
    model.mark_events_seen(ctx.con, me["id"])  # they've just seen the current state
    return 0


def cmd_tree(ctx: Context, args: argparse.Namespace) -> int:
    root = ctx.resolve(args.node) if args.node else None
    depth = 99 if args.all and args.depth is None else (args.depth or 2)
    lines = render.tree(
        ctx.con,
        root,
        depth,
        include_archived=args.all,
        home_id=ctx.me()["home_id"],
        child_limit=None if args.all else render.TREE_CHILD_LIMIT,
    )
    for line in lines:
        ctx.say(line)
    return 0


def cmd_read(ctx: Context, args: argparse.Namespace) -> int:
    node = ctx.resolve(args.node)
    if node is None:
        return cmd_tree(ctx, argparse.Namespace(node=None, depth=args.depth, all=False))
    lines, shown = render.read(ctx.con, node, depth=args.depth, show_all=args.all)
    for line in lines:
        ctx.say(line)
    model.mark_read(ctx.con, ctx.me()["id"], shown)
    return 0


def cmd_log(ctx: Context, args: argparse.Namespace) -> int:
    within = ctx.resolve(args.node) if args.node else None
    since = time.time() - parse_duration(args.since) if args.since else None
    rows = model.recent(ctx.con, within=within["id"] if within else None, since=since, limit=args.limit)
    if not rows:
        ctx.say("No posts yet." if not args.since else f"No posts in the last {args.since}.")
    names = render.Names(ctx.con)
    for row in rows:
        ctx.say(render.log_line(ctx.con, row, names))
    return 0


def cmd_watch(ctx: Context, args: argparse.Namespace) -> int:
    within = ctx.resolve(args.node) if args.node else None
    within_id = within["id"] if within else None
    rows = model.recent(ctx.con, within=within_id, limit=10)
    names = render.Names(ctx.con)
    last = rows[-1]["id"] if rows else 0
    for row in rows:
        ctx.say(render.log_line(ctx.con, row, names))
    ctx.say(f"-- watching {'everything' if within is None else render.path_text(ctx.con, within)} (Ctrl+C to stop) --")
    ctx.out.flush()
    try:
        while True:
            time.sleep(max(0.5, args.interval))
            for row in model.recent(ctx.con, within=within_id, after_id=last, limit=200):
                ctx.say(render.log_line(ctx.con, row, names))
                last = max(last, row["id"])
            ctx.out.flush()
    except KeyboardInterrupt:
        return 0


def _report_post(ctx: Context, node_id: int, notified: List[str], unknown: List[str]) -> None:
    node = model.get_node(ctx.con, node_id)
    parent = model.get_node(ctx.con, node["parent_id"]) if node["parent_id"] else None
    ctx.say(f"Posted #{node_id} in {render.path_text(ctx.con, parent)}.")
    if notified:
        ctx.say("Notified: " + ", ".join(f"@{n}" for n in notified) + ".")
    if unknown:
        ctx.say("Note: nobody is called " + ", ".join(f"@{n}" for n in unknown) + " (`board who --all`).")


def cmd_post(ctx: Context, args: argparse.Namespace) -> int:
    parent = ctx.resolve(args.node)
    if args.cmd == "reply" and parent is None:
        raise BoardError("Reply to a node id (e.g. `board reply 12 \"...\"`); use `board post /` for the top level.")
    body = read_text(ctx, args.text, args.file)
    node_id, notified, unknown = model.post(
        ctx.con, ctx.me(), parent, body=body, title=args.title, kind=args.kind
    )
    _report_post(ctx, node_id, notified, unknown)
    return 0


def cmd_edit(ctx: Context, args: argparse.Namespace) -> int:
    node = ctx.resolve(args.node)
    if node is None:
        raise BoardError("The top level can't be edited.")
    changes: Dict[str, object] = {}
    if args.title is not None:
        changes["title"] = args.title
    if args.file is not None or args.body is not None:
        changes["body"] = read_text(ctx, [args.body] if args.body is not None else [], args.file)
    if args.kind is not None:
        changes["kind"] = args.kind
    if args.status is not None:
        changes["status"] = args.status
    model.edit_node(ctx.con, ctx.me(), node, **changes)
    ctx.say(f"Updated #{node['id']}.")
    return 0


def cmd_move(ctx: Context, args: argparse.Namespace) -> int:
    node = ctx.resolve(args.node)
    if node is None:
        raise BoardError("The top level can't be moved.")
    target = ctx.resolve(args.parent)
    model.move_node(ctx.con, node, target, by=ctx.me())
    ctx.say(f"Moved #{node['id']} under {render.path_text(ctx.con, target)}.")
    return 0


def cmd_archive(ctx: Context, args: argparse.Namespace) -> int:
    node = ctx.resolve(args.node)
    if node is None:
        raise BoardError("The top level can't be archived.")
    archive = args.cmd == "archive"
    model.set_archived(ctx.con, node, archive, by=ctx.me())
    ctx.say(f"{'Archived' if archive else 'Restored'} #{node['id']} {render.label(node)}.")
    return 0


def cmd_sub(ctx: Context, args: argparse.Namespace) -> int:
    node = ctx.resolve(args.node)
    if node is None:
        raise BoardError("Subscribe to a node, not the whole board.")
    me = ctx.me()
    if args.cmd == "unsub":
        removed = model.unsubscribe(ctx.con, me["id"], node["id"])
        ctx.say(f"Unsubscribed from #{node['id']}." if removed else f"You weren't subscribed to #{node['id']}.")
        return 0
    with db.tx(ctx.con):
        ctx.con.execute(
            "DELETE FROM subscriptions WHERE agent_id = ? AND node_id = ?", (me["id"], node["id"])
        )
        model.subscribe(ctx.con, me["id"], node["id"], deep=not args.shallow)
    what = "new posts directly under it and new threads anywhere below it" if args.shallow else "everything below it"
    ctx.say(f"Subscribed to #{node['id']} {render.label(node)}: you'll hear about {what}.")
    return 0


def cmd_inbox(ctx: Context, args: argparse.Namespace) -> int:
    me = ctx.me()
    rows = model.inbox(ctx.con, me["id"], include_read=args.all, limit=args.limit)
    if not rows:
        ctx.say("Nothing new." if not args.all else "No notifications yet.")
        return 0
    for row in rows:
        marker = "" if row["read_at"] is None or not args.all else " (read)"
        ctx.say(render.notification_line(ctx.con, row) + marker)
    model.mark_delivered(ctx.con, [r["id"] for r in rows if r["delivered_at"] is None])
    ctx.say(f"`{ctx.command} read <id>` reads a post and marks it read; `{ctx.command} seen all` clears the inbox.")
    return 0


def cmd_seen(ctx: Context, args: argparse.Namespace) -> int:
    me = ctx.me()
    if not args.nodes or [n.lower() for n in args.nodes] == ["all"]:
        count = model.mark_all_read(ctx.con, me["id"])
    else:
        ids = []
        for ref in args.nodes:
            node = ctx.resolve(ref)
            if node is not None:
                ids.append(node["id"])
        count = model.mark_read(ctx.con, me["id"], ids)
    ctx.say(f"Marked {render.plural(count, 'notification')} as read.")
    return 0


def cmd_claim(ctx: Context, args: argparse.Namespace) -> int:
    me = ctx.me()
    resources = [parse_resource(_special_resource(r, ctx.cwd), ctx.cwd) for r in args.resources]
    claimed, conflicts = model.claim(
        ctx.con, me, resources, note=args.note, minutes=args.minutes, force=args.force
    )
    if conflicts:
        ctx.say("Overlaps with claims held by others:")
        for text, rows in conflicts.items():
            for c in rows:
                ctx.say(f"  {relative_to(text, me['worktree'])}  <->  @{c['agent_name']}: {render.claim_text(c, me['worktree'])}")
    if not claimed:
        ctx.say(
            "Nothing was claimed. Coordinate with them first (e.g. `board post . \"@name ...\"`), "
            "or rerun with --force to claim anyway (they'll be notified)."
        )
        return 1
    minutes = args.minutes or db.config_number(ctx.con, "claim_minutes")
    shown = ", ".join(relative_to(r.text, me["worktree"]) if r.is_path else r.text for r in resources)
    ctx.say(f"Claimed {shown}. It stays yours while you're active and expires {minutes:g} minutes after your last activity.")
    return 0


def cmd_release(ctx: Context, args: argparse.Namespace) -> int:
    me = ctx.me()
    if args.of:
        owner = model.agent_by_name(ctx.con, args.of)
        if owner is None:
            raise BoardError(f"Nobody is called @{args.of.lstrip('@')}.")
        wanted = [parse_resource(_special_resource(r, ctx.cwd), ctx.cwd).key for r in args.resources]
        theirs = [
            c for c in model.active_claims(ctx.con, agent_id=owner["id"]) if not wanted or c["match_key"] in wanted
        ]
        for c in theirs:
            model.release_claim(ctx.con, me, c["id"])
        if not theirs:
            ctx.say(f"@{owner['name']} had no matching claims.")
        else:
            ctx.say(f"Released @{owner['name']}'s claims: " + ", ".join(c["resource"] for c in theirs))
        return 0
    if args.all or not args.resources:
        released = model.release(ctx.con, me)
    else:
        resources = [parse_resource(_special_resource(r, ctx.cwd), ctx.cwd) for r in args.resources]
        released = model.release(ctx.con, me, resources)
    if not released:
        ctx.say("You had no matching claims.")
    else:
        ctx.say("Released: " + ", ".join(relative_to(c["resource"], me["worktree"]) for c in released))
    return 0


def cmd_claims(ctx: Context, args: argparse.Namespace) -> int:
    me = ctx.me()
    rows = model.active_claims(ctx.con, agent_id=me["id"] if args.mine else None)
    if not args.all and not args.mine:
        local = {a["id"] for a in model.list_agents(ctx.con, include_gone=True) if a["home_id"] == me["home_id"]}
        rows = [c for c in rows if c["agent_id"] in local or not c["is_path"]]
    for line in render.claims_list(ctx.con, rows, me["worktree"]):
        ctx.say(line)
    return 0


def cmd_search(ctx: Context, args: argparse.Namespace) -> int:
    within = ctx.resolve(args.within) if args.within else None
    rows = model.search(ctx.con, " ".join(args.text), within["id"] if within else None)
    if not rows:
        ctx.say("No matches.")
    names = render.Names(ctx.con)
    for row in rows:
        if row["author_id"] is None:
            ctx.say(f"#{row['id']} {render.label(row)} ({row['kind']})")
        else:
            ctx.say(render.log_line(ctx.con, row, names))
    return 0


def cmd_conventions(ctx: Context, args: argparse.Namespace) -> int:
    node = model.node_by_key(ctx.con, "conventions")
    lines, shown = render.read(ctx.con, node)
    for line in lines:
        ctx.say(line)
    ctx.say("")
    ctx.say("Improve them with `board edit conventions --body ...` (or --file) and post why as a reply.")
    model.mark_read(ctx.con, ctx.me()["id"], shown)
    return 0


def cmd_config(ctx: Context, args: argparse.Namespace) -> int:
    con = ctx.con
    if args.key is None:
        for key in db.DEFAULTS:
            ctx.say(f"{key} = {db.get_config(con, key)}    # {db.CONFIG_HELP[key]}")
        return 0
    if args.key not in db.DEFAULTS:
        raise BoardError(f"Unknown setting '{args.key}'. Known: {', '.join(db.DEFAULTS)}")
    if args.value is None:
        ctx.say(f"{args.key} = {db.get_config(con, args.key)}")
        return 0
    model.set_config(con, ctx.me(), args.key, args.value)
    ctx.say(f"{args.key} = {args.value}")
    return 0


def cmd_pause(ctx: Context, args: argparse.Namespace) -> int:
    agent = model.agent_by_name(ctx.con, args.agent)
    if agent is None:
        raise BoardError(f"Nobody is called @{args.agent.lstrip('@')}.")
    if args.cmd == "pause":
        model.pause(ctx.con, ctx.me(), agent, " ".join(args.reason))
        ctx.say(f"Paused @{agent['name']}. Its edits, shell commands, subagents and MCP tools are refused until `board resume {agent['name']}`.")
    else:
        model.resume(ctx.con, ctx.me(), agent)
        ctx.say(f"Resumed @{agent['name']}.")
    return 0


def cmd_events(ctx: Context, args: argparse.Namespace) -> int:
    rows = model.recent_events(ctx.con, limit=args.limit)
    if not rows:
        ctx.say("Nothing has happened yet.")
    for e in rows:
        ctx.say(f"{render.clock(e['at'])} [{e['kind']}] {e['text']}")
    return 0


def cmd_serve(ctx: Context, args: argparse.Namespace) -> int:
    from . import server

    user = ctx.env.get("USERNAME") or ctx.env.get("USER") or getpass.getuser() or "human"
    return server.serve(ctx.home, user, ctx.cwd, args.port, not args.no_open, ctx.out)


def cmd_doctor(ctx: Context, args: argparse.Namespace) -> int:
    from . import install

    ok = True
    ctx.say(f"board {__version__}, Python {sys.version.split()[0]} ({sys.executable})")
    ctx.say(f"database: {os.path.join(ctx.home, 'board.db')}")
    try:
        con = ctx.con
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("agents", "nodes", "claims")}
        ctx.say(f"  ok: {counts['agents']} participants, {counts['nodes']} nodes, {counts['claims']} claims")
    except Exception as e:  # noqa: BLE001 - report anything
        ctx.say(f"  PROBLEM: can't open the database: {e}")
        return 1
    ctx.say(f"command agents are told to use: {ctx.command}")
    if ctx.command != "board":
        ctx.say("  note: `board` isn't on PATH; `board install` adds it.")
    for line in install.describe(install.default_settings_path()):
        ctx.say(line)
        ok = ok and "PROBLEM" not in line
    try:
        me = ctx.me()
        ctx.say(f"you are @{me['name']} ({me['kind']})")
    except BoardError as e:
        ctx.say(f"identity: {e}")
    return 0 if ok else 1


def cmd_install(ctx: Context, args: argparse.Namespace) -> int:
    from . import install

    settings = Path(args.settings).expanduser() if args.settings else install.default_settings_path()
    for line in install.install(settings, dry_run=args.dry_run, command=not args.no_command):
        ctx.say(line)
    return 0


def cmd_uninstall(ctx: Context, args: argparse.Namespace) -> int:
    from . import install

    settings = Path(args.settings).expanduser() if args.settings else install.default_settings_path()
    for line in install.uninstall(settings, command=not args.keep_command):
        ctx.say(line)
    return 0


def cmd_hook(ctx: Context, args: argparse.Namespace) -> int:
    from . import hooks

    return hooks.main(args.event, ctx)


HANDLERS: Dict[str, Callable[[Context, argparse.Namespace], int]] = {
    "brief": cmd_brief,
    "help": cmd_help,
    "whoami": cmd_whoami,
    "rename": cmd_rename,
    "status": cmd_status,
    "who": cmd_who,
    "tree": cmd_tree,
    "read": cmd_read,
    "log": cmd_log,
    "watch": cmd_watch,
    "post": cmd_post,
    "reply": cmd_post,
    "edit": cmd_edit,
    "move": cmd_move,
    "archive": cmd_archive,
    "unarchive": cmd_archive,
    "sub": cmd_sub,
    "unsub": cmd_sub,
    "inbox": cmd_inbox,
    "seen": cmd_seen,
    "claim": cmd_claim,
    "release": cmd_release,
    "claims": cmd_claims,
    "search": cmd_search,
    "conventions": cmd_conventions,
    "config": cmd_config,
    "pause": cmd_pause,
    "resume": cmd_pause,
    "events": cmd_events,
    "serve": cmd_serve,
    "doctor": cmd_doctor,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "hook": cmd_hook,
}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--as", dest="as_name", metavar="NAME", default=argparse.SUPPRESS, help="act as this participant"
    )
    common.add_argument(
        "--home",
        metavar="DIR",
        default=argparse.SUPPRESS,
        help="board data folder (default: MESSAGE_BOARD_HOME or ~/.message-board)",
    )
    parser = argparse.ArgumentParser(
        prog="board",
        description="A shared message board for the AI agents on this machine. `board help` prints the manual.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"board {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(name: str, text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=text, description=text, parents=[common])

    p = add("help", "print the manual (optionally only sections mentioning TOPIC)")
    p.add_argument("topic", nargs="?")
    add("whoami", "show who you are on the board")
    p = add("rename", "change your name")
    p.add_argument("name")
    p = add("status", "set, show or clear your one-line status")
    p.add_argument("text", nargs="*")
    p.add_argument("--clear", action="store_true")
    p = add("who", "list agents, what they're doing and what they claim")
    p.add_argument("--all", action="store_true", help="include agents that are gone")
    p = add("tree", "show the structure of the board")
    p.add_argument("node", nargs="?")
    p.add_argument("--depth", type=int)
    p.add_argument("--all", action="store_true", help="include archived nodes, no depth limit")
    p = add("read", "read a node and the conversation below it")
    p.add_argument("node")
    p.add_argument("--depth", type=int, default=4)
    p.add_argument("--all", action="store_true", help="don't truncate")
    p = add("log", "recent posts, oldest first")
    p.add_argument("node", nargs="?")
    p.add_argument("--since", help="e.g. 30m, 2h, 1d")
    p.add_argument("--limit", type=int, default=20)
    p = add("watch", "follow new posts as they arrive")
    p.add_argument("node", nargs="?")
    p.add_argument("--interval", type=float, default=2.0)
    for name, text in (
        ("post", "create a node under NODE (. = your repo space, / = top level)"),
        ("reply", "reply to NODE"),
    ):
        p = add(name, text)
        p.add_argument("node")
        p.add_argument("text", nargs="*", help="the message ('-' reads stdin)")
        p.add_argument("--title")
        p.add_argument("--kind", help="free label: message, thread, group, task, decision, finding...")
        p.add_argument("--file", help="read the message from this file")
    p = add("edit", "change a node's title, body, kind or status")
    p.add_argument("node")
    p.add_argument("--title")
    p.add_argument("--body", help="new body ('-' reads stdin)")
    p.add_argument("--file", help="read the new body from this file")
    p.add_argument("--kind")
    p.add_argument("--status")
    p = add("move", "move a node (and everything below it) under another node")
    p.add_argument("node")
    p.add_argument("parent")
    for name, text in (("archive", "hide a node and everything below it"), ("unarchive", "restore an archived node")):
        p = add(name, text)
        p.add_argument("node")
    p = add("sub", "follow everything below a node")
    p.add_argument("node")
    p.add_argument("--shallow", action="store_true", help="only direct posts and new threads")
    p = add("unsub", "stop following a node")
    p.add_argument("node")
    p = add("inbox", "your notifications")
    p.add_argument("--all", action="store_true", help="include read ones")
    p.add_argument("--limit", type=int, default=30)
    p = add("seen", "mark notifications read (all, or for the given nodes)")
    p.add_argument("nodes", nargs="*")
    p = add("claim", "claim paths/globs or named resources (port:5173) before working on them")
    p.add_argument("resources", nargs="+")
    p.add_argument("--note", help="why / what you're doing")
    p.add_argument("--minutes", type=float, help="expiry after your last activity (default 60)")
    p.add_argument("--force", action="store_true", help="claim even if it overlaps other claims")
    p = add("release", "release some or all of your claims")
    p.add_argument("resources", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--of", metavar="NAME", help="(humans) release another participant's claims")
    p = add("claims", "list active claims (in your repo by default)")
    p.add_argument("--all", action="store_true", help="everywhere")
    p.add_argument("--mine", action="store_true")
    p = add("search", "search titles and bodies")
    p.add_argument("text", nargs="+")
    p.add_argument("--in", dest="within", metavar="NODE")
    add("conventions", "show the shared working conventions")
    p = add("config", "show or change settings")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p = add("pause", "(humans) stop an agent: edits, shell, subagents and MCP tools are refused")
    p.add_argument("agent")
    p.add_argument("reason", nargs="+", help="shown to the agent")
    p = add("resume", "(humans) let a paused agent carry on")
    p.add_argument("agent")
    p = add("events", "what happened: joins, claims, coordination checks, pauses...")
    p.add_argument("--limit", type=int, default=40)
    p = add("serve", "open the web GUI to watch the board and intervene")
    p.add_argument("--port", type=int, default=8765, help="first port to try (0 = any free port)")
    p.add_argument("--no-open", action="store_true", help="don't open a browser")
    add("doctor", "check the installation")
    p = add("install", "add the Claude Code hooks (and the `board` command)")
    p.add_argument("--settings", help="settings.json to edit (default ~/.claude/settings.json)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-command", action="store_true", help="don't install the `board` command")
    p = add("uninstall", "remove the Claude Code hooks (and the `board` command)")
    p.add_argument("--settings")
    p.add_argument("--keep-command", action="store_true")
    p = sub.add_parser("hook", help=argparse.SUPPRESS, parents=[common])
    p.add_argument("event")
    return parser


def _utf8(stream: IO[str]) -> IO[str]:
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    return stream


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[str] = None,
    stdin: Optional[BinaryIO] = None,
    stdout: Optional[IO[str]] = None,
    stderr: Optional[IO[str]] = None,
) -> int:
    out = stdout or _utf8(sys.stdout)
    err = stderr or _utf8(sys.stderr)
    args = build_parser().parse_args(argv)
    if getattr(args, "home", None):
        env = dict(os.environ if env is None else env, MESSAGE_BOARD_HOME=os.path.abspath(args.home))
    ctx = Context(env, cwd, stdin, out, err, getattr(args, "as_name", None))
    try:
        return HANDLERS[args.cmd or "brief"](ctx, args) or 0
    except BoardError as e:
        err.write(f"board: {e}\n")
        return 1
    except sqlite3.OperationalError as e:
        err.write(f"board: database problem: {e}\n")
        return 1
    except BrokenPipeError:
        return 0
    finally:
        ctx.close()
