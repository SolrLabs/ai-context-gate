"""The ratchet baseline under two developers: the merge driver's per-key rules, a raise that names
its keys and carries a reason, and the reason shown by `govern diff`.

    python3 -m unittest discover -s engine/tests -k test_baseline_merge
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import layout, merging, ratchet  # noqa: E402
from base_support import Case, fm, rtext, wtext  # noqa: E402

K1, K2 = "working_file_words:docs/k1.md", "working_file_words:docs/k2.md"
CAP = "\n[checks.doc-frontmatter]\nmax_working_words = 2\n"


def dump(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


class Rules(unittest.TestCase):
    def test_merge_baseline_rules(self):
        o = {"x": 3, "y": 5, "z": 1}
        a = {"x": 4, "y": 5}
        b = {"x": 6, "y": 2, "w": 1}
        self.assertEqual(merging.merge_baseline(o, a, b), {"x": 6, "y": 2, "w": 1})
        self.assertEqual(merging.merge_baseline(o, b, a), {"x": 6, "y": 2, "w": 1})
        # removed on one side, changed on the other: the changed value
        self.assertEqual(merging.merge_baseline({"k": 3}, {}, {"k": 4}), {"k": 4})
        self.assertEqual(merging.merge_baseline({"k": 3}, {"k": 4}, {}), {"k": 4})
        # added on both: the larger
        self.assertEqual(merging.merge_baseline({}, {"n": 2}, {"n": 7}), {"n": 7})
        self.assertEqual(merging.merge_baseline({}, {"n": 2}, {"n": 2}), {"n": 2})

    def test_merge_reasons_joins_both_changed(self):
        o = {"k": "old", "m": "keep"}
        self.assertEqual(merging.merge_reasons(o, {"k": "one", "m": "keep"}, {"k": "two"}),
                         {"k": "one / two"})
        self.assertEqual(merging.merge_reasons({}, {"n": "same"}, {"n": "same"}), {"n": "same"})
        self.assertEqual(merging.merge_reasons({}, {"n": "one"}, {"n": "two"}), {"n": "one / two"})


class Base(Case):
    def setUp(self) -> None:
        super().setUp()
        self.w = self.solo(extra=CAP)
        self.baseline = self.w.root / layout.BASELINE
        self.reasons = ratchet.reasons_path(self.w.context())

    def grow(self, name: str, words: int) -> None:
        self.w.write(f"docs/{name}.md", fm("working", status="active") + "word " * words)

    def load(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))


class Driver(Base):
    def setUp(self) -> None:
        super().setUp()
        self._old_cwd = os.getcwd()
        os.chdir(self.w.root)

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)      # the folder cannot be deleted on Windows while we are in it
        super().tearDown()

    def drive(self, o: str, a: str, b: str, rel: str) -> tuple[int, str]:
        paths = [self.tmp / n for n in ("o", "a", "b")]
        for path, text in zip(paths, (o, a, b)):
            wtext(path, text)
        return merging.drive(self.w.context(), *paths, rel), rtext(paths[1])

    def test_driver_merges_baseline_file(self):
        rel = self.w.context().rel(self.baseline)
        o, a, b = ({"x": 3, "y": 5, "z": 1}, {"x": 4, "y": 5}, {"x": 6, "y": 2, "w": 1})
        code, text = self.drive(dump(o), dump(a), dump(b), rel)
        self.assertEqual((code, text), (0, dump({"x": 6, "y": 2, "w": 1})))
        code, text = self.drive("", dump(a), dump(b), rel)           # no base: both added
        self.assertEqual((code, text), (0, dump({"x": 6, "y": 5, "w": 1})))

    def test_driver_merges_reasons_file(self):
        rel = self.w.context().rel(self.reasons)
        code, text = self.drive(dump({}), dump({"k": "one"}), dump({"k": "two"}), rel)
        self.assertEqual((code, text), (0, dump({"k": "one / two"})))

    def test_unparseable_side_falls_back_to_plain_merge(self):
        rel = self.w.context().rel(self.baseline)
        base = dump({"x": 1})
        code, text = self.drive(base, "{ not json", base, rel)       # only ours changed
        self.assertEqual((code, text), (0, "{ not json"))
        code, text = self.drive(base, "{ ours", "{ theirs", rel)
        self.assertEqual(code, 1)
        self.assertIn("<<<<<<<", text)


class Raise(Base):
    def test_raise_without_reason_refused(self):
        self.grow("k1", 10)
        before = rtext(self.baseline) if self.baseline.exists() else None
        code, out, err = self.w.run("baseline", "--allow-raise")
        self.assertEqual((code, out, err.strip()), (1, "", "a raise needs --reason"))
        after = rtext(self.baseline) if self.baseline.exists() else None
        self.assertEqual(after, before)
        self.assertFalse(self.reasons.exists())

    def test_reason_without_raise_refused(self):
        self.grow("k1", 10)
        code, out, err = self.w.run("baseline", "--reason", "r")
        self.assertEqual((code, out, err.strip()), (1, "", "--reason needs --allow-raise"))
        self.assertFalse(self.reasons.exists())

    def test_raise_records_reason(self):
        self.grow("k1", 10)
        code, out, _ = self.w.run("baseline", "--allow-raise", "--reason", "legacy")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.load(self.reasons).get(K1), "legacy")
        self.assertIn(K1, self.load(self.baseline))

    def test_scoped_raise_raises_only_named_key(self):
        self.grow("k1", 10)
        self.grow("k2", 10)
        self.assertEqual(self.w.run("baseline", "--allow-raise", "--reason", "seed")[0], 0)
        self.grow("k1", 20)
        self.grow("k2", 20)
        code, out, _ = self.w.run("baseline", "--allow-raise", K1, "--reason", "r")
        self.assertEqual(code, 1, out)
        recorded = self.load(self.baseline)
        self.assertGreater(recorded[K1], recorded[K2])
        self.assertEqual(self.load(self.reasons)[K1], "r")
        self.assertEqual(self.load(self.reasons)[K2], "seed")
        self.assertIn(f"refused '{K2}' grew", out)

    def test_lowered_key_drops_its_reason(self):
        self.grow("k1", 10)
        self.assertEqual(self.w.run("baseline", "--allow-raise", "--reason", "r")[0], 0)
        self.assertEqual(self.load(self.reasons)[K1], "r")
        self.grow("k1", 5)
        code, out, _ = self.w.run("baseline")
        self.assertEqual(code, 0, out)
        self.assertNotIn(K1, ratchet.load_reasons(self.w.context()))

    def test_removed_key_drops_its_reason(self):
        self.grow("k1", 10)
        self.assertEqual(self.w.run("baseline", "--allow-raise", "--reason", "r")[0], 0)
        self.grow("k1", 1)
        code, out, _ = self.w.run("baseline")
        self.assertEqual(code, 0, out)
        self.assertNotIn(K1, self.load(self.baseline))
        self.assertNotIn(K1, ratchet.load_reasons(self.w.context()))


class ReasonsFile(Base):
    def seed(self) -> None:
        self.grow("k1", 10)
        self.grow("k2", 10)
        self.assertEqual(self.w.run("baseline", "--allow-raise", "--reason", "seed")[0], 0)

    def test_bom_reasons_kept_and_bom_kept(self):
        self.seed()
        raw = self.reasons.read_bytes()
        self.reasons.write_bytes(b"\xef\xbb\xbf" + raw)
        self.grow("k1", 20)
        code, out, _ = self.w.run("baseline", "--allow-raise", K1, "--reason", "again")
        self.assertEqual(code, 0, out)
        after = self.reasons.read_bytes()
        self.assertTrue(after.startswith(b"\xef\xbb\xbf"))
        data = json.loads(after.decode("utf-8-sig"))
        self.assertEqual((data[K1], data[K2]), ("again", "seed"))
        self.assertEqual(len(data), 3)

    def test_conflicted_reasons_file_stops_the_raise(self):
        self.seed()
        self.reasons.write_text("<<<<<<< ours\n{}\n=======\n{}\n>>>>>>> theirs\n",
                                encoding="utf-8", newline="")
        self.grow("k1", 20)
        base_before = self.baseline.read_bytes()
        reasons_before = self.reasons.read_bytes()
        code, out, _ = self.w.run("baseline", "--allow-raise", K1, "--reason", "again")
        self.assertEqual(code, 1)
        self.assertIn("is not a valid reasons file", out)
        self.assertIn("nothing written", out)
        self.assertEqual(self.baseline.read_bytes(), base_before)
        self.assertEqual(self.reasons.read_bytes(), reasons_before)


class Hints(Base):
    def test_ratchet_finding_names_key_and_reason(self):
        self.grow("k1", 10)
        code, out, _ = self.w.run("check")
        self.assertIn(f'baseline --allow-raise {K1} --reason "<why>"`', out)

    def test_refusal_lines_name_key_and_reason(self):
        self.grow("k1", 10)
        code, out, _ = self.w.run("baseline")
        self.assertIn(f"(needs --allow-raise {K1} --reason TEXT)", out)

    def test_named_key_that_is_not_a_breach_is_ignored(self):
        self.grow("k1", 10)
        code, out, _ = self.w.run("baseline", "--allow-raise", K1, "docs/nope.md",
                                  "--reason", "r")
        self.assertEqual(code, 1, out)      # the working traps file is still refused
        self.assertIn("  ignored docs/nope.md (not a new or grown breach)", out)


class DiffReason(Base):
    def test_diff_shows_the_reason(self):
        self.grow("k1", 10)
        self.w.git("add", "-A")
        self.w.git("commit", "-qm", "doc")
        self.assertEqual(self.w.run("baseline", "--allow-raise", "--reason", "r")[0], 0)
        code, out, err = self.w.run("diff")
        self.assertEqual((code, err), (0, ""))
        line = next(row for row in out.splitlines() if K1 in row)
        self.assertTrue(line.endswith(" — r"), line)
        code, out, _ = self.w.run("diff", "--json")
        rows = json.loads(out)["baseline"]
        self.assertEqual([row["reason"] for row in rows if row["key"] == K1], ["r"])


if __name__ == "__main__":
    unittest.main()
