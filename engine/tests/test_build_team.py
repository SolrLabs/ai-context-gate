"""`govern build` across two clones: a phase another developer pushed is seen from fetched
branches, kept by `phase close` and `sweep`, and its name is not reused. Nothing here fetches
except the test itself.

    python3 -m unittest discover -s engine/tests -k test_build_team
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401
from team_support import TwoClones  # noqa: E402
from base_support import Case, Tree  # noqa: E402
from test_build_workspace import Space  # noqa: E402
from govern import build, phase  # noqa: E402

LEDGER = f"{build.BUILD_DIR}/b-x/{build.LEDGER}"


class Team(TwoClones):
    def at(self, clone: Path) -> Tree:
        return Tree(clone, self.seed.home)

    def govern(self, clone: Path, *argv: str) -> tuple[int, str, str]:
        return self.at(clone).run(*argv)

    def start(self, clone: Path, phase: str, branch: str) -> None:
        self.seed.git("checkout", "-qb", branch, at=clone)
        code, out, err = self.govern(clone, "build", "start", phase)
        self.assertEqual((code, err), (0, ""), out)
        self.commit(clone, f"start {phase}")
        self.push(clone)

    def b_builds(self) -> None:
        """B starts `b-x` on `phase/b-x` and pushes; A fetches."""
        self.start(self.b, "b-x", "phase/b-x")
        self.fetch(self.a)

    def team(self, clone: Path, *flags: str) -> tuple[int, str, str]:
        return self.govern(clone, "build", "status", "--team", *flags)


class TeamStatus(Team):
    def test_team_status_lists_a_fetched_build(self):
        self.b_builds()
        code, out, err = self.team(self.a)
        self.assertEqual((code, err), (0, ""))
        fields = out.strip().split("  ")
        self.assertEqual(fields[:5], ["b-x", "phase/b-x", "b@example.com", "build", "0/0"], out)
        self.assertRegex(fields[5], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")
        code, out, err = self.team(self.a, "--json")
        self.assertEqual((code, err), (0, ""))
        rows = json.loads(out)["builds"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(rows[0]), ["branch", "done", "last_commit", "owner", "phase",
                                           "stage", "total"])
        self.assertEqual((rows[0]["phase"], rows[0]["owner"]), ("b-x", "b@example.com"))

    def test_local_ledger_wins_and_none_reads_plainly(self):
        code, out, err = self.team(self.a)
        self.assertEqual((code, out.strip(), err), (0, "no builds in flight", ""))
        code, out, err = self.team(self.a, "--json")
        self.assertEqual(json.loads(out), {"builds": []})
        self.start(self.a, "b-x", "phase/b-x")
        self.fetch(self.a)
        code, out, err = self.team(self.a, "--json")
        rows = json.loads(out)["builds"]
        self.assertEqual([(r["phase"], r["branch"], r["owner"]) for r in rows],
                         [("b-x", "phase/b-x", "a@example.com")])

    def test_a_ledger_for_another_branch_is_not_open(self):
        self.start(self.b, "b-x", "phase/b-x")
        self.seed.git("push", "-q", "origin", "HEAD:refs/heads/beta-0.9.0", at=self.b)
        self.seed.git("push", "-q", "origin", "--delete", "phase/b-x", at=self.b)
        self.fetch(self.a)
        code, out, err = self.team(self.a)
        self.assertEqual((code, out.strip(), err), (0, "no builds in flight", ""))

    def test_a_deleted_remote_branch_is_forgotten(self):
        self.b_builds()
        self.seed.git("push", "-q", "origin", "--delete", "phase/b-x", at=self.b)
        self.seed.git("fetch", "-q", "--prune", "origin", at=self.a)
        code, out, err = self.team(self.a)
        self.assertEqual((code, out.strip(), err), (0, "no builds in flight", ""))

    def test_remote_false_is_today(self):
        self.b_builds()
        found, bad = build.open_builds(self.a)
        self.assertEqual((found, bad), ([("b-x", "phase/b-x")], []))
        self.assertEqual(build.open_builds(self.a, remote=False), ([], []))
        self.assertEqual(len(build.team_ledgers(self.a)[0]), 1)


class Kept(Team):
    def own_phase(self) -> None:
        self.start(self.a, "a-y", "phase/a-y")

    def test_phase_close_keeps_a_fetched_build(self):
        self.b_builds()
        self.own_phase()
        self.seed.git("branch", "--no-track", "phase/b-x", "origin/phase/b-x", at=self.a)
        self.seed.git("branch", "--no-track", "side", "origin/phase/b-x", at=self.a)
        code, out, err = self.govern(self.a, "phase", "close", "--path", str(self.a))
        self.assertEqual((code, err), (0, ""), out)
        heads = self.seed.git("branch", "--format=%(refname:short)", at=self.a).split()
        self.assertIn("phase/b-x", heads)
        self.assertIn("side", heads)
        self.assertIn("the branch of the build b-x, which is not closed", out)
        self.assertIn("merged into phase/b-x, whose build b-x is not closed", out)

    def test_sweep_apply_keeps_the_remote_branch(self):
        self.b_builds()
        # Merged into main, so sweep would remove it from the remote were it not claimed.
        self.seed.git("push", "-q", "origin", "phase/b-x:main", at=self.b)
        self.fetch(self.a)
        self.seed.git("checkout", "-q", "main", at=self.a)
        code, out, err = self.govern(self.a, "sweep", "--apply", "--only", "origin/phase/b-x")
        self.assertEqual(err, "", out)
        listed = self.seed.git("ls-remote", "origin", "phase/b-x", at=self.a)
        self.assertIn("refs/heads/phase/b-x", listed)

    def test_start_refuses_a_fetched_phase_name(self):
        self.b_builds()
        self.seed.git("checkout", "-q", "main", at=self.a)
        self.seed.git("checkout", "-qb", "phase/mine", at=self.a)
        code, out, err = self.govern(self.a, "build", "start", "b-x")
        self.assertEqual(code, 1, out)
        self.assertIn("error: the phase b-x is in flight on phase/b-x (a fetched branch): "
                      "pick another name", err)
        self.assertFalse((self.a / LEDGER).exists())

    def test_restarting_my_own_phase_is_allowed(self):
        self.own_phase()
        self.fetch(self.a)
        code, out, err = self.govern(self.a, "build", "start", "a-y")
        self.assertEqual((code, err), (0, ""), out)

    def test_phase_close_keeps_a_branch_merged_into_a_remote_only_build(self):
        self.b_builds()
        self.own_phase()
        heads = self.seed.git("branch", "--format=%(refname:short)", at=self.a).split()
        self.assertNotIn("phase/b-x", heads)
        self.seed.git("branch", "--no-track", "side", "origin/phase/b-x", at=self.a)
        code, out, err = self.govern(self.a, "phase", "close", "--path", str(self.a))
        self.assertEqual((code, err), (0, ""), out)
        heads = self.seed.git("branch", "--format=%(refname:short)", at=self.a).split()
        self.assertIn("side", heads)
        self.assertIn("merged into phase/b-x, whose build b-x is not closed", out)


class Unreadable(Team):
    def test_an_unreadable_remote_ledger_protects_its_branch(self):
        self.seed.git("checkout", "-qb", "junk", at=self.b)
        bad = self.b / build.BUILD_DIR / "z" / build.LEDGER
        bad.parent.mkdir(parents=True)
        bad.write_text("phase = 1\n", encoding="utf-8")
        self.commit(self.b, "junk")
        self.push(self.b)
        self.start(self.a, "a-y", "phase/a-y")
        self.fetch(self.a)
        self.seed.git("branch", "--no-track", "junk", "origin/junk", at=self.a)
        code, out, err = self.govern(self.a, "phase", "close", "--path", str(self.a))
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("Traceback", err)
        self.assertIn(f"refs/remotes/origin/junk:{build.BUILD_DIR}/z/{build.LEDGER}", err)
        heads = self.seed.git("branch", "--format=%(refname:short)", at=self.a).split()
        self.assertIn("junk", heads)
        found, errors = build.open_builds(self.a)
        self.assertIn(("z", "junk"), found)
        self.assertEqual(errors, [])

    def test_a_failing_ref_listing_is_an_error(self):
        self.b_builds()
        real = phase._run

        def fail(repo, *args, **kw):
            if args and args[0] == "for-each-ref":
                return subprocess.CompletedProcess(args, 128, b"", b"fatal: boom")
            return real(repo, *args, **kw)

        with mock.patch.object(phase, "_run", fail):
            found, errors = build.open_builds(self.a)
        self.assertEqual(found, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], build.BuildError)

    def test_a_failing_git_remote_is_an_error(self):
        self.b_builds()
        real = phase._ask

        def fail(repo, *args):
            if args and args[0] == "remote":
                raise phase.PhaseError("git remote failed: boom")
            return real(repo, *args)

        with mock.patch.object(phase, "_ask", fail):
            found, errors = build.open_builds(self.a)
        self.assertEqual(found, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("git remote failed", str(errors[0]))


class Workspaces(Case):
    def test_team_json_in_a_workspace_with_nothing_in_flight(self):
        self.w = Space(self.tmp)
        code, out, err = self.w.run("build", "status", "--team", "--json")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(json.loads(out), {"builds": []})
        code, out, err = self.w.run("build", "status", "--team")
        self.assertEqual((code, out.strip(), err), (0, "no builds in flight", ""))


if __name__ == "__main__":
    unittest.main()
