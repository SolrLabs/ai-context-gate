"""A starting tree built once per test process and copied for each test that wants it.

Starting git costs a process each time; copying a directory costs none. A copy is right only
if nothing inside the tree names the directory it was built in, so every copy is checked for
the template's path and refused when it holds it.
"""
from __future__ import annotations

import atexit
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Callable

_TEMPLATES: dict[tuple, Path] = {}


def _remove_all() -> None:
    for template in _TEMPLATES.values():
        for path in template.rglob("*"):   # git's objects are read-only on Windows
            if not path.is_symlink():
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        shutil.rmtree(template, ignore_errors=True)
    _TEMPLATES.clear()


atexit.register(_remove_all)


def _names(template: Path) -> list[bytes]:
    texts = [str(template), str(template.resolve())]
    texts += [t.replace("\\", "/") for t in texts]
    return list(dict.fromkeys(t.encode("utf-8") for t in texts))


def copy_of(key: tuple, dest: Path, build: Callable[[Path], None],
            fix: Callable[[Path], None] | None = None) -> None:
    """Copy the tree `build` makes for `key` into `dest`, building it on the first call."""
    template = _TEMPLATES.get(key)
    if template is None:
        template = Path(tempfile.mkdtemp())
        try:
            build(template)
        except BaseException:
            shutil.rmtree(template, ignore_errors=True)
            raise
        _TEMPLATES[key] = template
    shutil.copytree(template, dest, symlinks=True, dirs_exist_ok=True)
    if fix is not None:
        fix(dest)
    needles = _names(template)
    for path in sorted(dest.rglob("*")):
        if path.is_symlink():
            data = os.readlink(path).encode("utf-8", "surrogateescape")
        elif path.is_file():
            data = path.read_bytes()
        else:
            continue
        if any(needle in data for needle in needles):
            raise AssertionError(f"{path} names the template it was copied from ({template})")
