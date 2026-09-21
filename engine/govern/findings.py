"""Findings, and the reporters that print them.

The printed layout is a contract: projects' own scripts grep it. A layout is therefore a
dialect setting, never a restyle.
"""
from __future__ import annotations


class Findings:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def extend(self, other: "Findings") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)

    def capped(self, level: str) -> "Findings":
        """Apply a configured level: `warn` downgrades errors, `error` leaves them as written."""
        if level != "warn":
            return self
        out = Findings()
        out.warnings = self.errors + self.warnings
        return out

    @property
    def ok(self) -> bool:
        return not self.errors


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
