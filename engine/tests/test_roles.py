"""Rendered roles: the agent definitions and build rules `govern agents render` writes, the
`agent-roles` check that holds them to what the engine renders, and `agent-references`.

    python3 -m unittest discover -s engine/tests -k roles
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

import govern.checks  # noqa: E402,F401  (registers the built-in checks)
from govern import config, installer, layout, manifest, roles  # noqa: E402
from test_single_repo import CFG, Base, SingleRepo, fm, wtext  # noqa: E402

ON = '\n[checks.agent-roles]\nlevel = "error"\n'
AGENTS = ".claude/agents"
RULES = ".claude/rules/context-gate-build.md"
RENDER = "python3 .context-gate/bin/govern agents render"
CORE = ["developer", "senior-developer", "code-reviewer", "qa-engineer", "technical-writer"]
# A definition somebody wrote by hand: it carries no rendered note.
BY_HAND = ("---\nname: developer\ndescription: ours\nmodel: opus\neffort: low\n"
           "omitClaudeMd: true\n---\nOur own developer.\n")


def defaults(**over) -> dict:
    """The option's params as the engine ships them, with `over` on top."""
    return {**{k: p.default for k, p in manifest.CHECKS["agent-roles"].params.items()}, **over}


def rendered(role: str, **over) -> str:
    return roles.render(roles.shipped(role), role, defaults(**over))


def raw(path: Path) -> str:
    """The file's characters as they are on disk, line endings included."""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def record_install(root: Path) -> Path:
    """An `installed.toml` as an install that touched nothing outside its directory writes."""
    path = root / layout.MANIFEST
    installer.write_manifest(path, {"engine": "0.0.0", "installed": "2026-01-01",
                                    "entrypoint": [], "moved": [], "retired": [], "plugin": [],
                                    "marketplace": [], "settings": []})
    return path


class Render(unittest.TestCase):
    """A rendered file is a pure function of its template, the params and the inserts."""

    def test_every_shipped_template_renders_with_defaults_and_leaves_no_slot(self):
        shipped = sorted(p.stem for p in (roles.TEMPLATES / "agents").glob("*.md"))
        self.assertEqual(shipped, sorted(roles.ROLES))
        for role in (*roles.ROLES, roles.RULES):
            with self.subTest(role=role):
                text = rendered(role)
                self.assertNotIn("{{", text)
                self.assertNotIn("}}", text)
                self.assertNotIn("\r", text)

    def test_an_unknown_slot_is_an_error(self):
        with self.assertRaises(roles.UnknownSlot) as caught:
            roles.render("Run {{nope}} now.\n", "developer", defaults())
        self.assertEqual(caught.exception.slot, "nope")
        self.assertIn("{{nope}}", str(caught.exception))

    def test_a_slot_of_the_other_kind_of_template_is_unknown_there(self):
        with self.assertRaises(roles.UnknownSlot):
            roles.render("model: {{model}}\n", roles.RULES, defaults())
        with self.assertRaises(roles.UnknownSlot):
            roles.render("{{roles_table}}\n", "developer", defaults())

    def test_an_insert_is_verbatim_and_never_read_for_slots(self):
        text = roles.render(roles.shipped("developer"), "developer", defaults(),
                            "- Keep {{model}} out of prose.")
        self.assertIn("\n- Keep {{model}} out of prose.\n", text)
        self.assertNotIn(roles.NO_RULES, text)

    def test_every_role_renders_one_finish_section_that_ends_the_file(self):
        self.assertEqual(len(roles.ROLES), 7)
        for role in roles.ROLES:
            with self.subTest(role=role):
                text = rendered(role, extra_roles=["ux-reviewer", "architect"],
                                unit_gate="full")
                headings = [ln for ln in text.split("\n") if ln.startswith("## ")]
                self.assertEqual(headings.count("## Finish"), 1)
                self.assertEqual(headings[-1], "## Finish")
                for gone in ("## Report", "## Before you report", "## Before you hand back"):
                    self.assertNotIn("\n" + gone + "\n", text)
                finish = text.split("\n## Finish\n\n", 1)[1]
                self.assertEqual(finish.count(roles.HANDBACK_BINDING), 1)
                self.assertEqual(finish.count("\n\n**Your final message**\n\n"), 1)
                block, tail = finish.split("\n\n**Your final message**\n\n", 1)
                self.assertIn("Status:", tail)
                if role in ("developer", "senior-developer"):
                    self.assertTrue(block.startswith("- Re-read every comment"))
                    block = block.split("\n\n" + roles.HANDBACK_BINDING, 1)[1]
                    block = roles.HANDBACK_BINDING + block
                else:
                    self.assertTrue(block.startswith(roles.HANDBACK_BINDING))
                binding, items = block.split("\n\n", 1)
                self.assertEqual(binding, roles.HANDBACK_BINDING)
                keys = roles.HANDBACK_ROLES.get(role, roles.HANDBACK_DEFAULT)
                self.assertEqual(items.split("\n"), [
                    f"{n}. `{k}:` {roles.HANDBACK[k]}" for n, k in enumerate(keys, 1)])

    def test_the_final_message_formats_follow_the_final_message_line(self):
        for role in roles.ROLES:
            with self.subTest(role=role):
                tail = rendered(role, extra_roles=["ux-reviewer", "architect"]).split(
                    "**Your final message**", 1)[1]
                self.assertIn("Status:", tail)
                if role in ("code-reviewer", "qa-engineer", "architect", "ux-reviewer"):
                    self.assertIn("`Findings: critical=0 important=0 minor=0`", tail)

    def test_the_architect_ties_a_small_phase_to_a_migration_or_security(self):
        self.assertIn("a phase is everything up to the next owner checkpoint, reviewed once; "
                      "only a migration, or security work that later units build on, is a "
                      "small phase reviewed early.", rendered("architect", extra_roles=["architect"]))

    def test_the_rules_tie_a_small_phase_to_a_migration_or_security(self):
        text = rendered(roles.RULES)
        self.assertIn("Only a migration, or security work that later units build on, becomes a "
                      "small phase of its own, reviewed early.", text)
        self.assertNotIn("needs early eyes", text)

    def test_handback_keys_per_role(self):
        full = ["Scope", "Red", "Gate", "Commit", "Not verified", "Report"]
        want = {"developer": full, "senior-developer": full,
                "technical-writer": ["Scope", "Gate", "Commit", "Not verified", "Report"]}
        for role in roles.ROLES:
            with self.subTest(role=role):
                self.assertEqual(roles.handback_keys(role, defaults()),
                                 want.get(role, ["Not verified", "Report"]))

    def test_handback_checks_add_a_line_to_every_role_before_not_verified(self):
        line = "Licences: each new dependency's licence"
        params = defaults(handback_checks=[line])
        self.assertEqual(roles.handback_keys("code-reviewer", params),
                         ["Licences", "Not verified", "Report"])
        for role in roles.ROLES:
            with self.subTest(role=role):
                keys = roles.handback_keys(role, params)
                self.assertEqual(keys.index("Licences") + 1, keys.index("Not verified"))
                text = roles.render(roles.shipped(role), role, params)
                n = keys.index("Licences") + 1
                self.assertIn(f"\n{n}. `Licences:` each new dependency's licence\n"
                              f"{n + 1}. `Not verified:`", text)

    def test_bad_handback_checks_are_problems_that_name_the_entry(self):
        for entry in ("no colon here", "lower: case key", ": empty key", "Scope: a built-in",
                      "Licences:no space", "Trail : y"):
            with self.subTest(entry=entry):
                found = roles.problems(defaults(handback_checks=[entry]))
                self.assertEqual(len(found), 1)
                self.assertIn(repr(entry), found[0])
        found = roles.problems(defaults(handback_checks=["Same: a", "Same: b"]))
        self.assertEqual(len(found), 1)
        self.assertIn("'Same: b'", found[0])
        self.assertEqual(roles.problems(defaults(handback_checks=["Licences: ok"])), [])

    def test_a_line_problems_would_report_is_left_out_of_the_checklist(self):
        params = defaults(handback_checks=["Scope: mine", ": empty", "Trail : y", "Licences: l"])
        self.assertEqual(roles.handback_keys("code-reviewer", params),
                         ["Licences", "Not verified", "Report"])
        self.assertEqual(roles.handback_texts("code-reviewer", params)["Licences"], "l")

    def test_the_binding_and_report_line_carry_the_answer_shape_and_early_stop(self):
        self.assertIn("Answer a line as `Key: <your answer>`, on one line. When the brief has no",
                      roles.HANDBACK_BINDING)
        self.assertTrue(roles.HANDBACK["Report"].endswith(
            "A run that stops early (`BLOCKED`, `NEEDS_CONTEXT`) still writes the file and "
            "answers every line it can."))

    def test_a_bad_handback_entry_does_not_break_the_render(self):
        rendered("developer", handback_checks=["no colon here"])

    def test_no_slot_is_left_and_the_build_rules_do_not_carry_the_checklist(self):
        for role in roles.ROLES:
            with self.subTest(role=role):
                text = rendered(role, extra_roles=["ux-reviewer", "architect"])
                self.assertNotIn("{{report_first}}", text)
                self.assertNotIn("{{handback}}", text)
        self.assertNotIn(roles.HANDBACK_BINDING, rendered(roles.RULES))
        self.assertNotIn("Before you hand back", rendered(roles.RULES))

    def test_no_insert_renders_the_line_that_says_so(self):
        self.assertIn("\n- This project has added no rules of its own.\n", rendered("developer"))

    def test_operator_rules_default_renders_every_role_as_before(self):
        for role in roles.ROLES:
            with self.subTest(role=role):
                self.assertEqual(rendered(role), rendered(role, operator_rules=[]))
                self.assertIn("\n" + roles.NO_RULES + "\n", rendered(role))

    def test_operator_rules_come_first_and_in_order(self):
        for role in roles.ROLES:
            with self.subTest(role=role):
                text = roles.render(roles.shipped(role), role,
                                    defaults(operator_rules=["a rule", "b rule"]), "- Insert.")
                self.assertIn("\n- a rule\n- b rule\n- Insert.\n", text)
                self.assertNotIn(roles.NO_RULES, text)

    def test_operator_rules_without_inserts_drop_the_no_rules_line(self):
        for role in roles.ROLES:
            with self.subTest(role=role):
                text = rendered(role, operator_rules=["a rule"])
                self.assertIn("\n- a rule\n", text)
                self.assertNotIn(roles.NO_RULES, text)

    def test_operator_rules_are_not_in_the_build_rules(self):
        self.assertEqual(rendered(roles.RULES, operator_rules=["a rule"]),
                         rendered(roles.RULES))
        self.assertNotIn("a rule", rendered(roles.RULES, operator_rules=["a rule"]))

    def test_each_role_defaults_to_the_engines_model_and_effort(self):
        want = {"developer": ("sonnet", "medium"), "senior-developer": ("opus", "high"),
                "code-reviewer": ("opus", "medium"), "qa-engineer": ("opus", "high"),
                "technical-writer": ("sonnet", "low"), "ux-reviewer": ("sonnet", "medium"),
                "architect": ("opus", "high")}
        for role, (model, effort) in want.items():
            with self.subTest(role=role):
                head = rendered(role).split("---")[1]
                self.assertIn(f"\nname: {role}\n", head)
                self.assertIn(f"\nmodel: {model}\n", head)
                self.assertIn(f"\neffort: {effort}\n", head)
                self.assertIn("\nomitClaudeMd: true\n", head)
                self.assertIn("\nmaxTurns: false\n", head)

    def test_models_efforts_caps_and_claude_md_are_set_per_role(self):
        head = rendered("developer", models={"developer": "haiku"}, efforts={"developer": "max"},
                        max_turns={"developer": 80}, load_claude_md=["developer"]).split("---")[1]
        self.assertIn("\nmodel: haiku\n", head)
        self.assertIn("\neffort: max\n", head)
        self.assertIn("\nmaxTurns: 80\n", head)
        self.assertIn("\nomitClaudeMd: false\n", head)
        other = rendered("qa-engineer", models={"developer": "haiku"},
                         max_turns={"developer": 80}, load_claude_md=["developer"])
        self.assertIn("\nmodel: opus\n", other)
        self.assertIn("\nmaxTurns: false\n", other)
        self.assertIn("\nomitClaudeMd: true\n", other)

    def test_the_note_is_the_first_line_after_the_frontmatter(self):
        text = rendered("developer")
        body = text.split("\n---\n", 1)[1]
        self.assertEqual(body.split("\n")[0],
                         "<!-- context-gate:rendered role=developer — do not edit: change "
                         ".context-gate/config.toml or .context-gate/agents/developer.md, then "
                         f"run {RENDER} -->")
        self.assertEqual(roles.note_role(text), "developer")
        self.assertEqual(roles.note_role(text.replace("\n", "\r\n")), "developer")
        self.assertIsNone(roles.note_role(BY_HAND))

    def test_the_note_is_the_first_line_of_the_rules(self):
        text = rendered(roles.RULES)
        self.assertTrue(text.startswith("<!-- context-gate:rendered role=build-rules — do not "
                                        "edit: change .context-gate/config.toml, then run "
                                        f"{RENDER} -->\n"), text[:200])
        self.assertEqual(roles.note_role(text), "build-rules")

    def test_govern_is_the_projects_entry_point(self):
        self.assertEqual(roles.GOVERN, f"python3 {layout.ENTRYPOINT}")
        rules = rendered(roles.RULES)
        self.assertIn(f"`{roles.GOVERN} phase close`", rules)
        self.assertIn(f"`{roles.GOVERN} runs mark <label>`", rules)
        self.assertIn(f"`{roles.GOVERN} runs report`", rules)

    def test_rules_run_the_report_before_phase_close(self):
        rules = rendered(roles.RULES)
        self.assertIn("Before you close it with `phase close`, run", rules)
        self.assertNotIn("After closing it, run", rules)

    def test_rules_stop_for_the_owner_only_at_checkpoints(self):
        rules = rendered(roles.RULES)
        self.assertIn("A plan marks its owner checkpoints", rules)
        self.assertNotIn("The first gate of a build phase", rules)

    def test_rules_carry_on_by_default(self):
        self.assertIn("Carry on from one phase to the next without asking.",
                      rendered(roles.RULES))

    def test_authority_defaults_to_the_plan_or_spec_the_brief_names(self):
        self.assertIn("and the plan or spec it names is the authority behind it",
                      rendered("developer"))
        self.assertIn("and docs/plan.md is the authority behind it",
                      rendered("developer", authority="docs/plan.md"))

    def test_a_command_renders_in_backticks_or_not_at_all(self):
        plain = rendered("developer", unit_gate="full")
        self.assertIn("run only the tests your unit touches. A filter", plain)
        self.assertIn("once, before you report.\n", plain)
        named = rendered("developer", scoped_test="make test T=<name>", full_gate="make ci",
                         unit_gate="full")
        self.assertIn("run only the tests your unit touches (`make test T=<name>`). A filter",
                      named)
        self.assertIn("once, before you report (`make ci`).\n", named)
        rules = rendered(roles.RULES, full_gate="make ci")
        self.assertIn("on the phase branch (`make ci`). A unit that turns it red is failed.\n",
                      rules)

    def test_triggers_and_high_risk_paths_are_bulleted(self):
        rules = rendered(roles.RULES)
        self.assertIn("handed it back or failed it 2 times:\n"
                      "- " + roles.SENIOR_TRIGGERS[0] + "\n", rules)
        self.assertIn("\n- nothing configured: no phase is high-risk until `high_risk_paths` "
                      "names its paths\n", rules)
        mine = rendered(roles.RULES, senior_triggers=["money"], escalate_after=3,
                        high_risk_paths=["db/**", "auth/"])
        self.assertIn("failed it 3 times:\n- money\n- You may send", mine)
        self.assertIn("touches:\n- db/**\n- auth/\n" + roles.NO_VISUAL + "\n- Hand reviewers", mine)

    def test_rules_never_stop_a_running_agent_for_routing(self):
        rules = rendered(roles.RULES)
        self.assertIn("A fix pass goes to a fresh agent.\n- Never stop a running agent to change "
                      "its routing: let it finish, or let it fail.\n- `developer` takes", rules)

    def test_visual_paths_turn_on_ux_reviewer(self):
        rules = rendered(roles.RULES)
        self.assertIn("\n- The project can also render `ux-reviewer`, which launches the built "
                      "product and looks at it: set `visual_paths` on `agent-roles` to turn it "
                      "on.\n", rules)
        self.assertNotIn("A phase is visual", rules)
        self.assertNotIn("ux-reviewer", roles.rendered(defaults()))
        on = defaults(visual_paths=["web/**"])
        self.assertIn("ux-reviewer", roles.rendered(on))
        self.assertIn("ux-reviewer", roles.rendered(defaults(extra_roles=["ux-reviewer"])))
        text = rendered(roles.RULES, visual_paths=["web/**"])
        self.assertIn("\n- A phase is visual when it touches one of these. Start it with "
                      "`--visual`, and `ux-reviewer` then looks at the running product beside "
                      "`code-reviewer`:\n- web/**\n- Hand reviewers", text)
        self.assertNotIn("can also render", text)
        self.assertIn("| `ux-reviewer` |", roles.roles_table(on))
        self.assertNotIn("| `ux-reviewer` |", roles.roles_table(defaults()))

    def test_role_lines_from_the_last_builds(self):
        wt = ("In a worktree the harness refuses compound commands. One plain command per call: "
              "no chaining, pipes, substitution, loops or heredocs. Put a multi-line script in a "
              "file inside your own worktree, run the file, and delete it before you hand back. "
              "Edit files with the edit tool.")
        for role in ("developer", "senior-developer"):
            self.assertIn(wt, rendered(role), role)
        self.assertIn("- Measure what a screenshot cannot settle (positions, widths, overflow, "
                      "text that touches), and open every screenshot you take.\n",
                      rendered("ux-reviewer"))
        self.assertIn("\n4. When the phase has a running system, verify its rules and its flow "
                      "on that system, never on a fixture or a copied page.\n",
                      rendered("qa-engineer"))
        last = "- Leave no command running in the background when you hand back.\n\n## "
        for role in ("technical-writer", "qa-engineer", "ux-reviewer"):
            text = rendered(role)
            self.assertIn(last, text, role)
            self.assertEqual(text.count("Leave no command running"), 1, role)

    def test_default_rules_route_only_security_to_senior(self):
        rules = rendered(roles.RULES)
        sentence = ("security: credentials, keys, tokens, signing, sign-in and sessions, the "
                    "code that decides who may read or change protected data, or input from "
                    "outside the project (a download, an archive, pasted text). A product rule "
                    "about who may do what is not security.")
        self.assertIn("\n- " + sentence + "\n", rules)
        self.assertIn("\n- " + sentence + "\n", rendered("developer"))
        self.assertNotIn("a schema, a public API", rules)
        self.assertNotIn("a release or migration path", rules)

    def test_rules_let_the_coordinator_override_and_keep_senior_units_scoped(self):
        rules = rendered(roles.RULES)
        a = "- You may send a unit to `senior-developer` outside that list when you can say why."
        b = ("- A unit for `senior-developer` holds only the work that sends it there. "
             "The work around it that `developer` can do is a unit of its own.")
        c = "- A plan's own routing label is checked against the trigger list, never followed."
        self.assertIn(a + " Say the reason when you launch it. This is rare.\n" + b + "\n" + c + "\n",
                      rules)
        trigger = "\n".join("- " + t for t in roles.SENIOR_TRIGGERS)
        self.assertIn(trigger + "\n" + a, rules)

    def test_own_triggers_replace_the_list_and_keep_the_two_bullets(self):
        rules = rendered(roles.RULES, senior_triggers=["money"])
        self.assertIn("\n- money\n", rules)
        self.assertNotIn(roles.SENIOR_TRIGGERS[0], rules)
        self.assertIn("- You may send a unit to `senior-developer` outside that list", rules)
        self.assertIn("- A unit for `senior-developer` holds only the work that sends it there.",
                      rules)

    def test_role_files_list_triggers_without_the_coordinator_bullets(self):
        for role in ("developer", "senior-developer", "architect"):
            text = rendered(role)
            self.assertIn("- " + roles.SENIOR_TRIGGERS[0], text, role)
            self.assertNotIn("outside that list when you can say why", text, role)
            self.assertNotIn("holds only the work that sends it there", text, role)

    def test_developer_roles_say_how_to_work_under_the_worktree_guard(self):
        for role in ("developer", "senior-developer"):
            text = rendered(role)
            self.assertIn("One plain command per call", text, role)
            self.assertLess(text.index("One plain command per call"),
                            text.index("## Rules that bind every change"), role)

    def test_other_roles_do_not_carry_the_worktree_guard_rule(self):
        for role in ("code-reviewer", "qa-engineer", "technical-writer"):
            self.assertNotIn("One plain command per call", rendered(role), role)

    def test_housekeeping_renders_two_bullets_or_nothing(self):
        on = rendered(roles.RULES)
        self.assertIn("\n- When a phase's fix pass closes, launch `technical-writer` with a "
                      "brief that says the phase is closing, before you start the next phase.\n"
                      f"- Then run `{roles.GOVERN} phase close`: it pushes the phase branch to "
                      "its remote, confirms the remote holds it, and only then removes branches "
                      "and worktrees already merged into it. It opens no pull request and merges "
                      "nothing upward. Run it only when no agent is still working.\n"
                      "- When the project has `agent-measure` on", on)
        off = rendered(roles.RULES, housekeeping=False)
        self.assertNotIn("govern phase close", off)
        self.assertIn("things a command can show.\n- When the project has `agent-measure` on",
                      off)

    def test_the_roles_table_lists_the_rendered_roles_with_their_model_and_use(self):
        rules = rendered(roles.RULES)
        self.assertIn("| `developer` | sonnet | medium | Implements one unit of work from a "
                      "brief file, test first, looping until the tests are green. |\n", rules)
        for role in CORE:
            self.assertIn(f"\n| `{role}` | ", rules)
        self.assertNotIn("| `architect` |", rules)
        more = rendered(roles.RULES, extra_roles=["architect"], models={"architect": "sonnet"},
                        efforts={"architect": "low"})
        self.assertIn("\n| `architect` | sonnet | low | Reviews a large plan before code is "
                      "written", more)
        self.assertNotIn("| `ux-reviewer` |", more)


NO_FULL_GATE = ("Do not run the full gate: the coordinator runs it once your unit is merged.")
BOTH = ["ux-reviewer", "architect"]


class UnitGate(unittest.TestCase):
    """`unit_gate`: a unit runs its own tests, and the coordinator runs the full gate."""

    def test_scoped_is_the_default_and_the_developers_run_no_full_gate(self):
        for role in ("developer", "senior-developer"):
            with self.subTest(role=role):
                text = rendered(role)
                self.assertIn(NO_FULL_GATE, text)
                self.assertIn("A filter that matches nothing prints a pass: check the total "
                              "is not zero. " + NO_FULL_GATE.split(".")[0], text)
                self.assertIn("`DONE`: everything in the brief is done and your unit's tests "
                              "are green.", text)
                self.assertNotIn("Run the full gate once", text)
                self.assertNotIn("run the full gate once", text)
                self.assertNotIn("the full gate is green", text)
                self.assertIn("3. `Gate:` the command you ran for your unit's tests and its "
                              "result line (the count is not zero). The coordinator runs the "
                              "full gate.\n", text)

    def test_the_rules_hold_the_merge_gate_bullet_after_the_worktree_bullet(self):
        rules = rendered(roles.RULES, full_gate="make ci")
        self.assertIn(
            "You merge unit branches into the phase branch.\n"
            "- After you merge a unit, run the full gate on the phase branch (`make ci`). A "
            "unit that turns it red is failed.\n- After each unit you merge, commit", rules)
        self.assertIn("phase branch. A unit that turns it red is failed.\n",
                      rendered(roles.RULES))

    def test_the_roles_that_run_no_unit_gate_render_the_same_under_both(self):
        for role in ("architect", "code-reviewer", "qa-engineer", "technical-writer",
                     "ux-reviewer"):
            with self.subTest(role=role):
                self.assertEqual(rendered(role, extra_roles=BOTH, unit_gate="full"),
                                 rendered(role, extra_roles=BOTH, unit_gate="scoped"))

    def test_full_drops_the_scoped_text_from_the_developers_and_the_rules(self):
        for role in ("developer", "senior-developer", roles.RULES):
            with self.subTest(role=role):
                text = rendered(role, unit_gate="full")
                for gone in (roles.NO_FULL_GATE, roles.GATE_SCOPED, "After you merge a unit"):
                    self.assertNotIn(gone, text)
        for role in ("developer", "senior-developer"):
            with self.subTest(role=role):
                text = rendered(role, unit_gate="full")
                for kept in ("the full gate once, before you report",
                             "the full gate is green", roles.HANDBACK["Gate"]):
                    self.assertIn(kept, text)

    def test_full_renders_the_developers_with_the_full_gate(self):
        dev = rendered("developer", unit_gate="full", full_gate="make ci")
        self.assertIn("Run the full gate once, before you report (`make ci`).", dev)
        self.assertIn("`DONE`: everything in the brief is done and the full gate is green.", dev)
        senior = rendered("senior-developer", unit_gate="full", full_gate="make ci")
        self.assertIn("touches; run the full gate once, before you report (`make ci`).\n", senior)
        self.assertNotIn(NO_FULL_GATE, senior)
        self.assertIn("2. `Red:`", dev)
        self.assertIn("`Gate:` the full gate's result line. You ran it once, at the end.", dev)
        self.assertNotIn("After you merge a unit", rendered(roles.RULES, unit_gate="full"))

    def test_another_value_is_a_problem_that_names_it(self):
        found = roles.problems(defaults(unit_gate="sometimes"))
        self.assertEqual(len(found), 1)
        self.assertIn("sometimes", found[0])
        self.assertEqual(roles.problems(defaults(unit_gate="full")), [])
        self.assertEqual(roles.problems(defaults()), [])

    def test_the_technical_writer_renders_the_same_under_both(self):
        self.assertEqual(rendered("technical-writer", unit_gate="full"),
                         rendered("technical-writer", unit_gate="scoped"))
        self.assertEqual(roles.handback_texts("technical-writer", defaults())["Gate"],
                         roles.HANDBACK["Gate"])

    def test_a_params_dict_without_the_param_reads_as_scoped(self):
        self.assertEqual(roles.handback_texts("developer", {"handback_checks": []})["Gate"],
                         roles.GATE_SCOPED)


class Command(Base):
    """`govern agents render [--check]`."""

    def on(self, more: str = "", **kw) -> SingleRepo:
        return self.repo(extra=ON + more, **kw)

    def test_render_writes_the_five_core_roles_and_the_rules_and_the_gate_is_green(self):
        r = self.on()
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertIn("[OK] agents render  (6 written, 0 removed)", out)
        for role in CORE:
            self.assertIn(f"  rendered    {AGENTS}/{role}.md\n", out)
            self.assertEqual(raw(r.root / AGENTS / f"{role}.md"), rendered(role))
        self.assertEqual(raw(r.root / RULES), rendered(roles.RULES))
        self.assertFalse((r.root / AGENTS / "architect.md").exists())
        self.assertFalse((r.root / AGENTS / "ux-reviewer.md").exists())
        code, out, _ = r.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn(".claude/", out)

    def test_extra_roles_adds_the_two_optional_roles(self):
        r = self.on('extra_roles = ["ux-reviewer", "architect"]\n')
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p.stem for p in (r.root / AGENTS).glob("*.md")),
                         sorted([*roles.ROLES, "reviewer"]))
        self.assertIn("| `architect` |", raw(r.root / RULES))

    def test_rendering_twice_changes_nothing(self):
        r = self.on()
        record_install(r.root)
        r.run("agents", "render")
        before = snapshot(r.root)
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        self.assertIn("[OK] agents render  (0 written, 0 removed)", out)
        self.assertNotIn("rendered    ", out)
        self.assertNotIn("recorded    ", out)
        self.assertEqual(snapshot(r.root), before)

    def test_files_are_written_with_lf(self):
        r = self.on()
        r.run("agents", "render")
        for path in [*(r.root / AGENTS).glob("*.md"), r.root / RULES]:
            self.assertNotIn(b"\r", path.read_bytes(), path.name)

    def test_a_crlf_checkout_of_a_rendered_file_is_not_stale(self):
        r = self.on()
        r.run("agents", "render")
        for path in (r.root / AGENTS / "developer.md", r.root / RULES):
            wtext(path, raw(path).replace("\n", "\r\n"))
        before = snapshot(r.root)
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 0, out)
        code, out, _ = r.run("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("differs from what the engine renders", out)
        r.run("agents", "render")
        self.assertEqual(snapshot(r.root), before)       # and so it is not rewritten either

    def test_a_bom_is_not_stale_and_survives_a_rewrite(self):
        r = self.on()
        r.run("agents", "render")
        path = r.root / AGENTS / "developer.md"
        wtext(path, "\ufeff" + raw(path))
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 0, out)
        wtext(path, "\ufeff" + raw(path).lstrip("\ufeff").replace("You implement", "You do"))
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        self.assertEqual(raw(path), "\ufeff" + rendered("developer"))

    def test_check_flag_writes_nothing_and_exits_1_when_anything_would_change(self):
        r = self.on()
        before = snapshot(r.root)
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"  missing     {AGENTS}/developer.md\n", out)
        self.assertIn("[FAIL] agents render --check  (6 file(s) would change)", out)
        self.assertEqual(snapshot(r.root), before)
        r.run("agents", "render")
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 0, out)
        self.assertIn("[OK] agents render --check  (0 file(s) would change)", out)
        path = r.root / AGENTS / "developer.md"
        wtext(path, raw(path) + "\nAn edit.\n")
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"  stale       {AGENTS}/developer.md\n", out)
        self.assertTrue(raw(path).endswith("\nAn edit.\n"))

    def test_a_file_without_the_note_is_never_overwritten(self):
        r = self.on()
        path = r.write(f"{AGENTS}/developer.md", BY_HAND)
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 1, out)
        self.assertEqual(raw(path), BY_HAND)
        want = (f"{AGENTS}/developer.md: was not rendered by context-gate, so it is left alone "
                f"— move the file and run `{RENDER}`, or turn agent-roles off ('developer' is "
                f"always rendered)")
        self.assertIn(f"  conflict    {want}\n", out)
        self.assertIn("[FAIL] agents render  (5 written, 0 removed, 1 conflict(s))", out)
        self.assertEqual(raw(r.root / AGENTS / "qa-engineer.md"), rendered("qa-engineer"))
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"  ERROR  {want}\n", out)
        code, out, _ = r.run("agents", "render", "--check")
        self.assertEqual(code, 1, out)
        self.assertIn("(0 file(s) would change, 1 conflict(s))", out)

    def test_a_conflict_on_an_extra_role_or_the_rules_names_its_own_way_out(self):
        r = self.on('extra_roles = ["architect"]\n')
        r.write(f"{AGENTS}/architect.md", BY_HAND.replace("developer", "architect"))
        r.write(RULES, "# Our own rules\n")
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 1, out)
        self.assertIn("or drop 'architect' from [checks.agent-roles] extra_roles", out)
        self.assertIn("or point [checks.agent-roles] rules_file at another path", out)
        self.assertEqual(raw(r.root / RULES), "# Our own rules\n")

    def test_a_conflict_on_ux_reviewer_names_visual_paths(self):
        r = self.on('visual_paths = ["web/**"]\n')
        r.write(f"{AGENTS}/ux-reviewer.md", BY_HAND.replace("developer", "ux-reviewer"))
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 1, out)
        self.assertIn("or empty [checks.agent-roles] visual_paths", out)
        self.assertNotIn("extra_roles", out)

    def test_a_conflict_on_ux_reviewer_in_both_places_names_both(self):
        r = self.on('visual_paths = ["web/**"]\nextra_roles = ["ux-reviewer"]\n')
        r.write(f"{AGENTS}/ux-reviewer.md", BY_HAND.replace("developer", "ux-reviewer"))
        code, out, _ = r.run("agents", "render")
        self.assertIn("or drop 'ux-reviewer' from [checks.agent-roles] extra_roles and empty "
                      "[checks.agent-roles] visual_paths", out)

    def test_a_target_that_is_not_utf8_is_left_alone_and_reported(self):
        r = self.on()
        path = r.root / AGENTS / "developer.md"
        path.write_bytes(b"\xff\xfe not text\n")
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"{AGENTS}/developer.md: not valid UTF-8 (byte 0), so it is left alone",
                      out)
        self.assertEqual(path.read_bytes(), b"\xff\xfe not text\n")

    def test_a_role_no_longer_configured_is_removed_and_warned_about_until_then(self):
        r = self.on('extra_roles = ["architect"]\n')
        r.run("agents", "render")
        cfg = r.root / CFG
        wtext(cfg, raw(cfg).replace('extra_roles = ["architect"]\n', ""))
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)        # the rules still list the architect: stale
        self.assertIn(f"  warn   {AGENTS}/architect.md: rendered for `architect`, which is no "
                      f"longer rendered — run `{RENDER}` to remove it\n", out)
        code, out, _ = r.run("agents", "render", "--check")
        self.assertIn(f"  orphaned    {AGENTS}/architect.md", out)
        self.assertTrue((r.root / AGENTS / "architect.md").exists())
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        self.assertIn(f"  removed     {AGENTS}/architect.md\n", out)
        self.assertIn("(1 written, 1 removed)", out)
        self.assertFalse((r.root / AGENTS / "architect.md").exists())
        self.assertTrue((r.root / AGENTS / "reviewer.md").exists())     # never ours
        code, out, _ = r.run("check")
        self.assertEqual(code, 0, out)

    def test_a_moved_rules_file_leaves_no_copy_behind_where_an_install_recorded_it(self):
        r = self.on()
        record_install(r.root)
        r.run("agents", "render")
        cfg = r.root / CFG
        wtext(cfg, raw(cfg) + 'rules_file = ".claude/rules/build.md"\n')
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        self.assertFalse((r.root / RULES).exists())
        self.assertEqual(raw(r.root / ".claude/rules/build.md"), rendered(roles.RULES))

    def test_missing_and_stale_files_are_errors_that_name_the_command(self):
        r = self.on()
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"  ERROR  {AGENTS}/developer.md: `developer` is not rendered — run "
                      f"`{RENDER}`\n", out)
        self.assertIn(f"  ERROR  {RULES}: `build-rules` is not rendered — run `{RENDER}`\n", out)
        r.run("agents", "render")
        cfg = r.root / CFG
        wtext(cfg, raw(cfg) + 'full_gate = "make ci"\n')
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"  ERROR  {RULES}: differs from what the engine renders "
                      f"for `build-rules` (a setting, an insert or the engine changed, or the "
                      f"file was edited) — run `{RENDER}`\n", out)
        self.assertNotIn(f"{AGENTS}/code-reviewer.md", out)     # it names no full gate
        self.assertNotIn(f"{AGENTS}/developer.md", out)         # a unit runs no full gate
        r.run("agents", "render")
        self.assertIn("(`make ci`)", raw(r.root / RULES))
        self.assertEqual(r.run("check")[0], 0)

    def test_inserts_are_all_then_the_roles_own_and_a_change_makes_the_file_stale(self):
        r = self.on()
        r.write(".context-gate/agents/all.md", "- Everyone: no force pushes.\r\n\r\n")
        r.write(".context-gate/agents/developer.md", "- Developers: type hints.\n")
        r.run("agents", "render")
        dev = raw(r.root / AGENTS / "developer.md")
        self.assertIn("\n## Rules that bind every change\n\n- Everyone: no force pushes.\n"
                      "- Developers: type hints.\n\n- Never weaken", dev)
        qa = raw(r.root / AGENTS / "qa-engineer.md")
        self.assertIn("\n- Everyone: no force pushes.\n", qa)
        self.assertNotIn("type hints", qa)
        self.assertNotIn("\r", dev)
        r.write(".context-gate/agents/developer.md", "- Developers: docstrings.\n")
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn(f"{AGENTS}/developer.md: differs from what the engine renders", out)
        # The inserts are the project's: rendering never writes them.
        self.assertEqual(raw(r.root / ".context-gate/agents/all.md"),
                         "- Everyone: no force pushes.\r\n\r\n")

    def test_an_insert_that_is_not_utf8_is_reported_and_nothing_is_written(self):
        r = self.on()
        (r.root / ".context-gate/agents").mkdir()
        (r.root / ".context-gate/agents/all.md").write_bytes(b"- caf\xe9\n")
        before = snapshot(r.root)
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 1, out + err)
        self.assertIn(".context-gate/agents/all.md: not valid UTF-8 (byte 5) — nothing was "
                      "written", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(snapshot(r.root), before)
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn(".context-gate/agents/all.md: not valid UTF-8 (byte 5) — agent-roles "
                      "could not check it", out)

    def test_an_unknown_extra_role_is_a_finding_and_render_refuses(self):
        r = self.on('extra_roles = ["architect", "dba"]\n')
        before = snapshot(r.root)
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 1, out + err)
        want = ("[checks.agent-roles] extra_roles names 'dba': the roles it can add are "
                "ux-reviewer, architect")
        self.assertIn(f"  refused     {want}\n", out)
        self.assertIn("[FAIL] agents render  (nothing written", out)
        self.assertEqual(snapshot(r.root), before)
        code, out, err = r.run("check")
        self.assertEqual(code, 1, out + err)            # a finding: 2 would be a config error
        self.assertIn(f"  ERROR  {want} — nothing is rendered until this is fixed\n", out)

    def test_a_copy_of_a_role_under_another_name_is_never_removed_or_reported(self):
        r = self.on()
        r.run("agents", "render")
        copy = r.root / AGENTS / "my-dev.md"
        wtext(copy, raw(r.root / AGENTS / "developer.md"))
        for args in (("agents", "render"), ("agents", "render", "--check")):
            code, out, _ = r.run(*args)
            self.assertNotIn("my-dev", out)
        self.assertTrue(copy.is_file())
        code, out, _ = r.run("check")
        self.assertNotIn("rendered for", out)
        # the rules file is only the configured path
        other = r.root / ".claude/rules/mine.md"
        wtext(other, raw(r.root / RULES))
        r.run("agents", "render")
        self.assertTrue(other.is_file())

    def test_a_rules_file_that_is_a_roles_target_or_leaves_the_project_is_refused(self):
        for value in (f"{AGENTS}/developer.md", "/tmp/rules.md", "../rules.md"):
            with self.subTest(value), tempfile.TemporaryDirectory() as tmp:
                self.refused(SingleRepo(Path(tmp), extra=ON + f"rules_file = {value!r}\n"))

    def refused(self, r: SingleRepo) -> None:
        before = snapshot(r.root)
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 1, out + err)
        self.assertIn("refused", out)
        self.assertEqual(snapshot(r.root), before)
        code, out, err = r.run("check")
        self.assertEqual(code, 1, out + err)
        self.assertIn("ERROR  [checks.agent-roles] rules_file", out)

    def test_a_setting_for_a_role_that_is_not_rendered_is_a_finding_and_render_refuses(self):
        cases = {
            'models = { architect = "opus" }\n':
                "models names 'architect', which is not rendered (add it to extra_roles)",
            'efforts = { ghost = "low" }\n':
                "efforts names 'ghost', which is not rendered (rendered: developer, "
                "senior-developer, code-reviewer, qa-engineer, technical-writer)",
            'max_turns = { ux-reviewer = 40 }\n':
                "max_turns names 'ux-reviewer', which is not rendered (add it to extra_roles)",
            'load_claude_md = ["architect"]\n':
                "load_claude_md names 'architect', which is not rendered (add it to "
                "extra_roles)",
            'max_turns = { developer = "many" }\n':
                "max_turns.developer = 'many': a cap is a whole number above 0",
            'max_turns = { developer = 0 }\n':
                "max_turns.developer = 0: a cap is a whole number above 0",
            'models = { developer = 4 }\n': "models.developer = 4: expected a name",
            'rules_file = "../rules.md"\n':
                "rules_file = '../rules.md': name a file inside the project",
            'rules_file = "/etc/rules.md"\n':
                "rules_file = '/etc/rules.md': name a file inside the project",
            'rules_file = ""\n': "rules_file = '': name a file inside the project",
        }
        for i, (setting, want) in enumerate(cases.items()):
            with self.subTest(setting=setting):
                (self.tmp / f"case{i}").mkdir()
                r = SingleRepo(self.tmp / f"case{i}", extra=ON + setting)
                before = snapshot(r.root)
                code, out, err = r.run("agents", "render")
                self.assertEqual(code, 1, out + err)
                self.assertIn(f"  refused     [checks.agent-roles] {want}", out)
                self.assertEqual(snapshot(r.root), before)
                code, out, err = r.run("check")
                self.assertEqual(code, 1, out + err)
                self.assertIn(f"  ERROR  [checks.agent-roles] {want}", out)

    def test_with_the_option_off_render_writes_nothing_and_says_so(self):
        r = self.repo()
        before = snapshot(r.root)
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        self.assertIn("agent-roles is off: nothing rendered.", out)
        self.assertEqual(snapshot(r.root), before)
        self.assertEqual(r.run("agents", "render", "--check")[0], 0)
        self.assertEqual(r.run("check")[0], 0)

    def test_a_write_that_fails_part_way_leaves_every_file_as_it_was(self):
        r = self.on()
        stale = r.write(f"{AGENTS}/developer.md", rendered("developer") + "\nAn edit.\n")
        before = snapshot(r.root)
        real, calls = roles.write, []

        def failing(path, text):
            calls.append(path)
            if len(calls) == 4:
                raise PermissionError(13, "Permission denied", str(path))
            real(path, text)
        with mock.patch.object(roles, "write", failing):
            code, out, err = r.run("agents", "render")
        self.assertEqual(code, 1, out + err)
        self.assertEqual(len(calls), 4)
        self.assertIn("Permission denied — nothing was written", err)
        self.assertEqual(snapshot(r.root), before)
        self.assertTrue(raw(stale).endswith("\nAn edit.\n"))
        self.assertFalse((r.root / ".claude/rules").exists())

    def test_a_read_only_rendered_file_is_still_rewritten(self):
        r = self.on()
        path = r.write(f"{AGENTS}/developer.md", rendered("developer") + "\nAn edit.\n")
        path.chmod(0o444)
        self.addCleanup(lambda: path.exists() and path.chmod(0o644))
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(raw(path), rendered("developer"))

    def test_render_without_an_install_writes_the_files_and_records_nothing(self):
        r = self.on()
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        self.assertTrue((r.root / AGENTS / "developer.md").is_file())
        self.assertFalse((r.root / layout.MANIFEST).exists())
        self.assertNotIn("recorded", out)

    def test_render_records_each_file_with_the_sha256_of_its_lf_text(self):
        r = self.on()
        path = record_install(r.root)
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        self.assertIn(f"  recorded    6 file(s) in {layout.MANIFEST}\n", out)
        with path.open("rb") as fh:
            rows = tomllib.load(fh)["rendered"]
        self.assertEqual([row["path"] for row in rows],
                         [*(f"{AGENTS}/{role}.md" for role in CORE), RULES])
        for row in rows:
            data = (r.root / row["path"]).read_bytes()
            self.assertEqual(row["sha256"], hashlib.sha256(data).hexdigest())
        # A conflict is not ours, so it is not recorded.
        (self.tmp / "other").mkdir()
        r2 = SingleRepo(self.tmp / "other", extra=ON)
        record_install(r2.root)
        r2.write(f"{AGENTS}/developer.md", BY_HAND)
        r2.run("agents", "render")
        self.assertNotIn(f"{AGENTS}/developer.md",
                         [row["path"] for row in installer.read_manifest(r2.root)["rendered"]])

    def test_an_install_with_no_rendered_table_reads_as_empty(self):
        r = self.repo()
        record_install(r.root)
        self.assertNotIn("rendered", raw(r.root / layout.MANIFEST))
        self.assertEqual(installer.read_manifest(r.root)["rendered"], [])
        self.assertEqual(roles.recorded(r.root), [])

    def test_a_record_that_points_outside_the_project_is_never_followed(self):
        r = self.on()
        outside = self.tmp / "outside.md"
        wtext(outside, rendered("architect"))
        path = record_install(r.root)
        wtext(path, raw(path) + '\n[[rendered]]\npath = "../outside.md"\nsha256 = "0"\n'
              f'\n[[rendered]]\npath = "{outside.as_posix()}"\nsha256 = "0"\n')
        self.assertEqual(roles.recorded(r.root), [])
        code, out, _ = r.run("agents", "render")
        self.assertEqual(code, 0, out)
        self.assertEqual(raw(outside), rendered("architect"))

    def test_operator_rules_set_in_config_reach_the_render(self):
        r = self.on('operator_rules = ["no attribution lines"]\n')
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        dev = raw(r.root / AGENTS / "developer.md")
        self.assertIn("\n- no attribution lines\n", dev)
        self.assertNotIn(roles.NO_RULES, dev)

    def test_a_profile_can_set_every_param(self):
        prof = self.tmp / "prof"
        wtext(prof / "principles.toml", '''
[checks.agent-roles]
level = "error"
extra_roles = ["architect"]
models = { developer = "haiku" }
efforts = { developer = "high" }
max_turns = { developer = 120 }
load_claude_md = ["technical-writer"]
authority = "docs/plan.md"
scoped_test = "make test T=<name>"
full_gate = "make ci"
senior_triggers = ["money"]
operator_rules = ["no attribution lines"]
handback_checks = ["Licences: each new dependency's licence"]
high_risk_paths = ["db/**"]
visual_paths = ["web/**"]
escalate_after = 3
unit_gate = "full"
housekeeping = false
rules_file = ".claude/rules/build.md"
''')
        r = self.repo()
        cfg = r.root / CFG
        wtext(cfg, raw(cfg).replace("schema = 1", f'schema = 1\nprofile = "{prof.as_posix()}"', 1))
        set_here = tomllib.loads(raw(prof / "principles.toml"))["checks"]["agent-roles"]
        self.assertEqual(set(set_here) - {"level"}, set(manifest.CHECKS["agent-roles"].params))
        code, out, err = r.run("agents", "render")
        self.assertEqual(code, 0, out + err)
        dev = raw(r.root / AGENTS / "developer.md")
        for want in ("\nmodel: haiku\n", "\neffort: high\n", "\nmaxTurns: 120\n",
                     "docs/plan.md is the authority", "(`make test T=<name>`)", "(`make ci`)",
                     "senior-developer:\n- money\n",
                     "`Licences:` each new dependency's licence\n"):
            self.assertIn(want, dev)
        self.assertIn("\nomitClaudeMd: false\n", raw(r.root / AGENTS / "technical-writer.md"))
        self.assertTrue((r.root / AGENTS / "architect.md").is_file())
        rules = raw(r.root / ".claude/rules/build.md")
        self.assertIn("failed it 3 times:\n- money\n", rules)
        self.assertIn("touches:\n- db/**\n", rules)
        self.assertNotIn("govern phase close", rules)
        self.assertEqual(r.run("check")[0], 0)

    def test_a_rendered_agent_passes_the_agents_check_at_its_strictest(self):
        r = self.on('extra_roles = ["ux-reviewer", "architect"]\n'
                    '\n[checks.agents]\nrequire_max_turns = true\nwarn_keys = []\n')
        cfg = r.root / CFG
        wtext(cfg, raw(cfg).replace('markers = "gov"',
                                    'markers = "gov"\nagent_turns_prose = "forbid"'))
        r.write(f"{AGENTS}/reviewer.md", raw(r.root / AGENTS / "reviewer.md").replace(
            "omitClaudeMd: true\n", "omitClaudeMd: true\nmaxTurns: 40\n"))
        r.run("agents", "render")
        code, out, err = r.run("check")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("agents/", out)                # not an error, and not a warning
        self.assertNotIn("maxTurns", out)


class Manifest(unittest.TestCase):
    def test_both_checks_arrive_as_the_design_says(self):
        chk = manifest.CHECKS["agent-roles"]
        self.assertEqual((chk.default, chk.since, chk.scope), ("off", "0.7.0", "workspace"))
        self.assertEqual(chk.params["senior_triggers"].default, roles.SENIOR_TRIGGERS)
        self.assertEqual(len(roles.SENIOR_TRIGGERS), 1)
        self.assertEqual(chk.params["escalate_after"].default, 2)
        self.assertEqual(chk.params["rules_file"].default, RULES)
        ref = manifest.CHECKS["agent-references"]
        self.assertEqual((ref.default, ref.since, ref.scope), ("warn", "0.7.0", "workspace"))
        self.assertEqual(ref.params["builtin"].default,
                         ["general-purpose", "Explore", "Plan", "claude", "claude-code-guide",
                          "statusline-setup"])


def doc(body: str) -> str:
    return fm("reference") + "# Agents here\n\n" + body


class References(Base):
    """`agent-references`: a doc that names an agent with no definition."""

    def found(self, r: SingleRepo) -> list[str]:
        code, out, err = r.run("check")
        self.assertEqual(code, 0, out + err)            # it arrives at warn
        return [line.removeprefix("  warn   ") for line in out.splitlines()
                if "names agent" in line]

    def test_each_of_the_four_shapes_is_flagged_with_its_line(self):
        r = self.repo()
        r.write("docs/agents.md", doc(
            'Launch with subagent_type="ghost-a" for this.\n'            # line 10
            "Use `subagent_type: \"ghost-b\"` in the call.\n"
            "The `ghost-c` agent reviews it.\n"
            "A `ghost-d` subagent runs the tests.\n"
            "Hand it to the agent `ghost-e` when done.\n"
            "Then the subagent `ghost-f` closes.\n"
            "\n"
            "| Agent | Use |\n"
            "|---|---|\n"
            "| `ghost-g` | builds |\n"
            "| ghost-h | checks |\n"))
        where = "docs/agents.md"
        tail = f", which has no definition in {AGENTS} — define it or fix the name"
        self.assertEqual(self.found(r), [
            f"{where}:10: names agent `ghost-a`{tail}",
            f"{where}:11: names agent `ghost-b`{tail}",
            f"{where}:12: names agent `ghost-c`{tail}",
            f"{where}:13: names agent `ghost-d`{tail}",
            f"{where}:14: names agent `ghost-e`{tail}",
            f"{where}:15: names agent `ghost-f`{tail}",
            f"{where}:19: names agent `ghost-g`{tail}",
            f"{where}:20: names agent `ghost-h`{tail}",
        ])

    def test_what_is_not_one_of_the_shapes_is_left_alone(self):
        r = self.repo()
        r.write("docs/agents.md", doc(
            "Merge `feature/agents` into `development` before the agent starts.\n"
            "An agent reads the brief; the agent then reports. Agents never push.\n"
            "The `ghost` branch holds what each agent wrote.\n"
            "Set `subagent_type` to the role you want.\n"
            "The `--check` agent flag and the agent `.claude/agents/x.md` file.\n"
            "Run the `ghost` agent-roles option.\n"
            "\n"
            "| Role | Agent count |\n"
            "|---|---|\n"
            "| `ghost` | 2 |\n"
            "\n"
            "| Agent | Use |\n"
            "|---|---|\n"
            "| ghost and friends | several |\n"
            "| — | none |\n"
            "\n"
            "```\n"
            'Agent(subagent_type="ghost")\n'
            "```\n"
            "\n"
            "<!-- the `ghost` agent was retired -->\n"))
        self.assertEqual(self.found(r), [])

    def test_defined_engine_builtin_and_plugin_names_are_known(self):
        r = self.repo()
        r.write("docs/agents.md", doc(
            "The `reviewer` agent is defined here.\n"
            "The `architect` agent is one the engine renders.\n"
            "The `Explore` agent and the `general-purpose` agent are Claude Code's.\n"
            "The `some-plugin:helper` agent is a plugin's.\n"
            'subagent_type="other:thing"\n'))
        self.assertEqual(self.found(r), [])

    def test_builtin_is_a_param(self):
        r = self.repo(extra='\n[checks.agent-references]\nextend_builtin = ["house-agent"]\n'
                            'reasons = { builtin = "A user-level agent every machine has." }\n')
        r.write("docs/agents.md", doc("The `house-agent` agent and the `ghost` agent.\n"))
        self.assertEqual(len(self.found(r)), 1)
        self.assertIn("`ghost`", self.found(r)[0])

    def test_skills_agent_definitions_and_the_rules_file_are_read(self):
        r = self.repo()
        r.write(".claude/skills/ship/SKILL.md",
                "---\nname: ship\ndescription: ships\n---\nLaunch the `ghost-skill` agent.\n")
        r.write(f"{AGENTS}/reviewer.md", raw(r.root / AGENTS / "reviewer.md")
                + "Hand off to the `ghost-agent` agent.\n")
        r.write(RULES, "# Rules\n\nUse the `ghost-rules` agent.\n")
        self.assertEqual([line.split(":")[0] for line in self.found(r)],
                         [".claude/skills/ship/SKILL.md", f"{AGENTS}/reviewer.md", RULES])

    def test_agent_references_skips_a_rules_file_outside_the_project(self):
        out = self.tmp / "outside.md"
        out.write_text("Use the `ghost-outside` agent.\n", encoding="utf-8")
        r = self.repo(extra=ON + f'rules_file = "../{out.name}"\n')
        code, text, err = r.run("check")
        self.assertNotIn("ghost-outside", text + err)

    def test_at_error_a_finding_fails_the_gate(self):
        r = self.repo(extra='\n[checks.agent-references]\nlevel = "error"\n')
        r.write("docs/agents.md", doc("The `ghost` agent.\n"))
        code, out, _ = r.run("check")
        self.assertEqual(code, 1, out)
        self.assertIn("  ERROR  docs/agents.md:10: names agent `ghost`", out)

    def test_a_crlf_doc_is_read_the_same(self):
        r = self.repo()
        r.write("docs/agents.md", doc("The `ghost` agent.\n").replace("\n", "\r\n"))
        self.assertEqual(len(self.found(r)), 1)

    def test_a_rendered_project_names_no_unknown_agent(self):
        r = self.repo(extra=ON + 'extra_roles = ["ux-reviewer", "architect"]\n')
        r.run("agents", "render")
        self.assertEqual(self.found(r), [])


class QaEngineerGivenNoDiff(unittest.TestCase):
    def test_the_rendered_qa_engineer_says_it_is_given_no_diff(self):
        text = rendered("qa-engineer")
        self.assertIn("You are given no diff: open the files you probe.", text)
        self.assertNotIn("full diff", text)


class ClosingRecords(unittest.TestCase):
    def test_the_rendered_technical_writer_sets_last_reviewed_on_records_it_left_standing(self):
        text = rendered("technical-writer")
        self.assertIn("Set `last_reviewed` to today on each record you checked and left standing.", text)
        self.assertIn("you do not change code", text.lower())
        self.assertIn("never push", text.lower())


if __name__ == "__main__":
    unittest.main()
