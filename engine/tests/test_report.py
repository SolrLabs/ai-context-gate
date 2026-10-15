"""`govern report draft`, `check` and `send`: a field report on context-gate itself, refused while
it holds anything of the project's, opened as an issue page only when it is clean. Against a real
governed repository in a temporary directory with a HOME of its own (`base_support`). The browser
is a counted stand-in and the network is closed: no test opens a page or a connection.

    python3 -m unittest discover -s engine/tests -k test_report
"""
from __future__ import annotations

import os
import platform
import socket
import sys
import unittest
import urllib.parse
import webbrowser
from datetime import date
from pathlib import Path
from unittest import mock

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from govern import __version__, field_report, layout  # noqa: E402
from base_support import Case, Solo, Tree  # noqa: E402

REPORTS = field_report.REPORTS
TODAY = date.today().isoformat()
HOST, LOGIN = "build-box-7", "pat-lowe"
CLEAN = """# The index command drops a row

## What happened

Running index twice removed one row from the generated table.

## What was expected

The table keeps every row.

## Smallest way to see it

Make a log with two entries, run index, then run it again.

## Environment

- context-gate 0.7.0
- Python 3.12
- Linux
"""
OPENING = (f"opening the issue page on {layout.PUBLIC_REPO}: read it there and press Submit; "
           f"nothing is sent until you do")


class Report(Case):
    def setUp(self) -> None:
        super().setUp()
        for target, value in (("socket.gethostname", HOST), ("getpass.getuser", LOGIN)):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        no_net = mock.patch.object(socket.socket, "connect",
                                   side_effect=AssertionError("a test opened a connection"))
        no_net.start()
        self.addCleanup(no_net.stop)
        self.opened: list[str] = []
        self.browser = True
        self.w = Solo(self.tmp)

    def go(self, *argv: str) -> tuple[int, str, str]:
        def fake_open(url, *a, **k):
            self.opened.append(url)
            if isinstance(self.browser, Exception):
                raise self.browser
            return self.browser
        with mock.patch("webbrowser.open", side_effect=fake_open):
            return self.w.run("report", *argv)

    def put(self, text: str | bytes, name: str = "a.md") -> str:
        rel = f"{REPORTS}/{name}"
        self.w.write(rel, text)
        return rel

    def bad(self, line: str, name: str = "a.md") -> str:
        """The clean report with `line` added as line 6."""
        first = "Running index twice removed one row from the generated table.\n"
        return self.put(CLEAN.replace(first, first + line + "\n"), name)

    def hits(self, rel: str) -> tuple[int, list[str]]:
        code, out, err = self.go("check", rel)
        self.assertEqual(err, "")
        return code, [x for x in out.splitlines() if x.startswith("  private")]

    def assert_hit(self, line: str, what: str, at: int = 6) -> list[str]:
        rel = self.bad(line)
        code, found = self.hits(rel)
        self.assertEqual(code, 1, found)
        self.assertIn(f"  private     {rel}:{at}: {what}", found)
        return found


class Draft(Report):
    def test_report_draft_shape_and_ignore_file(self):
        code, out, err = self.go("draft", "--title", "Index drops a row!")
        self.assertEqual((code, err), (0, ""))
        rel = f"{REPORTS}/{TODAY}-index-drops-a-row.md"
        self.assertEqual(out.strip(), rel)
        lines = self.w.read(rel).split("\n")
        self.assertEqual(lines[0], "# Index drops a row!")
        heads = [x for x in lines if x.startswith("## ")]
        self.assertEqual(heads, ["## What happened", "## What was expected",
                                 "## Smallest way to see it", "## Environment"])
        for head in heads[:3]:
            hint = lines[lines.index(head) + 2]
            self.assertTrue(hint.startswith("<!-- ") and hint.endswith(" -->"), hint)
            self.assertIn("Nothing of the project's", hint)
        env = lines[lines.index("## Environment") + 2:]
        py = f"{sys.version_info.major}.{sys.version_info.minor}"
        self.assertEqual(env[:3], [f"- context-gate {__version__}", f"- Python {py}",
                                   f"- {platform.system()}"])
        self.assertEqual(self.w.read(f"{REPORTS}/.gitignore"), "*\n")
        # git ignores it: the draft never shows up as a change to commit
        self.assertEqual(self.w.git("status", "--porcelain", "--untracked-files=all"), "")

    def test_report_second_draft_of_one_title(self):
        first = self.go("draft", "--title", "Same")[1].strip()
        self.w.write(f"{REPORTS}/.gitignore", "*\n# mine\n")
        second = self.go("draft", "--title", "Same")[1].strip()
        third = self.go("draft", "--title", "Same")[1].strip()
        self.assertEqual([first, second, third], [f"{REPORTS}/{TODAY}-same.md",
                                                  f"{REPORTS}/{TODAY}-same-2.md",
                                                  f"{REPORTS}/{TODAY}-same-3.md"])
        self.assertTrue(self.w.read(first).startswith("# Same\n"))
        self.assertEqual(self.w.read(f"{REPORTS}/.gitignore"), "*\n# mine\n")

    def test_report_draft_title_refused(self):
        for title in ("   ", "two\nlines"):
            with self.subTest(title=title):
                code, out, err = self.go("draft", "--title", title)
                self.assertEqual(code, 2)
                self.assertFalse((self.w.root / REPORTS).exists())

    def test_report_draft_slug_of_other_letters(self):
        out = self.go("draft", "--title", "Ошибка")[1].strip()
        self.assertEqual(out, f"{REPORTS}/{TODAY}-report.md")

    def test_report_draft_that_cannot_be_written_leaves_nothing(self):
        real = open

        def failing(path, mode="r", *a, **k):
            if str(path).endswith(".md") and "x" in mode:
                raise PermissionError(13, "Permission denied")
            return real(path, mode, *a, **k)
        with mock.patch("builtins.open", side_effect=failing):
            code, out, err = self.go("draft", "--title", "T")
        self.assertEqual(code, 1)
        self.assertIn("nothing was written", err)
        self.assertFalse((self.w.root / REPORTS).exists())

    def test_report_fresh_draft_fails_until_filled(self):
        rel = self.go("draft", "--title", "Fresh")[1].strip()
        code, found = self.hits(rel)
        self.assertEqual(code, 1)
        self.assertEqual(found, [
            f'  private     {rel}:3: the section "What happened" still holds only its comment',
            f'  private     {rel}:7: the section "What was expected" still holds only its comment',
            f'  private     {rel}:11: the section "Smallest way to see it" still holds only its '
            f'comment'])
        out = self.go("check", rel)[1]
        self.assertIn("[FAIL] report check  (3 to remove)", out)


class Paths(Report):
    def test_report_path_hits(self):
        first = field_report.PATH_PREFIXES[0]
        for text, what in ((f"see {first}x/notes", f"a path ({first})"),
                           ("see /HOME/x", "a path (/home/)"),
                           ("under ~/work", "a path (~/)"),
                           ("under /opt/thing", "a path (/opt/)"),
                           ("on C:\\Work\\file", "a path (a Windows drive)"),
                           ("on D:/x", "a path (a Windows drive)"),
                           ("logs in /tmp/x", "a path (/tmp/)")):
            with self.subTest(text=text):
                self.assert_hit(text, what)

    def test_report_a_url_scheme_is_not_a_drive(self):
        for text in (f"see https://github.com/{layout.PUBLIC_REPO}",
                     "the ratio 3:/ and the time 12:30/", "x://"):
            with self.subTest(text=text):
                self.assertNotIn("a path (a Windows drive)",
                                 field_report._line_hits(text, [], [], []))

    def test_report_project_root_and_home(self):
        self.assert_hit(f"ran in {self.w.root.as_posix()}", "a path (the project root)")
        self.assert_hit(f"config in {Path.home().as_posix()}", "a path (the home folder)")


class Identities(Report):
    def test_report_root_folder_name(self):
        self.assert_hit("the SOLO project", "an identity: the root folder's name")

    def test_report_main_checkout_name_from_a_worktree(self):
        wt = self.tmp / "wt-elsewhere"
        self.w.git("worktree", "add", "-q", "-b", "side", str(wt))
        self.w = Tree(wt, self.w.home)
        self.assert_hit("the solo checkout", "an identity: the repository's folder name")

    def test_report_a_name_inside_a_longer_word_is_not_it(self):
        rel = self.bad("the soloist was fine")       # holds `solo` only inside a word
        self.assertEqual(self.hits(rel), (0, []))

    def test_report_remote_owner_repo_and_url(self):
        self.w.git("remote", "add", "origin", "git@example.org:acme-labs/widget-factory.git")
        self.assert_hit("built by Acme-Labs", "an identity: a git remote's owner")
        self.assert_hit("the widget-factory tests", "an identity: a git remote's repository name")
        found = self.assert_hit("cloned git@example.org:acme-labs/widget-factory.git",
                                "an identity: a git remote's URL")
        self.assertIn(f"  private     {REPORTS}/a.md:6: an email address", found)

    def test_report_remote_host_on_its_own(self):
        self.w.git("remote", "add", "origin", "git@git.acme-corp.internal:team/app.git")
        self.w.git("remote", "add", "ci", "https://ci@build.acme.example:8443/team/app.git")
        self.w.git("remote", "add", "hub", "https://GitHub.com/acme-labs/widget.git")
        what = "an identity: a git remote's host"
        self.assert_hit("the git.acme-corp.internal server", what)
        self.assert_hit("built on build.acme.example", what)
        rel = self.bad("hosted on github.com today")
        self.assertEqual(self.hits(rel), (0, []))

    def test_report_git_user(self):
        self.w.git("config", "user.name", "Rory Quill")
        self.w.git("config", "user.email", "rory@quill.example")
        self.assert_hit("thanks to rory quill", "an identity: git user.name")
        self.assert_hit("mail rory@quill.example", "an identity: git user.email")

    def test_report_host_and_login(self):
        self.assert_hit("on BUILD-BOX-7 only", "an identity: the host name")
        self.assert_hit("as pat-lowe", "an identity: the login name")

    def test_report_short_names_skipped(self):
        with mock.patch("socket.gethostname", return_value="ab"):
            rel = self.bad("ab testing")
            self.assertEqual(self.hits(rel), (0, []))

    def test_report_failing_git_is_a_hit(self):
        with mock.patch.object(field_report, "_git", return_value=None):
            code, found = self.hits(self.put(CLEAN))
        self.assertEqual(code, 1)
        self.assertIn(f"  private     {REPORTS}/a.md: cannot check identities: "
                      f"git rev-parse could not run", found)

    def test_report_not_a_repository_is_a_hit(self):
        with self.fresh():
            self.w = Solo(self.tmp, git=None)
            code, found = self.hits(self.put(CLEAN))
        self.assertEqual(code, 1)
        self.assertTrue(any("cannot check identities: git rev-parse failed" in x for x in found),
                        found)


class Content(Report):
    def test_report_email(self):
        self.assert_hit("write to someone@example.com", "an email address")

    def test_report_url(self):
        what = f"an address not on {layout.PUBLIC_REPO}"
        for text in ("see https://example.com/page", "see www.example.com",
                     f"see https://github.com/{layout.PUBLIC_REPO}-fork/issues/1"):
            with self.subTest(text=text):
                self.assert_hit(text, what)

    def test_report_url_on_the_public_repository_passes(self):
        rel = self.bad(f"like https://github.com/{layout.PUBLIC_REPO}/issues/12345678.")
        self.assertEqual(self.hits(rel), (0, []))

    def test_report_address_without_a_scheme(self):
        what = f"an address not on {layout.PUBLIC_REPO}"
        for text in ("see git.acme-corp.internal/team/app", "at build.example.net:8443/x",
                     f"see github.com/{layout.PUBLIC_REPO}-fork/issues/1",
                     f"(GitHub.com/{layout.PUBLIC_REPO}x)"):
            with self.subTest(text=text):
                self.assert_hit(text, what)

    def test_report_address_without_a_scheme_on_the_public_repository_passes(self):
        rel = self.bad(f"see github.com/{layout.PUBLIC_REPO}/issues/3, or "
                       f"GitHub.com/{layout.PUBLIC_REPO}.")
        self.assertEqual(self.hits(rel), (0, []))

    def test_report_file_names_are_not_addresses(self):
        rel = self.bad("edit docs/checks.md and engine/govern/field_report.py, then "
                       "README.md:3 and docs/checks.md:12")
        self.assertEqual(self.hits(rel), (0, []))

    def test_report_public_address_that_leaves_the_repository(self):
        what = f"an address not on {layout.PUBLIC_REPO}"
        base = f"github.com/{layout.PUBLIC_REPO}"
        for text in (f"https://{base}/../../other/repo", f"https://{base}/%2e%2e/%2E%2E/other",
                     f"https://{base}/%252e%252e/other", f"https://{base}/..",
                     f"https://{base}/blob/x?u=https://example.com/a", f"{base}/../x",
                     f"{base}/redirect?to=http://example.com"):
            with self.subTest(text=text):
                self.assert_hit(f"see {text}", what)

    def test_report_secret_shapes_never_print_the_value(self):
        hexed = "0123456789abcdef" * 2 + "01234567"
        cases = (("-----BEGIN OPENSSH PRIVATE KEY-----", "a private key or certificate block"),
                 ("ghp_Zq81bLm", "a GitHub token"),
                 ("github_pat_Zq81bLm", "a GitHub token"),
                 ("sk-Zq81bLmX", "an API key (sk-)"),
                 ("AKIAZQ81BLMX", "an AWS access key"),
                 ("xoxb-Zq81bLm", "a Slack token"),
                 ("Bearer Zq81bLmX", "a bearer token"),
                 ("password=Zq81bLmX", "password with a value"),
                 ("API_KEY: Zq81bLmX", "api_key with a value"),
                 ('"token": "Zq81bLmX"', "token with a value"),
                 (hexed, "a run of 32 or more characters with no space: a key, a hash or an id"))
        self.assertEqual(len(hexed), 40)
        for text, kind in cases:
            with self.subTest(text=text):
                self.assert_hit(f"value {text} here", f"a secret's shape: {kind}")
                code, out, err = self.go("check", f"{REPORTS}/a.md")
                secret = text.split()[-1].strip('"') if " " in text else text
                self.assertNotIn(secret, out + err)

    def test_report_words_holding_a_shape_are_not_one(self):
        rel = self.bad("the task-runner and a passwordless login")
        self.assertEqual(self.hits(rel), (0, []))

    def test_report_private_terms(self):
        self.w.write(f"{layout.GOV_DIR}/report-private.txt",
                     "# our words\nProject Nimbus  # the codename\n\nzx\n")
        found = self.assert_hit("as in project nimbus", f"a term from {layout.GOV_DIR}/"
                                                       f"report-private.txt")
        self.assertFalse(any("Nimbus" in x or "nimbus" in x for x in found))
        self.assert_hit("the zx flag", f"a term from {layout.GOV_DIR}/report-private.txt")

    def test_report_private_terms_unreadable_is_a_hit(self):
        self.w.write(f"{layout.GOV_DIR}/report-private.txt", b"\xff\xfe bad")
        code, found = self.hits(self.put(CLEAN))
        self.assertEqual(code, 1)
        self.assertIn(f"  private     {REPORTS}/a.md: cannot read {layout.GOV_DIR}/"
                      f"report-private.txt: not valid UTF-8 (byte 0)", found)


class Encoded(Report):
    """Private text written URL-encoded or with HTML entities is the same private text."""

    NAME, TERM = "Marisol Quintero", "Project Nightjar"

    def setUp(self) -> None:
        super().setUp()
        self.w.git("config", "user.name", self.NAME)
        self.w.write(f"{layout.GOV_DIR}/report-private.txt", f"{self.TERM}\n")

    def cases(self) -> tuple[tuple[str, str], ...]:
        """`(plain, what)` for each kind of private text."""
        return (
            (self.NAME, "an identity: git user.name"),
            (self.TERM, f"a term from {layout.GOV_DIR}/report-private.txt"),
            ("someone@example.com", "an email address"),
            ("C:\\Work\\file", "a path (a Windows drive)"),
            (f"{field_report.PATH_PREFIXES[0]}x", f"a path ({field_report.PATH_PREFIXES[0]})"),
            ("Bearer Zq81bLmX", "a secret's shape: a bearer token"),
            ("password=Zq81bLmX", "a secret's shape: password with a value"),
        )

    def test_report_url_encoded_private_text(self):
        for plain, what in self.cases():
            for encoded in (urllib.parse.quote_plus(plain), urllib.parse.quote(plain, safe="")):
                with self.subTest(encoded=encoded):
                    self.assertNotIn(plain, encoded)
                    self.assert_hit(f"see ?q={encoded}", what)

    def test_report_html_escaped_private_text(self):
        for plain, what in self.cases():
            for escaped in ("".join(f"&#{ord(c)};" for c in plain),
                            "".join(f"&#x{ord(c):x};" if c in " @:\\/= " else c for c in plain)):
                with self.subTest(escaped=escaped):
                    self.assertNotIn(plain, escaped)
                    self.assert_hit(f"see {escaped}", what)
        self.assert_hit("thanks to Marisol&nbsp;Quintero", "an identity: git user.name")

    def test_report_both_encodings_in_either_order(self):
        what = "an identity: git user.name"
        self.assert_hit("see ?q=" + urllib.parse.quote_plus("Marisol&#32;Quintero"), what)
        self.assert_hit("see Marisol&#43;Quintero", what)

    def test_report_each_hit_once_per_line(self):
        rel = self.bad("Marisol Quintero, Marisol+Quintero and Marisol&#32;Quintero")
        code, found = self.hits(rel)
        self.assertEqual(found, [f"  private     {rel}:6: an identity: git user.name"])
        hits = field_report._line_hits("Bearer x, Bearer%20y and Bearer&#32;z", [], [], [])
        self.assertEqual(hits, ["a secret's shape: a bearer token"])

    def test_report_text_that_only_looks_encoded_passes(self):
        rel = self.bad("about 100% of C++ and R&D runs, a+b, 50%off and &amp; &#unknown;")
        self.assertEqual(self.hits(rel), (0, []))


class Size(Report):
    def test_report_too_many_lines(self):
        rel = self.put(CLEAN + "".join(f"- step {i}\n" for i in range(50)))
        code, found = self.hits(rel)
        self.assertEqual(found, [f"  private     {rel}:61: the report is 69 lines: at most 60"])

    def test_report_too_many_characters(self):
        words = " ".join(["word"] * 900)               # 4,499 characters on line 6
        size = len(CLEAN) - 1 + len(words) + 1
        self.assert_hit(words, f"the report is {size:,} characters: at most 4,000")

    def test_report_four_thousand_characters_pass(self):
        rel = self.bad("w" * 20 + (" " + "w" * 20) * ((4000 - len(CLEAN) - 21) // 21))
        text = self.w.read(rel)
        self.assertLessEqual(len(text) - 1, 4000)
        self.assertEqual(self.hits(rel), (0, []))

    def test_report_too_much_code(self):
        block = "```\n" + "".join(f"x = {i}\n" for i in range(21)) + "```"
        rel = self.bad(block)
        code, found = self.hits(rel)
        self.assertEqual(found, [f"  private     {rel}:27: 21 lines inside code fences: "
                                 f"at most 20"])

    def test_report_twenty_lines_of_code_pass(self):
        rel = self.bad("```\n" + "".join(f"x = {i}\n" for i in range(20)) + "```")
        self.assertEqual(self.hits(rel), (0, []))

    def test_report_section_holding_only_its_comment(self):
        rel = self.put(CLEAN.replace("The table keeps every row.", "<!-- later -->"))
        code, found = self.hits(rel)
        self.assertEqual(found, [f'  private     {rel}:7: the section "What was expected" still '
                                 f'holds only its comment'])

    def test_report_clean_draft_passes(self):
        code, out, err = self.go("check", self.put(CLEAN))
        self.assertEqual((code, out, err), (0, "[OK] report check\n", ""))


class Refused(Report):
    def test_report_file_outside_reports(self):
        self.w.write("docs/x.md", CLEAN)
        for name in ("docs/x.md", f"{REPORTS}/../../docs/x.md",
                     str((self.w.root / "docs/x.md").resolve())):
            with self.subTest(name=name):
                code, out, err = self.go("check", name)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("not a draft in", err)

    @unittest.skipIf(os.name == "nt", "a symlink needs a privilege on Windows")
    def test_report_symlink_out_of_reports(self):
        self.w.write("docs/x.md", CLEAN)
        (self.w.root / REPORTS).mkdir(parents=True)
        (self.w.root / REPORTS / "a.md").symlink_to(self.w.root / "docs/x.md")
        code, out, err = self.go("send", f"{REPORTS}/a.md")
        self.assertEqual(code, 2)
        self.assertEqual(self.opened, [])

    def test_report_not_utf8_and_missing(self):
        rel = self.put(b"# T\n\n\xff\xfe\n")
        code, out, err = self.go("check", rel)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not valid UTF-8", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.go("check", f"{REPORTS}/none.md")[0], 1)

    def test_report_line_one_not_a_title(self):
        rel = self.put(CLEAN.replace("# The index", "The index"))
        code, found = self.hits(rel)
        self.assertIn(f'  private     {rel}:1: line 1 is not "# <title>"', found)


class Send(Report):
    def sent(self, url: str) -> dict:
        parts = urllib.parse.urlsplit(url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path),
                         ("https", "github.com", f"/{layout.PUBLIC_REPO}/issues/new"))
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        return {k: v[0] for k, v in query.items()}

    def test_report_send_refuses_on_a_hit_whatever_flags(self):
        rel = self.bad(f"see {field_report.PATH_PREFIXES[0]}x")
        before = (self.w.root / rel).read_bytes()
        for flags in ((), ("--print",)):
            with self.subTest(flags=flags):
                code, out, err = self.go("send", rel, *flags)
                self.assertEqual(code, 1)
                self.assertIn("[FAIL] report check  (1 to remove)", out)
                self.assertNotIn("github.com", out)
        self.assertEqual(self.opened, [])
        self.assertEqual((self.w.root / rel).read_bytes(), before)

    def test_report_send_opens_one_address(self):
        rel = self.put(CLEAN)
        code, out, err = self.go("send", rel)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(self.opened), 1)
        url = self.opened[0]
        self.assertEqual(out.splitlines(), [OPENING, url])
        q = self.sent(url)
        self.assertEqual(sorted(q), ["body", "title"])      # no template=: it may drop body=
        self.assertEqual(q["title"], "[field] The index command drops a row")
        self.assertEqual(q["body"], CLEAN.split("\n", 1)[1].strip("\n"))
        self.assertTrue(self.w.read(rel).endswith(f"- Linux\nOpened: {TODAY}\n"))

    def test_report_send_print_opens_nothing(self):
        rel = self.put(CLEAN)
        code, out, err = self.go("send", rel, "--print")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.opened, [])
        lines = out.splitlines()
        self.assertEqual(lines[0], "open this address in a browser")
        self.assertEqual(self.sent(lines[1])["title"], "[field] The index command drops a row")
        self.assertIn(f"Opened: {TODAY}", self.w.read(rel))

    def test_report_browser_that_cannot_open_falls_back(self):
        for failure in (False, webbrowser.Error("no browser")):
            with self.subTest(failure=failure):
                self.browser, self.opened = failure, []
                code, out, err = self.go("send", self.put(CLEAN))
                self.assertEqual(code, 0)
                url = self.opened[0]
                self.assertEqual(out.splitlines(),
                                 [OPENING, url, "open this address in a browser", url])

    def test_report_over_long_address_refused(self):
        rel = self.put(CLEAN.replace("The table keeps every row.", "漢字 " * 400))
        before = (self.w.root / rel).read_bytes()
        self.assertEqual(self.hits(rel), (0, []))
        code, out, err = self.go("send", rel, "--print")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("too long for an issue page: shorten it", err)
        self.assertEqual(self.opened, [])
        self.assertEqual((self.w.root / rel).read_bytes(), before)

    def test_report_sent_again(self):
        rel = self.put(CLEAN)
        self.go("send", rel)
        self.go("send", rel)
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(self.opened[0], self.opened[1])        # Opened: lines are not sent
        self.assertTrue(self.w.read(rel).endswith(f"Opened: {TODAY}\nOpened: {TODAY}\n"))

    def test_report_crlf_and_bom_kept(self):
        rel = self.put(b"\xef\xbb\xbf" + CLEAN.replace("\n", "\r\n").encode("utf-8"))
        self.assertEqual(self.go("check", rel)[0], 0)
        code, out, err = self.go("send", rel)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.sent(self.opened[0])["body"], CLEAN.split("\n", 1)[1].strip("\n"))
        raw = (self.w.root / rel).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf# The index"))
        self.assertTrue(raw.endswith(f"- Linux\r\nOpened: {TODAY}\r\n".encode()))
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_report_engine_never_calls_the_network(self):
        source = (ENGINE / "govern" / "field_report.py").read_text(encoding="utf-8")
        for name in ("urlopen", "urllib.request", "http.client", "create_connection", '"gh"'):
            self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
