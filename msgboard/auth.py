"""The human passphrase: how the board tells a human from an agent.

Agents run as the same OS user as the human, so nothing on disk is out of their reach. What
they don't have is the passphrase. Only a salted hash is stored, in its own file next to the
database. Deleting that file resets it: the next human action asks for a new one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from typing import Optional

from . import db, model
from .model import BoardError

FILE = "human-passphrase"
ITERATIONS = 600_000
MIN_LENGTH = 8
_MARKER = "passphrase_salt"  # config row: the salt of the passphrase the board last saw


def path(home: str) -> str:
    return os.path.join(home, FILE)


def is_set(home: str) -> bool:
    return os.path.isfile(path(home))


def _hash(passphrase: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), bytes.fromhex(salt), iterations).hex()


def _load(home: str) -> Optional[dict]:
    try:
        with open(path(home), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def set_passphrase(home: str, passphrase: str) -> None:
    if len(passphrase) < MIN_LENGTH:
        raise BoardError(f"The passphrase needs at least {MIN_LENGTH} characters.")
    salt = secrets.token_hex(16)
    data = {
        "scheme": "pbkdf2_sha256",
        "iterations": ITERATIONS,
        "salt": salt,
        "hash": _hash(passphrase, salt, ITERATIONS),
        "set_at": time.time(),
    }
    os.makedirs(home, exist_ok=True)
    tmp = path(home) + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path(home))


def verify(home: str, passphrase: str) -> bool:
    data = _load(home)
    if data is None:
        return False
    try:
        got = _hash(passphrase, str(data["salt"]), int(data["iterations"]))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(got, str(data.get("hash", "")))


def _marker(con: sqlite3.Connection) -> Optional[str]:
    row = con.execute("SELECT value FROM config WHERE key = ?", (_MARKER,)).fetchone()
    return row[0] if row else None


def _remember(con: sqlite3.Connection, home: str) -> None:
    data = _load(home) or {}
    con.execute("INSERT OR REPLACE INTO config(key, value) VALUES (?, ?)", (_MARKER, str(data.get("salt", ""))))


def record_new(con: sqlite3.Connection, home: str, human: str, changed: bool = False) -> None:
    """Log a passphrase that was just set: the first one, a change, or a reset after deletion."""
    with db.tx(con):
        if changed:
            what = f"@{human} changed the human passphrase"
        elif _marker(con) is not None:
            what = f"The human passphrase was reset (its file had been deleted) and @{human} set a new one"
        else:
            what = f"@{human} set the human passphrase"
        model.event(con, "passphrase", what)
        _remember(con, home)


def check_unchanged(con: sqlite3.Connection, home: str) -> None:
    """After a successful check: log it if the passphrase file was replaced outside the board."""
    data = _load(home) or {}
    marker = _marker(con)
    if marker == data.get("salt"):
        return
    with db.tx(con):
        if marker is not None:
            model.event(con, "passphrase", "The human passphrase file was replaced outside the board")
        _remember(con, home)
