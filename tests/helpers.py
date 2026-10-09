import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Optional, Tuple

from msgboard import cli, db


class BoardTestCase(unittest.TestCase):
    """A fresh board home and a fake git repository per test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="msgboard-test-"))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "src").mkdir()
        self.env: Dict[str, str] = {
            "MESSAGE_BOARD_HOME": str(self.home),
            "USERNAME": "tester",
            "PATH": os.environ.get("PATH", ""),
        }
        self._connections = []

    def tearDown(self) -> None:
        for con in self._connections:
            con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def con(self):
        con = db.connect(self.home)
        self._connections.append(con)
        return con

    def run_cli(
        self,
        *args: str,
        session: Optional[str] = "A",
        cwd: Optional[Path] = None,
        stdin: bytes = b"",
        env: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str, str]:
        e = dict(self.env)
        if session is not None:
            e["CLAUDECODE"] = "1"
            e["CLAUDE_CODE_SESSION_ID"] = session
        e.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            list(args),
            env=e,
            cwd=str(cwd or self.repo),
            stdin=io.BytesIO(stdin),
            stdout=out,
            stderr=err,
        )
        return code, out.getvalue(), err.getvalue()

    def ok(self, *args: str, **kw) -> str:
        code, out, err = self.run_cli(*args, **kw)
        self.assertEqual(code, 0, f"board {' '.join(args)} failed: {err}{out}")
        return out

    def hook(self, event: str, payload: Dict, session: str = "A") -> Optional[Dict]:
        payload = {"session_id": session, "cwd": str(self.repo), **payload}
        code, out, err = self.run_cli(
            "hook", event, session=session, stdin=json.dumps(payload).encode("utf-8")
        )
        self.assertEqual(code, 0)
        self.assertFalse((self.home / "hook-errors.log").exists(), self._hook_errors())
        return json.loads(out) if out.strip() else None

    def _hook_errors(self) -> str:
        path = self.home / "hook-errors.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def name_of(self, session: str) -> str:
        out = self.ok("whoami", session=session)
        return out.split("@", 1)[1].split(" ", 1)[0]
