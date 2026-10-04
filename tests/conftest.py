"""Shared test helpers."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from warden.schemas import Plan, RiskClass


@pytest.fixture
def make_plan() -> Callable[[list[RiskClass]], Plan]:
    """`make_plan(["money"])` — a minimal Plan with the given risk classes."""

    def _make(risk: list[RiskClass]) -> Plan:
        return Plan(
            issue=1,
            summary="s",
            files_to_touch=[],
            tests_to_add=[],
            landing_order=[],
            risk_class=risk,
            citations=[],
            acceptance=[],
        )

    return _make


@pytest.fixture(autouse=True)
def _offline_live_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never reach GitHub, Token Factory, Tavily or Contree: the live hunt/plan steps are
    replaced by canned ones, so a live run stops (NotReadyError) at `fix` like before."""
    import warden.pipeline as pipeline

    def hunt(ctx: pipeline.StepContext) -> pipeline.IssueRef:
        assert ctx.requested_issue is not None
        return pipeline.IssueRef(issue=ctx.requested_issue, title="offline")

    def plan(_ctx: pipeline.StepContext, ref: pipeline.IssueRef) -> Plan:
        return Plan(
            issue=ref.issue,
            summary="offline",
            files_to_touch=[],
            tests_to_add=[],
            landing_order=[],
            risk_class=["none"],
            citations=[],
            acceptance=[],
        )

    monkeypatch.setattr(pipeline, "_live_hunt", hunt)
    monkeypatch.setattr(pipeline, "_live_plan", plan)
