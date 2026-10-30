"""A HANDOFF per owner: the `handoff` fact as a glob, ratchet keys per file, the frontmatter
`owner` and the per-owner working-file count.

    python3 -m unittest discover -s engine/tests -k test_handoff_owners
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import layout, ratchet  # noqa: E402
from base_support import Case, fm, rtext, wtext  # noqa: E402

GLOB = 'handoff = "docs/handoff/*.md"'
WORDS = "\n[checks.handoff-words]\nmax_words = 10\n"


def cfg(w, repo_fact: str, tables: str = "") -> None:
    """Add a `[repo]` fact and tables after it: the solo config ends inside `[repo]`."""
    path = w.root / layout.CONFIG
    wtext(path, rtext(path) + repo_fact + "\n" + tables)


def words(n: int) -> str:
    return "word " * n


def handoff(n: int) -> str:
    return fm("working", status="active") + words(n)


class Handoffs(Case):
    def keys(self, w) -> dict[str, int]:
        return ratchet.compute(w.context())

    def test_two_handoffs_are_ratcheted_alone(self):
        w = self.solo()
        cfg(w, GLOB, WORDS)
        wtext(w.root / "docs/handoff/ana.md", handoff(30))
        wtext(w.root / "docs/handoff/bo.md", handoff(3))
        scope = w.context().project_scopes[0].name
        key = f"handoff_words:{scope}:docs/handoff/ana.md"
        self.assertEqual(list(self.keys(w)), [key])
        _, out, _ = w.run("check")
        self.assertIn(f"{scope}: docs/handoff/ana.md is 30 words", out)
        self.assertNotIn("bo.md is", out)
        wtext(w.root / "docs/handoff/ana.md", handoff(20))
        self.assertEqual(self.keys(w), {key: 20})
        wtext(w.root / "docs/handoff/ana.md", handoff(2))
        self.assertEqual(self.keys(w), {})

    def test_a_single_path_keeps_its_key(self):
        w = self.solo()
        cfg(w, 'handoff = "docs/HANDOFF.md"', WORDS)
        wtext(w.root / "docs/HANDOFF.md", handoff(30))
        scope = w.context().project_scopes[0].name
        self.assertEqual(list(self.keys(w)), [f"handoff_words:{scope}"])
        _, out, _ = w.run("check")
        self.assertIn(f"{scope}: HANDOFF.md is 30 words, over handoff-words max_words=10", out)

    def test_owns_reads_both_shapes(self):
        w = self.solo()
        ctx = w.context()
        scope = ctx.project_scopes[0]
        n = scope.name
        self.assertTrue(ratchet.owns(ctx, scope, f"handoff_words:{n}"))
        self.assertTrue(ratchet.owns(ctx, scope, f"handoff_words:{n}:docs/handoff/ana.md"))
        self.assertFalse(ratchet.owns(ctx, scope, f"handoff_words:{n}x"))
        self.assertFalse(ratchet.owns(ctx, scope, "handoff_words:other:docs/a.md"))


class AbsoluteGlob(Case):
    def test_an_absolute_glob_warns(self):
        w = self.solo()
        cfg(w, 'handoff = "/abs/*.md"', WORDS)
        code, out, err = w.run("check")
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(out.count("handoff glob /abs/*.md must be relative to the root"), 1, out)


class Owner(Case):
    def doc(self, w, owner: str | None) -> str:
        extra = {} if owner is None else {"owner": owner}
        wtext(w.root / "docs/note.md", fm("reference", **extra) + "body\n")
        return w.run("check")[1]

    def test_owner_must_be_text(self):
        w = self.solo()
        self.assertNotIn("owner is not a name", self.doc(w, None))
        self.assertNotIn("owner is not a name", self.doc(w, "ana"))
        self.assertIn("docs/note.md: owner is not a name, so the file counts as unowned",
                      self.doc(w, '""'))

    def test_a_number_like_owner_is_a_name(self):
        from govern.checks.docs import owner_name
        for value in ("2024", "Nan", "Infinity", " ana "):
            self.assertEqual(owner_name(value), value.strip())
        w = self.solo()
        for value in ("2024", "Nan", "Infinity"):
            self.assertNotIn("owner is not a name", self.doc(w, value))
        self.assertEqual(w.run("check")[0], 0)

    def test_a_list_owner_warns_not_errors(self):
        w = self.solo()
        out = self.doc(w, "[ana, bo]")
        self.assertIn("docs/note.md: owner is not a name, so the file counts as unowned", out)
        self.assertEqual(w.run("check")[0], 0, out)


class PerOwner(Case):
    def setUp(self) -> None:
        super().setUp()
        self.w = self.solo()
        spec = {"a1.md": "ana", "a2.md": "ana", "a3.md": "ana", "b1.md": "bo",
                "u1.md": None, "u2.md": None}
        for name, owner in spec.items():
            extra = {} if owner is None else {"owner": owner}
            wtext(self.w.root / "docs/working" / name,
                  fm("working", status="active", **extra) + "x\n")

    def out(self, params: str) -> str:
        cfg(self.w, "", '\n[checks.working-file-count]\nreason = "test"\n' + params)
        return self.w.run("check")[1]

    def test_per_owner_counts(self):
        out = self.out("max_files = 20\nper_owner_max = 2\n")
        self.assertIn("owner 'ana' holds 3 files", out)
        self.assertNotIn("owner 'bo'", out)
        self.assertNotIn("files in working/ exceeds", out)

    def test_unowned_files_count_against_max_files(self):
        out = self.out("max_files = 1\nper_owner_max = 5\n")
        self.assertNotIn("owner '", out)
        self.assertIn("files in working/ exceeds working-file-count max_files=1", out)

    def test_owned_files_do_not_count_against_max_files(self):
        out = self.out("max_files = 3\nper_owner_max = 5\n")
        self.assertNotIn("files in working/ exceeds", out)

    def test_per_owner_zero_is_today(self):
        out = self.out("max_files = 5\n")
        self.assertIn("files in working/ exceeds working-file-count max_files=5", out)
        self.assertNotIn("owner '", out)


if __name__ == "__main__":
    unittest.main()
