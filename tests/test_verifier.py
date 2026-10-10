"""decide(): theater + gate + candidate -> Verdict, deterministically and conservatively."""

from __future__ import annotations

from warden.schemas import Candidate
from warden.verify.gate import GateResult, GateStep
from warden.verify.theater import TheaterResult
from warden.verify.verifier import decide


def _cand(**kw: object) -> Candidate:
    base: dict[str, object] = dict(
        issue=196,
        image_uuid="c",
        tool_turns=18,
        tests_added=["t.py::test_a"],
        cost_usd=0.1,
        done=True,
    )
    base.update(kw)
    return Candidate.model_validate(base)


def _theater(failed: bool = True, inconclusive: bool = False) -> TheaterResult:
    return TheaterResult(
        failed_on_base=failed,
        exit_code=1 if failed else 0,
        stdout="",
        base_uuid="b",
        probe_uuid="p",
        outcomes={},
        inconclusive=inconclusive,
    )


def _gate(suite_exit: int = 0, **kw: object) -> GateResult:
    base: dict[str, object] = dict(
        suite={"passed": 1509, "failed": 0},
        mypy_ok=True,
        ruff_ok=True,
        alembic_single_head=True,
        openapi_drift=False,
        steps=(GateStep("suite", "pytest -q", suite_exit, 1, ""),),
        not_verified=["fe codegen"],
    )
    base.update(kw)
    return GateResult(**base)  # type: ignore[arg-type]


def _green(**kw: object) -> GateResult:
    return _gate(**kw)


def test_all_green_is_pass_and_not_verified_is_reported_not_blocking() -> None:
    v = decide(_cand(), _theater(), _green())
    assert v.decision == "PASS"
    assert v.theater_failed_on_base and not v.theater_inconclusive
    assert v.suite == {"passed": 1509, "failed": 0}
    assert v.not_verified == ["fe codegen"]
    assert v.reasons


def test_red_ruff_is_fail_with_a_reason() -> None:  # Fabric#220
    v = decide(_cand(), _theater(), _green(ruff_ok=False))
    assert v.decision == "FAIL" and not v.ruff_ok
    assert any("ruff" in r for r in v.reasons)


def test_theater_is_fail() -> None:
    v = decide(_cand(), _theater(failed=False), _green())
    assert v.decision == "FAIL" and any("theater" in r for r in v.reasons)


def test_inconclusive_theater_is_pass_with_concerns() -> None:
    v = decide(_cand(), _theater(failed=False, inconclusive=True), _green())
    assert v.decision == "PASS_WITH_CONCERNS" and v.theater_inconclusive
    assert any("inconclusive" in c for c in v.concerns)


def test_failing_suite_or_blocked_candidate_or_no_tests_is_fail() -> None:
    assert (
        decide(_cand(), _theater(), _gate(1, suite={"passed": 1, "failed": 2})).decision == "FAIL"
    )
    assert decide(_cand(done=False), _theater(), _green()).decision == "FAIL"
    assert decide(_cand(tests_added=[]), None, _green()).decision == "FAIL"


def test_no_gate_is_fail_never_pass() -> None:
    v = decide(_cand(), _theater(), None)
    assert v.decision == "FAIL" and any("gate" in r for r in v.reasons)
