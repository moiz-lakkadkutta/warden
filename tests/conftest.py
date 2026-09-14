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
