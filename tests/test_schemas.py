from warden.schemas import Citation, Plan, Verdict


def test_plan_needs_human_for_money() -> None:
    p = Plan(
        issue=1,
        summary="s",
        files_to_touch=[],
        tests_to_add=[],
        landing_order=[],
        risk_class=["money"],
        citations=[Citation(url="https://x", claim="c")],
        acceptance=[],
    )
    assert p.needs_human


def test_plan_no_human_for_none() -> None:
    p = Plan(
        issue=1,
        summary="s",
        files_to_touch=[],
        tests_to_add=[],
        landing_order=[],
        risk_class=["none"],
        citations=[],
        acceptance=[],
    )
    assert not p.needs_human


def test_schemas_round_trip() -> None:
    v = Verdict(
        decision="PASS",
        theater_failed_on_base=True,
        mypy_ok=True,
        ruff_ok=True,
        alembic_single_head=True,
        openapi_drift=False,
    )
    assert Verdict.model_validate_json(v.model_dump_json()) == v
    assert "decision" in Verdict.model_json_schema()["properties"]
