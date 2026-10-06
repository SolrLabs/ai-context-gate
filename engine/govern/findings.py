"""Findings, and the reporters that print them.

The printed layout is a contract: projects' own scripts grep it. A layout is therefore a
dialect setting, never a restyle.
"""
from __future__ import annotations


class Findings:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        # Message -> the ratchet key of the size breach it is about, for the messages that are
        # about one: how `one_per_breach` knows two checks are reporting the same breach
        # without reading either message's text.
        self.breach: dict[str, str] = {}

    def error(self, msg: str, breach: str | None = None) -> None:
        self.errors.append(msg)
        if breach is not None:
            self.breach[msg] = breach

    def warn(self, msg: str, breach: str | None = None) -> None:
        self.warnings.append(msg)
        if breach is not None:
            self.breach[msg] = breach

    def extend(self, other: "Findings") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.breach.update(other.breach)

    def capped(self, level: str) -> "Findings":
        """Apply a configured level: `warn` downgrades errors, `error` leaves them as written."""
        if level != "warn":
            return self
        out = Findings()
        out.warnings = self.errors + self.warnings
        out.breach = dict(self.breach)
        return out

    @property
    def ok(self) -> bool:
        return not self.errors


def one_per_breach(results: list[tuple[str, str, "Findings"]], reporter: str = "ratchet") -> None:
    """One finding per size breach, in place, over one run's `(scope label, check id,
    findings)`: a breach the ratchet reported in this run (new, or grown past its record) is
    not also shown as the limit's own warning. Decided from what the ratchet actually returned,
    never from what it would be expected to: a warning is dropped only when the ratchet's
    finding for the same key is in these same results, so wherever the ratchet says nothing
    about a file (the breach is recorded and unchanged, the rule's ratchet is off, the file is
    in no scope it measured, the baseline or a doc could not be read) the warning stays, and
    no breach ends with no finding at all."""
    reported = {key for _, cid, found in results if cid == reporter
                for msg, key in found.breach.items()
                if msg in found.errors or msg in found.warnings}
    if not reported:
        return
    for _, cid, found in results:
        if cid != reporter:
            found.warnings = [w for w in found.warnings if found.breach.get(w) not in reported]


def report_aggregate(label: str, f: Findings) -> bool:
    """Every error, then every warning, then one status line."""
    for e in f.errors:
        print(f"  ERROR  {e}")
    for w in f.warnings:
        print(f"  warn   {w}")
    status = "OK" if f.ok else "FAIL"
    print(f"[{status}] {label}  ({len(f.errors)} errors, {len(f.warnings)} warnings)")
    return f.ok


def report_per_scope(groups: list[tuple[str, Findings]]) -> bool:
    """Each scope's errors, then its warnings, then its status line; a total when there is more
    than one scope. A multi-project run shows at a glance which project is failing."""
    total = Findings()
    for label, f in groups:
        for e in f.errors:
            print(f"  ERROR  {e}")
        for w in f.warnings:
            print(f"  warn   {w}")
        print(f"[{'OK' if f.ok else 'FAIL'}] {label}  ({len(f.errors)} errors, "
              f"{len(f.warnings)} warnings)")
        total.extend(f)
    if len(groups) > 1:
        print(f"[{'OK' if total.ok else 'FAIL'}] total  ({len(total.errors)} errors, "
              f"{len(total.warnings)} warnings)")
    return total.ok


REPORTERS = {"aggregate": report_aggregate}
