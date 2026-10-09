import os
import shutil
import tempfile
import unittest
from pathlib import Path

from msgboard.workspace import (
    Resource,
    locate,
    overlaps,
    parse_resource,
    path_key,
    path_matches,
    risky_git_ops,
    to_native,
)


class LocateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="msgboard-ws-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_main_checkout(self):
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src" / "deep").mkdir(parents=True)
        ws = locate(str(repo / "src" / "deep"))
        self.assertTrue(ws.is_git)
        self.assertEqual(path_key(ws.root), path_key(str(repo)))
        self.assertEqual(path_key(ws.repo), path_key(str(repo)))
        self.assertEqual(ws.name, "repo")

    def test_linked_worktree_points_at_main_checkout(self):
        repo = self.tmp / "repo"
        gitdir = repo / ".git" / "worktrees" / "feature"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n")
        wt = self.tmp / "repo-feature"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n")
        ws = locate(str(wt))
        self.assertEqual(path_key(ws.root), path_key(str(wt)))
        self.assertEqual(path_key(ws.repo), path_key(str(repo)))
        self.assertEqual(ws.key, "repo:" + path_key(str(repo)))

    def test_submodule_is_its_own_repo(self):
        sub = self.tmp / "super" / "lib"
        sub.mkdir(parents=True)
        modules = self.tmp / "super" / ".git" / "modules" / "lib"
        modules.mkdir(parents=True)
        (sub / ".git").write_text("gitdir: ../.git/modules/lib\n")
        ws = locate(str(sub))
        self.assertEqual(path_key(ws.repo), path_key(str(sub)))

    def test_plain_folder_and_missing_file(self):
        folder = self.tmp / "plain"
        folder.mkdir()
        ws = locate(str(folder / "not" / "there.txt"))
        self.assertFalse(ws.is_git)
        self.assertEqual(path_key(ws.root), path_key(str(folder)))


class ResourceTest(unittest.TestCase):
    def test_named_resources(self):
        r = parse_resource("port:5173", "/x")
        self.assertFalse(r.is_path)
        self.assertEqual(r.key, "port:5173")
        self.assertTrue(parse_resource("DB:Test", "/x").key == "db:test")

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_windows_paths(self):
        r = parse_resource(r"C:\Git\Repo\src\**", r"C:\elsewhere")
        self.assertTrue(r.is_path)
        self.assertEqual(r.key, "c:/git/repo/src/**")
        self.assertEqual(r.text, "C:/Git/Repo/src/**")
        self.assertEqual(to_native("/c/git/repo"), "C:/git/repo")
        self.assertEqual(path_key("/c/Git/Repo/a.py"), "c:/git/repo/a.py")

    def test_relative_paths_resolve_against_cwd(self):
        cwd = os.path.abspath(os.sep + "work")
        r = parse_resource("src/a.py", cwd)
        self.assertEqual(r.key, path_key(os.path.join(cwd, "src", "a.py")))


class MatchTest(unittest.TestCase):
    def test_path_matches(self):
        self.assertTrue(path_matches("/r/src", "/r/src/a.py"))
        self.assertTrue(path_matches("/r/src", "/r/src"))
        self.assertFalse(path_matches("/r/src", "/r/srcx/a.py"))
        self.assertTrue(path_matches("/r/src/*.py", "/r/src/a.py"))
        self.assertFalse(path_matches("/r/src/*.py", "/r/src/sub/a.py"))
        self.assertTrue(path_matches("/r/src/**/*.py", "/r/src/a.py"))
        self.assertTrue(path_matches("/r/src/**/*.py", "/r/src/x/y/a.py"))
        self.assertTrue(path_matches("/r/src/**", "/r/src/x/y"))
        self.assertTrue(path_matches("/r/a?.md", "/r/ab.md"))
        self.assertTrue(path_matches("/r/[ab].md", "/r/b.md"))
        self.assertFalse(path_matches("/r/[!ab].md", "/r/b.md"))

    def test_overlaps(self):
        P = lambda k: Resource(k, k, True)  # noqa: E731
        N = lambda k: Resource(k, k, False)  # noqa: E731
        self.assertTrue(overlaps(P("/r/src"), P("/r/src/a.py")))
        self.assertFalse(overlaps(P("/r/src/a.py"), P("/r/src/b.py")))
        self.assertTrue(overlaps(P("/r/src/a.py"), P("/r/src/*.py")))
        self.assertFalse(overlaps(P("/r/src/a.py"), P("/r/src/*.md")))
        self.assertTrue(overlaps(P("/r/src"), P("/r/src/**/*.md")))
        self.assertFalse(overlaps(P("/r/lib"), P("/r/src/**")))
        self.assertTrue(overlaps(P("/r/src/**"), P("/r/src/x/**")))
        self.assertFalse(overlaps(P("/r/src/**"), P("/r/docs/**")))
        self.assertTrue(overlaps(N("port:5173"), N("port:5173")))
        self.assertTrue(overlaps(N("port:*"), N("port:5173")))
        self.assertFalse(overlaps(N("port:5173"), N("port:3000")))
        self.assertFalse(overlaps(N("port:5173"), P("/r/port:5173")))


class GitTest(unittest.TestCase):
    def subs(self, command):
        return [s for s, _ in risky_git_ops(command)]

    def test_risky(self):
        self.assertEqual(self.subs("git stash"), ["stash"])
        self.assertEqual(self.subs("git -C ../x checkout main"), ["checkout"])
        self.assertEqual(self.subs("git add -A && git commit -m 'x'"), ["add"])
        self.assertEqual(self.subs("git commit -am 'msg'"), ["commit"])
        self.assertEqual(self.subs("git commit --amend --no-edit"), ["commit"])
        self.assertEqual(self.subs("cd src; git reset --hard HEAD~1"), ["reset"])
        self.assertEqual(self.subs("C:/Program/git.exe pull"), ["pull"])
        self.assertEqual(self.subs("git --no-pager -c core.x=1 rebase main"), ["rebase"])

    def test_harmless(self):
        self.assertEqual(self.subs("git status"), [])
        self.assertEqual(self.subs("git stash list"), [])
        self.assertEqual(self.subs("git add src/a.py"), [])
        self.assertEqual(self.subs("git commit -m 'fix git checkout bug'"), [])
        self.assertEqual(self.subs('echo "git stash"'), [])
        self.assertEqual(self.subs("git log --oneline | head"), [])
        self.assertEqual(self.subs("legit stash"), [])


if __name__ == "__main__":
    unittest.main()
