"""Options: the checks that are off by default, and how a project or its profile turns them on.

    python3 -m unittest discover -s engine/tests -k options
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

import govern.checks  # noqa: E402,F401  (registers the built-in checks)
from govern import installer, layout, manifest, options, report  # noqa: E402
from test_single_repo import CFG, SingleRepo, wtext  # noqa: E402

PROFILE_ON = '''
[checks.writing-rules]
level = "error"
rules = [{ text = "colour", use = "color", ignore_case = true }]
'''


def repo(tmp: Path, profile: str | None = None, extra: str = "") -> SingleRepo:
    """A single repo, on a local profile directory when `profile` is given."""
    r = SingleRepo(tmp, extra=extra)
    if profile is not None:
        prof = tmp / "prof"
        prof.mkdir()
        wtext(prof / "principles.toml", profile)
        cfg = r.root / CFG
        wtext(cfg, cfg.read_text(encoding="utf-8").replace(
            "schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1))
    return r


class Base(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class LevelOverride(Base):
    def test_off_over_a_profile_on_without_a_reason_warns(self):
        r = repo(self.tmp, PROFILE_ON, '\n[checks.writing-rules]\nlevel = "off"\n')
        _, out, _ = r.run("check")
        self.assertIn("[checks.writing-rules] level overrides the profile with no reason", out)

    def test_with_a_reason_it_does_not(self):
        r = repo(self.tmp, PROFILE_ON,
                 '\n[checks.writing-rules]\nlevel = "off"\nreason = "Generated docs only."\n')
        _, out, _ = r.run("check")
        self.assertNotIn("level overrides the profile", out)


class Manifest(unittest.TestCase):
    def tearDown(self) -> None:
        manifest.forget("test")

    def test_needs_must_name_params(self):
        with self.assertRaisesRegex(ValueError, "needs names"):
            manifest.check("t-needs", scope="workspace", since="0.5.0", summary="s",
                           question="q", rationale="r", default="off", origin="test",
                           needs=("files",))

    def test_suggest_only_on_an_opt_in_check(self):
        with self.assertRaisesRegex(ValueError, "only an opt-in check"):
            manifest.check("t-suggest", scope="workspace", since="0.5.0", summary="s",
                           question="q", rationale="r", default="error", origin="test",
                           suggest=lambda ctx, s: None)

    def test_builtin_needs(self):
        self.assertEqual(manifest.CHECKS["writing-rules"].needs, ("files", "rules"))
        self.assertEqual(manifest.CHECKS["hooks-wired"].needs, ("hooks",))
        self.assertEqual(manifest.CHECKS["checkout-hygiene"].needs, ("roles",))
        self.assertEqual(manifest.CHECKS["licenses"].needs, ())


class Collect(Base):
    def by_id(self, r, done=None):
        return {o.id: o for o in options.collect(r.context(), done)}

    def test_engine_only(self):
        o = self.by_id(repo(self.tmp))
        self.assertEqual(sorted(o), ["checkout-hygiene", "hooks-wired", "licenses",
                                     "writing-rules"])
        w = o["writing-rules"]
        self.assertEqual((w.state, w.layer, w.inherit, w.new), ("off", "engine", True, True))

    def test_profile_on_without_files_is_inert(self):
        w = self.by_id(repo(self.tmp, PROFILE_ON))["writing-rules"]
        self.assertEqual((w.state, w.layer, w.inherit, w.missing),
                         ("inert", "profile", True, ["files"]))

    def test_project_on_with_files(self):
        w = self.by_id(repo(self.tmp, PROFILE_ON,
                            '\n[checks.writing-rules]\nlevel = "error"\nfiles = ["docs/*.md"]\n'))
        w = w["writing-rules"]
        self.assertEqual((w.state, w.layer, w.inherit, w.missing), ("on", "project", False, []))

    def test_project_off_over_profile_on(self):
        w = self.by_id(repo(self.tmp, PROFILE_ON,
                            '\n[checks.writing-rules]\nlevel = "off"\nreason = "r"\n'))
        self.assertEqual((w["writing-rules"].state, w["writing-rules"].layer), ("off", "project"))

    def test_an_extension_opt_in_check_is_an_option(self):
        manifest.check("t-ext", scope="workspace", since="0.5.0", summary="s", question="q",
                       rationale="r", default="off", origin="test")(lambda ctx, p: None)
        self.addCleanup(manifest.forget, "test")
        o = self.by_id(repo(self.tmp))["t-ext"]
        self.assertEqual((o.state, o.suggestion), ("off", None))

    def test_a_suggest_that_raises_offers_no_suggestion(self):
        def broken(ctx, s):
            raise TypeError("unhashable type: 'list'")
        manifest.check("t-broken", scope="workspace", since="0.5.0", summary="s", question="q",
                       rationale="r", default="off", origin="test", suggest=broken)(
            lambda ctx, p: None)
        self.addCleanup(manifest.forget, "test")
        o = self.by_id(repo(self.tmp))["t-broken"]
        self.assertEqual((o.state, o.suggestion), ("off", None))

    def test_answered_options_are_not_new(self):
        o = self.by_id(repo(self.tmp), {"writing-rules"})
        self.assertFalse(o["writing-rules"].new)
        self.assertTrue(o["licenses"].new)


class Record(Base):
    def manifest(self, r, text='engine = "0.5.0"\ninstalled = "2026-09-23"\n'):
        wtext(r.root / layout.MANIFEST, text)

    def test_record_keeps_the_rest_of_the_manifest(self):
        r = repo(self.tmp)
        self.manifest(r, 'engine = "0.5.0"\ninstalled = "2026-09-23"\n\n[[plugin]]\n'
                         'settings = ".claude/settings.json"\nid = "p@m"\nprevious = "absent"\n')
        options.record(r.root, ["writing-rules"])
        options.record(r.root, ["licenses"])
        self.assertEqual(options.answered(r.root), {"writing-rules", "licenses"})
        self.assertEqual(installer.read_manifest(r.root)["plugin"][0]["id"], "p@m")

    def test_an_answered_option_under_its_old_name_counts_as_answered(self):
        # `licences` is `licenses` now: an install record written before the rename still
        # counts, and the next write records the new name.
        r = repo(self.tmp)
        self.manifest(r, 'engine = "0.5.0"\ninstalled = "2026-09-23"\n'
                         'options_answered = ["licences"]\n')
        self.assertEqual(options.answered(r.root), {"licenses"})
        done = options.answered(r.root)
        self.assertFalse({o.id: o for o in options.collect(r.context(), done)}["licenses"].new)
        options.record(r.root, ["writing-rules"])
        self.assertEqual(installer.read_manifest(r.root)["options_answered"],
                         ["licenses", "writing-rules"])

    def test_record_refuses_a_check_that_is_not_an_option(self):
        r = repo(self.tmp)
        self.manifest(r)
        with self.assertRaisesRegex(ValueError, "not an option: agents"):
            options.record(r.root, ["agents"])

    def test_no_manifest(self):
        r = repo(self.tmp)
        self.assertIsNone(options.answered(r.root))
        with self.assertRaises(FileNotFoundError):
            options.record(r.root, ["writing-rules"])


class Global(Base):
    def test_profile_layer(self):
        o = {x.id: x for x in options.collect_global(repo(self.tmp, PROFILE_ON).context())}
        self.assertEqual((o["writing-rules"].state, o["writing-rules"].layer), ("on", "profile"))
        self.assertEqual((o["licenses"].state, o["licenses"].layer), ("off", "engine"))

    def test_no_profile(self):
        with self.assertRaisesRegex(ValueError, r"\[governance\] profile"):
            options.collect_global(repo(self.tmp).context())


def fake(root: Path, scopes=()):
    return SimpleNamespace(root=root, registry=SimpleNamespace(scopes=list(scopes)))


def settings(cid: str, level="off", source=None, **params):
    chk = manifest.CHECKS[cid]
    return SimpleNamespace(level=level, source=source or {},
                           params={**{k: p.default for k, p in chk.params.items()}, **params})


def suggest(cid, ctx, s):
    return manifest.CHECKS[cid].suggest(ctx, s)


class FakeScope:
    def __init__(self, name: str, **entry) -> None:
        self.name, self.entry = name, entry

    def get(self, key, default=None):
        return self.entry.get(key, default)


class Suggestions(Base):
    def test_writing_rules_with_rules_and_no_files(self):
        s = settings("writing-rules", "error", {"rules": "profile"}, rules=[{"text": "colour"}])
        self.assertEqual(suggest("writing-rules", fake(self.tmp), s),
                         "1 rule(s) set (profile) but no files named for them")

    def test_writing_rules_outward_copy(self):
        (self.tmp / "README.md").write_text("x", encoding="utf-8")
        self.assertEqual(suggest("writing-rules", fake(self.tmp), settings("writing-rules")),
                         "outward-facing copy is unchecked: README.md")

    def test_writing_rules_quiet(self):
        self.assertIsNone(suggest("writing-rules", fake(self.tmp), settings("writing-rules")))

    def test_hooks_unwired_script(self):
        hooks = self.tmp / ".claude/hooks"
        hooks.mkdir(parents=True)
        (hooks / "guard.py").write_text("", encoding="utf-8")
        (hooks / "wired.py").write_text("", encoding="utf-8")
        s = settings("hooks-wired", hooks=[{"script": ".claude/hooks/wired.py",
                                            "event": "PreToolUse", "matcher": "Bash"}])
        self.assertEqual(suggest("hooks-wired", fake(self.tmp), s),
                         ".claude/hooks/ has 1 script(s) nothing requires to be wired: guard.py")

    def test_hooks_quiet_without_the_dir(self):
        self.assertIsNone(suggest("hooks-wired", fake(self.tmp), settings("hooks-wired")))

    def test_checkouts_fork_with_no_role_check(self):
        ctx = fake(self.tmp, [FakeScope("downstream", upstream="git@x:y.git", role="contribute"),
                              FakeScope("mine")])
        self.assertEqual(suggest("checkout-hygiene", ctx, settings("checkout-hygiene")),
                         "registry entries with an upstream that no role check covers: downstream")

    def test_checkouts_quiet_when_covered(self):
        ctx = fake(self.tmp, [FakeScope("downstream", upstream="git@x:y.git", role="contribute")])
        s = settings("checkout-hygiene", roles=["contribute"])
        self.assertIsNone(suggest("checkout-hygiene", ctx, s))

    def test_licenses_mixed(self):
        ctx = fake(self.tmp, [FakeScope("a", license="MIT"), FakeScope("b", license="GPL-3.0"),
                              FakeScope("c")])
        self.assertEqual(suggest("licenses", ctx, settings("licenses")),
                         "3 registry entries; licenses: GPL-3.0, MIT; none declared: c")

    def test_licenses_with_a_list_valued_license(self):
        # Registry facts are untyped: a dual license written as a list is named, not a crash.
        ctx = fake(self.tmp, [FakeScope("a", license=["MIT", "Apache-2.0"]),
                              FakeScope("b", license="GPL-3.0")])
        self.assertEqual(suggest("licenses", ctx, settings("licenses")),
                         "2 registry entries; licenses: GPL-3.0, MIT + Apache-2.0")

    def test_licenses_quiet_when_one_license(self):
        ctx = fake(self.tmp, [FakeScope("a", license="MIT"), FakeScope("b", license="MIT")])
        self.assertIsNone(suggest("licenses", ctx, settings("licenses")))


class Command(Base):
    def test_table(self):
        code, out, _ = repo(self.tmp).run("options")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"writing-rules\s+off \(engine\)\s+0\.4\.0\s+yes")

    def test_json_field_set(self):
        code, out, _ = repo(self.tmp).run("options", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(list(json.loads(out)[0]),
                         ["id", "summary", "rationale", "question", "since", "state", "level",
                          "layer", "inherit", "new", "suggestion", "needs", "missing"])

    def test_record_without_a_manifest(self):
        code, _, err = repo(self.tmp).run("options", "--record", "writing-rules")
        self.assertEqual(code, 2)
        self.assertIn("install first", err)

    def test_record_then_not_new(self):
        r = repo(self.tmp)
        wtext(r.root / layout.MANIFEST, 'engine = "0.5.0"\ninstalled = "2026-09-23"\n')
        self.assertEqual(r.run("options", "--record", "writing-rules")[0], 0)
        o = {x["id"]: x for x in json.loads(r.run("options", "--json")[1])}
        self.assertFalse(o["writing-rules"]["new"])

    def test_global_without_a_profile(self):
        code, _, err = repo(self.tmp).run("options", "--global")
        self.assertEqual(code, 2)
        self.assertIn("[governance] profile", err)

    def test_global_with_a_profile(self):
        code, out, _ = repo(self.tmp, PROFILE_ON).run("options", "--global")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"writing-rules\s+on \(profile\)")



class UpgradeReport(Base):
    def write(self, opts):
        path = self.tmp / "r.md"
        report.write(path, "Upgrade report", None, [], "", "0.5.0", options=opts)
        return path.read_text(encoding="utf-8")

    def test_new_options_listed(self):
        o = options.collect(repo(self.tmp, PROFILE_ON).context(), None)
        text = self.write([x for x in o if x.new or x.state == "inert"])
        self.assertIn("## New options", text)
        self.assertIn("- `writing-rules` (inert, since 0.4.0)", text)
        self.assertLess(text.index("## New options"), text.index("## New findings"))

    def test_no_section_when_nothing_to_offer(self):
        self.assertNotIn("## New options", self.write([]))


if __name__ == "__main__":
    unittest.main()
