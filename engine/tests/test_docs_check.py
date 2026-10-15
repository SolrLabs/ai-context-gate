"""The doc-frontmatter finding for a file with no frontmatter names what to add.

    python3 -m unittest discover -s engine/tests -k docs
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from govern.checks.docs import (check_doc, could_go, finished_docs,  # noqa: E402
                                governed_doc_count)
from govern.findings import Findings  # noqa: E402
from govern.text import Markers  # noqa: E402

PARAMS = {"required_keys": ["doc_type", "audience"], "doc_types": ["manual", "working"],
          "audiences": ["agent", "human"], "working_statuses": [], "stale_types": [],
          "stale_days": 90, "max_working_words": 1000}


class NoFrontmatter(unittest.TestCase):
    def run_check(self, params):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.md"
            path.write_text("# Plain\n", encoding="utf-8")
            f = Findings()
            check_doc(None, params, path, "a.md", f)
        return f.errors

    def test_no_frontmatter_names_the_keys_and_allowed_values(self):
        self.assertEqual(self.run_check(PARAMS), [
            "a.md: no frontmatter \u2014 add doc_type, audience; doc_type is one of manual, "
            "working; audience is one of agent, human"])
        changed = {**PARAMS, "doc_types": ["note"], "required_keys": ["doc_type"]}
        self.assertEqual(self.run_check(changed), [
            "a.md: no frontmatter \u2014 add doc_type; doc_type is one of note"])
        none = {**PARAMS, "required_keys": []}
        self.assertEqual(self.run_check(none), ["a.md: no frontmatter"])
        aud = {**PARAMS, "required_keys": ["audience", "title"]}
        self.assertEqual(self.run_check(aud), [
            "a.md: no frontmatter \u2014 add audience, title; audience is one of agent, human"])


def working(status: str, doc_type: str = "working") -> str:
    return f"---\ndoc_type: {doc_type}\naudience: agent\nstatus: {status}\n---\nBody.\n"


def finished_ctx(docs, level="warn", statuses=("complete", "superseded"), exempt=("HANDOFF.md",),
                 committed=lambda path: True):
    cfg = SimpleNamespace(checks={"finished-files": SimpleNamespace(
        level=level, params={"finished_statuses": list(statuses), "exempt": list(exempt),
                                  "ticked": False})})
    return SimpleNamespace(cfg=cfg, governed_docs=lambda g: docs, committed=committed,
                           registry=SimpleNamespace(single=False),
                           markers=Markers("t"))


class FinishedDocs(unittest.TestCase):
    def names(self, files, **over):
        with tempfile.TemporaryDirectory() as tmp:
            gov = Path(tmp)
            docs = []
            for name, text in files:
                (gov / name).write_text(text, encoding="utf-8")
                docs.append((name, gov / name))
            return finished_docs(finished_ctx(docs, **over), SimpleNamespace(name="alpha", gov=gov))

    FILES = [("b.md", working("complete")), ("a.md", working("superseded, by x")),
             ("c.md", working("active")), ("d.md", working("complete", doc_type="manual")),
             ("HANDOFF.md", working("complete"))]

    def test_the_statuses_come_from_finished_files_and_the_names_are_sorted(self):
        self.assertEqual(self.names(self.FILES), ["alpha/a.md", "alpha/b.md"])
        self.assertEqual(self.names(self.FILES, statuses=["superseded"]), ["alpha/a.md"])

    def test_finished_files_off_gives_no_names(self):
        self.assertEqual(self.names(self.FILES, level="off"), [])

    def test_an_empty_list_gives_no_names(self):
        self.assertEqual(self.names(self.FILES, statuses=[]), [])

    def test_a_file_matching_exempt_is_not_named(self):
        self.assertNotIn("alpha/HANDOFF.md", self.names(self.FILES))
        self.assertIn("alpha/HANDOFF.md", self.names(self.FILES, exempt=[]))

    def test_an_unreadable_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            gov = Path(tmp)
            (gov / "x.md").write_bytes(b"\xff\xfe\x00bad")
            (gov / "y.md").write_text(working("complete"), encoding="utf-8")
            docs = [("x.md", gov / "x.md"), ("y.md", gov / "y.md")]
            got = finished_docs(finished_ctx(docs), SimpleNamespace(name="alpha", gov=gov))
        self.assertEqual(got, ["alpha/y.md"])


    def test_an_uncommitted_file_is_not_named_until_it_is_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            gov = Path(tmp)
            (gov / "a.md").write_text(working("complete"), encoding="utf-8")
            docs = [("a.md", gov / "a.md")]
            scope = SimpleNamespace(name="alpha", gov=gov)
            tracked = set()
            ctx = finished_ctx(docs, committed=lambda path: path in tracked)
            self.assertEqual(finished_docs(ctx, scope), [])
            tracked.add(gov / "a.md")
            self.assertEqual(finished_docs(ctx, scope), ["alpha/a.md"])


class TickedFinishedDocs(unittest.TestCase):
    def names(self, files, ticked=True, committed=lambda path: True):
        with tempfile.TemporaryDirectory() as tmp:
            gov = Path(tmp)
            docs = []
            for name, text in files:
                (gov / name).write_text(text, encoding="utf-8")
                docs.append((name, gov / name))
            ctx = finished_ctx(docs, committed=committed)
            ctx.cfg.checks["finished-files"].params["ticked"] = ticked
            return finished_docs(ctx, SimpleNamespace(name="alpha", gov=gov))

    FILES = [("t.md", working("active").replace("Body.", "- [x] a\n- [X] b")),
             ("u.md", working("active").replace("Body.", "- [x] a\n- [ ] b"))]

    def test_an_all_ticked_committed_file_is_named(self):
        self.assertEqual(self.names(self.FILES), ["alpha/t.md"])

    def test_not_when_ticked_is_false(self):
        self.assertEqual(self.names(self.FILES, ticked=False), [])

    def test_not_when_uncommitted(self):
        self.assertEqual(self.names(self.FILES, committed=lambda path: False), [])


class CouldGo(unittest.TestCase):
    def test_none_two_five_and_six_names(self):
        lead = " Finished working files that could go: "
        self.assertEqual(could_go([]), "")
        self.assertEqual(could_go(["a", "b"]), lead + "a, b")
        self.assertEqual(could_go(list("abcde")), lead + "a, b, c, d, e")
        self.assertEqual(could_go(list("abcdef")), lead + "a, b, c, d, e and 1 more")


class CountWarningNamesThem(unittest.TestCase):
    def test_a_scope_over_the_limit_ends_with_both_finished_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            gov = Path(tmp)
            docs = []
            for name, status in (("b.md", "complete"), ("a.md", "superseded, by x"),
                                 ("c.md", "active")):
                (gov / name).write_text(working(status), encoding="utf-8")
                docs.append((name, gov / name))
            ctx = finished_ctx(docs)
            scope = SimpleNamespace(name="alpha", gov=gov)
            f = governed_doc_count(ctx, {"max_docs": 2}, scope)
        self.assertEqual(len(f.warnings), 1)
        self.assertTrue(f.warnings[0].endswith(
            " Finished working files that could go: alpha/a.md, alpha/b.md"), f.warnings[0])


if __name__ == "__main__":
    unittest.main()
