"""Typed contracts between pipeline stages.

Every model output is one of these; every one is stored verbatim in runs/<id>/.
Schemas are passed to Token Factory as `response_format.json_schema` AND pasted into prompts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Axis = Literal["race", "idor", "money", "malformed", "flow"]
Severity = Literal["P0", "P1", "P2", "P3"]
RiskClass = Literal["money", "tax", "schema", "security", "concurrency", "none"]
Decision = Literal["PASS", "PASS_WITH_CONCERNS", "FAIL"]
Phase = Literal["hunt", "plan", "fix", "verify", "pr"]

HUMAN_GATE: frozenset[str] = frozenset({"money", "tax", "schema", "security"})


class Citation(BaseModel):
    url: str
    claim: str = Field(description="The specific behaviour this URL supports.")


class Finding(BaseModel):
    axis: Axis
    severity: Severity
    title: str
    repro: str = Field(description="Copy-paste shell commands that reproduce the bug.")
    expected: str
    actual: str
    request_id: str | None = None
    confirmed: bool = False
    reproduced_times: int = 0
    root_cause_key: str = Field(description="Stable key so findings with one cause group together.")


class ProbePlan(BaseModel):
    flow: str
    endpoints: list[str]
    probes: list[dict[str, Any]] = Field(
        description="Ordered probes: {axis, endpoint, method, payload_hint}."
    )


class Plan(BaseModel):
    issue: int
    summary: str
    files_to_touch: list[str]
    tests_to_add: list[str]
    landing_order: list[str]
    risk_class: list[RiskClass]
    citations: list[Citation]
    acceptance: list[str] = Field(description="Human-readable checks the verifier will run.")

    @property
    def needs_human(self) -> bool:
        return any(r in HUMAN_GATE for r in self.risk_class)


class Candidate(BaseModel):
    issue: int
    image_uuid: str
    tool_turns: int
    tests_added: list[str]
    cost_usd: float


class RaceResult(BaseModel):
    threads: int
    effects_on_base: int | None = None
    effects_on_candidate: int


class Verdict(BaseModel):
    decision: Decision
    theater_failed_on_base: bool
    suite: dict[str, int] = Field(
        default_factory=dict, description="e.g. {'passed': 214, 'failed': 0}"
    )
    mypy_ok: bool
    ruff_ok: bool
    alembic_single_head: bool
    openapi_drift: bool
    race: RaceResult | None = None
    not_verified: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class Wave(BaseModel):
    number: int
    issues: list[int]
    integration_image_uuid: str | None = None
    merged: list[int] = Field(default_factory=list)


class RunEvent(BaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase: Phase
    kind: Literal["llm", "sandbox", "tool", "note", "cost_cap", "error"]
    data: dict[str, Any] = Field(default_factory=dict)
    ms: int | None = None
    usd: float | None = None
