from dataclasses import replace
from pathlib import Path

import pytest

from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend, FakeImage
from warden.schemas import Verdict
from warden.verify.gate import DEFAULT_COMMANDS, GateResult, run_gate

VERDICT_KEYS = {"suite", "mypy_ok", "ruff_ok", "alembic_single_head", "openapi_drift"}


def _setup(tmp_path: Path) -> tuple[FakeBackend, Box, EventLog, FakeImage]:
    backend = FakeBackend()
    log = EventLog(tmp_path / "runs", "t")
    candidate = Box.from_ref(backend, "warden/fabric-base:latest", log, phase="verify")
    return backend, candidate, log, backend.images[candidate.uuid]


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


def test_all_green(tmp_path: Path) -> None:
    _, candidate, log, _ = _setup(tmp_path)
    result = run_gate(candidate, log)
    assert result.suite == {"passed": 1, "failed": 0}
    _assert_green_except(result)
    assert result.migration_roundtrip is None
    fields = result.verdict_fields()
    assert set(fields) == VERDICT_KEYS
    verdict = Verdict(decision="PASS", theater_failed_on_base=True, **fields)
    assert verdict.mypy_ok and verdict.suite == {"passed": 1, "failed": 0}


def test_suite_failure(tmp_path: Path) -> None:
    _, candidate, log, img = _setup(tmp_path)
    img.suite_counts = {"passed": 5, "failed": 2}
    result = run_gate(candidate, log)
    assert result.suite == {"passed": 5, "failed": 2}
    _assert_green_except(result, "suite_ok")


def test_mypy_flips(tmp_path: Path) -> None:
    _, candidate, log, img = _setup(tmp_path)
    img.gate_results["mypy"] = 1
    _assert_green_except(run_gate(candidate, log), "mypy_ok")


@pytest.mark.parametrize("key", ["ruff check", "ruff format"])
def test_ruff_flips(tmp_path: Path, key: str) -> None:
    _, candidate, log, img = _setup(tmp_path)
    img.gate_results[key] = 1
    _assert_green_except(run_gate(candidate, log), "ruff_ok")


@pytest.mark.parametrize("seed", ["upgrade_fails", "two_heads"])
def test_alembic_flips(tmp_path: Path, seed: str) -> None:
    _, candidate, log, img = _setup(tmp_path)
    if seed == "upgrade_fails":
        img.gate_results["alembic upgrade"] = 1
    else:
        img.alembic_heads = ["a (head)", "b (head)"]
    _assert_green_except(run_gate(candidate, log), "alembic_single_head")


@pytest.mark.parametrize(("key", "code"), [("make openapi-snapshot", 2), ("git diff", 1)])
def test_openapi_drift_flips(tmp_path: Path, key: str, code: int) -> None:
    _, candidate, log, img = _setup(tmp_path)
    img.gate_results[key] = code
    _assert_green_except(run_gate(candidate, log), "openapi_drift")


def test_every_gate_command_emits_sandbox_event(tmp_path: Path) -> None:
    _, candidate, log, img = _setup(tmp_path)
    img.gate_results["mypy"] = 1
    img.gate_results["git diff"] = 1
    result = run_gate(candidate, log)
    log.close()
    events = EventLog.read(tmp_path / "runs", "t")
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


def test_candidate_untouched_and_steps_recorded(tmp_path: Path) -> None:
    _, candidate, log, img = _setup(tmp_path)
    before = dict(img.files)
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
    assert img.files == before
