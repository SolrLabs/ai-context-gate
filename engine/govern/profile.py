"""The shared profile: an operator's or team's principles, one layer between the engine standard
and each project.

A project names it in `[governance] profile`: a local directory, or a git URL pinned to a ref
(`git@github.com:org/repo.git#v1`). A git profile is fetched once into
`~/.cache/context-gate/profiles/` and read from there after, so a pinned profile gives the
same result on every machine and offline.

Layout of a profile:
- `principles.toml`: settings (`[checks.*]`, `[dialect]`), merged key by key under the project's.
- `PRINCIPLES.md`, and any other document: used unless the project keeps its own copy in
  `.context-gate/`, which replaces the profile's whole file.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from govern import layout

PRINCIPLES = "principles.toml"


class ProfileError(Exception):
    pass


@dataclass
class Profile:
    source: str
    path: Path
    settings: dict

    def document(self, root: Path, name: str) -> Path | None:
        """The project's own copy if it keeps one, else the profile's, else none."""
        local = root / layout.GOV_DIR / name
        if local.is_file():
            return local
        shared = self.path / name
        return shared if shared.is_file() else None


def cache_dir(home: Path, url: str, ref: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", url).strip("-")
    return home / ".cache" / layout.TOOL / "profiles" / slug / ref


def split_source(source: str) -> tuple[str, str | None]:
    url, _, ref = source.partition("#")
    return url, (ref or None)


def is_git(url: str) -> bool:
    return url.startswith(("git@", "https://", "ssh://", "file://")) or url.endswith(".git")


def rmtree(path: Path) -> None:
    """Delete a tree, including git's read-only object files (Windows refuses otherwise) and a
    read-only engine install, whose directories POSIX needs writable to delete from."""
    import os
    import stat
    top = os.path.abspath(path)

    def unlock(func, p, exc):
        if func is os.path.islink:
            raise exc[1]                    # the tree is a symlink: never delete through it
        # Only inside the tree, and never through a symlink: chmod follows links.
        around = [p] if os.path.abspath(p) == top else [os.path.dirname(p), p]
        for q in around:
            if os.path.islink(q):
                continue
            if os.path.isdir(q):
                os.chmod(q, stat.S_IRWXU)
            elif os.path.exists(q):
                os.chmod(q, stat.S_IWRITE | stat.S_IREAD)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=unlock)


def fetch(url: str, ref: str, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    rmtree(tmp)
    res = subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", ref, url,
                          str(tmp)], capture_output=True, text=True,
                         env={"GIT_TERMINAL_PROMPT": "0", **_env()})
    if res.returncode != 0:
        rmtree(tmp)
        raise ProfileError(f"profile {url}#{ref} could not be fetched: {res.stderr.strip()}")
    rmtree(tmp / ".git")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(dest)


def _env() -> dict:
    import os
    return dict(os.environ)


def resolve(source: str, root: Path, home: Path) -> Path:
    url, ref = split_source(source)
    if is_git(url):
        if not ref:
            raise ProfileError(f"profile '{source}' is a git source with no pinned ref — add "
                               f"'#<tag>' so every machine reads the same principles")
        dest = cache_dir(home, url, ref)
        if not dest.is_dir():
            fetch(url, ref, dest)
        return dest
    path = (root / url).expanduser() if not Path(url).expanduser().is_absolute() \
        else Path(url).expanduser()
    if not path.is_dir():
        raise ProfileError(f"profile directory '{url}' does not exist")
    return path


def load(source: str, root: Path, home: Path) -> Profile:
    path = resolve(source, root, home)
    settings: dict = {}
    principles = path / PRINCIPLES
    if principles.is_file():
        try:
            with principles.open("rb") as fh:
                settings = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ProfileError(f"profile {PRINCIPLES}: not valid TOML ({exc})") from exc
    return Profile(source=source, path=path, settings=settings)
