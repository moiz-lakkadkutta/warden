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
from dataclasses import dataclass, field, replace
from typing import Any

from warden.runlog.events import EventLog
from warden.sandbox.box import TIMEOUT_EXIT, Box, RunResult
from warden.verify.tail import keep_tail

_SUMMARY = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")
# pytest's final line: "===== 1 failed, 4 passed in 0.10s =====", (-q) "5 passed in 0.10s", or
# "21314 passed, 42 skipped, 597 xfailed in 69s" (no fraction)
_SUMMARY_LINE = re.compile(r"^=+ .* =+$|\bin \d+(?:\.\d+)?s\b")
_TAIL = 2000
# The suite's tail is the pytest short summary (`FAILED node - Error: msg` lines + the counts),
# what a verify retry feeds back; stderr (a replay's alembic log) goes first so it never pushes
# the summary out.
_SUITE_TAIL = 4000


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
    # Shown to the implementer to run before `finish`; "" = none.
    lint: str = ""
    # Run by Warden on the candidate after the implementer finishes (formatter + safe fixes,
    # like a pre-commit hook); its checkpoint becomes the candidate. "" = none.
    autofix: str = ""


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
    lint="cd backend && ruff check . && ruff format --check .",
    autofix="cd backend && ruff format . && ruff check --fix .",
)

# SWE-rebench / any Python repo without known lint or DB tooling: the suite command is filled per
# task (the repo's own test command on the files the candidate touched); "" = not run.
SWE_COMMANDS = GateCommands(
    suite="",
    mypy="",
    ruff_check="",
    ruff_format="",
    db_create="",
    alembic_upgrade="",
    alembic_env={},
    alembic_heads="",
    openapi_snapshot="",
    openapi_diff="",
)

PROFILES: dict[str, GateCommands] = {
    "generic": DEFAULT_COMMANDS,
    "fabric": FABRIC_COMMANDS,
    "swe": SWE_COMMANDS,
    # Any repo (the GitHub Action's default): only the repo's own test command, WARDEN_TEST_CMD.
    "tests": SWE_COMMANDS,
}


# --- replay gate DB -------------------------------------------------------------------------
# On a `--at <rev>` replay the base image's `test`/`fabric_demo` DB is migrated to the image's
# (newer) head, so an old test can trip a constraint that postdates the rev (e.g. the audit-log
# immutability trigger → `test_rls_blocks_cross_org_item_reads` fails with "audit_log is
# immutable"). The suite, theater and regression runs therefore run against a fresh scratch DB
# migrated to the *rev's* alembic head, mirroring `hunt --at`. Fabric-only; superuser `postgres`
# (local trust) both migrates and connects — the one DB-bound test that needs RLS enforced
# (`test_rls_blocks_cross_org_item_reads`) creates its own non-bypassrls role internally.
REPLAY_GATE_DB = "warden_replay_gate"
_REPLAY_URL = f"postgresql+asyncpg://postgres@127.0.0.1:5432/{REPLAY_GATE_DB}"
_REPLAY_ADMIN = "postgresql://postgres@127.0.0.1:5432/postgres"


def replay_db_prefix(backend_dir: str = "backend") -> str:
    """A POSIX-sh snippet: drop+create the scratch DB, `alembic upgrade head` to the checked-out
    commit's schema, and export DATABASE_URL/MIGRATION_DATABASE_URL at it. Prepended (via
    :func:`apply_db_prefix`) to the DB-bound commands. Runs with cwd at the repo root; the commit
    must already be checked out (``run --at`` / ``_base_box`` do that)."""
    env = f'DATABASE_URL="{_REPLAY_URL}" MIGRATION_DATABASE_URL="{_REPLAY_URL}"'
    return (
        f'psql "{_REPLAY_ADMIN}" -v ON_ERROR_STOP=1 '
        f'-c "DROP DATABASE IF EXISTS {REPLAY_GATE_DB}" -c "CREATE DATABASE {REPLAY_GATE_DB}" '
        f"&& ( cd {backend_dir} && {env} alembic upgrade head ) "
        f"&& export {env}"
    )


def apply_db_prefix(command: str, db_prefix: str) -> str:
    """Prepend ``db_prefix`` to ``command`` (both run in one shell, so the export carries over)."""
    return f"{db_prefix} && {command}" if db_prefix and command else command


def with_test_cmd(commands: GateCommands, test_cmd: str) -> GateCommands:
    """`commands` with the suite step replaced by the repo's own test command (when set)."""
    return replace(commands, suite=test_cmd) if test_cmd.strip() else commands


# Check name -> the steps that make it up; a check whose steps are all "" is not run.
CHECKS: dict[str, tuple[str, ...]] = {
    "mypy": ("mypy",),
    "ruff": ("ruff_check", "ruff_format"),
    "alembic": ("db_create", "alembic_upgrade", "alembic_heads"),
    "openapi": ("openapi_snapshot", "openapi_diff"),
}


@dataclass(frozen=True)
class GateStep:
    name: str
    shell: str
    exit_code: int
    ms: int
    tail: str  # last 2000 chars of stdout+stderr (suite: 4000 of stderr+stdout)
    skipped: bool = False  # command was "" for this profile: not run, never evidence

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
    skipped: tuple[str, ...] = ()  # CHECKS keys this profile does not run

    def ran(self, check: str) -> bool:
        return check not in self.skipped

    @property
    def suite_ok(self) -> bool:
        return any(s.name == "suite" and s.ok for s in self.steps)

    @property
    def suite_timed_out(self) -> bool:
        """No summary and the run hit a deadline: Box's 124, or Contree's -1 when it kills the
        command at its own deadline. A hang shows nothing either way (e.g. a test needs a broker)."""
        return not self.suite and any(
            s.name == "suite" and (s.exit_code == TIMEOUT_EXIT or s.exit_code < 0)
            for s in self.steps
        )

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
    """The last line shaped like a pytest summary; "" when there is none. Searched from the end:
    the summary is pytest's last line, and the suite runs under `keep_tail` so it survives."""
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
    db_prefix: str = "",
) -> GateResult:
    """Run every gate command on disposable forks of `candidate`; never raises on a red command.
    `wrap` (e.g. `with-services sh -c`) prefixes every command; steps record the plain one.
    `db_prefix` (a Fabric replay) prepends a scratch-DB create+migrate to the DB-bound suite."""
    t0 = time.perf_counter()
    steps: list[GateStep] = []

    def step(name: str, box: Box, shell: str, *, keep_end: bool = False, **kw: Any) -> RunResult:
        """`keep_end`: keep the end of the output (the pytest summary) past the sandbox's cap."""
        if not shell:
            steps.append(GateStep(name, "", 0, 0, "", skipped=True))
            return RunResult("", "", 0, box, 0)
        run = keep_tail(shell) if keep_end else shell
        command = f"{wrap} {shlex.quote(run)}" if wrap else run
        res = box.run(command, cwd=cwd, **kw)
        out = res.stderr + res.stdout if keep_end else res.stdout + res.stderr
        tail = out[-(_SUITE_TAIL if keep_end else _TAIL) :]
        steps.append(GateStep(name, shell, res.exit_code, res.ms, tail))
        return res

    def child(res: RunResult, name: str) -> Box:
        if res.box is None:
            raise RuntimeError(f"{name} produced no checkpoint")
        return res.box

    c = commands
    suite = step(
        "suite", candidate, apply_db_prefix(c.suite, db_prefix),
        keep_end=True, keep=False, timeout=timeout,
    )  # fmt: skip
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

    by_name = {st.name: st for st in steps}
    skipped = tuple(
        check for check, names in CHECKS.items() if all(by_name[n].skipped for n in names)
    )
    result = GateResult(
        suite=parse_suite(suite.stdout),
        mypy_ok=mypy.ok,
        ruff_ok=ruff_check.ok and ruff_format.ok,
        alembic_single_head="alembic" in skipped or (upgrade.ok and count_heads(heads.stdout) == 1),
        openapi_drift="openapi" not in skipped and (not snapshot.ok or not diff.ok),
        steps=tuple(steps),
        not_verified=list(c.not_covered) + [f"{check} (not configured)" for check in skipped],
        skipped=skipped,
    )
    log.emit(
        "verify",
        "note",
        {"gate": {**result.verdict_fields(), "steps": {s.name: s.exit_code for s in steps}}},
        ms=int((time.perf_counter() - t0) * 1000),
    )
    return result
