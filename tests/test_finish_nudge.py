"""Finish nudge (pilot 3: fromager's new tests were green at turn 10 and the implementer never
called `finish`; 7 of 10 runs used every turn)."""

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import ScriptedLLM, TddBackend, finish_turn, tool_call, turn
from warden.fix.implementer import FINISH_NUDGE, run_implementer
from warden.fix.tools import ToolDispatcher
from warden.llm.client import ChatResult
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.schemas import Candidate, Plan

T = "tests/test_fix.py"
TEST_A = "def test_a():\n    assert fix() == 1\n"
IMPL = "def fix():\n    return 1\n"


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "runs", "t")
    yield log
    log.close()


def _plan() -> Plan:
    return Plan(
        issue=7,
        summary="fix() must return 1",
        files_to_touch=["src/fix.py"],
        tests_to_add=[T],
        landing_order=["src/fix.py"],
        risk_class=["none"],
        citations=[],
        acceptance=["test_fix passes"],
    )


def _run(
    log: EventLog, script: Sequence[ChatResult], *, max_tool_turns: int = 25
) -> tuple[ScriptedLLM, Candidate]:
    backend = TddBackend({"src/fix.py": T})
    base = Box.from_ref(backend, "warden/fabric-base:latest", log)
    backend.images[base.uuid].test_results.update({T: 1, "tests/test_other.py": 1})
    llm = ScriptedLLM(script, log=log)
    cand = run_implementer(
        llm,
        base,
        _plan(),
        log,
        dispatcher=ToolDispatcher(log),
        max_tool_turns=max_tool_turns,
        idle_nudge_after=0,
    )
    return llm, cand


def _notes(log: EventLog, name: str) -> list[dict[str, Any]]:
    log.close()
    events = EventLog.read(log.dir.parent, log.run_id)
    return [e.data for e in events if e.kind == "note" and e.data.get("implementer") == name]


def _user_texts(llm: ScriptedLLM) -> list[str]:
    msgs = llm.last_messages or []
    return [str(m["content"]) for m in msgs if m["role"] == "user"]


def _green_then(*extra: ChatResult) -> list[ChatResult]:
    return [
        turn(tool_call("write_file", path=T, content=TEST_A)),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),  # red
        turn(tool_call("write_file", path="src/fix.py", content=IMPL)),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),  # green: turn 4
        *extra,
    ]


def read() -> ChatResult:
    return turn(tool_call("list_files", path="src"))


def test_finish_nudge_after_green_tests_and_no_finish(log: EventLog) -> None:
    llm, cand = _run(log, _green_then(read(), read(), read(), read(), finish_turn()))
    assert cand.done
    nudges = [t for t in _user_texts(llm) if t == FINISH_NUDGE]
    assert len(nudges) == 1  # never repeated
    (n,) = _notes(log, "finish_nudge")
    assert n["green_turn"] == 4 and n["tool_turns"] == 6


def test_no_finish_nudge_when_the_model_finishes(log: EventLog) -> None:
    llm, _ = _run(log, _green_then(read(), finish_turn()))
    assert FINISH_NUDGE not in _user_texts(llm)
    assert _notes(log, "finish_nudge") == []


def test_no_finish_nudge_without_new_tests(log: EventLog) -> None:
    script = [
        turn(tool_call("write_file", path="src/fix.py", content=IMPL)),
        turn(tool_call("run_shell", command=f"pytest -q {T}")),
        read(),
        read(),
        read(),
        finish_turn(),
    ]
    llm, _ = _run(log, script)
    assert _notes(log, "finish_nudge") == []


def test_an_edit_after_green_rearms_the_wait(log: EventLog) -> None:
    script = _green_then(
        turn(tool_call("write_file", path="src/other.py", content="y = 2\n")),  # turn 5
        read(),  # turn 6: the green run is stale, no nudge
        read(),
        finish_turn(),
    )
    llm, _ = _run(log, script)
    assert _notes(log, "finish_nudge") == []


def test_a_failing_run_after_green_disarms(log: EventLog) -> None:
    script = _green_then(
        turn(tool_call("run_shell", command="pytest -q tests/test_other.py")),  # red
        read(),
        read(),
        finish_turn(),
    )
    _run(log, script)
    assert _notes(log, "finish_nudge") == []


def test_non_test_command_does_not_arm(log: EventLog) -> None:
    script = [
        turn(tool_call("write_file", path=T, content=TEST_A)),
        turn(tool_call("write_file", path="src/fix.py", content=IMPL)),
        turn(tool_call("run_shell", command="echo ok")),
        read(),
        read(),
        read(),
        finish_turn(),
    ]
    _run(log, script)
    assert _notes(log, "finish_nudge") == []
