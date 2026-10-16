"""Typed contracts between pipeline stages.

Every model output is one of these; every one is stored verbatim in runs/<id>/.
Schemas are passed to Token Factory as `response_format.json_schema` AND pasted into prompts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

Axis = Literal["race", "idor", "money", "malformed", "flow"]
Severity = Literal["P0", "P1", "P2", "P3"]
RiskClass = Literal["money", "tax", "schema", "security", "concurrency", "none"]
Decision = Literal["PASS", "PASS_WITH_CONCERNS", "FAIL"]
Phase = Literal["hunt", "plan", "fix", "verify", "pr"]
ToolName = Literal["list_files", "read_file", "grep", "write_file", "replace_in_file", "run_shell"]

HUMAN_GATE: frozenset[str] = frozenset({"money", "tax", "schema", "security"})


class Citation(BaseModel):
    url: str
    claim: str = Field(description="The specific behaviour this URL supports.")


class ResearchQueries(BaseModel):
    """Lightning's pre-plan pass: what to look up in docs, what to grep for in the repo."""

    queries: list[str] = Field(
        max_length=3, description="Documentation search queries about library behaviour."
    )
    code_terms: list[str] = Field(
        max_length=8, description="Identifiers (functions, fields, classes) to grep for."
    )


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
    risk_class: list[RiskClass] = Field(
        description=(
            "Every class the change itself touches; ['none'] when none applies. "
            "money: amounts, prices, balances, currency rounding, payments. "
            "tax: tax rates, tax computation, tax reporting. "
            "schema: a DATABASE or persistent-storage schema — tables, columns, constraints, "
            "indexes, ORM models backed by a database, migrations. Changing a public data or "
            "serialization format (JSON/XML/CSV/YAML output, key or field order, an API "
            "payload shape) is NOT schema. "
            "security: authentication, authorization, access control, secrets, injection, "
            "crypto. concurrency: races, locking, idempotency, transactions under parallel load."
        )
    )
    risk_rationale: str = Field(
        default="",
        description=(
            "One sentence per class in risk_class naming the code that puts the change in that "
            "class (e.g. 'schema: adds a column via an alembic migration'); for none, why none."
        ),
    )
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
    done: bool | None = None  # implementer called finish(done=true); None = not recorded


class FixReport(BaseModel):
    """Payload of the implementer's `finish` pseudo-tool: how the fix loop stopped."""

    model_config = ConfigDict(extra="forbid")

    done: bool = Field(description="true when every acceptance item passes; false when blocked")
    tests_added: list[str] = Field(
        default_factory=list, description="Test files written or extended, relative to /app."
    )
    unmet: list[str] = Field(
        default_factory=list, description="Acceptance items that still fail, quoted verbatim."
    )
    notes: str = Field("", description="One short paragraph on what was changed and why.")


class RaceResult(BaseModel):
    threads: int
    effects_on_base: int | None = None
    effects_on_candidate: int


class Verdict(BaseModel):
    decision: Decision
    theater_failed_on_base: bool
    theater_inconclusive: bool = False  # no tests ran / no per-test outcomes on base: not proof
    # red on base for a reason that is not the issue's (NameError, fixture, collection error), or
    # a new test still red with the fix: the repro does not count (verify/theater.py)
    theater_invalid: bool = False
    theater_tests: dict[str, str] = Field(
        default_factory=dict,
        description="New test node id -> why it does or does not count as a repro.",
    )
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
    # existing base tests green on base, red on the candidate (verify/regression.py)
    regressions: list[str] = Field(default_factory=list)
    # behaviours the issue states, quoted, that the diff does not implement (verify/spec.py)
    spec_unmet: list[str] = Field(default_factory=list)
    # new tests the spec review maps to the issue's main behaviour that all pass on the unfixed
    # base (guards, not repros): a concern, and a reason for one theater retry
    main_guard: list[str] = Field(default_factory=list)
    # FAIL only because a check could not finish (suite timed out): unverified, not rejected
    inconclusive: bool = False
    # lines added + removed in the candidate's diff (None: not measured); a best-of-N tiebreak
    diff_lines: int | None = None
    # best-of-N (WARDEN_CANDIDATES > 1): every candidate, the chosen index and why
    candidates: list[CandidateSummary] = Field(default_factory=list)
    chosen: int | None = None
    chosen_why: str = ""
    # theater retries (WARDEN_THEATER_RETRIES) the implementer took before this verdict; 0 = none
    theater_retry: int = 0

    @model_serializer(mode="wrap")
    def _omit_unset_extras(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """The fields added for best-of-N are left out while unset, so a single-candidate
        verdict.json is byte-identical to one written before they existed."""
        data: dict[str, Any] = handler(self)
        for key, empty in (
            ("diff_lines", None),
            ("candidates", []),
            ("chosen", None),
            ("theater_retry", 0),
            ("main_guard", []),
        ):
            if data.get(key) == empty:
                data.pop(key, None)
        if not data.get("chosen_why"):
            data.pop("chosen_why", None)
        return data


class CandidateSummary(BaseModel):
    """One best-of-N candidate as the verifier saw it (pipeline.rank_key orders them)."""

    index: int = Field(description="1-based candidate number")
    status: Literal["verified", "skipped"] = "verified"
    skipped_because: str = ""
    image_uuid: str = ""
    decision: Decision | None = None
    theater_failed_on_base: bool = False
    inconclusive: bool = False
    regressions: int = 0
    spec_unmet: int = 0
    concerns: int = 0
    diff_lines: int | None = None
    cost_usd: float = 0.0
    tool_turns: int = 0
    tests_added: int = 0
    reasons: list[str] = Field(default_factory=list)


class SpecBehaviour(BaseModel):
    behaviour: str = Field(
        description="One behaviour the issue explicitly asks for, in a sentence."
    )
    quote: str = Field(description="The issue's own words that state it, copied verbatim.")
    met: Literal["yes", "no", "unclear"] = Field(
        description="yes: the diff implements it; no: it does not; unclear: cannot tell."
    )
    evidence: str = Field(description="The diff lines or test names that decide it.")
    tests: list[str] = Field(
        default_factory=list,
        description="The new tests (names from the New tests list) that exercise this behaviour; "
        "empty when none does.",
    )


class SpecArtefact(BaseModel):
    """A concrete thing the issue shows (exception class, YAML kind, command, input) that the
    evidence test must use; a test of a look-alike proves nothing (E1: pre-commit-1997,
    checkov-831)."""

    artefact: str = Field(
        description="The concrete artefact the issue names, e.g. the exception class its "
        "traceback raises as printed (`requests.exceptions.HTTPError`), the `kind` its YAML uses "
        "(`kind: StatefulSet`), the command line it runs, the input values it gives."
    )
    quote: str = Field(description="The issue's own words or lines showing it, copied verbatim.")
    used: Literal["yes", "no", "unclear"] = Field(
        description="yes: a new test exercises this same artefact and the fix handles it; no: "
        "the tests use a different one of the same sort (another exception class, another kind, "
        "another command or input) or none; unclear: cannot tell."
    )
    evidence: str = Field(description="The test lines that use it, or the look-alike they use.")


class SpecReview(BaseModel):
    """The verifier's read of the diff against the issue's stated behaviours."""

    behaviours: list[SpecBehaviour]
    artefacts: list[SpecArtefact] = Field(
        default_factory=list,
        description="The issue's concrete artefacts (exception class, YAML kind, command line, "
        "input values) and whether the new tests use them; empty when the issue names none.",
    )
    summary: str = Field("", description="One sentence: does the diff do what the issue asks?")


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


class ToolArgs(BaseModel):
    """Base for tool-call arguments: unknown keys are a model mistake, not silently dropped."""

    model_config = ConfigDict(extra="forbid")


class ListFilesArgs(ToolArgs):
    path: str = Field(
        ".", description="Directory to list, relative to /app (default: the repo root)."
    )


class ReadFileArgs(ToolArgs):
    path: str = Field(
        min_length=1, description="File to read, relative to the repo root unless absolute."
    )
    start_line: int = Field(
        1, ge=1, description="First line to return (1-based); use it to continue a long file."
    )
    end_line: int | None = Field(
        None, ge=1, description="Last line to return (inclusive); null reads to the page limit."
    )


class GrepArgs(ToolArgs):
    pattern: str = Field(
        min_length=1, description="Extended regex (grep -E): `A|B` matches either."
    )
    path: str = Field(".", description="Directory or file to search, relative to the repo root.")


class WriteFileArgs(ToolArgs):
    path: str = Field(
        min_length=1, description="File to create or replace, relative to the repo root."
    )
    content: str = Field(description="The complete new contents of the file.")
    allow_shrink: bool = Field(
        False,
        description=(
            "true only to delete most of an existing file on purpose; otherwise a write that "
            "would shrink a file of 1 kB or more below half its size is refused."
        ),
    )


class ReplaceInFileArgs(ToolArgs):
    path: str = Field(min_length=1, description="File to edit, relative to the repo root.")
    old: str = Field(
        min_length=1,
        description=(
            "Exact text to replace, copied from read_file with its indentation; it must occur "
            "exactly once (include surrounding lines to make it unique)."
        ),
    )
    new: str = Field(description="Text that replaces `old`.")


class RunShellArgs(ToolArgs):
    command: str = Field(
        description="Shell command to run in /app, e.g. 'pytest -q tests/test_x.py'."
    )
    timeout: int = Field(
        600, ge=1, le=600, description="Seconds before the command is killed (1-600, default 600)."
    )


class RunTestsUnfixedArgs(ToolArgs):
    """`run_tests_unfixed`, offered on a theater retry: the model's current test files on a fork of
    the unfixed base (source as it was before the fix), never on its own checkpoint."""

    tests: list[str] = Field(
        min_length=1,
        description="Test files or pytest node ids (path::test_name), relative to the repo root.",
    )
