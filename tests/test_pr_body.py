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
