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
