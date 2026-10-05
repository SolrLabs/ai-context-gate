"""capture.py, the statusline writer: saves four fields per session, then runs the user's own
statusline with the same input and passes its output through untouched."""
from __future__ import annotations

import contextlib
import io
import json
import ntpath
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
CAPTURE = ENGINE / "govern" / "templates" / "capture.py"
HOOK = ENGINE.parent / "plugin" / "hooks" / "usage.py"
sys.path.insert(0, str(ENGINE))

from govern import usage_setup  # noqa: E402

PAYLOAD = {"session_id": "S", "transcript_path": "/t.jsonl", "cwd": "/w",
           "context_window": {"used_percentage": 7, "context_window_size": 1000000},
           "rate_limits": {"seven_day": {"used_percentage": 92, "resets_at": 1790499600}},
           "model": {"display_name": "Opus"}}

# A statusline as a Git Bash user writes it: cmd.exe cannot run it.
CMD = "~/.claude/statusline.sh"
GIT = r"D:\Apps\Git"
BASH = GIT + r"\bin\bash.exe"
WSL = r"C:\Windows\System32\bash.exe"
PWSH = r"C:\Program Files\PowerShell\7\pwsh.exe"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
EVAL = 'eval "$CONTEXT_GATE_STATUSLINE"'      # what Git Bash is asked to run; the command rides in that variable


def load_capture() -> types.ModuleType:
    """capture.py as a module, without running it and without leaving bytecode beside the
    template (it is copied out of that directory)."""
    mod = types.ModuleType("capture_template")
    mod.__file__ = str(CAPTURE)
    exec(compile(CAPTURE.read_text(encoding="utf-8"), str(CAPTURE), "exec"), mod.__dict__)
    return mod


class Capture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "a home"          # a space, on purpose
        self.share = self.home / ".local" / "share" / "context-gate"
        self.share.mkdir(parents=True)
        self.snap = self.home / ".local" / "state" / "context-gate" / "usage" / "S.json"

    def tearDown(self):
        self._tmp.cleanup()

    def chain(self, statusline):
        (self.share / "statusline-chain.json").write_text(json.dumps({"statusLine": statusline}),
                                                          encoding="utf-8")

    def run_(self, stdin: str):
        env = {**os.environ, "HOME": str(self.home)}
        return subprocess.run([sys.executable, str(CAPTURE)], input=stdin.encode(),
                              env=env, capture_output=True)

    def echo_cmd(self) -> str:
        script = self.home / "echo.py"
        script.write_text("import sys\nd=sys.stdin.read()\nsys.stdout.write('LINE:'+str(len(d)))\n"
                          "sys.exit(3)\n", encoding="utf-8")
        return f'"{sys.executable}" "{script}"'

    def test_snapshot_keeps_four_fields(self):
        self.chain(None)
        res = self.run_(json.dumps(PAYLOAD))
        self.assertEqual(res.returncode, 0)
        data = json.loads(self.snap.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"context_window", "rate_limits", "transcript_path", "captured_at"})
        self.assertEqual(data["context_window"], {"used_percentage": 7})
        self.assertEqual(res.stdout, b"")

    def test_chain_gets_same_stdin_and_output_passes_through(self):
        raw = json.dumps(PAYLOAD)
        self.chain({"type": "command", "command": self.echo_cmd()})
        res = self.run_(raw)
        self.assertEqual(res.stdout, f"LINE:{len(raw)}".encode())
        self.assertEqual(res.returncode, 3)

    def test_capture_failure_still_runs_chain(self):
        self.chain({"type": "command", "command": self.echo_cmd()})
        res = self.run_("not json")
        self.assertEqual(res.stdout, b"LINE:8")
        self.assertFalse(self.snap.exists())

    def test_no_partial_files(self):
        self.chain(None)
        self.run_(json.dumps(PAYLOAD))
        leftovers = [p.name for p in self.snap.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def installed(self, command: str, env: dict | None = None):
        """Install the capture over a statusLine running `command`, then run the installed copy
        with the payload: (stdout, exit code)."""
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"statusLine": {"type": "command", "command": command}}),
                            encoding="utf-8")
        usage_setup.install(self.home)
        res = subprocess.run([sys.executable, str(usage_setup.capture_path(self.home))],
                             input=json.dumps(PAYLOAD).encode(), capture_output=True,
                             cwd=self.home, env={**os.environ, "HOME": str(self.home), **(env or {})})
        return res.stdout, res.returncode

    def no_shell(self, reason: str):
        """The runner lacks the shell: a skip on a developer's machine, a failure in CI, where a
        skipped test would prove nothing."""
        if os.environ.get("GITHUB_ACTIONS"):
            self.fail(reason)
        self.skipTest(reason)

    @unittest.skipUnless(os.name == "nt", "the Git Bash rung is taken on Windows only")
    def test_windows_runs_a_bash_only_statusline_through_git_bash(self):
        found = load_capture().git_bash()
        if not found:
            self.no_shell(f"no Git Bash found on this runner (git on PATH: {shutil.which('git')})")
        # `$(…)`, a pipe into tr and `${HOME:+…}`: neither cmd.exe nor PowerShell runs this.
        out, code = self.installed("n=$(cat | wc -c | tr -d ' \\r\\n'); "
                                   "printf 'BASH:%s:%s' \"$n\" \"${HOME:+home}\"; exit 3")
        self.assertEqual((out, code), (f"BASH:{len(json.dumps(PAYLOAD))}:home".encode(), 3), found)

    @unittest.skipUnless(os.name == "nt", "the Git Bash rung is taken on Windows only")
    def test_windows_runs_a_tilde_statusline_from_a_home_with_a_space(self):
        found = load_capture().git_bash()
        if not found:
            self.no_shell(f"no Git Bash found on this runner (git on PATH: {shutil.which('git')})")
        (self.home / "sl.sh").write_text("printf 'TILDE'; exit 4\n", encoding="utf-8", newline="\n")
        out, code = self.installed("~/sl.sh")
        self.assertEqual((out, code), (b"TILDE", 4), found)

    @unittest.skipUnless(os.name == "nt", "the PowerShell rung is taken on Windows only")
    def test_windows_without_git_bash_runs_the_statusline_through_powershell(self):
        # Git Bash cannot be hidden from a child process through the environment: Windows sets
        # ProgramFiles itself in every new process, so the capture finds bash.exe there again.
        # So the lookup alone is answered here ("no Git Bash"), and the argv the capture then
        # builds is run for real, with the payload on stdin.
        mod = load_capture()
        mod.git_bash = lambda: None
        found = mod.windows_argv("$n = 0; $input | ForEach-Object { $n += $_.Length }; "
                                 "[Console]::Out.Write('PS:' + $n); exit 3")
        if not found:
            self.no_shell("no PowerShell on this runner's PATH")
        argv, env = found
        self.assertIn(ntpath.basename(argv[0]).lower().split(".")[0], ("pwsh", "powershell"))
        # `$input` is the payload on stdin; only PowerShell runs this.
        res = subprocess.run(argv, input=json.dumps(PAYLOAD).encode(), capture_output=True,
                             cwd=self.home, env=env)
        self.assertEqual((res.stdout.strip(), res.returncode),
                         (f"PS:{len(json.dumps(PAYLOAD))}".encode(), 3), (argv, res.stderr))


class WindowsShell(unittest.TestCase):
    """Which shell runs the user's statusline on Windows: the one Claude Code itself uses, Git
    Bash when installed, else PowerShell. Lookups are patched, so this runs on every platform."""

    def setUp(self):
        self.cap = load_capture()
        self.asked = []

    @contextlib.contextmanager
    def machine(self, name="nt", env=None, on_path=None, files=(), broken=False):
        """`os.name`, with only these environment variables, PATH hits (name to path) and
        files. `broken` makes every lookup raise."""
        def which(prog, *args, **kwargs):
            self.asked.append(prog)
            if broken:
                raise OSError("PATH unreadable")
            return (on_path or {}).get(prog)

        def isfile(path):
            if broken:
                raise OSError("disk unreadable")
            return str(path) in files

        cap = self.cap
        with mock.patch.dict(cap.os.environ, env or {}, clear=True), \
                mock.patch.object(cap.os, "name", name), \
                mock.patch.object(cap.shutil, "which", side_effect=which), \
                mock.patch.object(cap.os.path, "isfile", side_effect=isfile):
            yield

    def argv(self, **machine):
        with self.machine(**machine):
            found = self.cap.windows_argv(CMD)
        return found and found[0]

    def env_of(self, **machine):
        with self.machine(**machine):
            return self.cap.windows_argv(CMD)[1]

    def test_git_bash_path_override_wins(self):
        mine = r"E:\portable\bash.exe"
        got = self.argv(env={"CLAUDE_CODE_GIT_BASH_PATH": mine},
                        on_path={"git": GIT + r"\cmd\git.exe"}, files={mine, BASH})
        self.assertEqual(got, [mine, "-c", EVAL])

    def test_missing_override_target_falls_through(self):
        got = self.argv(env={"CLAUDE_CODE_GIT_BASH_PATH": r"E:\gone\bash.exe"},
                        on_path={"git": GIT + r"\cmd\git.exe"}, files={BASH})
        self.assertEqual(got, [BASH, "-c", EVAL])

    def test_bash_beside_the_git_on_path(self):
        # One root for all three: the wrong parent (`mingw64` for `mingw64\bin`) finds no file.
        for sub in ("cmd", "bin", r"mingw64\bin", "CMD", r"MinGW64\Bin"):
            with self.subTest(git=sub):
                got = self.argv(on_path={"git": rf"{GIT}\{sub}\git.exe"}, files={BASH})
                self.assertEqual(got, [BASH, "-c", EVAL])

    def test_git_in_an_unknown_layout_is_not_guessed(self):
        got = self.argv(on_path={"git": r"D:\scoop\shims\git.exe"},
                        files={r"D:\scoop\bin\bash.exe", r"D:\bin\bash.exe"})
        self.assertIsNone(got)

    def test_standard_install_directories_in_order(self):
        dirs = [("ProgramFiles", r"C:\Program Files", r"C:\Program Files\Git\bin\bash.exe"),
                ("ProgramFiles(x86)", r"C:\Program Files (x86)",
                 r"C:\Program Files (x86)\Git\bin\bash.exe"),
                ("LocalAppData", r"C:\Users\u\AppData\Local",
                 r"C:\Users\u\AppData\Local\Programs\Git\bin\bash.exe")]
        env = {var: value for var, value, _ in dirs}
        for var, value, bash in dirs:
            with self.subTest(only=var):
                self.assertEqual(self.argv(env={var: value}, files={bash}), [bash, "-c", EVAL])
                self.assertEqual(self.argv(env=env, files={bash}), [bash, "-c", EVAL])
        every = {bash for _, _, bash in dirs}
        self.assertEqual(self.argv(env=env, files=every), [dirs[0][2], "-c", EVAL])
        self.assertEqual(self.argv(env=env, files=every - {dirs[0][2]}), [dirs[1][2], "-c", EVAL])

    def test_git_on_path_comes_before_the_install_directories(self):
        standard = r"C:\Program Files\Git\bin\bash.exe"
        got = self.argv(env={"ProgramFiles": r"C:\Program Files"},
                        on_path={"git": GIT + r"\cmd\git.exe"}, files={BASH, standard})
        self.assertEqual(got, [BASH, "-c", EVAL])

    def test_wsl_bash_is_never_chosen(self):
        on_path = {"bash": WSL, "bash.exe": WSL}
        self.assertIsNone(self.argv(on_path=on_path, files={WSL}))
        got = self.argv(on_path={**on_path, "powershell.exe": POWERSHELL}, files={WSL})
        self.assertEqual(got[0], POWERSHELL)
        self.assertNotIn("bash", self.asked)
        self.assertNotIn("bash.exe", self.asked)

    def test_powershell_when_there_is_no_git_bash(self):
        flags = ["-NoProfile", "-NonInteractive", "-Command", CMD]
        both = {"pwsh": PWSH, "powershell.exe": POWERSHELL}
        self.assertEqual(self.argv(on_path=both), [PWSH, *flags])
        self.assertEqual(self.argv(on_path={"powershell.exe": POWERSHELL}), [POWERSHELL, *flags])

    def test_git_bash_comes_before_powershell(self):
        got = self.argv(on_path={"git": GIT + r"\cmd\git.exe", "pwsh": PWSH}, files={BASH})
        self.assertEqual(got, [BASH, "-c", EVAL])

    def test_the_command_reaches_git_bash_in_the_environment_byte_for_byte(self):
        cmd = r"~/my dir/sl.sh --a 'b' \\server\share"
        with self.machine(env={"PATH": "p", "HOME": "h"}, on_path={"git": GIT + r"\cmd\git.exe"},
                          files={BASH}):
            argv, env = self.cap.windows_argv(cmd)
        self.assertEqual(argv, [BASH, "-c", EVAL])
        self.assertEqual(env, {"PATH": "p", "HOME": "h", "CONTEXT_GATE_STATUSLINE": cmd})
        self.assertIsNone(self.env_of(on_path={"powershell.exe": POWERSHELL}))

    def test_a_shell_found_in_the_current_directory_is_never_taken(self):
        # which() on Windows looks in the current directory first and returns that hit relative.
        got = self.argv(on_path={"git": r".\git.EXE", "pwsh": r".\pwsh.BAT",
                                 "powershell.exe": r".\powershell.exe"}, files={BASH})
        self.assertIsNone(got)
        got = self.argv(on_path={"pwsh": r".\pwsh.BAT", "powershell.exe": POWERSHELL})
        self.assertEqual(got[0], POWERSHELL)

    def test_no_shell_found_is_none_and_nothing_raises(self):
        self.assertIsNone(self.argv())
        self.assertIsNone(self.argv(env={"CLAUDE_CODE_GIT_BASH_PATH": BASH, "ProgramFiles": "C:\\"},
                                    on_path={"git": GIT + r"\cmd\git.exe"}, broken=True))

    def main(self, raw=b'{"session_id": "S"}', **run):
        """main() with the chained command set to CMD and the subprocess call recorded:
        (exit code, stdout, stderr, the call)."""
        cap, out, err = self.cap, io.BytesIO(), io.BytesIO()
        run = run or {"return_value": types.SimpleNamespace(stdout=b"LINE", stderr=b"WARN",
                                                            returncode=3)}
        with mock.patch.object(cap, "capture"), \
                mock.patch.object(cap, "chained", return_value=CMD), \
                mock.patch.object(cap.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(raw))), \
                mock.patch.object(cap.sys, "stdout", types.SimpleNamespace(buffer=out)), \
                mock.patch.object(cap.sys, "stderr", types.SimpleNamespace(buffer=err)), \
                mock.patch.object(cap.subprocess, "run", **run) as ran:
            code = cap.main()
        return code, out.getvalue(), err.getvalue(), ran

    def assert_ran(self, ran, first, shell: bool, raw=b'{"session_id": "S"}'):
        ran.assert_called_once()
        args, kwargs = ran.call_args
        self.assertEqual(args, (first,))
        self.assertEqual(kwargs.get("shell", False), shell)
        self.assertEqual((kwargs.get("input"), kwargs.get("capture_output")), (raw, True))

    def test_windows_chain_runs_through_git_bash_and_passes_everything_through(self):
        with self.machine(on_path={"git": GIT + r"\cmd\git.exe"}, files={BASH}):
            code, out, err, ran = self.main()
        self.assert_ran(ran, [BASH, "-c", EVAL], shell=False)
        self.assertEqual(ran.call_args.kwargs["env"]["CONTEXT_GATE_STATUSLINE"], CMD)
        self.assertEqual((code, out, err), (3, b"LINE", b"WARN"))

    def test_windows_chain_runs_through_powershell(self):
        with self.machine(on_path={"powershell.exe": POWERSHELL}):
            code, out, err, ran = self.main()
        self.assert_ran(ran, [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", CMD],
                        shell=False)
        self.assertEqual((code, out, err), (3, b"LINE", b"WARN"))

    def test_windows_with_no_shell_found_runs_as_before(self):
        for broken in (False, True):
            with self.subTest(lookups_raise=broken), self.machine(broken=broken):
                code, out, err, ran = self.main()
                self.assert_ran(ran, CMD, shell=True)
                self.assertEqual((code, out, err), (3, b"LINE", b"WARN"))

    def test_posix_chain_is_unchanged_and_looks_nothing_up(self):
        with self.machine(name="posix", env={"CLAUDE_CODE_GIT_BASH_PATH": BASH},
                          on_path={"git": GIT + r"\cmd\git.exe", "pwsh": PWSH}, files={BASH}):
            code, out, err, ran = self.main()
        self.assert_ran(ran, CMD, shell=True)
        self.assertEqual((code, out, err), (3, b"LINE", b"WARN"))
        self.assertEqual(self.asked, [])

    def test_a_shell_that_cannot_start_is_exit_0_with_no_output(self):
        for name, machine in (("nt", {"on_path": {"git": GIT + r"\cmd\git.exe"}, "files": {BASH}}),
                              ("nt", {}), ("posix", {})):
            with self.subTest(os=name, **machine), self.machine(name=name, **machine):
                code, out, err, _ = self.main(side_effect=OSError("cannot start"))
                self.assertEqual((code, out, err), (0, b"", b""))


class HookLatency(unittest.TestCase):
    """What the per-tool-call hook costs with the option off, on whatever runs this: the number
    a release reads from the CI log. The bound is loose on purpose, so a slow runner passes."""

    def test_option_off_hook_latency(self):
        payload = json.dumps({"session_id": "S", "hook_event_name": "PostToolUse"}).encode()
        took = []
        with tempfile.TemporaryDirectory() as home:      # no resolved file: the option-off path
            env = {**os.environ, "HOME": home}
            for _ in range(5):
                start = time.perf_counter()
                res = subprocess.run([sys.executable, str(HOOK)], input=payload, env=env,
                                     capture_output=True)
                took.append(time.perf_counter() - start)
                self.assertEqual((res.returncode, res.stdout, res.stderr), (0, b"", b""))
            self.assertEqual(os.listdir(home), [])
        median = statistics.median(took)
        # On its own line, also under `unittest -v`, which leaves the cursor after the test name.
        sys.stderr.write(f"\nusage-hook latency: {median * 1000:.0f} ms median of 5 runs, "
                         f"option off ({sys.platform}, Python {sys.version.split()[0]})\n")
        sys.stderr.flush()
        self.assertLess(median, 1.5)


if __name__ == "__main__":
    unittest.main()
