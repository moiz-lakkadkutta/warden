from collections.abc import Callable

from warden.integrate.pr import PENDING_LABEL, evidence_table, labels_for
from warden.schemas import Citation, Plan, RaceResult, RiskClass, Verdict


def test_evidence_table_has_required_rows() -> None:
    plan = Plan(
        issue=7,
        summary="s",
        files_to_touch=["a.py"],
        tests_to_add=["t.py"],
        landing_order=[],
        risk_class=["money"],
        citations=[Citation(url="https://d", claim="c")],
        acceptance=[],
    )
    v = Verdict(
        decision="PASS",
        theater_failed_on_base=True,
        suite={"passed": 3},
        mypy_ok=True,
        ruff_ok=True,
        alembic_single_head=True,
        openapi_drift=False,
        race=RaceResult(threads=8, effects_on_base=3, effects_on_candidate=1),
        not_verified=["frontend"],
    )
    body = evidence_table(7, "t", plan, v, cost_usd=1.5, calls_by_model={"x/ultra": 2})
    for needle in (
        "New tests on unfixed base",
        "as required",
        "Not verified",
        "frontend",
        "PENDING-HUMAN",
        "$1.50",
        "https://d",
    ):
        assert needle in body


def test_labels_for_is_the_single_label_decision(
    make_plan: Callable[[list[RiskClass]], Plan],
) -> None:
    assert labels_for(make_plan(["money"])) == [PENDING_LABEL]
    assert labels_for(make_plan(["tax", "concurrency"])) == [PENDING_LABEL]
    assert labels_for(make_plan(["concurrency"])) == []
    assert labels_for(make_plan([])) == []


def test_evidence_table_says_inconclusive_and_shows_a_note() -> None:
    from warden.schemas import Plan, Verdict

    plan = Plan(
        issue=1,
        summary="s",
        files_to_touch=[],
        tests_to_add=[],
        landing_order=[],
        risk_class=["none"],
        citations=[],
        acceptance=[],
    )
    v = Verdict(
        decision="PASS_WITH_CONCERNS",
        theater_failed_on_base=False,
        theater_inconclusive=True,
        suite={"passed": 3, "failed": 0},
        mypy_ok=True,
        ruff_ok=True,
        alembic_single_head=True,
        openapi_drift=False,
    )
    body = evidence_table(1, "t", plan, v, cost_usd=0.1, calls_by_model={}, note="Replay of #1.")
    assert (
        "inconclusive" in body
        and "theater" not in body.split("New tests on unfixed base")[1].split("\n")[0]
    )
    assert "> Replay of #1." in body


def _bare_plan() -> Plan:
    return Plan(
        issue=1,
        summary="s",
        files_to_touch=[],
        tests_to_add=[],
        landing_order=[],
        risk_class=["none"],
        citations=[],
        acceptance=[],
    )


def _bare_verdict(**kw: object) -> Verdict:
    base: dict[str, object] = dict(
        decision="PASS",
        theater_failed_on_base=True,
        mypy_ok=True,
        ruff_ok=True,
        alembic_single_head=True,
        openapi_drift=False,
    )
    base.update(kw)
    return Verdict.model_validate(base)


def test_regression_and_spec_rows_appear_only_when_those_checks_ran() -> None:
    body = evidence_table(1, "t", _bare_plan(), _bare_verdict(), cost_usd=0, calls_by_model={})
    assert "Existing tests" not in body and "Issue's stated behaviours" not in body
    v = _bare_verdict(
        reasons=[
            "existing tests: 12 base tests in 2 files stay green",
            "spec: 2 of 2 stated behaviours implemented",
        ]
    )
    body = evidence_table(1, "t", _bare_plan(), v, cost_usd=0, calls_by_model={})
    assert "| Existing tests on candidate | 12 base tests in 2 files stay green |" in body
    assert "| Issue's stated behaviours | 2 of 2 stated behaviours implemented |" in body


def test_failed_regression_and_spec_rows_name_what_broke() -> None:
    v = _bare_verdict(
        decision="FAIL",
        regressions=["tests/test_x.py::a"],
        spec_unmet=["keeps num_steps"],
        reasons=[
            "regression: 1 test green on base is red on the candidate (tests/test_x.py::a)",
            "spec: 1 of 2 stated behaviours not implemented (keeps num_steps)",
        ],
    )
    body = evidence_table(1, "t", _bare_plan(), v, cost_usd=0, calls_by_model={})
    assert "| Existing tests on candidate | 1 test green on base is red" in body
    assert "| Issue's stated behaviours | 1 of 2 stated behaviours not implemented" in body


def test_evidence_table_shows_an_invalid_repro_and_each_new_tests_reason() -> None:
    plan = Plan(
        issue=1928,
        summary="s",
        files_to_touch=["a.py"],
        tests_to_add=["t.py"],
        landing_order=[],
        risk_class=["none"],
        citations=[],
        acceptance=[],
    )
    v = Verdict(
        decision="FAIL",
        theater_failed_on_base=False,
        theater_invalid=True,
        theater_tests={
            "src/x/tests/test_recur.py::test_xcal": "invalid: fails on base (AssertionError), "
            "and still fails with the fix",
            "src/x/tests/test_jcal.py::test_jcal": "repro: fails on base (AssertionError), "
            "passes with the fix",
        },
        mypy_ok=True,
        ruff_ok=True,
        alembic_single_head=True,
        openapi_drift=False,
    )
    body = evidence_table(1928, "t", plan, v, cost_usd=0.2, calls_by_model={})
    row = next(line for line in body.splitlines() if line.startswith("| New tests on unfixed"))
    assert "invalid repro" in row and "tests are theater" not in row
    assert "`test_recur.py::test_xcal`: invalid: fails on base (AssertionError), and still" in row
    assert "<br>`test_jcal.py::test_jcal`: repro: fails on base" in row
