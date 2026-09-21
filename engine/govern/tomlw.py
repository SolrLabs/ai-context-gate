"""A small TOML writer: enough to write a proposed `config.toml` (the stdlib reads TOML but
cannot write it, and the engine takes no dependency).

Writes str, int, bool, lists, inline tables (nested ones included), lists of inline tables, and
nested tables, keeping key order. A table's own tables follow its plain keys, as TOML requires. A
dict at the top level or directly inside such a table is a `[section]`; a dict inside a list or
inside an inline table is written inline. What it writes reads back equal:
`tomllib.loads(dumps(d)) == d`. The caller writes the text UTF-8 with `newline=""`.
"""
from __future__ import annotations

import re

BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f",
           "\r": "\\r"}
WRAP = 100    # an array longer than this on one line gets one item per line


def _key(k: str) -> str:
    if not isinstance(k, str):
        raise TypeError(f"TOML keys are strings, not {type(k).__name__}")
    return k if BARE_KEY.match(k) else _str(k)


def _str(s: str) -> str:
    out = []
    for ch in s:
        if ch in ESCAPES:
            out.append(ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _value(v, indent: str = "") -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, dict):
        if not v:
            return "{}"
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, (list, tuple)):
        items = [_value(x) for x in v]
        one_line = "[" + ", ".join(items) + "]"
        if len(one_line) <= WRAP and not any(isinstance(x, dict) for x in v):
            return one_line
        inner = indent + "  "
        return "[\n" + "".join(f"{inner}{item},\n" for item in items) + indent + "]"
    raise TypeError(f"cannot write a {type(v).__name__} as TOML")


def _table(path: list[str], table: dict, out: list[str]) -> None:
    plain = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
    subs = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    if path and (plain or not subs):
        if out:
            out.append("")
        out.append("[" + ".".join(_key(p) for p in path) + "]")
    for k, v in plain:
        out.append(f"{_key(k)} = {_value(v)}")
    for k, v in subs:
        _table([*path, k], v, out)


def dumps(data: dict, header: str = "") -> str:
    """`data` as TOML text. `header` is plain text, written first as comment lines."""
    out: list[str] = [f"# {line}".rstrip() for line in header.splitlines()]
    body: list[str] = []
    _table([], data, body)
    if out and body:
        out.append("")
    return "\n".join(out + body) + "\n"
