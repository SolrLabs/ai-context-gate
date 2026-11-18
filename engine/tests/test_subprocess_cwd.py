"""A test that runs the launcher or a release tool as a subprocess gives it a `cwd=`.

Why: a path the code gets wrong (an unexpanded `~`, a blank name) is created relative to the
working directory. Without `cwd=` that is the repository, where a folder named `~` is one careless
`rm -r` from a home directory. With `cwd=` a folder under the test's temporary directory, it lands
there.

The rule, settled against the real files. Every `test_*.py` under `engine/tests/` and
`tools/tests/` is parsed with `ast` and its call nodes are walked. A call is checked when it is
`subprocess.run`, `.Popen`, `.check_output`, `.check_call` or `.call` and its first argument is a
list literal whose first element is `sys.executable` and which has a second element. That second
element names the launcher or a release tool when the words of its `ast.unparse` text (letters and
underscores, lower-cased) include `entry`, `entrypoint`, `bin`, `govern`, `upgrade`, `uninstall`,
`release`, `install` or `publish` (so `self.entry`, `layout.ENTRYPOINT`, `... / "bin" / "upgrade"`,
`INSTALL`, `PUBLISH`, `tools/release/...`), or is a name the file assigns at module level from a
path under `tools/release/` or `templates/entrypoint.py`. A checked call needs a `cwd=` keyword.
`-m`, `-c` and plugin hook scripts are not the launcher and are not checked.

A file exempts calls with a module constant `SUBPROCESS_CWD_OK = {"<function or test name>":
"<reason>"}`: calls inside a function so named are skipped, and an empty reason fails. Violations
in files another unit is changing sit in `DEFERRED` below until that unit lands.
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_guard  # noqa: E402,F401

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
FOLDERS = (HERE, REPO / "tools" / "tests")
CALLS = {"run", "Popen", "check_output", "check_call", "call"}
WORDS = {"entry", "entrypoint", "bin", "govern", "upgrade", "uninstall", "release", "install",
         "publish"}
PATH_WORDS = {"release", "entrypoint"}

# file name -> comma-separated functions whose subprocess calls still lack `cwd=`. Another unit
# is editing each file; the next pass removes the entry.
DEFERRED: dict[str, str] = {}


def _words(node: ast.AST) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z_]+", ast.unparse(node))}


def _path_constants(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and _words(node.value) & PATH_WORDS:
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _is_subprocess_call(node: ast.Call) -> bool:
    f = node.func
    return (isinstance(f, ast.Attribute) and f.attr in CALLS
            and isinstance(f.value, ast.Name) and f.value.id == "subprocess")


def exemptions(tree: ast.Module) -> dict:
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "SUBPROCESS_CWD_OK" for t in node.targets)):
            return ast.literal_eval(node.value)
    return {}


def violations(source: str) -> list[tuple[int, str]]:
    """(line, enclosing function) of each checked call without `cwd=`, exemptions applied."""
    tree = ast.parse(source)
    consts = _path_constants(tree)
    exempt = {k for k, v in exemptions(tree).items() if str(v).strip()}
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, stack: tuple[str, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack = (*stack, node.name)
        if isinstance(node, ast.Call) and _is_subprocess_call(node) and node.args:
            cmd = node.args[0]
            if (isinstance(cmd, ast.List) and len(cmd.elts) > 1
                    and ast.unparse(cmd.elts[0]) == "sys.executable"):
                target = cmd.elts[1]
                names = {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
                named = bool(_words(target) & WORDS) or bool(names & consts)
                has_cwd = any(k.arg == "cwd" for k in node.keywords)
                if named and not has_cwd and not exempt & set(stack):
                    found.append((node.lineno, stack[-1] if stack else "<module>"))
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, ())
    return found


def test_files() -> list[Path]:
    return sorted(p for folder in FOLDERS for p in folder.glob("test_*.py"))


class SubprocessCwd(unittest.TestCase):
    def test_a_bare_launcher_call_fails(self):
        src = ("import subprocess, sys\n"
               "def test_x(self):\n"
               "    subprocess.run([sys.executable, str(ENTRY), \"beta\"])\n")
        self.assertEqual(violations(src.replace("ENTRY", "self.entry")), [(3, "test_x")])
        self.assertEqual(violations(src.replace("ENTRY", "INSTALL")), [(3, "test_x")])
        src_const = ("TOOL = REPO / 'tools' / 'release' / 'x.py'\n"
                     "def test_x(self):\n"
                     "    subprocess.check_call([sys.executable, str(TOOL)])\n")
        self.assertEqual(violations(src_const), [(3, "test_x")])

    def test_a_cwd_keyword_passes(self):
        src = ("def test_x(self):\n"
               "    subprocess.run([sys.executable, str(self.entry), \"beta\"], cwd=str(tmp))\n")
        self.assertEqual(violations(src), [])

    def test_other_scripts_are_not_checked(self):
        src = ("def test_x(self):\n"
               "    subprocess.run([sys.executable, '-m', 'govern.installer'])\n"
               "    subprocess.run([sys.executable, str(HOOK)])\n"
               "    subprocess.run(['git', 'init'])\n")
        self.assertEqual(violations(src), [])

    def test_an_exemption_needs_a_reason(self):
        body = ("def test_x(self):\n"
                "    subprocess.run([sys.executable, str(self.entry)])\n")
        self.assertEqual(violations("SUBPROCESS_CWD_OK = {'test_x': 'reads no path'}\n" + body), [])
        empty = ast.parse("SUBPROCESS_CWD_OK = {'test_x': ''}\n" + body)
        self.assertEqual([k for k, v in exemptions(empty).items() if not str(v).strip()],
                         ["test_x"])
        self.assertEqual(violations("SUBPROCESS_CWD_OK = {'test_x': ''}\n" + body),
                         [(3, "test_x")])

    def test_no_file_has_an_empty_exemption_reason(self):
        for path in test_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name, reason in exemptions(tree).items():
                self.assertTrue(str(reason).strip(), f"{path.name}: {name} has no reason")

    def test_the_real_tree_passes(self):
        files = test_files()
        self.assertGreater(len(files), 20)
        bad = []
        for path in files:
            skip = {f.strip() for f in DEFERRED.get(path.name, "").split(",") if f.strip()}
            for line, func in violations(path.read_text(encoding="utf-8")):
                if func not in skip:
                    bad.append(f"{path.name}:{line} in {func}")
        self.assertEqual(bad, [], "subprocess call without cwd=")


class TildeGuard(unittest.TestCase):
    """Without `cwd=` a broken `.expanduser()` writes `~/prof` into the repository."""

    def test_a_broken_expanduser_does_not_leak_into_the_repository(self):
        import test_profile_dir_tools as tools

        leaked = REPO / "~"
        self.assertFalse(leaked.exists(), f"{leaked} exists before the test")

        class Broken(tools.LauncherProfileDir):
            def setUp(self) -> None:
                super().setUp()
                # The installed launcher, in this test's own temporary folder only.
                text = self.entry.read_text(encoding="utf-8")
                lines = text.splitlines(keepends=True)
                hit = [i for i, line in enumerate(lines)
                       if "CLAUDE_DIR" in line and ".expanduser()" in line]
                assert hit, "no CLAUDE_DIR line with .expanduser() in the launcher"
                for i in hit:
                    lines[i] = lines[i].replace(".expanduser()", "")
                self.entry.write_text("".join(lines), encoding="utf-8")

            def runTest(self) -> None:
                self.test_a_tilde_in_the_variable_is_the_home()

        case = Broken()
        try:
            case.setUp()
            with self.assertRaises(AssertionError):
                case.runTest()
        finally:
            case.tearDown()
        self.assertFalse(leaked.exists(), f"{leaked} was created in the repository")


if __name__ == "__main__":
    unittest.main()
