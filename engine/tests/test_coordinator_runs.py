"""Coordinator rows: one main-session transcript, plus phase marks, into numbers per phase."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import profile_guard  # noqa: E402,F401

from govern import coordinator_runs  # noqa: E402

T0 = 1_800_000_000.0
SECRET = "made-up-secret-words"
SECRET_PATH = "/private/made-up/path.txt"


def stamp(offset: float) -> str:
    when = datetime.fromtimestamp(T0 + offset, tz=timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"


def usage(i=1, c=2, r=3, o=4) -> dict:
    return {"input_tokens": i, "cache_creation_input_tokens": c,
            "cache_read_input_tokens": r, "output_tokens": o}


def assistant(at, mid, use=None, model="model-a", blocks=None) -> dict:
    return {"type": "assistant", "timestamp": stamp(at),
            "message": {"id": mid, "model": model, "usage": use or usage(),
                        "content": blocks if blocks is not None else
                        [{"type": "text", "text": SECRET}]}}


def tool_use(at, mid, uid, name, use=None) -> dict:
    return assistant(at, mid, use, blocks=[
        {"type": "tool_use", "id": uid, "name": name, "input": {"file_path": SECRET_PATH}}])


def tool_result(at, uid, content) -> dict:
    return {"type": "user", "timestamp": stamp(at), "toolUseResult": {"x": SECRET},
            "message": {"content": [{"type": "tool_result", "tool_use_id": uid,
                                     "content": content}]}}


def user(at, origin, **extra) -> dict:
    return {"type": "user", "timestamp": stamp(at), "origin": origin,
            "message": {"content": SECRET}, **extra}


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def write(self, entries, name="sess.jsonl", raw=None) -> Path:
        p = self.tmp / name
        text = raw if raw is not None else "".join(json.dumps(e) + "\n" for e in entries)
        p.write_text(text, encoding="utf-8")
        return p

    def rows(self, entries, marks=()):
        return coordinator_runs.session_rows(self.write(entries), list(marks))


class Totals(Case):
    def test_requests_sharing_an_id_count_once_and_the_last_usage_wins(self):
        rows = self.rows([assistant(1, "m1", usage(1, 1, 1, 1)),
                          assistant(2, "m1", usage(10, 20, 30, 40)),
                          assistant(3, "m2", usage(5, 5, 5, 5), model="model-b"),
                          {"type": "attachment", "timestamp": stamp(4)}])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["requests"], row["input_tokens"], row["cache_creation_input_tokens"],
                          row["cache_read_input_tokens"], row["output_tokens"]),
                         (2, 15, 25, 35, 45))
        self.assertEqual((row["peak_context"], row["model"], row["phase"], row["session"]),
                         (100, "model-b", "", "sess"))
        self.assertTrue(row["coordinator"])
        self.assertEqual(row["role"], "coordinator")
        self.assertIsInstance(row["t"], float)

    def test_no_mark_gives_one_row_in_planning(self):
        rows = self.rows([assistant(1, "m1"), assistant(500, "m2")])
        self.assertEqual([(r["phase"], r["requests"]) for r in rows], [("", 2)])

    def test_requests_split_across_a_mark(self):
        rows = self.rows([user(0, {"kind": "human"}), assistant(10, "m1"),
                          user(150, {"kind": "human"}), assistant(200, "m2")],
                         [(T0 + 100, "p1")])
        self.assertEqual([(r["phase"], r["requests"], r["owner_turns"]) for r in rows],
                         [("", 1, 1), ("p1", 1, 1)])

    def test_a_request_belongs_to_the_phase_of_its_first_line(self):
        rows = self.rows([assistant(90, "m1"), assistant(110, "m1")], [(T0 + 100, "p1")])
        self.assertEqual([(r["phase"], r["requests"]) for r in rows], [("", 1)])

    def test_a_phase_with_nothing_gives_no_row(self):
        rows = self.rows([assistant(10, "m1")], [(T0 + 100, "p1"), (T0 + 200, "p2")])
        self.assertEqual([r["phase"] for r in rows], [""])


class Counts(Case):
    def test_compactions_are_counted_by_boundary(self):
        rows = self.rows([assistant(1, "m1"),
                          {"type": "system", "subtype": "compact_boundary", "timestamp": stamp(2)},
                          {"type": "system", "subtype": "other", "timestamp": stamp(3)},
                          user(2, None, isCompactSummary=True)])
        self.assertEqual(rows[0]["compactions"], 1)

    def test_owner_turns_handbacks_notifications_and_notices(self):
        rows = self.rows([
            user(1, {"kind": "human"}), user(2, {"kind": "human"}),
            user(3, {"kind": "peer", "from": "agent-1"}),
            user(4, {"kind": "task-notification"}),
            user(5, None, isMeta=True),
            {"type": "user", "timestamp": stamp(6), "message": {"content": "x"}}])
        self.assertEqual((rows[0]["owner_turns"], rows[0]["handbacks"], rows[0]["requests"]),
                         (2, 1, 0))
        self.assertEqual(rows[0]["model"], "")


class Tools(Case):
    def test_calls_seconds_and_size(self):
        rows = self.rows([tool_use(1, "m1", "u1", "Bash"), tool_result(4.5, "u1", "x" * 2048),
                          tool_use(5, "m2", "u2", "Bash"),
                          tool_use(6, "m3", "u3", "Read"), tool_result(7, "u3", "ok")])
        bash = rows[0]["tools"]["Bash"]
        self.assertEqual((bash["calls"], bash["seconds"]), (2, 3.5))
        self.assertEqual(bash["result_kb"], round(len(json.dumps("x" * 2048)) / 1024, 1))
        self.assertEqual(rows[0]["tools"]["Read"]["calls"], 1)

    def test_a_tool_belongs_to_the_phase_of_its_call(self):
        rows = self.rows([tool_use(90, "m1", "u1", "Bash"), tool_result(120, "u1", "x" * 1024)],
                         [(T0 + 100, "p1")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["phase"], "")
        self.assertEqual(rows[0]["tools"]["Bash"]["seconds"], 30.0)

    def test_largest_is_capped_at_five_biggest_first(self):
        entries = []
        for i in range(7):
            entries += [tool_use(i * 2, f"m{i}", f"u{i}", f"T{i}"),
                        tool_result(i * 2 + 1, f"u{i}", "y" * (i + 1) * 1000)]
        largest = self.rows(entries)[0]["largest"]
        self.assertEqual([x["tool"] for x in largest], ["T6", "T5", "T4", "T3", "T2"])
        self.assertIsInstance(largest[0]["kb"], float)


class Shapes(Case):
    def test_a_non_json_line_gives_none(self):
        p = self.write([], raw=json.dumps(assistant(1, "m1")) + "\nnot json\n")
        self.assertIsNone(coordinator_runs.session_rows(p, []))

    def test_usage_that_is_not_a_dict_gives_none(self):
        bad = assistant(1, "m1")
        bad["message"]["usage"] = [1, 2]
        self.assertIsNone(self.rows([bad]))

    def test_a_missing_timestamp_or_a_non_number_gives_none(self):
        bad = assistant(1, "m1")
        del bad["timestamp"]
        self.assertIsNone(self.rows([bad]))
        worse = assistant(1, "m1", usage(i="many"))
        self.assertIsNone(self.rows([worse]))

    def test_a_missing_file_gives_none(self):
        self.assertIsNone(coordinator_runs.session_rows(self.tmp / "none.jsonl", []))

    def test_no_text_input_or_path_from_the_conversation_is_in_a_row(self):
        rows = self.rows([user(0, {"kind": "human"}), tool_use(1, "m1", "u1", "Read"),
                          tool_result(2, "u1", SECRET + SECRET_PATH), assistant(3, "m2")])
        dumped = json.dumps(rows)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn(SECRET_PATH, dumped)
        self.assertNotIn("private", dumped)


class Folders(Case):
    def test_folder_names(self):
        home = self.tmp / "home"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            posix = coordinator_runs.transcript_folder(home, Path("/srv/x/Projects/ai-gov"))
            self.assertEqual(posix.name, "-srv-x-Projects-ai-gov")
            self.assertEqual(posix.parent, home / ".claude" / "projects")
            win = coordinator_runs.transcript_folder(home, mock.Mock(
                __str__=lambda self: "C:\\work\\proj"))
            self.assertEqual(win.name, "C--work-proj")
            for raw, name in (("/srv/x/My Project", "-srv-x-My-Project"),
                              ("/srv/x/.hidden/a_b", "-srv-x--hidden-a-b")):
                folder = coordinator_runs.transcript_folder(home, mock.Mock(
                    __str__=lambda self, raw=raw: raw))
                self.assertEqual(folder.name, name)

    def test_main_sessions_skip_subfolders_and_older_files(self):
        home, root = self.tmp / "home", Path("/proj/here")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            folder = coordinator_runs.transcript_folder(home, root)
            (folder / "s1" / "subagents").mkdir(parents=True)
            for name, mtime in (("old", T0 - 50), ("b", T0 + 20), ("a", T0 + 10)):
                p = folder / f"{name}.jsonl"
                p.write_text("{}\n", encoding="utf-8")
                os.utime(p, (mtime, mtime))
            (folder / "s1" / "inner.jsonl").write_text("{}\n", encoding="utf-8")
            os.utime(folder / "s1" / "inner.jsonl", (T0 + 30, T0 + 30))
            (folder / "note.txt").write_text("x", encoding="utf-8")
            found = coordinator_runs.main_sessions(home, root, T0)
            self.assertEqual([p.name for p in found], ["a.jsonl", "b.jsonl"])
            self.assertEqual(coordinator_runs.main_sessions(home, Path("/none"), T0), [])


if __name__ == "__main__":
    unittest.main()
