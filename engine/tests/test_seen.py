"""Since your last session: the decisions and traps teammates changed on the default branch
since this clone last saw it, and `changes.between`, the comparison of two commits under it.

    python3 -m unittest discover -s engine/tests -k test_seen
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import base, changes, seen  # noqa: E402
from base_support import LOG, Tree, decision  # noqa: E402
from changes_support import cut  # noqa: E402
from team_support import TwoClones  # noqa: E402

HEAD = "since your last session: "


class Seen(TwoClones):
    def tree(self, clone: Path) -> Tree:
        return Tree(clone, self.seed.home)

    def lines(self, clone: Path) -> list[str]:
        return seen.lines(self.tree(clone).context())

    def state(self, clone: Path) -> dict:
        path = seen.state_path(self.tree(clone).context())
        return json.loads(path.read_text(encoding="utf-8"))

    def tip(self, clone: Path) -> str:
        return self.seed.git("rev-parse", "origin/main", at=clone).strip()

    def edit(self, clone: Path, make) -> None:
        """Commit `make`'s change as the clone's own developer (the fixture's own `-c
        user.email` would name everyone `t@t`)."""
        t = self.tree(clone)
        make(t)
        t.run("index")
        self.seed.git("add", "-A", at=clone)
        who = "a@example.com" if clone == self.a else "b@example.com"
        res = subprocess.run(
            ["git", "-C", str(clone), "-c", "commit.gpgSign=false", "commit", "-qm", "change"],
            capture_output=True, cwd=self.tmp,
            env=self.git_env(GIT_AUTHOR_EMAIL=who, GIT_COMMITTER_EMAIL=who,
                             GIT_AUTHOR_NAME="x", GIT_COMMITTER_NAME="x"))
        self.assertEqual(res.returncode, 0, res.stderr)

    def supersede(self, clone: Path) -> None:
        def make(t: Tree) -> None:
            cut(t, LOG, "P-1")
            t.append(LOG, "\n## P-1 - Replaced by P-4\n" + decision("P", 4, title="The new rule"))
        self.edit(clone, make)
        self.push(clone)

    def test_first_run_records_only(self):
        self.assertEqual(self.lines(self.a), [])
        self.assertEqual(self.state(self.a), {"branch": "main", "commit": self.tip(self.a)})

    def test_a_superseding_decision_is_named_with_its_author(self):
        self.assertEqual(self.lines(self.a), [])
        self.supersede(self.b)
        self.fetch(self.a)
        got = self.lines(self.a)
        self.assertIn(f"{HEAD}P-1 superseded (b)", got)
        self.assertEqual(got, [f"{HEAD}P-1 superseded (b)", f"{HEAD}P-4 added (b)"])
        self.assertEqual(self.state(self.a)["commit"], self.tip(self.a))
        self.assertEqual(self.lines(self.a), [])

    def test_twelve_changes_give_ten_lines_and_a_tail(self):
        self.lines(self.a)

        def make(t: Tree) -> None:
            for n in range(10, 22):
                t.append(LOG, decision("P", n))
        self.edit(self.b, make)
        self.push(self.b)
        self.fetch(self.a)
        got = self.lines(self.a)
        self.assertEqual(len(got), 11)
        self.assertEqual(got[0], f"{HEAD}P-10 added (b)")
        self.assertEqual(got[9], f"{HEAD}P-19 added (b)")
        self.assertEqual(got[10], "and 2 more changes: run python3 .context-gate/bin/govern diff")

    def test_own_changes_are_not_listed(self):
        self.lines(self.a)
        self.edit(self.a, lambda t: t.append(LOG, decision("P", 4)))
        self.push(self.a)
        self.fetch(self.a)
        self.assertEqual(self.lines(self.a), [])
        self.assertEqual(self.state(self.a)["commit"], self.tip(self.a))

    def test_unknown_old_commit_records_only(self):
        self.lines(self.a)
        path = seen.state_path(self.tree(self.a).context())
        path.write_text(json.dumps({"branch": "main", "commit": "0123456789" * 4}),
                        encoding="utf-8")
        self.supersede(self.b)
        self.fetch(self.a)
        self.assertEqual(self.lines(self.a), [])
        self.assertEqual(self.state(self.a)["commit"], self.tip(self.a))

    def test_another_branch_in_the_state_records_only(self):
        self.lines(self.a)
        path = seen.state_path(self.tree(self.a).context())
        path.write_text(json.dumps({"branch": "trunk", "commit": self.tip(self.a)}),
                        encoding="utf-8")
        self.supersede(self.b)
        self.fetch(self.a)
        self.assertEqual(self.lines(self.a), [])
        self.assertEqual(self.state(self.a), {"branch": "main", "commit": self.tip(self.a)})

    def test_a_status_change_names_its_author(self):
        self.lines(self.a)

        def status(t: Tree) -> None:
            text = t.read(LOG)
            head, mark, rest = text.partition("## P-3 - Title 3")
            t.write(LOG, head + mark + rest.replace("**Status:** locked",
                                                     "**Status:** provisional", 1))
        self.edit(self.b, status)
        self.push(self.b)
        self.fetch(self.a)
        self.seed.git("merge", "-q", "--ff-only", "origin/main", at=self.a)
        self.edit(self.a, lambda t: t.append(LOG, decision("P", 9)))
        self.push(self.a)
        self.fetch(self.a)
        got = self.lines(self.a)
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].startswith(f"{HEAD}P-3 "), got)
        self.assertTrue(got[0].endswith("(b)"), got)

    def test_state_advances_before_authors(self):
        self.lines(self.a)
        self.supersede(self.b)
        self.fetch(self.a)
        with mock.patch.object(seen, "_author", side_effect=RuntimeError("killed")):
            self.assertEqual(self.lines(self.a), [])
        self.assertEqual(self.state(self.a)["commit"], self.tip(self.a))

    def test_only_shown_rows_are_looked_up(self):
        self.lines(self.a)

        def make(t: Tree) -> None:
            for n in range(10, 25):
                t.append(LOG, decision("P", n))
        self.edit(self.b, make)
        self.push(self.b)
        self.fetch(self.a)
        with mock.patch.object(seen, "_author", wraps=seen._author) as spy:
            got = self.lines(self.a)
        self.assertLessEqual(spy.call_count, 10)
        self.assertEqual(len(got), 11)
        self.assertEqual(got[10], "and 5 more changes: run python3 .context-gate/bin/govern diff")

    def test_no_origin_head_never_runs_ls_remote(self):
        self.lines(self.a)
        self.supersede(self.b)
        self.fetch(self.a)
        self.seed.git("remote", "set-head", "origin", "-d", at=self.a)
        runs: list[tuple] = []
        real = subprocess.run

        def spy(argv, *args, **kw):
            runs.append(tuple(argv))
            return real(argv, *args, **kw)
        with mock.patch("subprocess.run", spy):
            got = self.lines(self.a)
        self.assertIn(f"{HEAD}P-1 superseded (b)", got)
        flat = [w for argv in runs for w in argv]
        self.assertNotIn("ls-remote", flat)
        self.assertNotIn("fetch", flat)

    def test_no_change_reads_refs_only(self):
        self.lines(self.a)
        runs: list[tuple] = []
        real = subprocess.run

        def spy(argv, *args, **kw):
            runs.append(tuple(argv))
            return real(argv, *args, **kw)
        with mock.patch("subprocess.run", spy):
            self.assertEqual(self.lines(self.a), [])
        self.assertTrue(runs)
        flat = {w for argv in runs for w in argv}
        self.assertFalse(flat & {"log", "ls-remote", "fetch"}, runs)
        self.assertNotIn("-p", flat)

    def test_a_corrupt_state_is_replaced(self):
        self.lines(self.a)
        path = seen.state_path(self.tree(self.a).context())
        for bad in ('{"branch": "main", "commit": 5}', "not json", "[1]"):
            path.write_text(bad, encoding="utf-8")
            self.assertEqual(self.lines(self.a), [])
            self.assertEqual(self.state(self.a),
                             {"branch": "main", "commit": self.tip(self.a)})

    def test_between_pairs_added_removed_changed(self):
        old = self.seed.git("rev-parse", "HEAD", at=self.a).strip()

        def make(t: Tree) -> None:
            cut(t, LOG, "P-2")
            t.edit(LOG, "## P-3 - Title 3", "## P-3 - Retitled three")
            t.append(LOG, decision("P", 4, title="Fresh"))
            t.append("docs/working/traps.md", "\n## T-2 - A new trap\n\n**Bites when:** now.\n")
        self.edit(self.a, make)
        new = self.seed.git("rev-parse", "HEAD", at=self.a).strip()
        rows = changes.between(self.tree(self.a).context(), old, new)
        self.assertEqual([(r.data["id"], r.data["change"]) for r in rows], [
            ("P-2", "removed"), ("P-3", "changed"), ("P-4", "added"), ("T-2", "added")])
        # Neither side is the working tree: an uncommitted edit is not in it.
        self.tree(self.a).append(LOG, decision("P", 9))
        again = changes.between(self.tree(self.a).context(), old, new)
        self.assertEqual(len(again), 4)

    def test_between_raises_for_an_unknown_commit(self):
        with self.assertRaises(base.CannotCompare):
            changes.between(self.tree(self.a).context(), "0123456789" * 4, "HEAD")


if __name__ == "__main__":
    unittest.main()
