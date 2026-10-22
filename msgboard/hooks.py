"""Claude Code hooks: presence, notifications and coordination checks.

Every handler fails open: an error is logged to the board home and the hook prints nothing,
so a broken board never blocks an agent.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from . import db, model, render
from .workspace import display_path, locate, path_key, relative_to, risky_git_ops

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
CONTEXT_LIMIT = 9500  # Claude Code caps injected context at 10,000 characters


class HookContext:
    """The slice of cli.Context that hooks need, without loading the CLI."""

    def __init__(self) -> None:
        self.env = os.environ
        self.cwd = os.getcwd()
        self.stdin = sys.stdin.buffer
        self.out = sys.stdout
        self.home = db.board_home()
        self._con = None
        self._command: Optional[str] = None

    @property
    def con(self):
        if self._con is None:
            self._con = db.connect(self.home, busy_ms=5000)
        return self._con

    @property
    def command(self) -> str:
        if self._command is None:
            self._command = render.invocation()
        return self._command

    def close(self) -> None:
        if self._con is not None:
            self._con.close()


def run(argv: Sequence[str]) -> int:
    """Entry point used by the hook commands: board.py hook <event>."""
    ctx = HookContext()
    try:
        return main(argv[0] if argv else "", ctx)
    finally:
        ctx.close()


def main(event: str, ctx: Any) -> int:
    try:
        raw = ctx.stdin.read()
        payload = json.loads(raw.decode("utf-8-sig")) if raw.strip() else {}
        result = handle(event, payload, ctx)
        if result:
            ctx.out.write(json.dumps(result, ensure_ascii=True) + "\n")
    except Exception:  # noqa: BLE001 - never break the agent's session
        _log_error(ctx, event)
    return 0


def handle(event: str, payload: Dict[str, Any], ctx: Any) -> Optional[Dict[str, Any]]:
    session_id = payload.get("session_id") or ctx.env.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return None
    cwd = payload.get("cwd") or ctx.cwd
    con = ctx.con
    if event == "session-end":
        agent = model.agent_by_session(con, session_id)
        if agent is not None:
            model.end_session(con, agent)
        return None
    me = model.ensure_agent(con, session_id, cwd)
    if event == "pre-tool-use":
        return pre_tool_use(con, me, payload, cwd, ctx.command)
    # The board stays quiet for an agent that's alone in its repository (all worktrees counted).
    together = me["paused_at"] is not None or bool(model.active_in_space(con, me))
    if event == "session-start":
        model.purge(con)
        if not together:
            # Also after /compact: whatever it knew about the board is gone, so brief it again later.
            model.mark_briefed(con, me, False)
            return None
        model.mark_briefed(con, me)
        return _context("SessionStart", render.briefing(con, me, ctx.command))
    notes: List[str] = []
    if me["paused_at"] is not None:
        notes.append(render.paused_notice(con, me, ctx.command))
    if event == "post-tool-use":
        notes += post_tool_use(con, me, payload, cwd)
    elif event != "user-prompt-submit":
        return None
    # Subagents share the session id; leave the inbox and the briefing to the main agent.
    if not payload.get("agent_id"):
        if not together:
            # Alone: only what's addressed to it (an @mention, a reply, a human resuming it).
            text = render.delivery(con, me, ctx.command, direct_only=True, introduce=me["briefed_at"] is None)
            if text:
                notes.append(text)
        elif me["briefed_at"] is None:
            # Someone else just started working here (or the hooks were installed mid-session).
            model.mark_briefed(con, me)
            notes.append(render.briefing(con, me, ctx.command))
        else:
            for text in (render.delivery(con, me, ctx.command), render.changes(con, me, ctx.command)):
                if text:
                    notes.append(text)
    name = "PostToolUse" if event == "post-tool-use" else "UserPromptSubmit"
    return _context(name, "\n".join(notes))


def _context(event_name: str, text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    if len(text) > CONTEXT_LIMIT:
        text = text[: CONTEXT_LIMIT - 60].rstrip() + "\n[...truncated: run `board` to see everything]"
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}


def _deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _target(tool_input: Dict[str, Any]) -> Optional[str]:
    return tool_input.get("file_path") or tool_input.get("notebook_path")


def _notify_once(con, me, owner_id: int, key: str, window_s: float, text: str) -> None:
    """Notify owner_id, at most once per window for the same key."""
    if model.gate_passed(con, me["id"], key, window_s):
        return
    model.set_gate(con, me["id"], key)
    with db.tx(con):
        model.notify(con, owner_id, "claim", text=text)


# ---------------------------------------------------------------- before a tool runs


_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_PYTHONS = ("python", "python.exe", "python3", "python3.exe", "py", "py.exe")


def is_board_command(shell_command: str) -> bool:
    """A single `board ...` (or python .../board.py ...) command, with nothing chained to it.

    Quoted text and a trailing heredoc are the message, not commands; but anything the shell would
    run or expand inside them ($(...), backticks), and any output redirection, makes it not one."""
    text = shell_command.strip()
    if text.startswith("&"):  # PowerShell call operator
        text = text[1:].strip()
    command = _command_part(text)
    if command is None:
        return False
    try:
        # Windows paths are full of backslashes, which POSIX shell rules would read as escapes.
        words = [w.strip("\"'") for w in shlex.split(command, posix=os.name != "nt")]
    except ValueError:
        return False
    if not words:
        return False
    first = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if first in ("board", "board.exe", "board.cmd"):
        return True
    if first not in _PYTHONS:
        return False
    rest = words[1:]
    while rest and rest[0].startswith("-"):
        if rest[0] in ("-c", "-m") or not re.fullmatch(r"-[BEIOSsuqv]+", rest[0]):
            return False  # code given inline, a module, or a flag we don't know: not just the board
        rest = rest[1:]
    return bool(rest) and rest[0].replace("\\", "/").rsplit("/", 1)[-1].lower() == "board.py"


def _command_part(text: str) -> Optional[str]:
    """The command line without a trailing heredoc body; None if anything else would run."""
    i, n = 0, len(text)
    heredoc = None
    while i < n:
        c = text[i]
        if c == "'":
            j = text.find("'", i + 1)
            if j == -1:
                return None
            i = j + 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                elif text[i] == "`" or text.startswith("$(", i):
                    return None  # the shell runs these even inside double quotes
                i += 1
            if i >= n:
                return None
            i += 1
            continue
        if c == "<" and text.startswith("<<", i):
            m = _HEREDOC_RE.match(text, i)
            if m is None or heredoc is not None:
                return None
            heredoc = m
            i = m.end()
            continue
        if c == "\n":
            if heredoc is None:
                return None
            return text[:heredoc.start()] if _heredoc_ok(text[i + 1 :], heredoc) else None
        if c in ";&|>`" or text.startswith(("$(", "<("), i):
            return None
        i += 1
    return text if heredoc is None else None


def _heredoc_ok(rest: str, m: "re.Match[str]") -> bool:
    """The heredoc body ends with its delimiter and nothing follows; unquoted, it expands nothing."""
    lines = rest.split("\n")
    strip_tabs, quoted, delimiter = m.group(1) == "-", bool(m.group(2)), m.group(3)
    for k, line in enumerate(lines):
        if (line.lstrip("\t") if strip_tabs else line).rstrip("\r") == delimiter:
            if any(more.strip() for more in lines[k + 1 :]):
                return False
            body = "\n".join(lines[:k])
            return quoted or not ("`" in body or "$(" in body)
    return False


def pre_tool_use(con, me, payload: Dict[str, Any], cwd: str, command: str) -> Optional[Dict[str, Any]]:
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if me["paused_at"] is not None:
        if tool in SHELL_TOOLS and is_board_command(tool_input.get("command") or ""):
            return None
        return _deny(render.paused_notice(con, me, command))
    mode = db.get_config(con, "enforce")
    if mode == "off":
        return None
    if tool in EDIT_TOOLS:
        target = _target(tool_input)
        return _check_edit(con, me, target, cwd, mode, command) if target else None
    if tool in SHELL_TOOLS:
        return _check_git(con, me, tool_input.get("command") or "", cwd, mode, command)
    return None


def _check_edit(con, me, target: str, cwd: str, mode: str, command: str) -> Optional[Dict[str, Any]]:
    key = path_key(target, cwd)
    blocking = [c for c in model.claims_covering(con, key, me["id"]) if not c["soft"]]
    if not blocking:
        return None
    # A claim held by a human always holds; between agents, warn mode lets a retry through.
    claim = next((c for c in blocking if c["agent_kind"] == "human"), blocking[0])
    strict = mode == "block" or claim["agent_kind"] == "human"
    shown = relative_to(display_path(target, cwd), me["worktree"])
    resource = relative_to(claim["resource"], claim["agent_worktree"] or me["worktree"])
    gate = f"claim:{claim['id']}"
    window = db.config_number(con, "gate_minutes") * 60
    if not strict and model.gate_passed(con, me["id"], gate, window):
        if not model.gate_passed(con, me["id"], f"edited:{claim['id']}", window):
            model.log_event(
                con,
                "override",
                f"@{me['name']} edited {shown} inside @{claim['agent_name']}'s claim after the warning",
                agent_id=me["id"],
                other_id=claim["agent_id"],
            )
        _notify_once(
            con,
            me,
            claim["agent_id"],
            f"edited:{claim['id']}",
            window,
            f"@{me['name']} is editing {shown} inside your claim on {resource} (after being asked to coordinate).",
        )
        return None
    first_time = not model.gate_passed(con, me["id"], gate, window)
    model.set_gate(con, me["id"], gate)
    if first_time:
        with db.tx(con):
            model.notify(
                con,
                claim["agent_id"],
                "claim",
                text=f"@{me['name']} wants to edit {shown}, inside your claim on {resource}. "
                "Answer them on the board, or release the claim if you're done with it.",
            )
            model.event(
                con,
                "blocked",
                f"@{me['name']} was stopped from editing {shown} (claimed by @{claim['agent_name']})",
                agent_id=me["id"],
                other_id=claim["agent_id"],
            )
    return _deny(render.claim_block(claim, shown, resource, strict, window, command))


def _check_git(con, me, shell_command: str, cwd: str, mode: str, command: str) -> Optional[Dict[str, Any]]:
    ops = risky_git_ops(shell_command)
    if not ops:
        return None
    ws = locate(cwd)
    if not ws.is_git:
        return None
    worktree_key = path_key(ws.root)
    others = model.active_in_worktree(con, worktree_key, me["id"])
    git_claims = [
        c for c in model.claims_covering(con, path_key(ws.root + "/.git"), me["id"]) if not c["soft"]
    ]
    if not others and not git_claims:
        return None
    sub, why = ops[0]
    gate = f"git:{worktree_key}:{sub}"
    window = db.config_number(con, "gate_minutes") * 60
    strict = bool(git_claims) and (mode == "block" or any(c["agent_kind"] == "human" for c in git_claims))
    if not strict and model.gate_passed(con, me["id"], gate, window):
        return None
    first_time = not model.gate_passed(con, me["id"], gate, window)
    model.set_gate(con, me["id"], gate)
    if first_time:
        names = ", ".join(f"@{a['name']}" for a in others) or ", ".join(f"@{c['agent_name']}" for c in git_claims)
        where = os.path.basename(ws.root.rstrip("/\\")) or ws.root
        model.log_event(
            con, "blocked", f"@{me['name']} was stopped from running `git {sub}` ({names} active in {where})", agent_id=me["id"]
        )
    return _deny(render.git_block(sub, why, ws.root, others, git_claims, strict, window, command))


# ---------------------------------------------------------------- after a tool ran


def post_tool_use(con, me, payload: Dict[str, Any], cwd: str) -> List[str]:
    """Track edits and git commands; return heads-ups for the agent."""
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if tool in EDIT_TOOLS and _target(tool_input):
        return _after_edit(con, me, _target(tool_input), cwd)
    if tool in SHELL_TOOLS:
        _after_shell(con, me, tool_input.get("command") or "", cwd)
    return []


def _after_edit(con, me, target: str, cwd: str) -> List[str]:
    key = path_key(target, cwd)
    shown_abs = display_path(target, cwd)
    shown = relative_to(shown_abs, me["worktree"])
    model.touch_file(con, me["id"], shown_abs, key, locate(shown_abs))
    window = db.config_number(con, "touch_minutes") * 60
    notes: List[str] = []
    seen = set()
    t = time.time()
    for c in model.claims_covering(con, key, me["id"]):
        if not c["soft"] or c["agent_id"] in seen:
            continue  # explicit claims were dealt with before the edit
        seen.add(c["agent_id"])
        gate = f"overlap:{key}:{c['agent_id']}"
        if model.gate_passed(con, me["id"], gate, window):
            continue
        model.set_gate(con, me["id"], gate)
        when = render.ago(c["expires_at"] - c["ttl"], t)
        status = f' (status: "{c["agent_status"]}")' if c["agent_status"] else ""
        notes.append(
            f"[message board] Heads-up: @{c['agent_name']}{status} also edited {shown} {when}. "
            "Make sure your changes don't collide with theirs; coordinate on the board if they might."
        )
        with db.tx(con):
            model.notify(
                con,
                c["agent_id"],
                "overlap",
                text=f"@{me['name']} just edited {shown}, which you edited {when}. "
                "Check that your changes still fit together.",
            )
            model.event(
                con,
                "overlap",
                f"@{me['name']} and @{c['agent_name']} both edited {shown}",
                agent_id=me["id"],
                other_id=c["agent_id"],
            )
    notes += _other_worktrees(con, me, key, shown, window)
    return notes


def _other_worktrees(con, me, key: str, shown: str, window: float) -> List[str]:
    """Heads-ups (never stops) when the same file is claimed or being edited in another worktree."""
    notes: List[str] = []
    seen = set()
    t = time.time()
    for c in model.claims_in_other_worktrees(con, key, me["id"]):
        if c["agent_id"] in seen:
            continue
        seen.add(c["agent_id"])
        gate = f"worktree:{key}:{c['agent_id']}"
        if model.gate_passed(con, me["id"], gate, window):
            continue
        model.set_gate(con, me["id"], gate)
        where = c["agent_worktree"] or "another worktree"
        if c["soft"]:
            what = f"also edited {shown} {render.ago(c['expires_at'] - c['ttl'], t)}"
            theirs = "which you also edited"
        else:
            note = f' ("{c["note"]}")' if c["note"] else ""
            what = f"has claimed {relative_to(c['resource'], c['agent_worktree'])}{note}, which covers {shown}"
            theirs = f"inside your claim on {relative_to(c['resource'], c['agent_worktree'])}"
        notes.append(
            f"[message board] Heads-up: @{c['agent_name']} {what}, in another worktree of this repository "
            f"({where}). You can't overwrite each other's files, but expect a merge conflict when the branches "
            "are merged. Coordinate on the board if it matters."
        )
        with db.tx(con):
            model.notify(
                con,
                c["agent_id"],
                "overlap",
                text=f"@{me['name']} is editing {shown} in another worktree ({me['worktree']}), {theirs}. "
                "Expect a merge conflict when the branches are merged.",
            )
            model.event(
                con,
                "overlap",
                f"@{me['name']} and @{c['agent_name']} are both changing {shown}, in different worktrees",
                agent_id=me["id"],
                other_id=c["agent_id"],
            )
    return notes


def _after_shell(con, me, shell_command: str, cwd: str) -> None:
    ops = risky_git_ops(shell_command)
    if not ops:
        return
    ws = locate(cwd)
    if not ws.is_git:
        return
    others = model.active_in_worktree(con, path_key(ws.root), me["id"])
    if not others:
        return
    subs = ", ".join(f"git {sub}" for sub, _ in ops)
    with db.tx(con):
        for other in others:
            model.notify(
                con,
                other["id"],
                "git",
                text=f"@{me['name']} ran `{subs}` in {ws.root}. Check that your uncommitted work is intact.",
            )
        names = ", ".join(f"@{o['name']}" for o in others)
        where = os.path.basename(ws.root.rstrip("/\\")) or ws.root
        model.event(con, "git", f"@{me['name']} ran `{subs}` in {where} while {names} were active", agent_id=me["id"])


def _log_error(ctx: Any, event: str) -> None:
    import traceback

    try:
        os.makedirs(ctx.home, exist_ok=True)
        path = os.path.join(ctx.home, "hook-errors.log")
        if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {event}\n{traceback.format_exc()}\n")
    except Exception:  # noqa: BLE001
        pass
