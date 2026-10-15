"""Regression check: the base's own tests, unchanged, must stay green on the candidate.

Relative to the base, never absolute: a test that already fails on the unfixed base is not the
candidate's fault. The tests are the existing test files related to a module the candidate
changed, chosen on the base (`related_tests`: named after it first, then mentioning it
case-insensitively or by dotted path; capped). Nothing related is an explicit "not run", which the
verdict lists as not verified, never green. The base's version of each file is copied onto a
fork of the candidate, so a candidate that edited a test cannot hide a regression by weakening it.
A red test in a file the candidate itself edited is a *changed expectation* (the issue may ask for
new behaviour); a red test in a file it did not touch is *broken*. No per-test outcomes on either
side, or a timeout, is inconclusive — never proof either way. Both runs keep the tail of their
output (verify/tail.py: the sandbox keeps only the head of long output, and the `-rA` lines are
last); when a tail was cut, a node missing from one side is not compared.

Hidden grading data (a task's test patch, FAIL_TO_PASS / PASS_TO_PASS) is never an input here.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field

from warden.sandbox.box import TIMEOUT_EXIT, Box
from warden.verify.tail import keep_tail, was_cut
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
    skipped: str = ""  # why the check did not run (nothing to run it on): not verified

    @classmethod
    def not_run(cls, reason: str) -> RegressionResult:
        return cls((), {}, {}, skipped=reason)

    @property
    def ran(self) -> bool:
        return not self.skipped

    @property
    def label(self) -> str:
        if self.skipped:
            return f"not run: {self.skipped}"
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


# Module names that say nothing about which tests exercise them: a bare-word content match on
# `utils` selects half a repo. These (and stems shorter than MIN_STEM) match tests only by the
# exact file name (`test_utils.py`) or by their dotted import path (`pkg.utils`).
GENERIC_STEMS = frozenset(
    {
        "__main__",
        "api",
        "base",
        "common",
        "compat",
        "config",
        "conftest",
        "constants",
        "core",
        "errors",
        "exceptions",
        "helpers",
        "main",
        "misc",
        "models",
        "settings",
        "setup",
        "tools",
        "types",
        "util",
        "utils",
        "version",
    }
)
MIN_STEM = 4
# Both the listing and the content grep stay inside test files (git pathspec `*` spans `/`).
TEST_GLOBS = "'*test_*.py' '*_test.py'"
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Target:
    """A changed source module, as tests would name it."""

    stem: str  # `curationsorting`; a package's `__init__.py` is named by its package
    module: str  # dotted import path, `spikeinterface.curation.curationsorting`
    generic: bool  # too common a name to match by a bare word


def targets(changed: list[str]) -> list[Target]:
    out: dict[str, Target] = {}
    for path in changed:
        if not path.endswith(".py") or _is_test_file(path):
            continue
        parts = path[: -len(".py")].split("/")
        if parts[0] == "src" and len(parts) > 1:
            parts = parts[1:]
        init = parts[-1] == "__init__"
        if init:
            parts = parts[:-1]
        if not parts or not all(_IDENT.fullmatch(p) for p in parts):
            continue
        stem = parts[-1].strip("_") or parts[-1]
        generic = (
            stem in GENERIC_STEMS or len(stem) < MIN_STEM or (init and len(parts) == 1)
        )  # a top-level package's __init__ is imported by every test
        out.setdefault(stem, Target(stem, ".".join(parts), generic))
    return list(out.values())


def content_pattern(tgts: list[Target]) -> str:
    """ERE for `git grep -i`: a specific stem as a word anywhere (`CurationSorting`), a generic
    one only by its dotted path (`pkg\\.utils`). Empty when nothing is specific enough."""
    terms: list[str] = []
    for t in tgts:
        if not t.generic:
            terms.append(t.stem)
        if "." in t.module:  # a top-level module's path is its bare name: covered or too generic
            terms.append(t.module.replace(".", r"\."))
    return "|".join(dict.fromkeys(terms))


def related_tests(
    changed: list[str], tests: list[str], hits: list[str], *, cap: int = MAX_FILES
) -> list[str]:
    """Existing test files related to a changed module, best evidence first, at most `cap`.

    1. the file is named after the module: `test_<stem>.py` / `<stem>_test.py`;
    2. (specific stems only) the name contains the stem, or it sits in a `<stem>/` directory;
    3. its content mentions the module (`hits`: the base's `git grep -il` results).
    `tests` is the base's test-file listing; filename tiers look at it and at `hits`."""
    tgts = targets(changed)
    pool = [p for p in dict.fromkeys([*tests, *hits]) if _is_test_file(p)]
    exact = {n for t in tgts for n in (f"test_{t.stem}.py", f"{t.stem}_test.py")}
    specific = [t.stem for t in tgts if not t.generic]

    def near(path: str) -> bool:
        dirs, name = path.split("/")[:-1], posixpath.basename(path)
        return any(s in name or s in dirs for s in specific)

    tier1 = [p for p in pool if posixpath.basename(p) in exact]
    tier2 = [p for p in pool if p not in tier1 and near(p)]
    seen = {*tier1, *tier2}
    tier3 = [p for p in dict.fromkeys(hits) if _is_test_file(p) and p not in seen]
    return (tier1 + tier2 + tier3)[:cap]


def regression_check(
    base: Box,
    candidate: Box,
    *,
    test_cmd: str,
    cwd: str,
    wrap: str = "",
    max_files: int = MAX_FILES,
    timeout: int = 900,
    db_prefix: str = "",
) -> RegressionResult:
    """`RegressionResult.not_run(...)` when the candidate changed no Python module or no existing
    test file relates to one: an explicit gap, never a green row. `timeout` bounds each of the
    two pytest runs, so the check costs at most 2 × timeout. Every command is POSIX sh (dash).
    `db_prefix` (a Fabric replay) creates + migrates a scratch DB at the rev's head on each box."""
    from warden.verify.gate import apply_db_prefix

    def wrapped(cmd: str) -> str:
        return f"{wrap} {shlex.quote(cmd)}" if wrap else cmd

    changed = candidate.run("git diff --name-only HEAD", cwd=cwd, keep=False).stdout.split()
    tgts = targets(changed)
    if not tgts:
        return RegressionResult.not_run("the candidate changed no Python source module")
    listing = base.run(f"git ls-files -- {TEST_GLOBS}", cwd=cwd, keep=False).stdout.split()
    pattern = content_pattern(tgts)
    hits: list[str] = []
    if pattern:
        grep = f"git grep -l -i -E {shlex.quote(pattern)} -- {TEST_GLOBS}"
        hits = base.run(grep, cwd=cwd, keep=False).stdout.split()
    files = related_tests(changed, listing, hits, cap=max_files)
    if not files:
        names = ", ".join(t.stem for t in tgts)
        return RegressionResult.not_run(f"no existing test file names or mentions {names}")

    contents = {
        f: base.run(f"cat {shlex.quote(f)}", cwd=cwd, keep=False).stdout.encode() for f in files
    }
    command = apply_db_prefix(f"{test_cmd} {' '.join(shlex.quote(f) for f in files)}", db_prefix)
    on_base = base.run(wrapped(keep_tail(command)), cwd=cwd, keep=False, timeout=timeout)
    probe = candidate.copy_in(contents, cwd=cwd)
    on_cand = probe.run(wrapped(keep_tail(command)), cwd=cwd, keep=False, timeout=timeout)

    before, after = parse_outcomes(on_base.stdout), parse_outcomes(on_cand.stdout)
    output = (
        f"$ {command}\n\n# base\n{on_base.stdout[-6000:]}\n\n# candidate\n{on_cand.stdout[-6000:]}"
    )
    if not before or not after or TIMEOUT_EXIT in (on_base.exit_code, on_cand.exit_code):
        return RegressionResult(tuple(files), before, after, inconclusive=True, output=output)

    edited = set(changed)
    cut = was_cut(on_base.stdout) or was_cut(on_cand.stdout)
    broken: list[str] = []
    changed_exp: list[str] = []
    for node, outcome in before.items():
        if cut and node not in after:
            continue  # scrolled out of one tail: unknown, not red
        if outcome != "passed" or after.get(node, "error") not in _RED:
            continue
        (changed_exp if node.split("::", 1)[0] in edited else broken).append(node)
    return RegressionResult(
        tuple(files), before, after, broken=broken, changed_expectations=changed_exp, output=output
    )
