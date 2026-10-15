"""Theater check: a new test must FAIL on the unfixed base, for the issue's reason, and PASS with
the fix.

Fork `base`, copy in ONLY the new/changed test files, run the new tests. If they all pass, they do
not exercise the change and the candidate cannot be trusted. This is the row judges will
screenshot.

Failing on base is not enough (run 20261014-123340-85cd: an xcal test failed on base only because
its own expectation was wrong, and still failed with the fix). Each new test is classified:

- **repro**: FAILED on base with an assertion (or an exception the issue itself names, or any
  other exception that is the behaviour under test), and PASSED on the candidate.
- **invalid**: red on base for a reason that is not the behaviour — NameError / ImportError /
  ModuleNotFoundError / SyntaxError, a TypeError or AttributeError the issue does not name (the
  test calls an API that does not exist yet), a fixture or setup ERROR, a collection error — or
  still red on the candidate. A new test that fails with the fix applied invalidates the repro.
- **guard**: passes on base (e.g. "AR still excludes drafts"); expected, never proof.

The check passes (`failed_on_base`) with at least one repro and no new test red on the candidate.
Red on base with no repro is `invalid` (FAIL in the verdict); no per-test evidence at all (usage
error, no tests ran, a timeout) is `inconclusive`, never a pass of the check.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from warden.sandbox.box import Box
from warden.verify.tail import keep_tail

DEFAULT_CMD = "pytest -q -p no:cacheprovider -rA --tb=short"
_OUTCOME = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+)", re.MULTILINE)
# `-rA` line with its message: "FAILED t.py::test_x - NameError: name 'f' is not defined"
_SUMMARY_MSG = re.compile(r"^(?:FAILED|ERROR)\s+(\S+) - (.+)$", re.MULTILINE)
# a --tb section header: "____ test_x[a-1] ____", "____ ERROR at setup of test_x ____",
# "____ ERROR collecting t.py ____"; a long name gets a single "_" each side. Not the "_ _ _ _"
# frame separator of long tracebacks.
_SECTION = re.compile(
    r"^_+ (?!_)(?:(ERROR at (?:setup|teardown) of )|(ERROR collecting ))?(.+?) _+$"
)
_E_LINE = re.compile(r"^E\s+(.*\S)")
_EXC = re.compile(r"^([A-Za-z_][\w.]*):?(?:\s|$)")
_EXC_SUFFIX = ("Error", "Exception", "Exit", "Failed", "Interrupt", "Warning")

# Red on base for these is the test failing to run, never the behaviour.
_ALWAYS_INVALID = frozenset(
    {"NameError", "ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError"}
)
# Usually "the API under test does not exist yet"; a real repro only when the issue names it.
_INVALID_UNLESS_NAMED = frozenset({"TypeError", "AttributeError"})
_RED = ("failed", "error")


@dataclass(frozen=True)
class TheaterResult:
    failed_on_base: bool  # proven: >= 1 repro (red on base for the issue's reason, green fixed)
    exit_code: int
    stdout: str
    base_uuid: str
    probe_uuid: str
    outcomes: dict[str, str] = field(default_factory=dict)  # node id -> passed/failed/error/...
    inconclusive: bool = False
    # node id -> why it does or does not count as a repro (the evidence table shows these)
    reasons: dict[str, str] = field(default_factory=dict)
    candidate_outcomes: dict[str, str] = field(default_factory=dict)
    invalid: bool = False  # red on base, but no test is a valid repro (or one is red when fixed)

    @property
    def label(self) -> str:
        if self.failed_on_base:
            n = sum(1 for r in self.reasons.values() if r.startswith("repro"))
            return f"FAIL on unfixed base — as required ({n} of {len(self.reasons)} reproduce)"
        if self.invalid:
            return f"invalid repro on unfixed base — {self.why}"
        if self.inconclusive:
            return f"inconclusive on unfixed base (pytest exit {self.exit_code}) — not proof"
        return "PASSED on unfixed base — tests are theater"

    @property
    def why(self) -> str:
        """The invalid tests and their reasons, short (node: reason; ...)."""
        bad = [f"{short_node(n)}: {r}" for n, r in self.reasons.items() if r.startswith("invalid")]
        return "; ".join(bad[:4]) or "no new test fails on base for the issue's reason"


def short_node(node: str) -> str:
    """`a/b/test_x.py::TestC::test_y[p]` -> `test_x.py::TestC::test_y[p]`."""
    path, sep, rest = node.partition("::")
    return f"{path.rsplit('/', 1)[-1]}{sep}{rest}"


def parse_outcomes(output: str) -> dict[str, str]:
    """`-rA` summary lines → {node id: outcome}; the last line for a node wins."""
    return {node: kind.lower() for kind, node in _OUTCOME.findall(output)}


def expand_targets(targets: list[str], outcomes: dict[str, str]) -> list[str]:
    """The tests to classify: a file target stands for the node ids pytest reported under it
    (run 20261014-131452-cd9b: a new file's four tests were filed as one "no result on base").
    A file with no per-test line (a collection error, or no outcomes at all) stays itself; a
    node listed next to its own file is not counted twice."""
    out: list[str] = []
    for t in targets:
        nodes = [n for n in outcomes if "::" not in t and n.split("::", 1)[0] == t and "::" in n]
        for n in nodes or [t]:
            if n not in out:
                out.append(n)
    return out


def _head(node: str) -> str:
    """The --tb section headline pytest prints for a node: `Class.test_x[p]`."""
    _, _, rest = node.partition("::")
    return rest.replace("::", ".") if rest else node


def failure_messages(output: str) -> dict[str, str]:
    """{node id or section headline: first exception line}, from `-rA` messages and --tb sections.

    Section keys are headlines (`test_x[p]`, `setup:test_x`, `collect:t.py`); summary keys are
    node ids. The summary message is dropped when the node id is long, so both are read.
    """
    found: dict[str, str] = {}
    head: str | None = None
    for line in output.splitlines():
        m = _SECTION.match(line)
        if m:
            setup, collect, name = m.groups()
            head = f"setup:{name}" if setup else f"collect:{name}" if collect else name
            continue
        e = _E_LINE.match(line)
        if head is not None and e and head not in found:
            found[head] = e.group(1)
    for node, msg in _SUMMARY_MSG.findall(output):
        found[node] = msg.strip()
    return found


def exception_kind(message: str) -> str:
    """`AssertionError` for `assert …` / `AssertionError: …`; `fixture` for a missing fixture;
    otherwise the exception's (last dotted) class name; "" when unknown."""
    msg = message.strip()
    if msg.startswith("assert ") or msg == "assert":
        return "AssertionError"
    if msg.startswith("fixture '") and "not found" in msg:
        return "fixture"
    m = _EXC.match(msg)
    name = m.group(1).rsplit(".", 1)[-1] if m else ""
    return name if name.endswith(_EXC_SUFFIX) else ""


def _base_reason(
    node: str, outcome: str, messages: dict[str, str], outcomes: dict[str, str], issue: str
) -> tuple[str, bool]:
    """(reason, counts_as_red_for_the_issue) for one new test's base outcome."""
    file = node.split("::", 1)[0]
    if outcomes.get(file) == "error" or f"collect:{file}" in messages:
        kind = exception_kind(messages.get(f"collect:{file}", messages.get(file, "")))
        return f"invalid: collection error on base{f' ({kind})' if kind else ''}", False
    if outcome == "error":
        msg = messages.get(node) or messages.get(f"setup:{_head(node)}", "")
        kind = exception_kind(msg)
        if kind == "fixture":
            return "invalid: fixture not found on base", False
        return f"invalid: fixture/setup error on base{f' ({kind})' if kind else ''}", False
    msg = messages.get(node) or messages.get(_head(node), "")
    kind = exception_kind(msg)
    if kind in _ALWAYS_INVALID or (kind in _INVALID_UNLESS_NAMED and kind not in issue):
        return f"invalid: fails on base with {kind}, not on the behaviour", False
    return (f"fails on base ({kind})" if kind else "fails on base"), True


def theater_check(
    base: Box,
    new_tests: dict[str, bytes],
    *,
    node_ids: list[str] | None = None,
    cwd: str = "/app",
    wrap: str = "",
    test_cmd: str = DEFAULT_CMD,
    candidate: Box | None = None,
    issue_text: str = "",
    db_prefix: str = "",
) -> TheaterResult:
    """`new_tests` maps repo-relative test file paths to their candidate content; `node_ids`
    (default: the files) are what runs. `wrap` starts services, as for run_shell. With
    `candidate`, the same tests also run there: a test red with the fix is no repro. An exception
    named in `issue_text` (e.g. "raises TypeError") is the behaviour, not a broken test.
    `db_prefix` (a Fabric replay) creates + migrates a scratch DB at the rev's head first."""
    from warden.verify.gate import apply_db_prefix

    if not new_tests:
        raise ValueError("theater check needs at least one new test file")
    probe = base.copy_in(new_tests, cwd=cwd)
    targets = node_ids or sorted(new_tests)
    run_cmd = apply_db_prefix(f"{test_cmd} {' '.join(shlex.quote(t) for t in targets)}", db_prefix)
    command = keep_tail(run_cmd)
    if wrap:
        command = f"{wrap} {shlex.quote(command)}"
    res = probe.run(command, cwd=cwd, keep=False)
    outcomes = parse_outcomes(res.stdout)
    messages = failure_messages(res.stdout)

    tests = expand_targets(targets, outcomes)
    base_red: dict[str, str] = {}  # node -> reason, for the red-for-the-issue ones
    reasons: dict[str, str] = {}
    any_red = False
    for t in tests:
        if outcomes:
            o = outcomes.get(t) or ("error" if outcomes.get(t.split("::", 1)[0]) == "error" else "")
        else:  # no per-test lines (e.g. the fake backend): the exit code speaks for every target
            o = "failed" if res.exit_code == 1 else ("passed" if res.exit_code == 0 else "")
        if o in _RED:
            any_red = True
            why, counts = _base_reason(t, o, messages, outcomes, issue_text)
            reasons[t] = why
            if counts:
                base_red[t] = why
        elif o == "passed":
            reasons[t] = "guard: passes on base"
        elif o:
            reasons[t] = f"{o} on base"
        else:
            reasons[t] = "no result on base"

    cand_outcomes: dict[str, str] = {}
    red_when_fixed = False
    if candidate is not None and any_red:
        on_cand = candidate.run(command, cwd=cwd, keep=False)
        cand_outcomes = parse_outcomes(on_cand.stdout)
        for t in tests:
            if cand_outcomes:
                c = cand_outcomes.get(t, "")
            else:
                c = {0: "passed", 1: "failed"}.get(on_cand.exit_code, "")
            if c in _RED:
                red_when_fixed = True
                if t in base_red:
                    reasons[t] = f"invalid: {base_red.pop(t)}, and still fails with the fix"
                elif reasons[t].startswith("invalid"):
                    reasons[t] += "; still red with the fix"
                else:
                    reasons[t] = f"invalid: {reasons[t]}, but {c} with the fix applied"
            elif t in base_red and c == "passed":
                reasons[t] = f"repro: {base_red[t]}, passes with the fix"
    for t, why in base_red.items():
        if not reasons[t].startswith("repro"):
            reasons[t] = f"repro: {why}" + (
                "" if candidate is None else "; not confirmed with the fix"
            )

    proven = bool(base_red) and not red_when_fixed
    invalid = not proven and (any_red or red_when_fixed)
    inconclusive = not proven and not invalid and (res.exit_code not in (0, 1) or not outcomes)
    if not outcomes and res.exit_code == 0:
        inconclusive = False  # fake backend: green on base with no per-test lines is theater
    return TheaterResult(
        failed_on_base=proven,
        exit_code=res.exit_code,
        stdout=res.stdout[-4000:],
        base_uuid=base.uuid,
        probe_uuid=probe.uuid,
        outcomes=outcomes,
        inconclusive=inconclusive,
        reasons=reasons,
        candidate_outcomes=cand_outcomes,
        invalid=invalid,
    )
