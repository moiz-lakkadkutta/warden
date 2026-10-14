"""Regression check: the base's own tests, unchanged, must stay green on the candidate.

Relative to the base, never absolute: a test that already fails on the unfixed base is not the
candidate's fault. The tests are the existing test files that mention a module the candidate
changed (`git grep` on the base, name matches first, capped). The base's version of each file is
copied onto a fork of the candidate, so a candidate that edited a test cannot hide a regression by
weakening it. A red test in a file the candidate itself edited is a *changed expectation* (the
issue may ask for new behaviour); a red test in a file it did not touch is *broken*. No per-test
outcomes on either side, or a timeout, is inconclusive — never proof either way.

Hidden grading data (a task's test patch, FAIL_TO_PASS / PASS_TO_PASS) is never an input here.
"""

from __future__ import annotations

import posixpath
import shlex
from dataclasses import dataclass, field

from warden.sandbox.box import TIMEOUT_EXIT, Box
from warden.verify.theater import parse_outcomes

MAX_FILES = 8
_RED = ("failed", "error")


@dataclass(frozen=True)
class RegressionResult:
    files: tuple[str, ...]
    base: dict[str, str]  # node id -> outcome on the unfixed base
    candidate: dict[str, str]  # node id -> outcome on the candidate (base's test files)
    broken: list[str] = field(default_factory=list)
    changed_expectations: list[str] = field(default_factory=list)
    inconclusive: bool = False
    output: str = ""  # both runs, for the artifact

    @property
    def label(self) -> str:
        if self.inconclusive:
            return f"inconclusive ({len(self.files)} files; no per-test outcomes) — not proof"
        if self.broken:
            n = len(self.broken)
            return f"{n} test{'s' if n > 1 else ''} green on base is red on the candidate"
        green = sum(1 for o in self.base.values() if o == "passed")
        note = (
            f"; {len(self.changed_expectations)} changed expectations in edited tests"
            if self.changed_expectations
            else ""
        )
        return f"{green} base tests in {len(self.files)} files stay green{note}"


def _is_test_file(path: str) -> bool:
    name = posixpath.basename(path)
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _stem(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0]


def _sources(changed: list[str]) -> list[str]:
    return [p for p in changed if p.endswith(".py") and not _is_test_file(p)]


def related_tests(changed: list[str], hits: list[str], *, cap: int = MAX_FILES) -> list[str]:
    """Test files among `hits` (git grep results), ones named after a changed module first."""
    stems = {_stem(p) for p in _sources(changed) if _stem(p) != "__init__"}
    tests = [h for h in dict.fromkeys(hits) if _is_test_file(h)]
    named = [t for t in tests if any(s in posixpath.basename(t) for s in stems)]
    rest = [t for t in tests if t not in named]
    return (named + rest)[:cap]


def regression_check(
    base: Box,
    candidate: Box,
    *,
    test_cmd: str,
    cwd: str,
    wrap: str = "",
    max_files: int = MAX_FILES,
    timeout: int = 900,
) -> RegressionResult | None:
    """None when the candidate changed no module that an existing test mentions (not run).
    `timeout` bounds each of the two pytest runs, so the check costs at most 2 × timeout."""

    def wrapped(cmd: str) -> str:
        return f"{wrap} {shlex.quote(cmd)}" if wrap else cmd

    changed = candidate.run("git diff --name-only HEAD", cwd=cwd, keep=False).stdout.split()
    stems = sorted({_stem(p) for p in _sources(changed)} - {"__init__"})
    if not stems:
        return None
    pattern = shlex.quote("|".join(stems))
    grep = base.run(f"git grep -l -E {pattern} -- '*.py'", cwd=cwd, keep=False)
    files = related_tests(changed, grep.stdout.split(), cap=max_files)
    if not files:
        return None

    contents = {
        f: base.run(f"cat {shlex.quote(f)}", cwd=cwd, keep=False).stdout.encode() for f in files
    }
    command = f"{test_cmd} {' '.join(shlex.quote(f) for f in files)}"
    on_base = base.run(wrapped(command), cwd=cwd, keep=False, timeout=timeout)
    probe = candidate.copy_in(contents, cwd=cwd)
    on_cand = probe.run(wrapped(command), cwd=cwd, keep=False, timeout=timeout)

    before, after = parse_outcomes(on_base.stdout), parse_outcomes(on_cand.stdout)
    output = (
        f"$ {command}\n\n# base\n{on_base.stdout[-6000:]}\n\n# candidate\n{on_cand.stdout[-6000:]}"
    )
    if not before or not after or TIMEOUT_EXIT in (on_base.exit_code, on_cand.exit_code):
        return RegressionResult(tuple(files), before, after, inconclusive=True, output=output)

    edited = set(changed)
    broken: list[str] = []
    changed_exp: list[str] = []
    for node, outcome in before.items():
        if outcome != "passed" or after.get(node, "error") not in _RED:
            continue
        (changed_exp if node.split("::", 1)[0] in edited else broken).append(node)
    return RegressionResult(
        tuple(files), before, after, broken=broken, changed_expectations=changed_exp, output=output
    )
