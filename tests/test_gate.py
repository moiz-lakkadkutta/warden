from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend, FakeImage
from warden.schemas import Verdict
from warden.verify.gate import DEFAULT_COMMANDS, GateResult, parse_suite, run_gate

VERDICT_KEYS = {"suite", "mypy_ok", "ruff_ok", "alembic_single_head", "openapi_drift"}


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "runs", "t")
    yield log
    log.close()


def _setup(log: EventLog) -> tuple[FakeBackend, Box, FakeImage]:
    backend = FakeBackend()
    candidate = Box.from_ref(backend, "warden/fabric-base:latest", log, phase="verify")
    return backend, candidate, backend.images[candidate.uuid]


def _sandbox_events(log: EventLog) -> list[dict[str, object]]:
    log.close()
    events = EventLog.read(log.dir.parent, log.run_id)
    return [e.data for e in events if e.kind == "sandbox"]


def _assert_green_except(result: GateResult, *flipped: str) -> None:
    expected = {
        "suite_ok": True,
        "mypy_ok": True,
        "ruff_ok": True,
        "alembic_single_head": True,
        "openapi_drift": False,
    }
    for name, good in expected.items():
        actual = getattr(result, name)
        assert actual == (not good if name in flipped else good), name
    assert result.ok is (not flipped)


def test_all_green(log: EventLog) -> None:
    _, candidate, _ = _setup(log)
    result = run_gate(candidate, log)
    assert result.suite == {"passed": 1, "failed": 0}
    _assert_green_except(result)
    assert result.migration_roundtrip is None
    fields = result.verdict_fields()
    assert set(fields) == VERDICT_KEYS
    verdict = Verdict(decision="PASS", theater_failed_on_base=True, **fields)
    assert verdict.mypy_ok and verdict.suite == {"passed": 1, "failed": 0}


def test_suite_failure(log: EventLog) -> None:
    _, candidate, img = _setup(log)
    img.suite_counts = {"passed": 5, "failed": 2}
    result = run_gate(candidate, log)
    assert result.suite == {"passed": 5, "failed": 2}
    _assert_green_except(result, "suite_ok")


def test_mypy_flips(log: EventLog) -> None:
    _, candidate, img = _setup(log)
    img.gate_results["mypy"] = 1
    _assert_green_except(run_gate(candidate, log), "mypy_ok")


@pytest.mark.parametrize("key", ["ruff check", "ruff format"])
def test_ruff_flips(log: EventLog, key: str) -> None:
    _, candidate, img = _setup(log)
    img.gate_results[key] = 1
    _assert_green_except(run_gate(candidate, log), "ruff_ok")


@pytest.mark.parametrize("seed", ["upgrade_fails", "two_heads"])
def test_alembic_flips(log: EventLog, seed: str) -> None:
    _, candidate, img = _setup(log)
    if seed == "upgrade_fails":
        img.gate_results["alembic upgrade"] = 1
    else:
        img.alembic_heads = ["a (head)", "b (head)"]
    _assert_green_except(run_gate(candidate, log), "alembic_single_head")


@pytest.mark.parametrize(("key", "code"), [("make openapi-snapshot", 2), ("git diff", 1)])
def test_openapi_drift_flips(log: EventLog, key: str, code: int) -> None:
    _, candidate, img = _setup(log)
    img.gate_results[key] = code
    _assert_green_except(run_gate(candidate, log), "openapi_drift")


def test_every_gate_command_emits_sandbox_event(log: EventLog) -> None:
    _, candidate, img = _setup(log)
    img.gate_results["mypy"] = 1
    img.gate_results["git diff"] = 1
    result = run_gate(candidate, log)
    log.close()
    events = EventLog.read(log.dir.parent, log.run_id)
    sandbox = [e for e in events if e.kind == "sandbox"]
    c = DEFAULT_COMMANDS
    expected = [
        c.suite,
        c.mypy,
        c.ruff_check,
        c.ruff_format,
        c.db_create,
        c.alembic_upgrade,
        c.alembic_heads,
        c.openapi_snapshot,
        c.openapi_diff,
    ]
    assert [e.data["shell"] for e in sandbox] == expected
    assert [e.data["exit_code"] for e in sandbox] == [s.exit_code for s in result.steps]
    assert [e.data["exit_code"] for e in sandbox] == [0, 1, 0, 0, 0, 0, 0, 0, 1]
    assert all(e.phase == "verify" for e in sandbox)
    final = events[-1]
    assert final.kind == "note" and final.phase == "verify"
    assert final.data["gate"]["mypy_ok"] is False
    assert final.data["gate"]["openapi_drift"] is True
    assert final.data["gate"]["steps"]["mypy"] == 1
    assert final.ms is not None


def test_candidate_untouched_and_steps_recorded(log: EventLog) -> None:
    _, candidate, _ = _setup(log)
    commands = replace(DEFAULT_COMMANDS, suite="pytest -q backend/tests")
    result = run_gate(candidate, log, commands=commands)
    assert len(result.steps) == 9
    names = [s.name for s in result.steps]
    assert len(set(names)) == 9
    assert result.steps[0].name == "suite"
    assert result.steps[0].shell == "pytest -q backend/tests"
    assert all(s.ok for s in result.steps)
    assert all(s.ms >= 0 for s in result.steps)
    assert "passed" in result.steps[0].tail
    # fork topology: steps 1-4 and 8 on the candidate; 6-7 on the db child; 9 on the snap child
    sandbox = _sandbox_events(log)
    cand = candidate.uuid
    db, snap = sandbox[4]["to"], sandbox[7]["to"]
    assert db != cand and snap != cand and db != snap
    assert [e["from"] for e in sandbox] == [cand] * 4 + [cand, db, db, cand, snap]
    assert [e["to"] for e in sandbox[:4]] == [cand] * 4  # disposable runs leave no checkpoint


def test_parse_suite_ignores_counts_outside_summary_line() -> None:
    probe = "FAILED t.py::x - assert 3 passed\n=== 1 failed, 4 passed =="
    assert parse_suite(probe) == {"passed": 4, "failed": 1}
    quiet = "collected 3 passed hints\n3 passed in 0.02s\n"
    assert parse_suite(quiet) == {"passed": 3, "failed": 0}


@pytest.mark.parametrize("stdout", ["", "no tests ran in 0.01s\n", "Traceback: ImportError\n"])
def test_parse_suite_empty_when_no_counts(stdout: str) -> None:
    assert parse_suite(stdout) == {}


class _Recorder:
    """Every command succeeds; records (shell, cwd, env, keep). Kept runs fork."""

    def __init__(self) -> None:
        self.runs: list[dict[str, object]] = []

    def use(self, ref: str) -> str:
        return ref

    def run(
        self, image: object, *, shell: str, cwd: str, keep: bool, env: object = None, **_: object
    ) -> object:
        self.runs.append({"shell": shell, "cwd": cwd, "env": env, "keep": keep})
        out = "f2_ap_payment_schema (head)\n" if "heads" in shell else "3 passed in 0.5s\n"
        return out, "", 0, (f"{image}+" if keep else None), (f"{image}+" if keep else "")

    def tag(self, image: object, name: str) -> None:
        return None

    def uuid_of(self, image: object) -> str:
        return str(image)


def test_wrap_and_workdir_apply_to_every_step_but_steps_record_the_plain_command(
    log: EventLog,
) -> None:
    from warden.verify.gate import PROFILES

    rec = _Recorder()
    result = run_gate(
        Box(rec, "cand", log),
        log,
        commands=PROFILES["fabric"],
        cwd="/src",
        wrap="with-services sh -c",
    )
    assert result.ok
    assert all(r["cwd"] == "/src" for r in rec.runs)
    assert all(str(r["shell"]).startswith("with-services sh -c '") for r in rec.runs)
    suite = next(s for s in result.steps if s.name == "suite")
    assert suite.shell == "cd backend && pytest -q -p no:cacheprovider"


def test_fabric_profile_matches_fabric_ci() -> None:
    from warden.verify.gate import PROFILES

    f = PROFILES["fabric"]
    assert f.mypy == "cd backend && mypy ."
    assert f.ruff_check == "cd backend && ruff check ."
    assert f.ruff_format == "cd backend && ruff format --check ."
    assert "127.0.0.1" in f.db_create and "-U postgres" in f.db_create
    url = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/warden_gate"
    assert f.alembic_env == {"DATABASE_URL": url, "MIGRATION_DATABASE_URL": url}
    assert f.openapi_snapshot.endswith("dump-openapi.py --out /tmp/openapi-fresh.json")
    assert (
        f.openapi_diff == "diff -q frontend/scripts/openapi-snapshot.json /tmp/openapi-fresh.json"
    )
    for cmd in (f.suite, f.mypy, f.ruff_check, f.ruff_format, f.alembic_upgrade, f.openapi_diff):
        assert "|" not in cmd  # a pipe would hide the exit code (gate probe, 2026-10-10)
    assert f.not_covered  # FE type codegen needs pnpm, absent from fabric-base


def test_not_covered_checks_surface_in_the_result(log: EventLog) -> None:
    from warden.verify.gate import PROFILES

    result = run_gate(Box(_Recorder(), "c", log), log, commands=PROFILES["fabric"])
    assert result.not_verified == list(PROFILES["fabric"].not_covered)
    assert run_gate(Box(_Recorder(), "c", log), log).not_verified == []


def test_fabric_profile_has_lint_and_autofix() -> None:
    from warden.verify.gate import PROFILES

    f = PROFILES["fabric"]
    assert f.lint == "cd backend && ruff check . && ruff format --check ."
    assert f.autofix == "cd backend && ruff format . && ruff check --fix ."
    assert PROFILES["generic"].autofix == ""
