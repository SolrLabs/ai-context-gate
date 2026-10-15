"""`govern report`: a field report on context-gate itself, for its maintainers.

`draft` writes the headings and the environment lines and nothing of the project's. `check`
reads every line that would be sent and names each piece of private text in it: a path, a name
the machine or git knows the project by, an address, a secret's shape, a term the project lists
in `report-private.txt`, or a report too big to have been written by hand. `send` builds the
public repository's new-issue page with the title and body filled in and opens it in the
person's browser, only for a draft that passes the check; the person reads it there and submits
it. Nothing here calls the network, and no flag skips the check.

The module is not `report.py`: that name is the install and upgrade reports'.
"""
from __future__ import annotations

import getpass
import html
import os
import platform
import re
import socket
import subprocess
import sys
import urllib.parse
import webbrowser
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from govern import __version__, layout
from govern.context import GIT_TIMEOUT, git_env
from govern.decisions import FENCE, FENCE_RE, LIVE, line_kinds
from govern.text import COMMENT_RE, CONTROL_RE, has_bom, read_text

REPORTS = f"{layout.GOV_DIR}/reports"
PRIVATE_LIST = f"{layout.GOV_DIR}/report-private.txt"
PUBLIC_URL = f"https://github.com/{layout.PUBLIC_REPO}"
PUBLIC_PLACE = f"github.com/{layout.PUBLIC_REPO}"     # the same, written without a scheme

MAX_LINES = 60
MAX_CHARS = 4000
MAX_FENCED = 20
MAX_ADDRESS = 7500
SHORTEST_NAME = 3

SECTIONS = (
    ("What happened", "What context-gate did, in your own words. Nothing of the project's: "
                      "no paths, names, code or output."),
    ("What was expected", "What it should have done instead. Nothing of the project's."),
    ("Smallest way to see it", "The fewest steps that show it, on a made-up example. Nothing "
                               "of the project's."),
)

OPENED_RE = re.compile(r"^Opened: \d{4}-\d{2}-\d{2}$")
TITLE_RE = re.compile(r"^# (.*\S.*)$")

HOME_ROOTS = ("Users", "home", "root", "var", "tmp", "private", "opt")
PATH_PREFIXES = tuple(f"/{d}/" for d in HOME_ROOTS) + ("~/",)
# `C:\` or `D:/`; not the `s:/` of `https://`, nor a one-letter scheme's `x://`.
DRIVE_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]:\\|(?<![A-Za-z0-9+.-])[A-Za-z]:/(?!/)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
URL_RE = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>\"'`]+")
# An address with no scheme: a domain-shaped token followed by `/`, perhaps after a port. A file
# name (`docs/checks.md`) is not one: nothing follows it, or a `/` comes before it.
BARE_RE = re.compile(r"(?<![\w@/.-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d+)?/[^\s<>\"'`]*")
TRAILING = ".,;:!?)]}"
# A step up out of a path (`/..`, `\..`), read after decoding.
UP_RE = re.compile(r"(?:^|[/\\])\.\.(?![\w-])")
_START = r"(?<![A-Za-z0-9])"
SECRETS = (
    (re.compile(r"-----BEGIN"), "a private key or certificate block"),
    (re.compile(_START + r"(?:ghp_|gho_|github_pat_)"), "a GitHub token"),
    (re.compile(_START + r"sk-"), "an API key (sk-)"),
    (re.compile(_START + r"AKIA"), "an AWS access key"),
    (re.compile(_START + r"xox[abp]-"), "a Slack token"),
    (re.compile(r"(?i)\bBearer\s+\S"), "a bearer token"),
    (re.compile(r"(?i)(password|passwd|secret|token|api_key)[\"']?\s*[=:]\s*[\"']?[^\s\"']"),
     None),
    (re.compile(r"[A-Za-z0-9+/_=-]{32,}"), "a run of 32 or more characters with no space: "
                                           "a key, a hash or an id"),
)


class ReportError(Exception):
    """Something that stops the command before it changes anything; `code` is its exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Hit:
    line: int          # 0: about the whole file, not one line of it
    what: str


@dataclass
class Draft:
    path: Path
    shown: str                 # the path as printed: from the project's root
    text: str                  # without its BOM
    lines: list[str]           # every line, line endings removed
    end: int                   # lines[:end] is what is sent; the rest is `Opened:` lines

    @property
    def title(self) -> str:
        m = TITLE_RE.match(self.lines[0]) if self.lines else None
        return m.group(1).strip() if m else ""

    @property
    def body(self) -> str:
        return "\n".join(self.lines[1:self.end]).strip("\n")


# ---------------------------------------------------------------------------- draft

def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].rstrip("-")
    return s or "report"


def template(title: str) -> str:
    parts = [f"# {title}", ""]
    for heading, hint in SECTIONS:
        parts += [f"## {heading}", "", f"<!-- {hint} -->", ""]
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    parts += ["## Environment", "", f"- {layout.DISPLAY_NAME} {__version__}", f"- Python {py}",
              f"- {platform.system() or 'unknown'}", ""]
    return "\n".join(parts)


def draft(root: Path, title: str, today: date | None = None) -> Path:
    """Write a new draft and, when missing, the ignore file beside it. Either both are there
    afterwards or neither is: a draft that cannot be written takes back what this call made."""
    title = title.strip()
    if not title:
        raise ReportError("the title is empty", 2)
    if CONTROL_RE.search(title):
        raise ReportError("the title must be one line of plain text", 2)
    folder = root / REPORTS
    made_folder = not folder.exists()
    ignore = folder / ".gitignore"
    made_ignore = False
    stem = f"{(today or date.today()).isoformat()}-{slug(title)}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            with open(ignore, "x", encoding="utf-8", newline="") as fh:
                made_ignore = True
                fh.write("*\n")
        except FileExistsError:
            pass                      # the project's own: left as it is
        n = 1
        while True:
            path = folder / (f"{stem}.md" if n == 1 else f"{stem}-{n}.md")
            try:
                with open(path, "x", encoding="utf-8", newline="") as fh:
                    fh.write(template(title))
                return path
            except FileExistsError:
                n += 1
    except OSError as exc:
        if made_ignore:
            ignore.unlink(missing_ok=True)
        if made_folder:
            try:
                folder.rmdir()
            except OSError:
                pass
        raise ReportError(f"cannot write the draft in {REPORTS}: {exc.strerror or exc}; "
                          f"nothing was written") from exc


# ---------------------------------------------------------------------------- reading a draft

def load(root: Path, name: str) -> Draft:
    """The draft `name` (from the project's root, or absolute). Refuses a file outside the
    reports folder, a missing one, and one that is not UTF-8: never read as empty."""
    folder = (root / REPORTS).resolve()
    path = Path(name) if Path(name).is_absolute() else root / name
    real = path.resolve()
    if folder not in real.parents:
        raise ReportError(f"{name}: not a draft in {REPORTS}; run `report draft` and use the "
                          f"path it prints", 2)
    if not real.is_file():
        raise ReportError(f"{name}: no such draft")
    got = read_text(real)
    if got.error:
        raise ReportError(f"{name}: {got.error}; nothing was checked or sent")
    shown = real.relative_to(root.resolve()).as_posix()
    lines = got.text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    end = len(lines)
    while end > 1 and (OPENED_RE.match(lines[end - 1]) or not lines[end - 1].strip()):
        end -= 1
    return Draft(path=real, shown=shown, text=got.text, lines=lines, end=end)


# ---------------------------------------------------------------------------- identities

def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              timeout=GIT_TIMEOUT, env=git_env())
    except (OSError, subprocess.SubprocessError):
        return None


def _reason(args: tuple[str, ...], res: subprocess.CompletedProcess | None) -> str:
    if res is None:
        return f"git {args[0]} could not run"
    text = " ".join(res.stderr.decode("utf-8", errors="replace").split())
    return f"git {args[0]} failed: {text or f'exit {res.returncode}'}"


def _owner_repo(url: str) -> list[str]:
    """The owner and repository name in a remote's URL: its last two path parts."""
    path = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", url.strip(), flags=re.I)
    path = re.sub(r"^[^/:]*@[^:/]*:", "", path)            # scp form: git@host:owner/repo
    parts = [p for p in re.split(r"[/:\\]", path) if p]
    if parts and parts[-1].lower().endswith(".git"):
        parts[-1] = parts[-1][:-4]
    return parts[-2:]


def _remote_host(url: str) -> str:
    """The host a remote's URL names (`https://host/...`, `ssh://user@host:22/...`,
    `user@host:owner/repo`), or "" for a local path."""
    url = url.strip()
    m = re.match(r"(?i)^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?(\[[^\]/]*\]|[^/:?#\\]*)", url)
    if not m:
        m = re.match(r"^(?:[^@/:\\]+@)?([^@/:\\]{2,}):", url)    # scp form; `C:` is a drive
    return m.group(1).strip("[]") if m else ""


def identities(root: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """`([(kind, value)], [failure])`: every name this machine and its git know the project by,
    and each question that could not be answered. A failure is a hit of its own: an identity
    that could not be read is never read as no identity."""
    found: list[tuple[str, str]] = [("the root folder's name", root.name)]
    failed: list[str] = []

    def ask(*args: str, unset_ok: bool = False) -> str | None:
        res = _git(root, *args)
        if res is not None and res.returncode == 0:
            return res.stdout.decode("utf-8", errors="replace").strip()
        if res is not None and unset_ok and res.returncode == 1 and not res.stderr.strip():
            return ""
        failed.append(_reason(args, res))
        return None

    top = ask("rev-parse", "--show-toplevel")
    if top:
        found.append(("the repository's folder name", Path(top).name))
    common = ask("rev-parse", "--git-common-dir")      # a linked worktree's main checkout
    if common:
        found.append(("the repository's folder name", (root / common).resolve().parent.name))
    remotes = ask("remote", "-v")
    for line in (remotes or "").splitlines():
        bits = line.split()
        if len(bits) >= 2:
            found.append(("a git remote's URL", bits[1]))
            parts = _owner_repo(bits[1])
            if len(parts) == 2:
                found.append(("a git remote's owner", parts[0]))
            if parts:
                found.append(("a git remote's repository name", parts[-1]))
            host = _remote_host(bits[1])
            if host.casefold() != "github.com":
                found.append(("a git remote's host", host))
    for key in ("user.name", "user.email"):
        value = ask("config", "--get", key, unset_ok=True)
        if value:
            found.append((f"git {key}", value))
    try:
        found.append(("the host name", socket.gethostname()))
        found.append(("the host name", socket.gethostname().split(".")[0]))
    except OSError as exc:
        failed.append(f"no host name ({exc})")
    try:
        found.append(("the login name", getpass.getuser()))
    except Exception as exc:  # noqa: BLE001 (OSError, KeyError, ImportError by platform)
        failed.append(f"no login name ({type(exc).__name__})")
    kept, seen = [], set()
    for kind, value in found:
        value = value.strip()
        if len(value) >= SHORTEST_NAME and (kind, value.casefold()) not in seen:
            seen.add((kind, value.casefold()))
            kept.append((kind, value))
    return kept, failed


def private_terms(root: Path) -> tuple[list[str], list[str]]:
    """`(terms, [failure])` from the project's own list, when it has one."""
    path = root / PRIVATE_LIST
    if not path.exists():
        return [], []
    got = read_text(path)
    if got.error:
        return [], [f"cannot read {PRIVATE_LIST}: {got.error}"]
    terms = [line.split("#", 1)[0].strip() for line in got.text.splitlines()]
    return [t for t in terms if t], []


def _places(root: Path) -> list[tuple[str, str]]:
    """The project root and the home folder, in each spelling a report could hold them."""
    out = []
    for kind, p in (("the project root", root), ("the home folder", Path.home())):
        for form in {str(p), p.as_posix(), str(p.resolve()), p.resolve().as_posix()}:
            if len(form.strip("/\\")) >= SHORTEST_NAME:
                out.append((kind, form))
    return out


# ---------------------------------------------------------------------------- the check

def _name_re(value: str) -> re.Pattern:
    """A name as a word of its own, case ignored: `app` is not found in `apply`."""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", re.I)


def _allowed(token: str, base: str) -> bool:
    """Whether the address `token` is on the public repository (`base`, with or without its
    scheme): it starts with it at a path boundary, never steps up out of it (`../`, encoded or
    not) and holds no other address (`://`)."""
    url = token.rstrip(TRAILING)
    rest = url[len(base):]
    if not url.lower().startswith(base.lower()) or (rest and rest[0] not in "/?#"):
        return False
    seen = token[len(base):]
    for _ in range(4):               # each layer of encoding: `%252e` is `%2e` is `.`
        seen = urllib.parse.unquote(seen)
    return not (UP_RE.search(seen) or "://" in seen)


def _readings(line: str) -> list[str]:
    """The line as written, URL-decoded, with its HTML entities read, and both in either order:
    each way a browser or a reader could see it. An entity's space (`&nbsp;`) reads as a space."""
    def url(s: str) -> str:
        return urllib.parse.unquote_plus(s)

    def ent(s: str) -> str:
        return re.sub(r"\s", " ", html.unescape(s))
    return list(dict.fromkeys((line, url(line), ent(line), ent(url(line)), url(ent(line)))))


def _masked(line: str) -> str:
    """The line with the public repository's own name blanked out, for the rules a name or a
    long run would otherwise find in an address on that repository."""
    return re.sub(re.escape(layout.PUBLIC_REPO), lambda m: " " * len(m.group()), line,
                  flags=re.I)


def _line_hits(line: str, names: list[tuple[str, re.Pattern]],
               places: list[tuple[str, str]], terms: list[str]) -> list[str]:
    """What is private in `line`, in any of its readings: each kind once."""
    hits: list[str] = []
    for reading in _readings(line):
        for what in _reading_hits(reading, names, places, terms):
            if what not in hits:
                hits.append(what)
    return hits


def _reading_hits(line: str, names: list[tuple[str, re.Pattern]],
                  places: list[tuple[str, str]], terms: list[str]) -> list[str]:
    hits = []
    low = line.casefold()
    for kind, form in places:
        if form.casefold() in low:
            hits.append(f"a path ({kind})")
    for prefix in PATH_PREFIXES:
        if prefix.casefold() in low:
            hits.append(f"a path ({prefix})")
    if DRIVE_RE.search(line):
        hits.append("a path (a Windows drive)")
    masked = _masked(line)
    for kind, pattern in names:
        if pattern.search(masked):
            hits.append(f"an identity: {kind}")
    if EMAIL_RE.search(line):
        hits.append("an email address")
    if (any(not _allowed(m.group(), PUBLIC_URL) for m in URL_RE.finditer(line))
            or any(not _allowed(m.group(), PUBLIC_PLACE) for m in BARE_RE.finditer(line))):
        hits.append(f"an address not on {layout.PUBLIC_REPO}")
    for pattern, kind in SECRETS:
        m = pattern.search(masked)
        if m:
            hits.append(f"a secret's shape: {kind or m.group(1).lower() + ' with a value'}")
    masked_low = masked.casefold()
    if any(t.casefold() in masked_low for t in terms):
        hits.append(f"a term from {PRIVATE_LIST}")
    return hits


def _size_hits(d: Draft, kinds: list[str]) -> list[Hit]:
    sent = d.lines[:d.end]
    hits = []
    if len(sent) > MAX_LINES:
        hits.append(Hit(MAX_LINES + 1, f"the report is {len(sent)} lines: at most {MAX_LINES}"))
    total = 0
    for i, line in enumerate(sent, 1):
        total += len(line) + 1
        if total > MAX_CHARS + 1:     # the last line's newline is not sent
            size = sum(len(x) + 1 for x in sent) - 1
            hits.append(Hit(i, f"the report is {size:,} characters: at most {MAX_CHARS:,}"))
            break
    fenced, first = 0, 0
    for i, (line, kind) in enumerate(zip(sent, kinds), 1):
        if kind == FENCE and not FENCE_RE.match(line):
            fenced += 1
            if fenced == MAX_FENCED + 1:
                first = i
    if fenced > MAX_FENCED:
        hits.append(Hit(first, f"{fenced} lines inside code fences: at most {MAX_FENCED}"))
    heads = [i for i, (line, kind) in enumerate(zip(sent, kinds))
             if kind == LIVE and line.startswith("## ")] + [len(sent)]
    for start, stop in zip(heads, heads[1:]):
        body = "\n".join(sent[start + 1:stop])
        if not re.sub(r"<!--.*?(-->|\Z)", "", COMMENT_RE.sub("", body), flags=re.S).strip():
            name = sent[start][3:].strip()
            hits.append(Hit(start + 1, f'the section "{name}" still holds only its comment'))
    return hits


def check(root: Path, d: Draft, markers) -> list[Hit]:
    """Every piece of private text in what `send` would send: line 1 and the body, without the
    `Opened:` lines at the end. Sorted by line; one hit per line and kind."""
    ids, failed = identities(root)
    terms, unread = private_terms(root)
    hits = [Hit(0, f"cannot check identities: {why}") for why in failed]
    hits += [Hit(0, why) for why in unread]
    if not d.title:
        hits.append(Hit(1, 'line 1 is not "# <title>"'))
    names = [(kind, _name_re(value)) for kind, value in ids]
    places = _places(root)
    sent = d.lines[:d.end]
    for i, line in enumerate(sent, 1):
        hits += [Hit(i, what) for what in _line_hits(line, names, places, terms)]
    hits += _size_hits(d, line_kinds(sent, markers))
    out, seen = [], set()
    for h in sorted(hits, key=lambda h: h.line):
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def hit_lines(d: Draft, hits: list[Hit]) -> list[str]:
    return [f"  private     {d.shown}" + (f":{h.line}" if h.line else "") + f": {h.what}"
            for h in hits]


# ---------------------------------------------------------------------------- send

def address(d: Draft) -> str:
    title = urllib.parse.quote(f"[field] {d.title}", safe="")
    body = urllib.parse.quote(d.body, safe="")
    # No `template=`: GitHub may then ignore `body=`. The title and body carry everything.
    return f"{PUBLIC_URL}/issues/new?title={title}&body={body}"


def mark_opened(d: Draft, today: date | None = None) -> None:
    """Append `Opened: <date>` to the draft, keeping its BOM and line endings. Written to a
    temporary file and swapped in, so a failure leaves the draft as it was."""
    nl = "\r\n" if "\r\n" in d.text else "\n"
    text = d.text if d.text.endswith(("\n", "\r")) or not d.text else d.text + nl
    text += f"Opened: {(today or date.today()).isoformat()}{nl}"
    bom = b"\xef\xbb\xbf" if has_bom(d.path) else b""
    tmp = d.path.with_name(d.path.name + ".tmp")
    try:
        tmp.write_bytes(bom + text.encode("utf-8"))
        os.replace(tmp, d.path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------- the command

def _err(message: str, code: int) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def run(ctx, args) -> int:
    try:
        if args.report_cmd == "draft":
            path = draft(ctx.root, args.title)
            print(path.relative_to(ctx.root).as_posix())
            return 0
        d = load(ctx.root, args.file)
    except ReportError as exc:
        return _err(str(exc), exc.code)
    hits = check(ctx.root, d, ctx.markers)
    if hits:
        print("\n".join(hit_lines(d, hits)))
        print(f"[FAIL] report check  ({len(hits)} to remove)")
        return 1
    if args.report_cmd == "check":
        print("[OK] report check")
        return 0
    url = address(d)
    if len(url) > MAX_ADDRESS:
        return _err(f"too long for an issue page: shorten it ({len(url):,} characters in the "
                    f"address, at most {MAX_ADDRESS:,}); nothing was opened", 1)
    opened = False
    if not args.print_only:
        print(f"opening the issue page on {layout.PUBLIC_REPO}: read it there and press Submit; "
              f"nothing is sent until you do")
        print(url)
        try:
            opened = bool(webbrowser.open(url))
        except webbrowser.Error:
            opened = False
    if not opened:
        print("open this address in a browser")
        print(url)
    try:
        mark_opened(d)
    except OSError as exc:
        return _err(f"{d.shown}: cannot add the Opened: line ({exc.strerror or exc})", 1)
    return 0
