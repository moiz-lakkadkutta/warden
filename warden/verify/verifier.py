"""theater + gate + candidate → Verdict, deterministically.

FAIL when the fix is not shown to work or not shown to be tested: the implementer did not finish,
added no tests, the new tests pass on the unfixed base (theater) or fail there only for the wrong
reason / still fail with the fix (invalid repro), the suite fails, or any gate
check is red (a red lint is exactly what the target's CI would reject). PASS_WITH_CONCERNS when the
only doubt is an inconclusive theater run. Checks the image cannot run are listed in
`not_verified` and never block. The regression check (existing tests green on base, red on the
candidate) and the spec review (a behaviour the issue states, quoted, that the diff does not
implement) add FAIL reasons; their inconclusive or unclear results are concerns. A regression
check with no related test to run is `not_verified` *and* a concern (PASS → PASS_WITH_CONCERNS),
because unlike an unconfigured lint it is a gap in this candidate's evidence. The spec review
is the one LLM input and it never upgrades a decision. A suite that times out is still FAIL (no
run showed the new tests passing on the candidate) but marked `inconclusive` when it is the only
fail reason: the candidate was not verified, which is different from being rejected.
"""

from __future__ import annotations

from warden.schemas import Candidate, Verdict
from warden.verify.gate import GateResult
from warden.verify.regression import RegressionResult
from warden.verify.spec import SpecResult
from warden.verify.theater import TheaterResult

THEATER_PASSED = "a new test fails on the unfixed base (theater check)"
MAIN_GUARD = "the issue's main behaviour is tested only by a guard (passes on the unfixed base)"


def _leaf(name: str) -> str:
    """`a/t.py::C::test_x[p]` → `test_x`; a file target stays itself."""
    return name.rsplit("::", 1)[-1].split("[", 1)[0] if "::" in name else name


def _maps(test: str, node: str) -> bool:
    """Does the spec review's test name (`t.py::test_x`, `test_x`, a file) name theater node?"""
    test = test.strip()
    if not test:
        return False
    if node == test or node.startswith((f"{test}::", f"{test}[")) or node.endswith(f"/{test}"):
        return True
    if "::" not in test and test.endswith(".py"):  # a file: every node under it
        return node.split("::", 1)[0].endswith(test)
    return "::" in node and _leaf(node) == _leaf(test.rsplit("/", 1)[-1])


def main_guard(main_tests: list[str], theater_tests: dict[str, str]) -> list[str]:
    """The theater nodes mapped to the issue's main behaviour, when every one of them is a guard
    (passes on the unfixed base); [] when one reproduces, or nothing maps (no claim either way).
    E1 checkov-831: a `kind: Pod` test reproduced, while the test of the issue's own scenario
    passed on base, and the PASS was false."""
    nodes = [n for n in theater_tests if any(_maps(t, n) for t in main_tests)]
    if not nodes or not all(theater_tests[n].startswith("guard") for n in nodes):
        return []
    return nodes


def decide(
    cand: Candidate,
    theater: TheaterResult | None,
    gate: GateResult | None,
    *,
    regression: RegressionResult | None = None,
    spec: SpecResult | None = None,
) -> Verdict:
    fail: list[str] = []
    unverified: list[str] = []  # fail reasons that are "could not check", not "checked and red"
    concerns: list[str] = []
    passed: list[str] = []

    if cand.done is False:
        fail.append("implementer did not finish (budget or blocked)")
    if not cand.tests_added:
        fail.append("no new tests were added")

    failed_on_base = bool(theater and theater.failed_on_base)
    inconclusive = bool(theater and theater.inconclusive)
    invalid = bool(theater and theater.invalid)
    if theater is not None:
        if failed_on_base:
            passed.append(THEATER_PASSED)
        elif invalid:
            fail.append(f"theater: invalid repro — {theater.why}")
        elif inconclusive:
            concerns.append(f"theater check inconclusive (pytest exit {theater.exit_code})")
        else:
            fail.append("theater: every new test passes on the unfixed base")

    if gate is None:
        fail.append("gate did not run")
    else:
        if gate.suite_ok and gate.suite.get("failed", 0) == 0 and gate.suite.get("passed", 0) > 0:
            passed.append(f"full suite green ({gate.suite.get('passed')} passed)")
        elif gate.suite_timed_out:
            unverified.append("full suite timed out (no summary): inconclusive, not proof")
        else:
            fail.append(f"full suite not green ({gate.suite or 'no summary'})")
        for check, ok, name in (
            ("mypy", gate.mypy_ok, "mypy"),
            ("ruff", gate.ruff_ok, "ruff check / format"),
            ("alembic", gate.alembic_single_head, "alembic upgrade + single head"),
            ("openapi", not gate.openapi_drift, "OpenAPI snapshot drift"),
        ):
            if gate.ran(check):  # a check the profile does not run is in not_verified instead
                (passed if ok else fail).append(f"{name} {'clean' if ok else 'red'}")

    not_verified = list(gate.not_verified) if gate else ["gate"]
    if regression is not None:
        if not regression.ran:
            # Not a repo-level gap like an unconfigured lint: this candidate's change went
            # unchecked against the repo's own tests, the main false-PASS guard. Say so.
            not_verified.append(f"existing tests ({regression.skipped})")
            concerns.append(f"existing tests {regression.label}")
        elif regression.broken:
            fail.append(f"regression: {regression.label} ({', '.join(regression.broken[:5])})")
        elif regression.inconclusive:
            concerns.append(f"regression check {regression.label}")
        else:
            passed.append(f"existing tests: {regression.label}")
        if regression.changed_expectations:
            concerns.append(
                "edited tests changed expectations: "
                + ", ".join(regression.changed_expectations[:5])
            )

    if spec is not None:
        if spec.unmet:
            fail.append(f"spec: {spec.label} ({'; '.join(spec.unmet[:5])})")
        else:
            passed.append(f"spec: {spec.label}")
        if spec.unclear:
            concerns.append(f"spec unclear: {'; '.join(spec.unclear[:5])}")
    guards = main_guard(spec.main_tests, theater.reasons) if spec and theater else []
    if guards:
        concerns.append(f"{MAIN_GUARD}: {', '.join(guards[:4])}")

    fail_all = fail + unverified
    decision = "FAIL" if fail_all else ("PASS_WITH_CONCERNS" if concerns else "PASS")
    return Verdict(
        decision=decision,  # type: ignore[arg-type]
        theater_failed_on_base=failed_on_base,
        theater_inconclusive=inconclusive,
        theater_invalid=invalid,
        theater_tests=dict(theater.reasons) if theater else {},
        suite=dict(gate.suite) if gate else {},
        mypy_ok=bool(gate and gate.mypy_ok),
        ruff_ok=bool(gate and gate.ruff_ok),
        alembic_single_head=bool(gate and gate.alembic_single_head),
        openapi_drift=bool(gate is None or gate.openapi_drift),
        not_verified=not_verified,
        concerns=concerns,
        reasons=fail_all + passed if fail_all else passed,
        regressions=list(regression.broken) if regression else [],
        spec_unmet=list(spec.unmet) if spec else [],
        main_guard=guards,
        inconclusive=bool(unverified) and not fail,
    )


def theater_retryable(cand: Candidate, v: Verdict) -> bool:
    """A FAIL whose only cause is the theater check: no new test reproduces the issue on the
    unfixed base (all pass there, or red only for the wrong reason / still red with the fix),
    while the implementer finished and everything else is green. Only then can the fix be kept
    and the tests rewritten. A red suite is allowed when the new tests that stay red with the
    fix account for every failure (they are the invalid repro, not a broken fix).

    Also a non-FAIL verdict whose main-behaviour tests are all guards (`main_guard`): the fix
    may be right, but nothing shows it on the issue's own scenario, so the implementer gets the
    same one retry to write that test. If the fix does not handle the scenario, the new test
    stays red with the fix (invalid repro → FAIL) instead of hiding behind a look-alike repro."""
    if cand.done is False or not cand.tests_added:
        return False
    if v.decision != "FAIL":
        return bool(v.main_guard)
    if v.theater_failed_on_base or v.theater_inconclusive or v.inconclusive or not v.theater_tests:
        return False
    red_new = sum(
        1 for why in v.theater_tests.values() if why.startswith("invalid") and "with the fix" in why
    )
    red = v.suite.get("failed", 0) + v.suite.get("errors", 0)
    suite_ok = v.suite.get("passed", 0) > 0 and red <= red_new
    gate_ok = v.mypy_ok and v.ruff_ok and v.alembic_single_head and not v.openapi_drift
    return suite_ok and gate_ok and not v.regressions and not v.spec_unmet


def with_theater_retry(v: Verdict, retries: int) -> Verdict:
    """The last attempt's verdict, saying how many theater retries came before it."""
    if retries <= 0:
        return v
    word = "retry" if retries == 1 else "retries"

    def note(reason: str) -> str:
        if reason == THEATER_PASSED:
            return f"{reason} — repro found on retry {retries}"
        if reason.startswith("theater:"):
            return f"{reason} (still, after {retries} theater {word})"
        return reason

    return v.model_copy(update={"theater_retry": retries, "reasons": [note(r) for r in v.reasons]})
