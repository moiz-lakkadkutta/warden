"""theater + gate + candidate → Verdict, deterministically.

FAIL when the fix is not shown to work or not shown to be tested: the implementer did not finish,
added no tests, the new tests pass on the unfixed base (theater), the suite fails, or any gate
check is red (a red lint is exactly what the target's CI would reject). PASS_WITH_CONCERNS when the
only doubt is an inconclusive theater run. Checks the image cannot run are listed in
`not_verified` and never block. An LLM verdict (Role.VERDICT) may add concerns later; it never
upgrades a decision.
"""

from __future__ import annotations

from warden.schemas import Candidate, Verdict
from warden.verify.gate import GateResult
from warden.verify.theater import TheaterResult


def decide(cand: Candidate, theater: TheaterResult | None, gate: GateResult | None) -> Verdict:
    fail: list[str] = []
    concerns: list[str] = []
    passed: list[str] = []

    if cand.done is False:
        fail.append("implementer did not finish (budget or blocked)")
    if not cand.tests_added:
        fail.append("no new tests were added")

    failed_on_base = bool(theater and theater.failed_on_base)
    inconclusive = bool(theater and theater.inconclusive)
    if theater is not None:
        if failed_on_base:
            passed.append("a new test fails on the unfixed base (theater check)")
        elif inconclusive:
            concerns.append(f"theater check inconclusive (pytest exit {theater.exit_code})")
        else:
            fail.append("theater: every new test passes on the unfixed base")

    if gate is None:
        fail.append("gate did not run")
    else:
        if gate.suite_ok and gate.suite.get("failed", 0) == 0 and gate.suite.get("passed", 0) > 0:
            passed.append(f"full suite green ({gate.suite.get('passed')} passed)")
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

    decision = "FAIL" if fail else ("PASS_WITH_CONCERNS" if concerns else "PASS")
    return Verdict(
        decision=decision,  # type: ignore[arg-type]
        theater_failed_on_base=failed_on_base,
        theater_inconclusive=inconclusive,
        suite=dict(gate.suite) if gate else {},
        mypy_ok=bool(gate and gate.mypy_ok),
        ruff_ok=bool(gate and gate.ruff_ok),
        alembic_single_head=bool(gate and gate.alembic_single_head),
        openapi_drift=bool(gate is None or gate.openapi_drift),
        not_verified=list(gate.not_verified) if gate else ["gate"],
        concerns=concerns,
        reasons=fail + passed if fail else passed,
    )
