"""decide(): theater + gate + candidate -> Verdict, deterministically and conservatively."""

from __future__ import annotations

from warden.schemas import Candidate, SpecReview
from warden.verify.gate import GateResult, GateStep
from warden.verify.regression import RegressionResult
from warden.verify.spec import SpecResult
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


# --------------------------------------------------------------- regression + spec (D3 false PASS)


def _reg(
    broken: list[str] | None = None, changed: list[str] | None = None, inconclusive: bool = False
) -> RegressionResult:
    return RegressionResult(
        files=("tests/test_x.py",),
        base={"tests/test_x.py::a": "passed"},
        candidate={},
        broken=broken or [],
        changed_expectations=changed or [],
        inconclusive=inconclusive,
    )


def _spec(unmet: list[str] | None = None, unclear: list[str] | None = None) -> SpecResult:
    return SpecResult(SpecReview(behaviours=[], summary=""), unmet or [], unclear or [])


def test_a_broken_base_test_fails_the_candidate() -> None:
    v = decide(_cand(), _theater(), _green(), regression=_reg(broken=["tests/test_x.py::a"]))
    assert v.decision == "FAIL"
    assert v.regressions == ["tests/test_x.py::a"]
    assert any("regression" in r for r in v.reasons)


def test_changed_expectations_and_inconclusive_regression_are_concerns() -> None:
    v = decide(_cand(), _theater(), _green(), regression=_reg(changed=["tests/test_x.py::a"]))
    assert v.decision == "PASS_WITH_CONCERNS" and v.regressions == []
    v = decide(_cand(), _theater(), _green(), regression=_reg(inconclusive=True))
    assert v.decision == "PASS_WITH_CONCERNS"


def test_clean_regression_is_a_pass_reason() -> None:
    v = decide(_cand(), _theater(), _green(), regression=_reg())
    assert v.decision == "PASS" and any("stay green" in r for r in v.reasons)


def test_an_unmet_stated_behaviour_fails_and_unclear_is_a_concern() -> None:
    v = decide(_cand(), _theater(), _green(), spec=_spec(unmet=["keeps num_steps"]))
    assert v.decision == "FAIL" and v.spec_unmet == ["keeps num_steps"]
    v = decide(_cand(), _theater(), _green(), spec=_spec(unclear=["keeps num_steps"]))
    assert v.decision == "PASS_WITH_CONCERNS" and v.spec_unmet == []


def test_without_regression_or_spec_the_old_decision_stands() -> None:
    v = decide(_cand(), _theater(), _green())
    assert v.decision == "PASS"
    assert "fe codegen" in v.not_verified


def _timed_out(code: int = 124) -> GateResult:
    return _gate(code, suite={})


def test_a_suite_with_no_summary_after_a_timeout_or_kill_is_timed_out() -> None:
    assert _timed_out(124).suite_timed_out
    assert _timed_out(-1).suite_timed_out  # Contree kills at its own deadline with exit -1
    assert not _gate(1, suite={}).suite_timed_out  # a crash, not a hang
    assert not _gate(124, suite={"passed": 3, "failed": 1}).suite_timed_out


def test_a_timed_out_suite_is_an_inconclusive_fail_not_a_red_suite() -> None:
    v = decide(_cand(), _theater(inconclusive=True, failed=False), _timed_out(-1))
    # never a pass: nothing showed the new tests passing on the candidate
    assert v.decision == "FAIL" and v.inconclusive
    assert any("timed out" in r and "inconclusive" in r for r in v.reasons)
    assert not any("not green" in r for r in v.reasons)


def test_a_timeout_beside_a_real_failure_is_a_rejection() -> None:
    v = decide(_cand(), _theater(failed=False), _timed_out())  # theater: tests pass on base
    assert v.decision == "FAIL" and not v.inconclusive


def test_a_red_suite_is_not_inconclusive() -> None:
    v = decide(_cand(), _theater(), _gate(1, suite={"passed": 1, "failed": 2}))
    assert v.decision == "FAIL" and not v.inconclusive


def test_invalid_repro_is_fail_and_carries_the_per_test_reasons() -> None:
    """Run 20261014-123340-85cd: red on base, but red with the fix too — not a repro."""
    reasons = {
        "t.py::test_a": "invalid: fails on base (AssertionError), and still fails with the fix",
        "t.py::test_b": "guard: passes on base",
    }
    theater = TheaterResult(
        failed_on_base=False,
        exit_code=1,
        stdout="",
        base_uuid="b",
        probe_uuid="p",
        reasons=reasons,
        invalid=True,
    )
    v = decide(_cand(), theater, _green())
    assert v.decision == "FAIL" and v.theater_invalid and not v.theater_failed_on_base
    assert not v.inconclusive  # checked and rejected, not "could not check"
    assert v.theater_tests == reasons
    assert any(r.startswith("theater: invalid repro — t.py::test_a: invalid") for r in v.reasons)
