"""Finding repositories and worktrees, normalizing paths and matching claims."""

from __future__ import annotations

import fnmatch
import os
import re
from functools import lru_cache
from typing import List, NamedTuple, Optional, Tuple

GLOB_CHARS = "*?["

# "name:value" with a name of 2+ characters, so a Windows drive ("C:\...") stays a path.
_NAMED_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]+):(.+)$")
_MSYS_RE = re.compile(r"^/([A-Za-z])(/.*)?$")


def to_native(path: str) -> str:
    """Translate Git-Bash style /c/foo paths to C:/foo on Windows."""
    if os.name == "nt":
        m = _MSYS_RE.match(path)
        if m:
            return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return path


def absolute(path: str, cwd: str) -> str:
    path = to_native(os.path.expanduser(path))
    return os.path.abspath(os.path.join(to_native(cwd), path))


def _clean(p: str) -> str:
    p = p.replace("\\", "/")
    return p.rstrip("/") if len(p) > 3 else p


def path_key(path: str, cwd: Optional[str] = None) -> str:
    """Normalized absolute form used for comparisons (case-folded on Windows, forward slashes)."""
    return _clean(os.path.normcase(absolute(path, cwd or os.getcwd())))


def display_path(path: str, cwd: Optional[str] = None) -> str:
    return _clean(absolute(path, cwd or os.getcwd()))


def relative_to(path_text: str, root: Optional[str]) -> str:
    """Show path_text relative to root when it lies inside it."""
    if not root:
        return path_text
    root_text = _clean(root)
    if os.path.normcase(path_text).replace("\\", "/").startswith(
        os.path.normcase(root_text).replace("\\", "/") + "/"
    ):
        return path_text[len(root_text) + 1 :]
    return path_text


# ---------------------------------------------------------------- repositories


class Workspace(NamedTuple):
    root: str  # the worktree (or plain directory) the path is in
    repo: str  # main checkout shared by all worktrees of the repository
    is_git: bool

    @property
    def name(self) -> str:
        return os.path.basename(self.repo.rstrip("/\\")) or self.repo

    @property
    def key(self) -> str:
        return ("repo:" if self.is_git else "dir:") + path_key(self.repo)


def locate(start: str) -> Workspace:
    start = os.path.abspath(to_native(start))
    d = start
    while not os.path.isdir(d):  # the path may not exist yet (a file about to be written)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    cur = d
    while True:
        dot = os.path.join(cur, ".git")
        if os.path.isdir(dot):
            return Workspace(_clean(cur), _clean(cur), True)
        if os.path.isfile(dot):
            return Workspace(_clean(cur), _clean(_main_checkout(cur, dot)), True)
        parent = os.path.dirname(cur)
        if parent == cur:
            return Workspace(_clean(d), _clean(d), False)
        cur = parent


def _main_checkout(root: str, dotgit_file: str) -> str:
    """For a linked worktree, the main checkout; for a submodule, the submodule itself."""
    text = _read(dotgit_file)
    if text is None or not text.startswith("gitdir:"):
        return root
    gitdir = os.path.normpath(os.path.join(root, text[len("gitdir:") :].strip()))
    common = _read(os.path.join(gitdir, "commondir"))
    if common is None:
        return root
    common = os.path.normpath(os.path.join(gitdir, common))
    if os.path.basename(common).lower() == ".git":
        return os.path.dirname(common)
    return root


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


# ---------------------------------------------------------------- claims


class Resource(NamedTuple):
    text: str  # as shown to people
    key: str  # as compared
    is_path: bool


def parse_resource(spec: str, cwd: str) -> Resource:
    """A path/glob (relative to cwd) or a named resource such as port:5173."""
    spec = spec.strip()
    if _NAMED_RE.match(spec):
        return Resource(spec, spec.lower(), False)
    return Resource(display_path(spec, cwd), path_key(spec, cwd), True)


def is_glob(key: str) -> bool:
    return any(c in key for c in GLOB_CHARS)


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> "re.Pattern[str]":
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    # A pattern that matches a directory also covers everything inside it.
    return re.compile("".join(out) + r"(?:/.*)?\Z")


def path_matches(pattern_key: str, key: str) -> bool:
    """Whether path `key` is covered by the claim pattern `pattern_key`."""
    if not is_glob(pattern_key):
        return key == pattern_key or key.startswith(pattern_key + "/")
    return bool(_glob_regex(pattern_key).match(key))


def literal_base(key: str) -> str:
    parts = []
    for part in key.split("/"):
        if is_glob(part):
            break
        parts.append(part)
    return "/".join(parts)


def _under(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/")


def overlaps(a: Resource, b: Resource) -> bool:
    """Whether two claims could cover a common path (conservative for two globs)."""
    if a.is_path != b.is_path:
        return False
    if not a.is_path:
        return fnmatch.fnmatchcase(a.key, b.key) or fnmatch.fnmatchcase(b.key, a.key)
    ga, gb = is_glob(a.key), is_glob(b.key)
    if not ga and not gb:
        return _under(a.key, b.key) or _under(b.key, a.key)
    if not ga:
        return path_matches(b.key, a.key) or _under(literal_base(b.key), a.key)
    if not gb:
        return path_matches(a.key, b.key) or _under(literal_base(a.key), b.key)
    base_a, base_b = literal_base(a.key), literal_base(b.key)
    return _under(base_a, base_b) or _under(base_b, base_a)


# ---------------------------------------------------------------- git commands

GIT_RISKS = {
    "stash": "stashes uncommitted changes across the whole worktree, including other agents' edits",
    "checkout": "can switch the branch or overwrite files for everyone in the worktree",
    "switch": "switches the branch for everyone in the worktree",
    "reset": "moves HEAD and can discard uncommitted changes",
    "restore": "overwrites working files with committed or staged versions",
    "clean": "deletes untracked files, including other agents' new files",
    "rebase": "rewrites the branch and the working files",
    "merge": "changes the working files and can stop with conflicts",
    "pull": "merges or rebases remote changes into the working files",
    "cherry-pick": "changes the working files and can stop with conflicts",
    "revert": "changes the working files and creates commits",
    "am": "applies patches to the working files and creates commits",
    "add": "stages every change in the worktree, including other agents' work",
    "commit": "commits every change in the worktree (or rewrites the last commit), "
    "including other agents' work",
}

_GIT_GLOBAL_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_QUOTED_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_SEPARATOR_RE = re.compile(r"&&|\|\||[;|\n&]")


def risky_git_ops(command: str) -> List[Tuple[str, str]]:
    """(subcommand, why) for git commands that affect the whole worktree."""
    text = _QUOTED_RE.sub("''", command or "")
    found: List[Tuple[str, str]] = []
    for segment in _SEPARATOR_RE.split(text):
        tokens = segment.split()
        for i, token in enumerate(tokens):
            name = token.lower().replace("\\", "/").rsplit("/", 1)[-1]
            if name not in ("git", "git.exe"):
                continue
            rest = tokens[i + 1 :]
            j = 0
            while j < len(rest) and rest[j].startswith("-"):
                j += 2 if rest[j] in _GIT_GLOBAL_WITH_VALUE else 1
            if j < len(rest) and _is_risky(rest[j].lower(), rest[j + 1 :]):
                sub = rest[j].lower()
                found.append((sub, GIT_RISKS[sub]))
            break
    return found


def _is_risky(sub: str, args: List[str]) -> bool:
    if sub not in GIT_RISKS:
        return False
    if sub == "stash":
        return not (args and args[0].lower() in ("list", "show"))
    if sub == "add":
        return any(a in ("-A", "--all", ".", ":/", "-u", "--update") for a in args)
    if sub == "commit":
        return any(
            a in ("--all", "--amend") or re.fullmatch(r"-[A-Za-z]*a[A-Za-z]*", a) for a in args
        )
    return True
