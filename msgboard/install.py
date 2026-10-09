"""Adding and removing the Claude Code hooks and the `board` command."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model import BoardError

PROJECT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT / "board.py"
EVENT_ARGS = ("session-start", "user-prompt-submit", "pre-tool-use", "post-tool-use", "session-end")

# (Claude Code event, matcher, our event argument, timeout in seconds)
HOOKS = [
    ("SessionStart", None, "session-start", 30),
    ("UserPromptSubmit", None, "user-prompt-submit", 15),
    # Edits and shell commands get coordination checks; while an agent is paused, subagents
    # (Agent/Task) and MCP tools are refused too.
    ("PreToolUse", "^(Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell|Agent|Task|mcp__.*)$", "pre-tool-use", 15),
    ("PostToolUse", "*", "post-tool-use", 15),
    ("SessionEnd", None, "session-end", None),
]

SHIM_MARKER = "message-board launcher"


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def handler(event_arg: str, timeout: Optional[int]) -> Dict[str, Any]:
    # Exec form (command + args): Claude Code starts python directly, with no shell in between.
    # -S skips site-packages (the board only needs the standard library) to start faster.
    entry: Dict[str, Any] = {
        "type": "command",
        "command": sys.executable,
        "args": ["-S", str(SCRIPT), "hook", event_arg],
    }
    if timeout:
        entry["timeout"] = timeout
    return entry


def is_ours(h: Dict[str, Any]) -> bool:
    args = [str(a) for a in h.get("args") or []]
    if len(args) >= 2 and args[-2] == "hook" and args[-1] in EVENT_ARGS and "board" in " ".join(args).lower():
        return True
    command = str(h.get("command") or "")
    return "board" in command.lower() and any(command.rstrip().endswith(f"hook {e}") for e in EVENT_ARGS)


def without_ours(settings: Dict[str, Any]) -> Dict[str, Any]:
    data = copy.deepcopy(settings)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data
    for event in list(hooks):
        groups = []
        for group in hooks[event] or []:
            kept = [h for h in group.get("hooks", []) if not is_ours(h)]
            if kept:
                groups.append({**group, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        del data["hooks"]
    return data


def with_ours(settings: Dict[str, Any]) -> Dict[str, Any]:
    data = without_ours(settings)
    hooks = data.setdefault("hooks", {})
    for event, matcher, arg, timeout in HOOKS:
        group: Dict[str, Any] = {"hooks": [handler(arg, timeout)]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
    return data


def load_settings(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise BoardError(f"{path} isn't valid JSON ({e}); fix it first, nothing was changed.") from None
    if not isinstance(data, dict):
        raise BoardError(f"{path} doesn't contain a JSON object; nothing was changed.")
    return data


def save_settings(path: Path, data: Dict[str, Any]) -> Optional[Path]:
    """Write settings atomically, keeping a timestamped backup of the previous file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def install(settings_path: Path, dry_run: bool = False, command: bool = True) -> List[str]:
    updated = with_ours(load_settings(settings_path))
    if dry_run:
        return [f"Would set these hooks in {settings_path}:", json.dumps(updated["hooks"], indent=2)]
    backup = save_settings(settings_path, updated)
    lines = [f"Hooks installed in {settings_path}" + (f" (backup: {backup.name})" if backup else "") + "."]
    if command:
        lines += install_command()
    lines.append("Running Claude Code sessions reload settings within a few seconds; `board doctor` checks everything.")
    return lines


def uninstall(settings_path: Path, command: bool = True) -> List[str]:
    lines = []
    current = load_settings(settings_path)
    updated = without_ours(current)
    if updated != current:
        backup = save_settings(settings_path, updated)
        lines.append(f"Hooks removed from {settings_path}" + (f" (backup: {backup.name})" if backup else "") + ".")
    else:
        lines.append(f"No board hooks in {settings_path}.")
    if command:
        lines += uninstall_command()
    lines.append(f"The board data stays in {Path.home() / '.message-board'}; delete that folder to erase it.")
    return lines


# ---------------------------------------------------------------- the `board` command


def _on_path(folder: Path) -> bool:
    wanted = os.path.normcase(str(folder.resolve()))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and os.path.normcase(os.path.abspath(entry)) == wanted:
            return True
    return False


def _shim_folder() -> Optional[Path]:
    candidates = [Path.home() / ".local" / "bin", Path(sys.executable).parent / "Scripts"]
    for folder in candidates:
        if folder.is_dir() and _on_path(folder) and os.access(folder, os.W_OK):
            return folder
    return None


def install_command() -> List[str]:
    found = shutil.which("board")
    if found:
        return [f"`board` command: already available ({found})."]
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-e", str(PROJECT)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return [f"`board` command: installed with pip (editable, so it tracks {PROJECT})."]
    reason = (result.stderr or result.stdout).strip().splitlines()
    lines = [f"`board` command: pip install failed ({reason[-1] if reason else 'unknown error'})."]
    folder = _shim_folder()
    if folder is None:
        lines.append(f"  No writable folder on PATH found; agents will be told to run `python {SCRIPT.as_posix()}`.")
        return lines
    python, script = sys.executable, str(SCRIPT)
    if os.name == "nt":
        (folder / "board.cmd").write_text(
            f'@echo off\r\nrem {SHIM_MARKER}\r\n"{python}" "{script}" %*\r\n', encoding="utf-8"
        )
        drive_python = "/" + python[0].lower() + python[2:].replace("\\", "/") if python[1:2] == ":" else python
        (folder / "board").write_text(
            f'#!/bin/sh\n# {SHIM_MARKER}\nexec "{drive_python}" "{SCRIPT.as_posix()}" "$@"\n', encoding="utf-8"
        )
        lines.append(f"  Wrote launchers {folder / 'board.cmd'} and {folder / 'board'} instead.")
    else:
        shim = folder / "board"
        shim.write_text(f'#!/bin/sh\n# {SHIM_MARKER}\nexec "{python}" "{script}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        lines.append(f"  Wrote launcher {shim} instead.")
    return lines


def uninstall_command() -> List[str]:
    lines = []
    show = subprocess.run(
        [sys.executable, "-m", "pip", "show", "message-board"], capture_output=True, text=True
    )
    if show.returncode == 0:
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "-q", "message-board"], capture_output=True, text=True
        )
        lines.append("`board` command: pip package removed.")
    for folder in (Path.home() / ".local" / "bin", Path(sys.executable).parent / "Scripts"):
        for name in ("board", "board.cmd"):
            shim = folder / name
            try:
                if shim.is_file() and SHIM_MARKER in shim.read_text(encoding="utf-8", errors="replace"):
                    shim.unlink()
                    lines.append(f"Removed launcher {shim}.")
            except OSError:
                pass
    return lines or ["`board` command: nothing to remove."]


def describe(settings_path: Path) -> List[str]:
    """Installation status, for `board doctor`."""
    try:
        settings = load_settings(settings_path)
    except BoardError as e:
        return [f"hooks: PROBLEM: {e}"]
    hooks = settings.get("hooks") or {}
    lines = []
    found = 0
    for event, _, arg, _ in HOOKS:
        mine = [h for g in hooks.get(event, []) for h in g.get("hooks", []) if is_ours(h)]
        if not mine:
            continue
        found += 1
        h = mine[0]
        scripts = [str(a) for a in h.get("args") or [] if str(a).endswith(".py")]
        if h.get("command") and not Path(h["command"]).exists():
            lines.append(f"hooks: PROBLEM: {event} runs {h['command']}, which doesn't exist")
        elif scripts and not Path(scripts[0]).exists():
            lines.append(f"hooks: PROBLEM: {event} runs {scripts[0]}, which doesn't exist")
    if found == 0:
        lines.insert(0, f"hooks: not installed in {settings_path} (run `board install`)")
    elif found < len(HOOKS):
        lines.insert(0, f"hooks: PROBLEM: only {found}/{len(HOOKS)} installed in {settings_path} (rerun `board install`)")
    else:
        lines.insert(0, f"hooks: installed in {settings_path}")
    return lines
