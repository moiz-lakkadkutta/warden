"""Theater check: the new tests must FAIL on the unfixed base.

Fork `base`, copy in ONLY the new/changed test files, run the new tests. If they all pass, they do
not exercise the change and the candidate cannot be trusted. This is the row judges will
screenshot.

Only a real assertion failure counts. pytest exit 1 with at least one new test FAILED is proof;
a collection error, an ERRORing fixture, "no tests ran" or a usage error is *inconclusive*, never
a pass of the check — otherwise a broken import would look like a test that bites. Regression
guards among the new tests (e.g. "AR still excludes drafts") are expected to pass on base, so
the check needs one failing test, not all of them.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from warden.sandbox.box import Box

DEFAULT_CMD = "pytest -q -p no:cacheprovider -rA --tb=short"
_OUTCOME = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+)", re.MULTILINE)


@dataclass(frozen=True)
class TheaterResult:
    failed_on_base: bool
    exit_code: int
    stdout: str
    base_uuid: str
    probe_uuid: str
    outcomes: dict[str, str] = field(default_factory=dict)  # node id -> passed/failed/error/...
    inconclusive: bool = False

    @property
    def label(self) -> str:
        if self.failed_on_base:
            return "FAIL on unfixed base — as required"
        if self.inconclusive:
            return f"inconclusive on unfixed base (pytest exit {self.exit_code}) — not proof"
        return "PASSED on unfixed base — tests are theater"


def parse_outcomes(output: str) -> dict[str, str]:
    """`-rA` summary lines → {node id: outcome}; the last line for a node wins."""
    return {node: kind.lower() for kind, node in _OUTCOME.findall(output)}


def theater_check(
    base: Box,
    new_tests: dict[str, bytes],
    *,
    node_ids: list[str] | None = None,
    cwd: str = "/app",
    wrap: str = "",
    test_cmd: str = DEFAULT_CMD,
) -> TheaterResult:
    """`new_tests` maps repo-relative test file paths to their candidate content; `node_ids`
    (default: the files) are what runs. `wrap` starts services, as for run_shell."""
    if not new_tests:
        raise ValueError("theater check needs at least one new test file")
    probe = base.copy_in(new_tests, cwd=cwd)
    targets = node_ids or sorted(new_tests)
    command = f"{test_cmd} {' '.join(shlex.quote(t) for t in targets)}"
    if wrap:
        command = f"{wrap} {shlex.quote(command)}"
    res = probe.run(command, cwd=cwd, keep=False)
    outcomes = parse_outcomes(res.stdout)
    if outcomes:
        failed = res.exit_code == 1 and any(outcomes.get(t) == "failed" for t in targets)
    else:  # no per-test lines (e.g. the fake backend): fall back to the exit code alone
        failed = res.exit_code == 1
    inconclusive = not failed and (
        res.exit_code not in (0, 1) or any(o == "error" for o in outcomes.values())
    )
    return TheaterResult(
        failed_on_base=failed,
        exit_code=res.exit_code,
        stdout=res.stdout[-4000:],
        base_uuid=base.uuid,
        probe_uuid=probe.uuid,
        outcomes=outcomes,
        inconclusive=inconclusive,
    )
