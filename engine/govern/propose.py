"""Propose a project's `config.toml` from what `measure` found: the layout
only — registry, scopes, decision logs, traps, docs, blocks, markers — never check
levels or limits, which come from the engine standard and the profile.

Where measurement settles a setting, it is proposed. Where two readings are left, it is a
`Question` whose first option is the recommendation, and the setting stays out of the config
until an answer (keyed by `Question.key`) picks one. A setting every scope shares (`[projects]
decision_log`, `working_dir`, `trap_glob`) is proposed only when the scopes agree; otherwise that
too is a question.

Two questions come first, always. `shape`: "single" or "workspace", the recommendation (a
workspace when a registry or nested checkouts exist) first. For a workspace, `repos`: every
registry entry and every nested checkout no entry names, answered as a comma list of names. Its
first option is the recommended answer (the governed entries, or with no registry every nested
checkout), and the rest are the names, one each. Until they are answered the proposal follows the
recommendation. An existing registry is never edited: its unselected governed entries go in
`[registry] skip`. With no registry, the selected checkouts become `registry_file`, the
`projects.toml` adopt writes, whose repos must then be measured (`measure(root, planned=...)`)
and proposed again before their layout is known.

`propose` writes nothing. It reads the project's registry file (to note entries missing an id
range) and asks whether a log it would create already exists. `validate` loads a proposed config
the way the engine will, in a temporary root, so a proposal that would not load is caught before
adopt installs it.
"""
from __future__ import annotations

import copy
import fnmatch
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from govern import __version__, config, layout, registry, tomlw
from govern import profile as profiles
from govern.measure import Measurement, ScopeMeasure, TrapSet

DEFAULT_LOG = "DECISIONS.md"
FRONTMATTER_ONLY = "only files with frontmatter"
WHOLE_DIR = "the whole dir"
SINGLE, WORKSPACE = "single", "workspace"
# The registry adopt writes for a workspace that has none: one `[[project]]` per selected repo.
REGISTRY_FILE, REGISTRY_ENTRIES = "projects.toml", "project"
PENDING = "<pending>"        # a question was asked; the setting waits for its answer


@dataclass
class Question:
    key: str                  # unique: "decision_log", or "decision_log:client" per scope
    text: str
    options: list[str]        # first is the recommendation
    why: str


@dataclass
class Proposal:
    config: dict
    questions: list[Question]
    create: list[str]         # decision logs adopt must create
    migrate: bool             # measure found sectioned logs or bullet traps
    notes: list[str] = field(default_factory=list)
    registry_file: dict | None = None   # a workspace with no registry: the `projects.toml` to write


def _under(path: str, base: str) -> str | None:
    """`path` relative to `base` (both repo-relative, posix), or None when it lies outside."""
    if base in (".", ""):
        return path
    try:
        return PurePosixPath(path).relative_to(base).as_posix()
    except ValueError:
        return None


def _join(base: str, rel: str) -> str:
    return rel if base in (".", "") else f"{base}/{rel}"


def glob_matches(pattern: str, path: str) -> bool:
    """Whether `Path.glob(pattern)` from a directory would yield `path` (both posix and relative
    to it): `*` stays inside one path segment, `**` spans any number of them."""
    def match(ps: list[str], xs: list[str]) -> bool:
        if not ps:
            return not xs
        if ps[0] == "**":
            return any(match(ps[1:], xs[i:]) for i in range(len(xs) + 1))
        return bool(xs) and fnmatch.fnmatchcase(xs[0], ps[0]) and match(ps[1:], xs[1:])
    return match(pattern.split("/"), path.split("/"))


def _breadth(pattern: str) -> tuple[int, int]:
    """How much a glob matches, for picking the narrowest: fewer wildcards, then more literal."""
    return sum(pattern.count(c) for c in "*?["), -len(pattern)


class _Proposer:
    def __init__(self, m: Measurement, answers: dict[str, str]):
        self.m, self.answers = m, answers
        self.single = m.registry is None
        # A single repo's workspace measurement is its one project scope (never [workspace]).
        self.scopes: list[ScopeMeasure] = [m.workspace] if self.single else list(m.scopes)
        self.questions: list[Question] = []
        self.notes: list[str] = list(m.notes)
        self.create: list[str] = []
        self.used: set[str] = set()
        self.pairs = {(f, i) for f, i in m.blocks}
        self.migrate = False
        self.registry_file: dict | None = None

    # ------------------------------------------------------------------------ shape and repos
    def shape(self) -> str:
        """Single repo or workspace: the answer, else the recommendation (and the question)."""
        m = self.m
        rec = WORKSPACE if m.registry or m.subrepos else SINGLE
        options = [rec, SINGLE if rec == WORKSPACE else WORKSPACE]
        a = self.answer("shape")
        if a in options:
            return a
        if a is not None:
            self.notes.append(f"answer 'shape' = '{a}' is not one of {options}; asked again")
        found = ([f"the registry {m.registry}"] if m.registry else []) \
            + ([f"nested checkouts ({', '.join(m.subrepos)})"] if m.subrepos else [])
        self.ask("shape", "Is this one repo, or a workspace governing several?", options,
                 f"found {' and '.join(found)}" if found
                 else "no registry and no nested checkout found")
        return rec

    def planned(self) -> bool:
        """The registry measured is the one adopt will write, not a file on disk."""
        return self.m.registry is not None and not (Path(self.m.root) / self.m.registry).is_file()

    def repos(self, cfg: dict) -> list[str]:
        """Which repos a workspace governs. An existing registry is never edited: its governed
        entries left unselected go in `[registry] skip`. With none, the selection becomes the
        registry file adopt writes (`self.registry_file`). Returns the names skipped."""
        m, root = self.m, Path(self.m.root)
        existing = m.registry is not None and not self.planned()
        entries: list[str] = []
        governed: list[str] = []
        listed: set[str] = set()
        if existing:
            c = config.Config(root=root, path=root / layout.CONFIG, raw=cfg, dialect={},
                              checks={})
            try:
                for s in registry.load(c).scopes:
                    entries.append(s.name)
                    if s.governed:
                        governed.append(s.name)
                    for d in (s.get("dir"), s.get("governance")):
                        if isinstance(d, str) and d:
                            listed.add(PurePosixPath(d).as_posix())
            except (registry.RegistryMissing, config.ConfigError) as exc:
                self.notes.append(f"registry: could not be read ({exc})")
        taken = set(entries)
        checkouts: dict[str, str] = {}      # name -> dir, for checkouts no entry names
        for d in m.subrepos:
            if d in listed:
                continue
            name = PurePosixPath(d).name
            name = d if name in taken else name
            taken.add(name)
            checkouts[name] = d
        names = entries + list(checkouts)
        default = governed if existing else list(checkouts)

        selected = default
        a = self.answer("repos")
        if a is not None:
            chosen = [n.strip() for n in a.split(",") if n.strip()]
            unknown = [n for n in chosen if n not in names]
            if unknown:
                self.notes.append(f"answer 'repos' names {', '.join(unknown)}, not a repo "
                                  f"here; asked again")
                a = None
            else:
                selected = chosen
        if a is None:
            held = f"the entries of {m.registry}" if existing else "the nested checkouts"
            self.ask("repos", "Which repos does this workspace govern? (a comma list of names)",
                     [",".join(default), *names],
                     f"{held}; the first option is the recommended answer"
                     + (" (the entries governed now)" if existing else ""))

        if not existing:
            if not selected:
                self.notes.append("repos: none selected; the workspace governs only itself")
            self.registry_file = {REGISTRY_ENTRIES: [
                {"name": n, "dir": checkouts[n], "tier": "full"} for n in selected]}
            return []
        for n in selected:
            if n in checkouts:
                self.notes.append(f"repos: '{n}' ({checkouts[n]}) has no entry in {m.registry}; "
                                  f"add one there to govern it")
            elif n not in governed:
                self.notes.append(f"repos: '{n}' is not governed by its entry in {m.registry} "
                                  f"(its tier, or an empty governance); edit the entry to "
                                  f"govern it")
        return [n for n in governed if n not in selected]

    def key(self, name: str, scope: ScopeMeasure) -> str:
        return name if self.single else f"{name}:{scope.name}"

    def answer(self, key: str) -> str | None:
        if key in self.answers:
            self.used.add(key)
            return self.answers[key]
        return None

    def ask(self, key: str, text: str, options: list[str], why: str) -> str:
        self.questions.append(Question(key=key, text=text, options=options, why=why))
        return PENDING

    def plan_create(self, path: str) -> None:
        if path not in self.create and not (Path(self.m.root) / path).exists():
            self.create.append(path)

    def agree(self, name: str, values: dict[str, str | None], text: str) -> str | None:
        """The one value every scope shares; None when no scope has one; PENDING when a scope is
        still undecided, or when the scopes disagree (then it is a question)."""
        a = self.answer(name)
        if a is not None:
            return a
        if PENDING in values.values():
            return PENDING
        counts = Counter(v for v in values.values() if v is not None)
        if not counts:
            return None
        if len(counts) == 1:
            return next(iter(counts))
        held = "; ".join(f"{n}: {v}" for n, v in values.items() if v is not None)
        return self.ask(name, text, [v for v, _ in counts.most_common()],
                        f"[projects] {name} is one path for every scope, and the scopes differ "
                        f"({held})")

    # ------------------------------------------------------------------------ decision logs
    def scope_log(self, s: ScopeMeasure, key: str) -> str | None:
        """The scope's log relative to its dir; None when it has none; PENDING when asked."""
        a = self.answer(key)
        if a is not None:
            return a
        cands = [(c, _under(c.path, s.dir)) for c in s.decision_logs]
        cands = [(c, rel) for c, rel in cands if rel is not None]
        if not cands:
            return None
        top = cands[0][0]
        named = sum(c.named for c, _ in cands)
        with_entries = sum(c.entries > 0 for c, _ in cands)
        if (top.named and named == 1) or (top.entries and with_entries == 1):
            return cands[0][1]
        why = "; ".join(f"{rel} ({'named, ' if c.named else ''}{c.entries} entries)"
                        for c, rel in cands)
        return self.ask(key, f"Which file is {s.name}'s decision log?",
                        [rel for _, rel in cands],
                        f"more than one candidate, and no single named log or single log "
                        f"with entries: {why}")

    def decision_logs(self, cfg: dict) -> None:
        per = {s.name: self.scope_log(s, self.key("decision_log", s)) for s in self.scopes}
        agreed = self.agree("decision_log", per, "Which path is every project's decision log?")
        if agreed is None:
            docs = self.single and ("docs" in self.m.workspace.doc_dirs
                                    or (Path(self.m.root) / "docs").is_dir())
            agreed = f"docs/{DEFAULT_LOG}" if docs else DEFAULT_LOG
        if agreed != PENDING:
            cfg["projects"]["decision_log"] = agreed
            for s in self.scopes:
                if per[s.name] is None:
                    self.plan_create(_join(s.dir, agreed))
                elif per[s.name] not in (agreed, PENDING):
                    self.notes.append(f"{s.name}: its decision log is {per[s.name]}, but "
                                      f"[projects] decision_log is {agreed}")
            pending = [s.name for s in self.scopes
                       if (_join(s.dir, agreed), "decision-index") not in self.pairs
                       and _join(s.dir, agreed) not in self.create]
            if pending:
                self.notes.append(f"no project decision-index block proposed: "
                                  f"{', '.join(pending)} has no decision-index pair in {agreed}")
            else:
                cfg["blocks"]["project"].append({"file": agreed, "id": "decision-index"})
        if self.single:
            return
        ws = self.m.workspace
        log = self.scope_log(ws, f"decision_log:{ws.name}")
        if log is None:
            log = DEFAULT_LOG
            self.plan_create(_join(ws.dir, log))
        if log != PENDING:
            path = _join(ws.dir, log)
            cfg["workspace"]["decision_log"] = path
            if (path, "decision-index") in self.pairs or path in self.create:
                cfg["blocks"]["workspace"].append({"file": path, "id": "decision-index"})

    # ------------------------------------------------------------------------ traps
    def trap_set(self, s: ScopeMeasure, wd: str | None) -> TrapSet | None:
        """The scope's trap set under its working dir; the others are noted, not governed."""
        if wd == PENDING:
            return None
        base = _join(s.dir, wd) if wd is not None else None
        chosen = None
        for ts in s.trap_sets:
            inside = base is not None and ts.files \
                and all(_under(f, base) is not None for f in ts.files)
            if inside and chosen is None:
                chosen = ts
            else:
                self.notes.append(f"{s.name}: trap files {', '.join(ts.files)} are not under "
                                  f"its working dir; not governed")
        return chosen

    def traps(self, cfg: dict) -> None:
        own_wd = {}
        for s in self.scopes:
            a = self.answer(self.key("working_dir", s))
            own_wd[s.name] = a if a is not None else s.working_dir
        wd = self.agree("working_dir", own_wd, "Which directory is every project's working dir?")
        if wd not in (None, PENDING):
            cfg["projects"]["working_dir"] = wd
        sets = [(s, own_wd[s.name], ts) for s in self.scopes
                if (ts := self.trap_set(s, own_wd[s.name])) is not None]
        glob = self.answer("trap_glob")
        if glob is None and sets:
            cands = [g for g, _ in Counter(ts.glob for _, _, ts in sets).most_common()]
            files = [_under(f, _join(s.dir, w)) for s, w, ts in sets for f in ts.files]
            covering = [g for g in cands if all(glob_matches(g, f) for f in files)]
            glob = min(covering, key=_breadth) if covering else self.ask(
                "trap_glob", "Which glob, relative to the working dir, finds every trap file?",
                cands, "no one scope's glob matches every scope's trap files")
        if glob is None:
            return
        if glob != PENDING:
            cfg["projects"]["trap_glob"] = glob
        if wd in (None, PENDING) or glob == PENDING:
            return
        index: dict[str, str | None] = {}
        for s, w, ts in sets:
            if w != wd:
                self.notes.append(f"{s.name}: its working dir is {w}, not {wd}; its traps get "
                                  f"no trap-index block")
                continue
            if ts.index_spans_files:
                name = _under(ts.files[0], _join(s.dir, wd))
                target, sources = _join(wd, name), _join(wd, glob)
            else:
                target, sources = _join(wd, glob), None
            # One entry per glob (as migrate suggests them): `sources` kept if any scope needs it.
            index[target] = index.get(target) or sources
        for target, sources in index.items():
            # A `[[blocks.project]]` entry applies to every scope: each file it matches anywhere
            # needs the pair, now or once migrate writes it.
            missing = [f for s, _, ts in sets for f in ts.files
                       if glob_matches(target, _under(f, s.dir)) and not self.index_ready(ts, f)]
            if missing:
                self.notes.append(f"no trap-index block for {target}: {', '.join(missing)} "
                                  f"has no trap-index pair, and migrate will not write one")
                continue
            entry = {"glob": target, "id": "trap-index"}
            if sources:
                entry["sources"] = sources
            cfg["blocks"]["project"].append(entry)

    def index_ready(self, ts: TrapSet, f: str) -> bool:
        """Whether trap file `f` holds a trap-index pair, or will once migrate converts the
        hand-written `## Index` in the set's index file (`files[0]`, when it has one)."""
        if (f, "trap-index") in self.pairs:
            return True
        return self.migrate and ts.has_index and f == ts.files[0] \
            and (ts.index_spans_files or len(ts.files) == 1)

    # ------------------------------------------------------------------------ docs
    def docs(self, cfg: dict) -> None:
        globs: list[str] = []
        mixed: dict[str, list[str]] = {}
        typed: dict[str, list[str]] = {}     # a mixed dir -> its files with frontmatter
        for s in self.scopes:
            for d, (n, with_fm) in s.doc_dirs.items():
                if n == 0 or d.startswith("."):
                    continue
                glob, where = f"{d}/**/*.md", f"{d}/"
                if with_fm == n:
                    if glob not in globs:
                        globs.append(glob)
                elif with_fm == 0:
                    self.notes.append(f"{s.name}: {where} has {n} markdown file(s), none with "
                                      f"frontmatter; left out of [projects] docs")
                else:
                    mixed.setdefault(d, []).append(f"{s.name}: {with_fm} of {n}")
                    for f in s.doc_files.get(d, []):
                        rel = _under(f, s.dir)
                        if rel is not None and rel not in typed.setdefault(d, []):
                            typed[d].append(rel)
        for d, counts in mixed.items():
            key, glob = f"docs:{d}", f"{d}/**/*.md"
            files = typed.get(d, [])
            a = self.answer(key)
            if a == WHOLE_DIR:
                if glob not in globs:
                    globs.append(glob)
            elif a == FRONTMATTER_ONLY:
                globs.extend(f for f in files if f not in globs)
            else:
                if a is not None:
                    self.notes.append(f"answer '{key}' = '{a}' is not one of "
                                      f"{[FRONTMATTER_ONLY, WHOLE_DIR]}; asked again")
                self.ask(key, f"Which markdown files under {d}/ are governed docs?",
                         [FRONTMATTER_ONLY, WHOLE_DIR],
                         f"some have frontmatter and some don't ({'; '.join(counts)}); "
                         f"'{FRONTMATTER_ONLY}' lists {', '.join(files) or 'none'}")
        if globs:
            cfg["projects"]["docs"] = globs

    # ------------------------------------------------------------------------ the whole config
    def project_blocks(self, cfg: dict) -> set[tuple[str, str]]:
        """A project's other generated blocks (a doc-registry, say), kept generated: a pair in a
        governed scope becomes a `glob` entry for its path relative to the scope, when every
        scope's file at that path holds the pair (a glob matching nothing in a scope is harmless
        there). An `INDEX.md` doc-registry left unfilled would fail `doc-reachability`. Returns
        the (file, id) pairs these entries cover."""
        kinds = [k for k in config.BLOCK_RENDERERS["project"]
                 if k not in ("decision-index", "trap-index")]
        root, kept = Path(self.m.root), set()
        found: dict[tuple[str, str], None] = {}
        for f, bid in self.m.blocks:
            if bid in kinds:
                for s in self.scopes:
                    rel = _under(f, s.dir)
                    if rel is not None:
                        found[(rel, bid)] = None
        for rel, bid in found:
            files = [_join(s.dir, rel) for s in self.scopes
                     if (root / _join(s.dir, rel)).is_file()]
            if any((f, bid) not in self.pairs for f in files):
                continue          # a file there without the pair: the note below says so
            cfg["blocks"]["project"].append({"glob": rel, "id": bid})
            kept.update((f, bid) for f in files)
        return kept

    def registry(self, cfg: dict) -> None:
        m = self.m
        reg = {"file": m.registry, "entries": m.registry_entries or "project"}
        keys = dict(m.registry_keys)
        if m.governance_key_missing:
            keys.setdefault("governance", "dir")
        if keys:
            reg["keys"] = keys
        cfg["registry"] = reg
        cfg["projects"]["governed_tiers"] = ["full"]

    def id_ranges(self, cfg: dict) -> None:
        """Each entry of the registry adopt writes gets an `id_prefix` (its log's most common;
        for a log adopt creates, the workspace log's, else `D`) and an `id_range` of `1-999`,
        widened to 9999 past 900 as in single-repo mode. Scopes sharing a prefix cannot all
        have that range: each is asked `id_range:<scope>`, offered non-overlapping blocks."""
        chosen = cfg["projects"].get("decision_log")
        ws = next((c.prefixes[0] for c in self.m.workspace.decision_logs if c.prefixes), "D")
        prefix: dict[str, str] = {}
        for s in self.scopes:
            logs = [c for c in s.decision_logs if c.prefixes]
            mine = [c for c in logs if chosen and _under(c.path, s.dir) == chosen] or logs
            prefix[s.name] = mine[0].prefixes[0] if mine else ws
        sharing = Counter(prefix.values())
        entries = {e["name"]: e for e in self.registry_file[REGISTRY_ENTRIES]}
        for s in self.scopes:
            e, p = entries[s.name], prefix[s.name]
            e["id_prefix"] = p
            if sharing[p] == 1:
                e["id_range"] = f"1-{9999 if self.m.max_ids.get(p, 0) + 1 > 900 else 999}"
                continue
            peers = [t.name for t in self.scopes if prefix[t.name] == p]
            blocks = [f"{max(1, i * 1000)}-{i * 1000 + 999}" for i in range(len(peers))]
            own = blocks[peers.index(s.name)]
            key = f"id_range:{s.name}"
            a = self.answer(key)
            if a is not None and registry.parse_range(a) is not None:
                e["id_range"] = a
                continue
            if a is not None:
                self.notes.append(f"answer '{key}' = '{a}' is not a range like 1-999; asked "
                                  f"again")
            self.ask(key, f"Which id range does {s.name} use for its {p}- ids?",
                     [own, *(b for b in blocks if b != own)],
                     f"{', '.join(peers)} all use the prefix {p}, so their ranges must not "
                     f"overlap")

    def id_notes(self, cfg: dict) -> None:
        """id_prefix and id_range live in the registry file, which propose never writes."""
        if self.registry_file is not None:
            for e in self.registry_file[REGISTRY_ENTRIES]:
                if "id_range" not in e:
                    self.notes.append(f"registry: '{e['name']}' has no id_range until "
                                      f"id_range:{e['name']} is answered")
            self.notes.append(f"registry: the workspace has no id_prefix and id_range; add "
                              f"them to {REGISTRY_FILE}")
            return
        root = Path(self.m.root)
        c = config.Config(root=root, path=root / layout.CONFIG, raw=cfg, dialect={}, checks={})
        try:
            reg = registry.load(c)
        except (registry.RegistryMissing, config.ConfigError) as exc:
            self.notes.append(f"registry: could not be read ({exc})")
            return
        self.notes.extend(reg.problems)
        for s in reg.scopes:
            if s.governed and (not s.id_prefix or s.id_range is None):
                self.notes.append(f"registry: '{s.name}' has no id_prefix and id_range; add "
                                  f"them to {self.m.registry}")
        if not reg.workspace_prefix or reg.workspace_range is None:
            self.notes.append(f"registry: the workspace has no id_prefix and id_range; add "
                              f"them to {self.m.registry}")

    def repo(self, cfg: dict) -> None:
        """The range starts at the chosen log's lowest entry, so every existing entry is in
        it; with no entries, past the highest id any heading uses (headings `### D1`..`### D7`
        give 8), else 1. The top is 999, widened to 9999 past 900."""
        chosen = cfg["projects"].get("decision_log")
        prefix, lowest = "D", None
        for c in self.m.workspace.decision_logs:
            if c.path == chosen and c.prefixes:
                prefix, lowest = c.prefixes[0], c.lowest
        top = self.m.max_ids.get(prefix, 0) + 1
        start = lowest if lowest is not None else top
        cfg["repo"] = {"name": Path(self.m.root).name, "id_prefix": prefix,
                       "id_range": f"{start}-{9999 if top > 900 else 999}"}


def propose(m: Measurement, source: str | None, profile: str | None,
            answers: dict[str, str]) -> Proposal:
    """The config, questions and adopt steps for a measured project. Pure: it writes nothing."""
    p = _Proposer(m, answers)
    p.single = p.shape() == SINGLE
    gov: dict = {"engine": __version__}
    if source:
        gov["source"] = source
    if profile:
        gov["profile"] = profile
    gov["schema"] = config.SCHEMA
    cfg: dict = {"governance": gov, "dialect": {}, "registry": {}, "repo": {}, "workspace": {},
                 "projects": {}, "blocks": {"workspace": [], "project": []}}
    if m.markers and m.markers != "gov":
        cfg["dialect"]["markers"] = m.markers
    if p.single:
        p.scopes = [m.workspace]
        if m.registry and not p.planned():
            p.notes.append(f"{m.registry}: not used; the root is adopted as a single repo")
    elif m.registry is None:
        # No registry yet: the selected checkouts become the one adopt writes, and their layout
        # is known only once they are measured as its members.
        cfg["registry"] = {"file": REGISTRY_FILE, "entries": REGISTRY_ENTRIES,
                           "keys": {"governance": "dir"}}
        cfg["projects"]["governed_tiers"] = ["full"]
        p.repos(cfg)
        if (Path(m.root) / REGISTRY_FILE).exists():
            p.notes.append(f"{REGISTRY_FILE} exists and is not a registry measure recognised; "
                           f"adopt will not overwrite it")
        p.notes.append(f"the repos' layout is proposed once they are measured as members of "
                       f"{REGISTRY_FILE}: measure(root, planned=...), then propose again (adopt "
                       f"does both)")
        cfg = {k: v for k, v in cfg.items() if v}
        return Proposal(config=cfg, questions=p.questions, create=[],
                        migrate=False, notes=p.notes, registry_file=p.registry_file)
    else:
        p.registry(cfg)
        skip = p.repos(cfg)
        if skip:
            cfg["registry"]["skip"] = skip
            p.scopes = [s for s in p.scopes if s.name not in skip]
    logs = [c for s in [m.workspace, *p.scopes] for c in s.decision_logs]
    sets = [ts for s in [m.workspace, *p.scopes] for ts in s.trap_sets]
    p.migrate = any(c.level == 3 for c in logs) or any(ts.bullets for ts in sets)
    p.decision_logs(cfg)
    if p.registry_file is not None:
        p.id_ranges(cfg)
    p.traps(cfg)
    p.docs(cfg)
    if p.single:
        p.repo(cfg)
    kept = p.project_blocks(cfg)
    for f, i in m.blocks:
        if i not in ("decision-index", "trap-index") and (f, i) not in kept:
            p.notes.append(f"{f}: an existing '{i}' block is not proposed; add it to [blocks] "
                           f"after install to keep it generated")
    cfg["blocks"] = {k: v for k, v in cfg["blocks"].items() if v}
    cfg = {k: v for k, v in cfg.items() if v}
    # The workspace comes before [projects], as in a hand-written config.
    order = ["governance", "dialect", "registry", "repo", "workspace", "projects", "blocks"]
    cfg = {k: cfg[k] for k in order if k in cfg}
    if not p.single:
        p.id_notes(cfg)
    for key in sorted(set(answers) - p.used):
        p.notes.append(f"answer '{key}' matches no question; ignored")
    return Proposal(config=cfg, questions=p.questions, create=p.create,
                    migrate=p.migrate, notes=p.notes, registry_file=p.registry_file)


def validate(cfg: dict, m: Measurement, home: Path, registry_file: dict | None = None
             ) -> list[str]:
    """Load a proposed config as the engine will: written to a temporary root's config.toml,
    beside a copy of the registry file (or `registry_file`, the one adopt will write), through
    `config.load` and `registry.load`. Raises
    `config.ConfigError` (or `registry.RegistryMissing`) when it would not load; returns the
    registry's problems."""
    raw = copy.deepcopy(cfg)
    gov = raw.get("governance", {})
    url, _ = profiles.split_source(gov.get("profile", ""))
    if url and not profiles.is_git(url) and not Path(url).expanduser().is_absolute():
        gov["profile"] = (Path(m.root) / url).as_posix()   # relative to the project, not here
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / layout.CONFIG
        path.parent.mkdir(parents=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(tomlw.dumps(raw))
        if registry_file is not None:
            with (root / raw["registry"]["file"]).open("w", encoding="utf-8", newline="") as fh:
                fh.write(tomlw.dumps(registry_file))
        elif m.registry and "registry" in raw:
            shutil.copyfile(Path(m.root) / m.registry, root / m.registry)
        cfg_obj = config.load(root, home)
        return list(registry.load(cfg_obj).problems)
