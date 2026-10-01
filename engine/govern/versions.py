"""Engine versions: X.Y.Z releases and X.Y.Z-beta.N betas, which order below their release."""
from __future__ import annotations

import re

_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-beta\.([1-9]\d*))?", re.ASCII)


def parse(v: str) -> tuple | None:
    m = _RE.fullmatch(v) if isinstance(v, str) else None
    if not m:
        return None
    major, minor, patch, beta = m.groups()
    # A release sorts above every beta of it: (…, 1, 0) > (…, 0, N).
    return (int(major), int(minor), int(patch), 0 if beta else 1, int(beta or 0))


def key(v: str) -> tuple:
    k = parse(v)
    if k is None:
        raise ValueError(f"'{v}' is not a version like 0.6.0 or 0.6.0-beta.1")
    return k


def is_beta(v: str) -> bool:
    k = parse(v)
    return bool(k) and k[3] == 0


def is_stable(v: str) -> bool:
    k = parse(v)
    return bool(k) and k[3] == 1


def final(v: str) -> str:
    return v.split("-", 1)[0]
