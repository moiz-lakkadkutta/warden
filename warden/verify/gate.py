"""Deterministic quality gate on a candidate checkpoint.

Full suite, `mypy .`, `ruff check` + `ruff format --check`, `alembic upgrade head` on an empty DB
plus single-head check, and openapi drift. Every command is a disposable fork of the candidate
(`keep=False`, or a throwaway child for the DB / snapshot chains), so the candidate image is never
modified. Exit codes are evidence, not exceptions: every step runs, every row is filled, and the
result maps 1:1 onto the five deterministic `Verdict` fields.
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any

from warden.runlog.events import EventLog
from warden.sandbox.box import Box, RunResult

_SUMMARY = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")
# pytest's final line: "===== 1 failed, 4 passed in 0.10s =====" or (-q) "5 passed in 0.10s"
_SUMMARY_LINE = re.compile(r"^=+ .* =+$|\bin \d+\.\d+s\b")
_TAIL = 2000


@dataclass(frozen=True)
class GateCommands:
    """Shell strings the gate runs. Fabric's real targets are unknown: override via kwargs."""

    suite: str = "pytest -q"
    mypy: str = "mypy ."
    ruff_check: str = "ruff check ."
    ruff_format: str = "ruff format --check ."
    db_create: str = "createdb warden_gate"
    alembic_upgrade: str = "alembic upgrade head"
    alembic_env: dict[str, str] = field(
        default_factory=lambda: {"DATABASE_URL": "postgresql://localhost/warden_gate"}
    )
    alembic_heads: str = "alembic heads"
    openapi_snapshot: str = "make openapi-snapshot"
    openapi_diff: str = "git diff --exit-code --quiet -- openapi.json"
    # Checks this profile cannot run in the base image; reported as "not verified", never green.
    not_covered: tuple[str, ...] = ()


DEFAULT_COMMANDS = GateCommands()

# Fabric-Inventory, mirroring its CI (`make lint`, the openapi-drift job) inside fabric-base:
# run from the repo root, backend tooling from backend/, DB on 127.0.0.1 as postgres.
# No command may pipe: `x | tail` exits with tail's status and would hide a red step.
_FABRIC_DB = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/warden_gate"
FABRIC_COMMANDS = GateCommands(
    suite="cd backend && pytest -q -p no:cacheprovider",
    mypy="cd backend && mypy .",
    ruff_check="cd backend && ruff check .",
    ruff_format="cd backend && ruff format --check .",
    db_create="psql -h 127.0.0.1 -U postgres -qc 'CREATE DATABASE warden_gate'",
    alembic_upgrade="cd backend && alembic upgrade head",
    alembic_env={"DATABASE_URL": _FABRIC_DB, "MIGRATION_DATABASE_URL": _FABRIC_DB},
    alembic_heads="cd backend && alembic heads",
    openapi_snapshot="cd backend && python ../frontend/scripts/dump-openapi.py --out /tmp/openapi-fresh.json",
    openapi_diff="diff -q frontend/scripts/openapi-snapshot.json /tmp/openapi-fresh.json",
    not_covered=("frontend type codegen drift (pnpm gen:types; no node in fabric-base)",),
)

PROFILES: dict[str, GateCommands] = {"generic": DEFAULT_COMMANDS, "fabric": FABRIC_COMMANDS}


@dataclass(frozen=True)
class GateStep:
    name: str
    shell: str
    exit_code: int
    ms: int
    tail: str  # last 2000 chars of stdout+stderr

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class GateResult:
    suite: dict[str, int]
    mypy_ok: bool
    ruff_ok: bool
    alembic_single_head: bool
    openapi_drift: bool
    steps: tuple[GateStep, ...]
    migration_roundtrip: bool | None = None  # C3 fills it; None = not run
    not_verified: list[str] = field(default_factory=list)

    @property
    def suite_ok(self) -> bool:
        return any(s.name == "suite" and s.ok for s in self.steps)

    @property
    def ok(self) -> bool:
        return (
            self.suite_ok
            and self.mypy_ok
            and self.ruff_ok
            and self.alembic_single_head
            and not self.openapi_drift
        )

    def verdict_fields(self) -> dict[str, Any]:
        """Exactly the five deterministic `Verdict` keys."""
        return {
            "suite": dict(self.suite),
            "mypy_ok": self.mypy_ok,
            "ruff_ok": self.ruff_ok,
            "alembic_single_head": self.alembic_single_head,
            "openapi_drift": self.openapi_drift,
        }


def summary_line(stdout: str) -> str:
    """The last line shaped like a pytest summary; "" when there is none."""
    for line in reversed(stdout.splitlines()):
        if _SUMMARY_LINE.search(line.strip()):
            return line
    return ""


def parse_suite(stdout: str) -> dict[str, int]:
    """pytest summary line -> counts. `{}` when nothing matched (collection crash / timeout).

    Only the summary line is parsed: assertion messages or captured prints elsewhere in stdout
    that happen to contain "N passed" must not inflate the counts.
    """
    counts: dict[str, int] = {}
    for n, key in _SUMMARY.findall(summary_line(stdout)):
        norm = "errors" if key == "error" else key
        counts[norm] = counts.get(norm, 0) + int(n)
    if counts:
        counts.setdefault("passed", 0)
        counts.setdefault("failed", 0)
    return counts


def count_heads(stdout: str) -> int:
    """`(head)` markers first, non-blank lines second (guards multi-line-per-head output)."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    marked = sum(1 for ln in lines if "(head)" in ln)
    return marked or len(lines)


def run_gate(
    candidate: Box,
    log: EventLog,
    *,
    commands: GateCommands = DEFAULT_COMMANDS,
    cwd: str = "/app",
    timeout: int = 900,
    wrap: str = "",
) -> GateResult:
    """Run every gate command on disposable forks of `candidate`; never raises on a red command.
    `wrap` (e.g. `with-services sh -c`) prefixes every command; steps record the plain one."""
    t0 = time.perf_counter()
    steps: list[GateStep] = []

    def step(name: str, box: Box, shell: str, **kw: Any) -> RunResult:
        command = f"{wrap} {shlex.quote(shell)}" if wrap else shell
        res = box.run(command, cwd=cwd, **kw)
        tail = (res.stdout + res.stderr)[-_TAIL:]
        steps.append(GateStep(name, shell, res.exit_code, res.ms, tail))
        return res

    def child(res: RunResult, name: str) -> Box:
        if res.box is None:
            raise RuntimeError(f"{name} produced no checkpoint")
        return res.box

    c = commands
    suite = step("suite", candidate, c.suite, keep=False, timeout=timeout)
    mypy = step("mypy", candidate, c.mypy, keep=False)
    ruff_check = step("ruff_check", candidate, c.ruff_check, keep=False)
    ruff_format = step("ruff_format", candidate, c.ruff_format, keep=False)

    db = child(step("db_create", candidate, c.db_create, keep=True), "db_create")
    env = dict(c.alembic_env)
    upgrade = step("alembic_upgrade", db, c.alembic_upgrade, keep=False, env=env)
    heads = step("alembic_heads", db, c.alembic_heads, keep=False, env=env)

    snapshot = step("openapi_snapshot", candidate, c.openapi_snapshot, keep=True)
    snap = child(snapshot, "openapi_snapshot")
    diff = step("openapi_diff", snap, c.openapi_diff, keep=False)

    result = GateResult(
        suite=parse_suite(suite.stdout),
        mypy_ok=mypy.ok,
        ruff_ok=ruff_check.ok and ruff_format.ok,
        alembic_single_head=upgrade.ok and count_heads(heads.stdout) == 1,
        openapi_drift=not snapshot.ok or not diff.ok,
        steps=tuple(steps),
        not_verified=list(c.not_covered),
    )
    log.emit(
        "verify",
        "note",
        {"gate": {**result.verdict_fields(), "steps": {s.name: s.exit_code for s in steps}}},
        ms=int((time.perf_counter() - t0) * 1000),
    )
    return result
