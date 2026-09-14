from warden.integrate.pr import evidence_table
from warden.schemas import Citation, Plan, RaceResult, Verdict


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


def test_labels_for_is_the_single_label_decision() -> None:
    from warden.integrate.pr import PENDING_LABEL, labels_for

    def plan(risk: list[str]) -> Plan:
        return Plan(
            issue=1,
            summary="s",
            files_to_touch=[],
            tests_to_add=[],
            landing_order=[],
            risk_class=risk,  # type: ignore[arg-type]
            citations=[],
            acceptance=[],
        )

    assert labels_for(plan(["money"])) == [PENDING_LABEL]
    assert labels_for(plan(["tax", "concurrency"])) == [PENDING_LABEL]
    assert labels_for(plan(["concurrency"])) == []
    assert labels_for(plan([])) == []
