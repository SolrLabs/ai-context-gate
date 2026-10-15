"""Behaviour tests on small throwaway workspaces: malformed input, finding names, ranges, config edge cases and the engine pin."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from regressions_support import *  # noqa: E402,F401,F403

class MalformedRange(Base):
    def test_finding_not_crash(self):
        code, out, err = self.ws(alpha_range="100–199").run("check")
        self.assertEqual(code, 1)
        self.assertIn("registry: 'alpha' id_range '100–199' is not LO-HI", out)
        self.assertNotIn("Traceback", err)


class PipeInTitle(Base):
    def test_cell_escaped(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8").replace("Title 100", "Pick A | B"))
        w.index()
        self.assertIn("Pick A \\| B", log.read_text(encoding="utf-8"))
        code, out, _ = w.run("check")
        self.assertEqual(code, 0, out)


class DateFormat(Base):
    def test_strict_dates(self):
        w = self.ws()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        w.write("projects/alpha/a.md", fm(last_reviewed="20260918"))
        w.write("projects/alpha/b.md", fm(last_reviewed="2026-9-5"))
        w.write("projects/alpha/c.md", fm(last_reviewed=tomorrow))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("alpha/a.md: last_reviewed '20260918' is not YYYY-MM-DD", out)
        self.assertIn("alpha/b.md: last_reviewed '2026-9-5' is not YYYY-MM-DD", out)
        self.assertIn(f"alpha/c.md: last_reviewed '{tomorrow}' is in the future", out)


class WorkspaceDocs(Base):
    def test_same_rules_as_project_docs(self):
        w = self.ws()
        old = (date.today() - timedelta(days=400)).isoformat()
        w.write("governance/notes.md", fm("working", status="active", last_reviewed=old))
        w.write("governance/bad.md", fm("manual"))
        code, out, _ = w.run("check")
        self.assertNotIn("notes.md: last_reviewed", out)
        self.assertIn("governance/bad.md: doc_type 'manual' not in", out)


class MemorySlug(Base):
    def test_slug_derived_from_root(self):
        self.assertEqual(text.claude_project_slug(Path("/home/a.b/My_Proj")),
                         "-home-a-b-My-Proj")

    def test_memory_index_found_under_root_slug(self):
        w = self.ws()
        mem = (w.home / ".claude" / "projects" / text.claude_project_slug(w.root) / "memory"
               / "MEMORY.md")
        mem.parent.mkdir(parents=True)
        wtext(mem, "- line\n" * 50)
        code, out, _ = w.run("check")
        self.assertIn("memory index: 50 non-blank lines", out)


class FindingNamesItsSetting(Base):
    def test_a_limit_finding_names_the_check_and_parameter_that_set_it(self):
        # What a project changes in config.toml to move the limit, not an internal name.
        w = self.ws(extra="\n[checks.doc-frontmatter]\nmax_working_words = 3\nstale_days = 10\n"
                          "\n[checks.governed-doc-count]\nmax_docs = 1\n")
        old = (date.today() - timedelta(days=30)).isoformat()
        w.write("projects/alpha/guide.md", fm("reference", last_reviewed=old))
        w.write("projects/alpha/working-files/notes.md",
                fm("working", status="active") + "word " * 10)
        mem = (w.home / ".claude" / "projects" / text.claude_project_slug(w.root) / "memory"
               / "MEMORY.md")
        mem.parent.mkdir(parents=True)
        wtext(mem, "- line\n" * 50)
        # Recorded first: a new size breach is the ratchet's finding alone, and the
        # limit's own warning, the one that names the setting, is what a recorded one shows.
        w.run("baseline", "--allow-raise")
        _, out, _ = w.run("check")
        self.assertIn("last_reviewed 30d ago exceeds doc-frontmatter stale_days=10", out)
        self.assertIn("words exceeds doc-frontmatter max_working_words=3", out)
        self.assertIn("governed docs exceeds governed-doc-count max_docs=1", out)
        self.assertIn("50 non-blank lines exceeds memory-index max_lines=40", out)


class FindingNamesItsSettingWhenTheRatchetReports(Base):
    """The one finding of a new or grown working file still says the limit and its setting."""

    def test_a_new_breach_finding_names_the_setting(self):
        w = self.ws(extra="\n[checks.doc-frontmatter]\nmax_working_words = 3\n")
        w.write("projects/alpha/working-files/notes.md",
                fm("working", status="active") + "word " * 10)
        _, out, _ = w.run("check")
        found = [ln for ln in out.splitlines() if "notes.md" in ln and ln.startswith("  ")]
        self.assertEqual(len(found), 1, out)
        self.assertIn("at 10 words (doc-frontmatter max_working_words=3)", found[0])

    def test_a_grown_breach_finding_names_the_setting(self):
        w = self.ws(extra="\n[checks.doc-frontmatter]\nmax_working_words = 3\n")
        w.write("projects/alpha/working-files/notes.md",
                fm("working", status="active") + "word " * 10)
        w.run("baseline", "--allow-raise")
        w.write("projects/alpha/working-files/notes.md",
                fm("working", status="active") + "word " * 12)
        _, out, _ = w.run("check")
        found = [ln for ln in out.splitlines() if "notes.md" in ln and ln.startswith("  ")]
        self.assertEqual(len(found), 1, out)
        self.assertIn("to 12 words (doc-frontmatter max_working_words=3, baseline 10)", found[0])


class RangesFromConfig(Base):
    def test_workspace_range_is_checked_for_overlap(self):
        w = self.ws(ws_range="1-150", alpha_range="100-199")
        code, out, _ = w.run("check")
        self.assertNotIn("id ranges overlap", out)   # prefix-aware: W and A cannot collide
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace('markers = "t"',
                                               'markers = "t"\nid_overlap = "prefix-blind"'))
        code, out, _ = w.run("check")
        self.assertIn("id ranges overlap: workspace 1-150 and alpha 100-199", out)


class ExitCodes(Base):
    def test_refused_baseline_exits_nonzero(self):
        w = self.ws(extra="\n[checks.governed-doc-count]\nmax_docs = 0\n")
        code, out, _ = w.run("baseline")
        self.assertEqual(code, 1)
        self.assertIn("refused new entry 'governed_docs:alpha'", out)
        code, out, _ = w.run("baseline", "--allow-raise")
        self.assertEqual(code, 0)


class NextIdPrefix(Base):
    def test_other_prefix_does_not_block(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("X", 101))
        code, out, _ = w.run("next-id", "--project", "alpha")
        self.assertEqual(out.strip(), "A-101")


class UnknownScope(Base):
    def test_unknown_project_is_usage_error(self):
        w = self.ws()
        for argv in (["find", "--project", "nope", "x"], ["next-id", "--project", "nope"],
                     ["show", "--project", "nope", "--list"]):
            code, _, err = w.run(*argv)
            self.assertEqual(code, 2, argv)


class Config(Base):
    def test_unknown_key_rejected(self):
        code, _, err = self.ws(extra="\n[checks.decision-log]\nmax_wrods = 5\n").run("check")
        self.assertEqual(code, 2)

    def test_a_retired_key_is_named_no_longer_used(self):
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "[blocks]\n", '[blocks]\nplaceholder = "_Run index._"\n', 1))
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn(f"{CFG}: [blocks] placeholder is no longer used (it was never read): "
                      f"remove it", err)
        self.assertNotIn("unknown key", err)

    def test_loosening_needs_a_reason(self):
        # agents.max_turns is excluded from this test: its engine default (0) means no ceiling,
        # already the loosest a ceiling can be, so no positive value can loosen it further.
        w = self.ws(extra="\n[checks.working-file-count]\nmax_files = 999\n")
        code, _, err = w.run("check")
        self.assertEqual(code, 2)
        self.assertIn("without a 'reason'", err)

    def test_fixed_core_cannot_be_off(self):
        code, _, err = self.ws(extra='\n[checks.ratchet]\nlevel = "off"\n').run("check")
        self.assertEqual(code, 2)
        self.assertIn("fixed core", err)


class EdgeCases(Base):
    """Unreadable logs, masking, malformed headings, bad config tables and other edge cases
    across the CLI."""

    def test_unreadable_log_blocks_every_write(self):
        w = self.ws(extra="\n[checks.governed-doc-count]\nmax_docs = 0\n")
        self.assertEqual(w.run("baseline", "--allow-raise")[0], 0)
        base = (w.root / layout.BASELINE).read_text(encoding="utf-8")
        log = w.root / "projects/alpha/DECISIONS.md"
        log.write_bytes(log.read_bytes() + b"\xff")
        for argv in (["baseline"], ["index"], ["next-id", "--project", "alpha"]):
            code, out, err = w.run(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("not valid UTF-8", err)
        self.assertEqual((w.root / layout.BASELINE).read_text(encoding="utf-8"), base)
        code, out, _ = w.run("check")
        self.assertIn("projects/alpha/DECISIONS.md: not valid UTF-8", out)

    def test_masking(self):
        from govern.decisions import Grammar
        grammar = Grammar("em-dash", ["Rule", "Why"], text.Markers("t"))
        e = lambda n: f"## W-{n} — T{n}\n\n**Status:** locked\n"
        for mid in ("see `<!-- t:generated:start id=x -->` here\n", "use `<!--` to open\n",
                    "~~~\n```\n~~~\n", "````\n```\n## W-9 — phantom\n````\n",
                    "<!-- a --> x <!-- b\n## W-8 — hidden\n-->\n"):
            ids = [x.ident for x in grammar.parse(e(1) + mid + e(2) + e(3)).entries]
            self.assertEqual(ids, ["W-1", "W-2", "W-3"], mid)

    def test_malformed_heading_spends_its_number(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + entry("A", 101, sep=" - "))
        code, out, _ = w.run("next-id", "--project", "alpha")
        self.assertEqual(out.strip(), "A-102")

    def test_show_lines_matches_show(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + "\n```\n## Example\n```\nmore\n" + entry("A", 101))
        code, out, _ = w.run("show", "--project", "alpha", "--lines", "A-100")
        start, end = map(int, out.split()[0].rsplit(":", 1)[1].split("-"))
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines[start - 1].startswith("## A-100"))
        self.assertIn("more", lines[start - 1:end])

    def test_bad_config_tables_are_usage_errors(self):
        w = self.ws()
        cfg = w.root / CFG
        good = cfg.read_text(encoding="utf-8")
        for bad in ('project = [{ file = "DECISIONS.md", id = "nope" }]',
                    'project = [{ file = "DECISIONS.md", id = "decision-index" }]\n'
                    'registry_columns = [{ header = "X", kye = "role" }]'):
            wtext(cfg, good.replace(
                'project = [{ file = "DECISIONS.md", id = "decision-index" }]', bad))
            code, _, err = w.run("check")
            self.assertEqual(code, 2, bad)
            self.assertNotIn("Traceback", err)
        wtext(cfg, good)
        wtext((w.root / "projects.toml"), "[[project]\n")
        code, _, err = w.run("check")
        self.assertEqual(code, 2)

    def test_frontmatter_yaml_forms(self):
        f, _ = text.parse_frontmatter("---\nrelated:\n- a.md\n- b.md\npurpose: >\n  one\n"
                                      "  two\ndoc_type: control\n---\n")
        self.assertEqual(f.get_list("related"), ["a.md", "b.md"])
        self.assertEqual(f.get("purpose"), "one two")
        self.assertEqual(f.get("doc_type"), "control")

    def test_misplaced_leaf_beside_configured_one(self):
        code, out, _ = self.ws(alpha_extra='handoff = "x.md"').run("check")
        self.assertIn("registry: 'alpha' has 'handoff'", out)

    def test_missing_extension_dir(self):
        code, _, err = self.ws(extensions='extensions = ["nope"]').run("check")
        self.assertEqual(code, 2)
        self.assertIn("extensions directory 'nope' does not exist", err)

    def test_find_workspace_and_show_unknown(self):
        w = self.ws()
        code, out, _ = w.run("find", "--project", "workspace", "Title")
        self.assertEqual(code, 0)
        self.assertIn("W-1", out)
        code, _, _ = w.run("show", "--project", "typo", "W-1")
        self.assertEqual(code, 2)

    def test_section_titles_are_not_malformed_entries(self):
        w = self.ws()
        log = w.root / "projects/alpha/DECISIONS.md"
        wtext(log, log.read_text(encoding="utf-8") + "\n## UTF-8 and BOMs\n\n### A-150 — too deep\n")
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertNotIn("UTF-8 and BOMs", out)
        self.assertIn("'### A-150 — too deep' is not a decision entry heading", out)

    def test_finished_file_outside_git_is_flagged_not_aged_by_mtime(self):
        # "Committed" means "in git history" (`Context.committed`, checked with `git log`):
        # outside a repo entirely that can never be true, so the file is flagged as never
        # committed rather than falling back to its mtime (see the `Committed` test class).
        w = self.ws()
        path = w.write("projects/alpha/working-files/plan.md",
                       fm("working", status="complete") + "body\n")
        old = (date.today() - timedelta(days=30))
        stamp = datetime_timestamp(old)
        os.utime(path, (stamp, stamp))
        code, out, _ = w.run("check", "--project", "alpha")
        self.assertIn("plan.md is finished but was never committed", out)
        self.assertNotIn("last touched", out)


class EnginePin(Base):
    """A project runs only the engine it pins (or, for a series pin, one from that series)."""

    def test_missing_newer_and_older_pins_are_refused(self):
        w = self.ws()
        cfg = w.root / CFG
        good = cfg.read_text(encoding="utf-8")
        for pin, words in (('', "is required"), (f'engine = "{_MAJOR}.{_MINOR + 1}"', "newer"),
                           (f'engine = "{PREV_SERIES}"', "older"),
                           (f'engine = "{SERIES}.999"', "but this is engine")):
            wtext(cfg, good.replace(f'engine = "{__version__}"', pin))
            code, _, err = w.run("check")
            self.assertEqual(code, 2, pin)
            self.assertIn(words, err)

    def test_series_pin_runs_with_a_note(self):
        w = self.ws()
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"',
                                                            f'engine = "{SERIES}"'))
        code, _, err = w.run("check")
        self.assertEqual(code, 0)
        self.assertIn(f"this project pins the series {SERIES}", err)
        self.assertIn(f"Use python3 .context-gate/bin/upgrade to pin {__version__} exactly", err)

    def test_entrypoint_runs_exactly_the_pinned_engine(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (f"{PREV_SERIES}.0", __version__, f"{SERIES}.99", NEXT_RELEASE))
        res = subprocess.run([sys.executable, str(entry), "check"], env=env,
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertTrue(self._which(entry, env).endswith(__version__))
        # A newer engine is installed: one plain line (not a terminal), with the command.
        self.assertIn(f"context-gate {NEXT_RELEASE} available (this project runs {__version__}). "
                      f"Use python3 .context-gate/bin/upgrade to upgrade.", res.stderr)
        self.assertNotIn("\033[", res.stderr)

    def test_upgrade_help_prints_usage_and_changes_nothing(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__, NEXT_RELEASE))
        cfg = w.root / CFG
        before = cfg.read_bytes()
        for flag in ("--help", "-h"):
            res = subprocess.run([sys.executable, str(entry.with_name("upgrade")), flag],
                                 env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("usage: upgrade [--to X.Y.Z]", res.stdout)
            self.assertEqual(cfg.read_bytes(), before)

    def test_upgrade_refuses_an_unknown_argument(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__, NEXT_RELEASE))
        cfg = w.root / CFG
        before = cfg.read_bytes()
        res = subprocess.run([sys.executable, str(entry.with_name("upgrade")), "--dry-run"],
                             env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        self.assertIn("usage: upgrade [--to X.Y.Z]", res.stderr)
        self.assertEqual(cfg.read_bytes(), before)

    def test_upgrade_to_without_a_version_exits_2(self):
        w = self.ws()
        entry, env = self._installed(w)
        cfg = w.root / CFG
        before = cfg.read_bytes()
        res = subprocess.run([sys.executable, str(entry.with_name("upgrade")), "--to"],
                             env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        self.assertIn("usage: upgrade [--to X.Y.Z]", res.stderr)
        self.assertEqual(cfg.read_bytes(), before)

    def test_uninstall_help_prints_usage_and_removes_nothing(self):
        w = self.ws()
        entry, env = self._installed(w)
        for flag in ("--help", "-h"):
            res = subprocess.run([sys.executable, str(entry.with_name("uninstall")), flag],
                                 env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("usage: uninstall [--force]", res.stdout)
            self.assertTrue((w.root / layout.GOV_DIR).is_dir())

    def test_uninstall_refuses_an_unknown_argument(self):
        w = self.ws()
        entry, env = self._installed(w)
        res = subprocess.run([sys.executable, str(entry.with_name("uninstall")), "--purge"],
                             env=env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        self.assertIn("usage: uninstall [--force]", res.stderr)
        self.assertTrue((w.root / layout.GOV_DIR).is_dir())

    def test_series_pin_runs_newest_in_the_series(self):
        w = self.ws()
        entry, env = self._installed(w)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(f'engine = "{__version__}"',
                                                            f'engine = "{SERIES}"'))
        self._engines(w, (f"{SERIES}.0", f"{SERIES}.10", f"{SERIES}.2", NEXT_RELEASE))
        self.assertIn(f"{SERIES}.10", self._which(entry, env))

    def test_missing_engine_bootstraps_from_source(self):
        src = self.tmp / "source"
        shutil.copytree(ENGINE / "govern", src / "engine" / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))
        git = lambda *a: subprocess.run(
            ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t",
             "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *a],
            check=True, capture_output=True)
        git("init", "-q")
        git("add", "-A")
        git("commit", "-qm", "engine")
        git("tag", f"v{__version__}")
        w = self.ws()
        entry, env = self._installed(w)
        cfg = w.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nsource = "{src.as_posix()}"', 1))
        res = subprocess.run([sys.executable, str(entry), "check"], env=env,
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn(f"installing engine {__version__}", res.stderr)
        self.assertTrue((layout.engines_dir(w.home) / __version__ / "govern" / "cli.py").is_file())

    def test_entrypoint_without_an_installed_engine(self):
        w = self.ws()
        entry, env = self._installed(w)
        res = subprocess.run([sys.executable, str(entry)], env=env, capture_output=True,
                             text=True)
        self.assertEqual(res.returncode, 2)
        self.assertIn(f"engine {__version__} is not installed", res.stderr)

    # A beta this machine opted into, in .context-gate/local.toml (never committed).
    def _local(self, w, text):
        wtext(w.root / layout.LOCAL, text)

    def _check(self, entry, env):
        return subprocess.run([sys.executable, str(entry), "check"], env=env,
                              capture_output=True, text=True)

    def test_no_local_file_changes_nothing(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__, BETA))
        res = self._check(entry, env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("beta", res.stderr)
        self.assertTrue(self._which(entry, env).endswith(__version__))

    def test_local_beta_runs(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__, BETA))
        self._local(w, f'[governance]\nengine = "{BETA}"\n')
        self.assertTrue(self._which(entry, env).endswith(BETA))
        self._stamp(w).unlink()         # the probe above said the line already
        res = self._check(entry, env)
        self.assertIn(f"context-gate: running beta {BETA} on this machine (committed pin "
                      f"{__version__}); govern beta off to leave it", res.stderr)

    def _stamp(self, w, beta: str = BETA) -> Path:
        digest = hashlib.sha256(f"{w.root.resolve()}\n{beta}".encode("utf-8")).hexdigest()[:12]
        return w.home / ".local" / "state" / "context-gate" / f"beta-notice-{digest}"

    def _on_a_beta(self, w):
        entry, env = self._installed(w)
        self._engines(w, (__version__, BETA))
        self._local(w, f'[governance]\nengine = "{BETA}"\n')
        return entry, env

    def test_the_beta_line_is_said_once_an_hour(self):
        w = self.ws()
        entry, env = self._on_a_beta(w)
        line = f"context-gate: running beta {BETA} on this machine"
        self.assertIn(line, self._check(entry, env).stderr)
        self.assertNotIn(line, self._check(entry, env).stderr)
        stamp = self._stamp(w)
        self.assertTrue(stamp.is_file())
        old = time.time() - 7200
        os.utime(stamp, (old, old))
        self.assertIn(line, self._check(entry, env).stderr)
        self.assertGreater(stamp.stat().st_mtime, time.time() - 600)

    def test_a_project_that_moves_to_another_beta_hears_the_line_again(self):
        w = self.ws()
        entry, env = self._on_a_beta(w)
        line = f"context-gate: running beta {BETA} on this machine"
        self.assertIn(line, self._check(entry, env).stderr)
        self.assertNotIn(line, self._check(entry, env).stderr)
        other = f"{SERIES}.99-beta.2"
        self._engines(w, (other,))
        self._local(w, f'[governance]\nengine = "{other}"\n')
        self.assertIn(f"context-gate: running beta {other} on this machine",
                      self._check(entry, env).stderr)
        self.assertTrue(self._stamp(w, other).is_file())

    def test_each_project_has_its_own_beta_stamp(self):
        w1 = self.ws()
        entry1, env1 = self._on_a_beta(w1)
        line = f"context-gate: running beta {BETA} on this machine"
        self.assertIn(line, self._check(entry1, env1).stderr)
        other = self.tmp / "other-project"
        shutil.copytree(w1.root, other, ignore=shutil.ignore_patterns(".git"))
        self.assertNotEqual(w1.root.resolve(), other.resolve())
        res = self._check(other / layout.ENTRYPOINT, env1)
        self.assertIn(line, res.stderr)
        self.assertNotIn(line, self._check(other / layout.ENTRYPOINT, env1).stderr)
        self.assertNotIn(line, self._check(entry1, env1).stderr)
        self.assertEqual(len(list(self._stamp(w1).parent.glob("beta-notice-*"))), 2)

    def test_a_beta_that_is_not_installed_is_noted_on_every_run(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__,))
        self._local(w, f'[governance]\nengine = "{BETA}"\n')
        for _ in range(2):
            self.assertIn(f"beta {BETA} is not installed", self._check(entry, env).stderr)
        self.assertFalse(self._stamp(w).exists())

    def test_missing_beta_falls_back(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__,))
        self._local(w, f'[governance]\nengine = "{BETA}"\n')
        res = self._check(entry, env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"beta {BETA} is not installed, running the committed pin "
                      f"{__version__}", res.stderr)
        self.assertEqual(res.stderr.count("context-gate: beta"), 1, res.stderr)
        self.assertTrue(self._which(entry, env).endswith(__version__))

    def test_local_naming_a_stable_is_ignored(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__, NEXT_RELEASE))
        self._local(w, f'[governance]\nengine = "{NEXT_RELEASE}"\n')
        self.assertTrue(self._which(entry, env).endswith(__version__))
        self.assertIn(f"local.toml names no beta engine ('{NEXT_RELEASE}')",
                      self._check(entry, env).stderr)

    def test_unreadable_local_runs_the_pin(self):
        w = self.ws()
        entry, env = self._installed(w)
        self._engines(w, (__version__, BETA))
        for text in (f'[governance\nengine = "{BETA}"\n', "[governance]\n",
                     'governance = "x"\n', b"\xff"):
            (w.root / layout.LOCAL).write_bytes(text if isinstance(text, bytes) else text.encode())
            res = self._check(entry, env)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn(f"running the committed pin {__version__}; govern beta off to clear it",
                          res.stderr)
            self.assertTrue(self._which(entry, env).endswith(__version__))


class OldLauncherSwitchCorrected(Base):
    """A beta engine run in a project whose settings.local.json an old launcher switched
    (`context-gate@skills-dir`) corrects the switch and says so on stderr; the command's stdout
    and exit code are the ones it would have had."""

    OLD, NEW = layout.OLD_SKILLS_DIR_PLUGIN_ID, layout.SKILLS_DIR_PLUGIN_ID

    def setUp(self) -> None:
        super().setUp()
        self.w = self.ws()
        (self.w.root / ".claude").mkdir()
        self.settings = self.w.root / ".claude" / "settings.local.json"
        wtext(self.settings, json.dumps({"enabledPlugins": {self.OLD: True}}, indent=2) + "\n")

    def run_as(self, version: str, *argv: str) -> tuple[int, str, str]:
        with mock.patch.object(cli, "__version__", version):
            return self.w.run(*argv)

    def test_a_beta_corrects_the_switch_and_the_command_is_unchanged(self):
        plain = self.run_as(__version__, "check")
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")),
                         {"enabledPlugins": {self.OLD: True}})     # a release leaves it alone
        code, out, err = self.run_as(BETA, "check")
        self.assertEqual((code, out), plain[:2])
        said = [line for line in err.splitlines() if self.NEW in line]
        self.assertEqual(len(said), 1, err)
        self.assertIn("restart Claude Code to load it", said[0])
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")),
                         {"enabledPlugins": {self.NEW: True}})
        # Said once: the next command finds nothing to correct.
        code, out, err = self.run_as(BETA, "check")
        self.assertNotIn(self.NEW, err)

    def test_a_failing_correction_never_changes_the_command(self):
        plain = self.run_as(BETA, "check")
        wtext(self.settings, json.dumps({"enabledPlugins": {self.OLD: True}}, indent=2) + "\n")
        for fault in (mock.patch.object(os, "replace", side_effect=PermissionError(13, "in use")),
                      mock.patch.object(notice, "correct_old_launcher_switch",
                                        side_effect=RuntimeError("boom"))):
            with fault:
                code, out, err = self.run_as(BETA, "check")
            self.assertEqual((code, out), plain[:2])
            self.assertNotIn("Traceback", err)
            self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")),
                             {"enabledPlugins": {self.OLD: True}})

    def test_a_project_with_no_claude_directory_is_not_given_one(self):
        shutil.rmtree(self.w.root / ".claude")
        self.run_as(BETA, "check")
        self.assertFalse((self.w.root / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
