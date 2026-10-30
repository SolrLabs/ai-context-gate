"""Turning the merge driver on: the `merge-driver` check and the `.gitattributes` region `index`
keeps, `govern merge-driver install` for a clone, the session-start notice, and two developers
merging through git itself.

    python3 -m unittest discover -s engine/tests -k test_merge_install
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_guard  # noqa: E402,F401

from govern import installer, layout, merging  # noqa: E402
from base_support import LOG, TRAPS, Case, Tree, decision, fm, rtext, trap, wtext  # noqa: E402
from team_support import TwoClones  # noqa: E402
import test_nested_checkout as nested  # noqa: E402

PLUGIN = ENGINE.parent / "plugin"
ON = '\n[checks.merge-driver]\nlevel = "error"\n'
CAP = "\n[checks.doc-frontmatter]\nmax_working_words = 2\n"
ATTRS = ".gitattributes"
PYTHON = f'"{Path(sys.executable).as_posix()}"'
DRIVER = f"{PYTHON} .context-gate/bin/govern merge-driver %O %A %B %P"
SAID = ("This clone merges decision logs without context-gate's driver: run python3 "
        ".context-gate/bin/govern merge-driver install")
REGION = "\n".join([
    "# context-gate:start",
    f"/{layout.BASELINE.replace('baseline', 'baseline-reasons')} merge=context-gate",
    f"/{layout.BASELINE} merge=context-gate",
    f"/{LOG} merge=context-gate",
    "/docs/working/traps*.md merge=context-gate",
    f"/{TRAPS} merge=context-gate",
    "# context-gate:end",
]) + "\n"


def hooks_dir(w: Tree) -> Path:
    return w.root / w.git("rev-parse", "--git-path", "hooks").strip()


class Region(Case):
    def test_index_writes_the_region_and_the_check_passes(self):
        w = self.solo(extra=ON)
        self.assertEqual(w.read(ATTRS), REGION)
        code, out, err = w.run("check")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("merge region", out)
        code, out, _ = w.run("index")
        self.assertEqual(w.read(ATTRS), REGION)       # a second run changes nothing
        self.assertNotIn(".gitattributes", out)

    def test_index_keeps_every_other_line_and_the_line_ending(self):
        w = self.solo(extra=ON)
        w.write(ATTRS, "*.png binary\r\n" + REGION.replace("\n", "\r\n") + "*.svg text\r\n")
        w.edit(ATTRS, f"{LOG} merge=context-gate", "docs/old.md merge=context-gate")
        code, out, _ = w.run("index")
        self.assertEqual(code, 0, out)
        self.assertIn("  regenerated .gitattributes :: merge region", out)
        self.assertEqual(w.read(ATTRS),
                         ("*.png binary\n" + REGION + "*.svg text\n").replace("\n", "\r\n"))

    def test_stale_region_fails_the_check(self):
        w = self.solo(extra=ON)
        w.edit(ATTRS, f"{LOG} merge=context-gate", "docs/other.md merge=context-gate")
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn(".gitattributes: the context-gate merge region is stale — run index", out)
        w.run("index")
        self.assertEqual(w.run("check")[0], 0)

    def test_a_missing_region_fails_the_check(self):
        w = self.solo(extra=ON)
        (w.root / ATTRS).unlink()
        code, out, _ = w.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn(".gitattributes: the context-gate merge region is missing — run index",
                      out)

    def test_off_writes_no_gitattributes(self):
        w = self.solo()
        self.assertFalse((w.root / ATTRS).exists())
        w.write(ATTRS, "*.png binary\n")
        w.run("index")
        self.assertEqual(w.read(ATTRS), "*.png binary\n")

    def test_a_bom_survives_and_an_unreadable_file_stops_index(self):
        w = self.solo(extra=ON)
        w.write(ATTRS, b"\xef\xbb\xbf*.png binary\n")
        w.run("index")
        raw = (w.root / ATTRS).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf*.png binary\n"))
        self.assertIn(b"# context-gate:start", raw)
        w.write(ATTRS, b"\xff\xfe bad")
        code, out, err = w.run("index")
        self.assertEqual(code, 1, out + err)
        self.assertIn("nothing was written", out + err)
        self.assertEqual((w.root / ATTRS).read_bytes(), b"\xff\xfe bad")

    def test_the_region_names_the_workspace_log_and_every_project(self):
        w = self.team(extra="")
        w.write(layout.CONFIG, w.read(layout.CONFIG) + ON)
        w.run("index")
        text = w.read(ATTRS)
        ctx = w.context()
        self.assertIn(f"{ctx.rel(ctx.workspace_log)} merge=context-gate", text)
        for scope in ctx.registry.scopes:
            self.assertIn(f"{ctx.rel(ctx.scope_log(scope))} merge=context-gate", text)
            self.assertIn(f"{ctx.rel(ctx.working_dir(scope))}/traps*.md merge=context-gate", text)
        self.assertEqual(w.run("check")[0], 0)
        self.assertEqual(text, merging.attributes_region(ctx))


class Patterns(Case):
    """Each `.gitattributes` pattern, asked of git itself with `check-attr`."""

    def attr(self, repo: Path, path: str) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(HOME=str(self.tmp), GIT_CONFIG_NOSYSTEM="1")
        res = subprocess.run(["git", "check-attr", "merge", "--", path], cwd=repo, env=env,
                             capture_output=True, text=True, encoding="utf-8")
        return res.stdout.rsplit(": ", 1)[-1].strip()

    def repo_with(self, *patterns: str) -> Path:
        repo = self.tmp / "repo"
        repo.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(HOME=str(self.tmp), GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
        wtext(repo / ATTRS, "".join(f"{p} merge=context-gate\n" for p in patterns))
        return repo

    def test_a_root_log_does_not_match_a_nested_one(self):
        pattern = merging.attribute_pattern("DECISIONS.md")
        self.assertEqual(pattern, "/DECISIONS.md")
        repo = self.repo_with(pattern)
        self.assertEqual(self.attr(repo, "DECISIONS.md"), "context-gate")
        self.assertEqual(self.attr(repo, "sub/DECISIONS.md"), "unspecified")

    def test_a_working_dir_that_is_the_root_has_no_dot_slash(self):
        pattern = merging.attribute_pattern(".", "traps*.md")
        self.assertEqual(pattern, "/traps*.md")
        repo = self.repo_with(pattern)
        self.assertEqual(self.attr(repo, "traps-1.md"), "context-gate")
        self.assertEqual(self.attr(repo, "sub/traps-1.md"), "unspecified")

    def test_a_path_with_a_space_is_quoted(self):
        pattern = merging.attribute_pattern("a b/DECISIONS.md")
        self.assertEqual(pattern, '"/a b/DECISIONS.md"')
        repo = self.repo_with(pattern, merging.attribute_pattern("a b", "traps*.md"))
        self.assertEqual(self.attr(repo, "a b/DECISIONS.md"), "context-gate")
        self.assertEqual(self.attr(repo, "a b/traps-1.md"), "context-gate")
        self.assertEqual(self.attr(repo, "a/DECISIONS.md"), "unspecified")

    def test_glob_characters_in_a_directory_name_are_literal(self):
        pattern = merging.attribute_pattern("a[1]/D*.md".replace("D*", "D"))
        repo = self.repo_with(pattern)
        self.assertEqual(self.attr(repo, "a[1]/D.md"), "context-gate")
        self.assertEqual(self.attr(repo, "a1/D.md"), "unspecified")


class Uninstall(Case):
    def installed(self) -> Tree:
        w = self.solo(extra=ON)
        cfg = self.tmp / "config.toml"
        shutil.move(str(w.root / layout.CONFIG), str(cfg))
        shutil.rmtree(w.root / layout.GOV_DIR)
        (w.root / ATTRS).unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.installer("install", "--root", str(w.root), "--config", str(cfg),
                                  "--no-report")[0]
        self.assertEqual(code, 0)
        return w

    def installer(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(self.tmp / "home")
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = installer.main(list(args))
        finally:
            if old is not None:
                os.environ["HOME"] = old
        return code, out.getvalue()

    def test_uninstall_removes_the_region(self):
        w = self.installed()
        w.run("index")
        w.write(ATTRS, "*.png binary\n" + w.read(ATTRS))
        code, out = self.installer("uninstall", "--root", str(w.root))
        self.assertEqual(code, 0, out)
        self.assertIn("  removed      .gitattributes merge region", out)
        self.assertEqual(w.read(ATTRS), "*.png binary\n")

    def test_uninstall_removes_a_file_holding_only_the_region(self):
        w = self.installed()
        w.run("index")
        self.assertEqual(w.read(ATTRS), REGION)
        code, out = self.installer("uninstall", "--root", str(w.root))
        self.assertEqual(code, 0, out)
        self.assertFalse((w.root / ATTRS).exists())


class Install(Case):
    def test_install_sets_config_and_hooks(self):
        w = self.solo(extra=ON)
        code, out, err = w.run("merge-driver", "install")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(w.git("config", "--get", "merge.context-gate.driver").strip(), DRIVER)
        self.assertEqual(w.git("config", "--get", "merge.context-gate.name").strip(),
                         "context-gate")
        for name in ("post-merge", "post-rewrite"):
            hook = hooks_dir(w) / name
            raw = hook.read_bytes()
            self.assertNotIn(b"\r", raw)
            text = raw.decode("utf-8")
            self.assertTrue(text.startswith("#!/bin/sh\n# context-gate: refresh generated "
                                            "blocks\n"), text)
            self.assertIn(f"{PYTHON} .context-gate/bin/govern index", text)
            self.assertIn("context-gate: generated blocks refreshed after the merge; commit "
                          "them", text)
            self.assertTrue(text.endswith("exit 0\n"))
            if os.name != "nt":
                self.assertTrue(os.access(hook, os.X_OK))
            self.assertIn(name, out)
        again = w.run("merge-driver", "install")
        self.assertEqual(again[0], 0, again[1] + again[2])      # ours already there: rewritten

    def test_a_foreign_hook_is_kept(self):
        w = self.solo(extra=ON)
        hook = hooks_dir(w) / "post-merge"
        hook.parent.mkdir(parents=True, exist_ok=True)
        wtext(hook, "#!/bin/sh\necho mine\n")
        code, out, err = w.run("merge-driver", "install")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(rtext(hook), "#!/bin/sh\necho mine\n")
        self.assertIn("  kept         post-merge: not context-gate's; add this line to it: "
                      "python3 .context-gate/bin/govern index", out)
        self.assertTrue((hooks_dir(w) / "post-rewrite").is_file())

    def test_core_hookspath_set_writes_no_hook(self):
        w = self.solo(extra=ON)
        w.git("config", "core.hooksPath", "our-hooks")
        code, out, err = w.run("merge-driver", "install")
        self.assertEqual(code, 0, out + err)
        for name in ("post-merge", "post-rewrite"):
            self.assertIn(f"  kept         {name}: not context-gate's", out)
            self.assertFalse((w.root / "our-hooks" / name).exists())
            self.assertFalse((w.root / ".git" / "hooks" / name).exists())
        self.assertEqual(w.git("config", "--get", "merge.context-gate.driver").strip(), DRIVER)

    def test_outside_a_git_repository_it_fails_closed(self):
        w = self.solo(extra=ON, git=None)
        code, out, err = w.run("merge-driver", "install")
        self.assertEqual(code, 1, out)
        self.assertIn("merge-driver install:", err)


class SessionStart(Case):
    def setUp(self) -> None:
        super().setUp()
        self.w = self.solo(extra=ON)
        self.built = self.tmp / "built"
        shutil.copytree(PLUGIN, self.built)
        shutil.copytree(ENGINE / "govern", self.built / "govern",
                        ignore=shutil.ignore_patterns("__pycache__"))

    def start(self, **extra: str) -> str:
        env = {**os.environ, "HOME": str(self.w.home), "CLAUDE_PROJECT_DIR": str(self.w.root),
               "CONTEXT_GATE_NO_UPDATE_CHECK": "1", **extra}
        res = subprocess.run(
            [sys.executable, str(self.built / "hooks" / "session_start.py")],
            input=json.dumps({"session_id": "S", "hook_event_name": "SessionStart"}),
            env=env, capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return res.stdout

    def test_session_start_notice_when_not_wired(self):
        self.assertIn(SAID, json.loads(self.start())["systemMessage"].splitlines())

    def test_silent_when_wired(self):
        self.w.run("merge-driver", "install")
        self.assertNotIn("merge-driver install", self.start())

    def test_silent_when_the_check_is_off(self):
        (self.w.root / ATTRS).unlink()
        self.assertNotIn("merge-driver install", self.start())

    def test_silent_when_gitattributes_does_not_assign_the_driver(self):
        # the file exists and the driver is not configured, but nothing asks for it
        wtext(self.w.root / ATTRS, "*.md text\n")
        self.assertNotIn("merge-driver install", self.start())

    def test_silent_when_git_cannot_run(self):
        self.assertNotIn("merge-driver install", self.start(PATH=str(self.tmp / "nowhere")))


class NestedScope(nested.Fixture):
    def test_a_scope_that_is_its_own_checkout_is_warned_about(self):
        cfg = self.root / layout.CONFIG
        wtext(cfg, rtext(cfg) + ON)
        code, out = self.run_cli("check")
        warn = (f"{nested.NESTED}: its own git checkout; the merge driver does not cover "
                f"{nested.NESTED}/DECISIONS.md")
        self.assertIn(warn, out)
        self.assertIn("warn", next(ln for ln in out.splitlines() if warn in ln))


class Pair(TwoClones):
    """Two developers, the check on and the driver installed in both clones."""

    EXTRA = ON

    def solo(self, **kw):
        return super().solo(extra=self.EXTRA, **kw)

    BIN = ("#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, {engine!r})\n"
           "from govern import cli\nsys.exit(cli.main(sys.argv[1:]))\n")

    def setUp(self) -> None:
        super().setUp()
        self.trees = {}
        for clone in (self.a, self.b):
            tree = Tree(clone, self.seed.home)
            wtext(clone / layout.ENTRYPOINT, self.BIN.format(engine=str(ENGINE)))
            tree.git("config", "merge.context-gate.name", "context-gate", at=clone)
            self.trees[clone] = tree
            code, out, err = self.run_in(clone, "merge-driver", "install")
            self.assertEqual(code, 0, out + err)
            self.commit(clone, "bin")
        self.push(self.a)
        self.fetch(self.b)
        self.seed.git("reset", "-q", "--hard", "origin/main", at=self.b)

    def run_in(self, clone: Path, *argv: str) -> tuple[int, str, str]:
        return self.trees[clone].run(*argv)

    def add(self, clone: Path, num: int, *, working: bool = True) -> None:
        wtext(clone / LOG, rtext(clone / LOG) + decision("P", num))
        wtext(clone / TRAPS, rtext(clone / TRAPS) + trap(num))
        if working:
            wtext(clone / f"docs/working/note-{num}.md",
                  fm("working", status="active") + f"# Note {num}\n")
        code, out, err = self.run_in(clone, "index")
        self.assertEqual(code, 0, out + err)
        self.commit(clone, f"work {num}")


class Clones(Pair):
    def test_the_seed_carries_the_region(self):
        self.assertEqual(rtext(self.a / ATTRS), REGION)

    def test_two_clones_end_to_end(self):
        self.add(self.a, 4)
        self.add(self.b, 5)
        self.push(self.b)
        self.fetch(self.a)
        res = self.merge(self.a, "origin/main")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        for path in (LOG, TRAPS, "docs/working/note-4.md", "docs/working/note-5.md"):
            self.assertNotIn("<<<<<<<", rtext(self.a / path))
        text = rtext(self.a / LOG)
        for num in (4, 5):
            self.assertIn(f"## P-{num} - Title {num}", text)
            self.assertIn(f"## T-{num} - Trap {num}", rtext(self.a / TRAPS))
        code, out, err = self.run_in(self.a, "check")
        self.assertEqual(code, 0, out + err)

    def test_same_id_collision_leaves_one_conflict_naming_it(self):
        for clone, title in ((self.a, "ours"), (self.b, "theirs")):
            wtext(clone / LOG, rtext(clone / LOG) + decision("P", 4, title=title))
            self.commit(clone, "P-4")
        self.push(self.b)
        self.fetch(self.a)
        res = self.merge(self.a, "origin/main")
        self.assertNotEqual(res.returncode, 0)
        conflicted = self.seed.git("diff", "--name-only", "--diff-filter=U", at=self.a)
        self.assertEqual(conflicted.split(), [LOG])
        text = rtext(self.a / LOG)
        self.assertEqual(text.count("P-4 was added on both branches"), 1)


class CappedClones(Pair):
    """The same two clones, with a word cap on working files low enough that a short one breaches."""

    EXTRA = ON + CAP

    def test_different_baseline_keys_merge_without_false_grew(self):
        k1, k2 = "working_file_words:docs/k1.md", "working_file_words:docs/k2.md"
        code, out, err = self.run_in(self.a, "baseline", "--allow-raise", "--reason", "seed")
        self.assertEqual(code, 0, out + err)
        self.commit(self.a, "seed baseline")
        self.push(self.a)
        self.fetch(self.b)
        self.seed.git("reset", "-q", "--hard", "origin/main", at=self.b)
        for clone, name, key in ((self.a, "k1", k1), (self.b, "k2", k2)):
            wtext(clone / f"docs/{name}.md", fm("working", status="active") + "word " * 10)
            code, out, err = self.run_in(clone, "baseline", "--allow-raise", key, "--reason", "r")
            self.assertEqual(code, 0, out + err)
            self.commit(clone, f"raise {key}")
        self.push(self.b)
        self.fetch(self.a)
        res = self.merge(self.a, "origin/main")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        held = json.loads(rtext(self.a / layout.BASELINE))
        self.assertIn(k1, held)
        self.assertIn(k2, held)
        code, out, err = self.run_in(self.a, "check")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("grew", out + err)


if __name__ == "__main__":
    unittest.main()
