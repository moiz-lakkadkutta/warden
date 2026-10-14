"""Pilot 3 implementer fixes: the write_file clobber guard, replace_in_file, the finish nudge.

Evidence: Super wrote a 309-byte fragment over 19 kB pennylane exp.py and spent 30 turns
recovering; fromager's tests were green at turn 10 and it never called `finish`."""

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import ScriptedLLM, TddBackend, finish_turn, tool_call, turn
from warden.fix.implementer import run_implementer
from warden.fix.tools import TOOL_NAMES, ToolDispatcher
from warden.llm.client import ChatResult
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend, FakeImage
from warden.schemas import Candidate, Plan

BIG = "".join(f"def f{i}():\n    return {i}\n" for i in range(800))  # ~19 kB
FRAGMENT = "def f3():\n    return 'fixed'\n"


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "runs", "t")
    yield log
    log.close()


def _box(log: EventLog) -> tuple[FakeBackend, Box, FakeImage]:
    backend = FakeBackend()
    box = Box.from_ref(backend, "warden/fabric-base:latest", log)
    return backend, box, backend.images[box.uuid]


def _events(log: EventLog) -> list[Any]:
    log.close()
    return EventLog.read(log.dir.parent, log.run_id)


# ------------------------------------------------------------------ clobber guard


def test_write_file_refuses_a_large_shrink(log: EventLog) -> None:
    backend, box, img = _box(log)
    img.files["/app/src/exp.py"] = BIG.encode()
    res = ToolDispatcher(log).dispatch(
        box, "write_file", {"path": "src/exp.py", "content": FRAGMENT}
    )
    assert res.ok is False and res.exit_code is None
    assert res.box is box  # nothing written
    old, new = len(BIG.encode()), len(FRAGMENT.encode())
    assert f"from {old} to {new} bytes" in res.output
    assert "allow_shrink" in res.output and "replace_in_file" in res.output
    assert backend.images[box.uuid].files["/app/src/exp.py"] == BIG.encode()
    (ev,) = [e for e in _events(log) if e.kind == "tool"]
    assert ev.data["ok"] is False
    assert ev.data["refused"] == {"reason": "shrink", "old_bytes": old, "new_bytes": new}


def test_allow_shrink_overrides(log: EventLog) -> None:
    _, box, img = _box(log)
    img.files["/app/src/exp.py"] = BIG.encode()
    res = ToolDispatcher(log).dispatch(
        box, "write_file", {"path": "src/exp.py", "content": FRAGMENT, "allow_shrink": True}
    )
    assert res.ok and res.box.uuid != box.uuid


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("x" * 900, "y"),  # old file under 1 kB: never guarded
        ("x" * 4000, "y" * 2000),  # exactly half: allowed
        (None, "y"),  # new file
    ],
)
def test_small_or_moderate_writes_pass(log: EventLog, old: str | None, new: str) -> None:
    _, box, img = _box(log)
    if old is not None:
        img.files["/app/src/a.py"] = old.encode()
    res = ToolDispatcher(log).dispatch(box, "write_file", {"path": "src/a.py", "content": new})
    assert res.ok


# ------------------------------------------------------------------ replace_in_file


def test_replace_in_file_is_a_tool() -> None:
    assert "replace_in_file" in TOOL_NAMES


def test_replace_in_file_unique_match_forks(log: EventLog) -> None:
    backend, box, img = _box(log)
    img.files["/app/src/exp.py"] = BIG.encode()
    d = ToolDispatcher(log)
    res = d.dispatch(
        box,
        "replace_in_file",
        {"path": "src/exp.py", "old": "def f3():\n    return 3\n", "new": FRAGMENT},
    )
    assert res.ok and res.exit_code == 0
    assert res.box.uuid != box.uuid
    after = backend.images[res.box.uuid].files["/app/src/exp.py"].decode()
    assert after == BIG.replace("def f3():\n    return 3\n", FRAGMENT)
    assert "replaced 1 occurrence in src/exp.py" in res.output
    assert backend.images[box.uuid].files["/app/src/exp.py"] == BIG.encode()


@pytest.mark.parametrize(
    ("old", "expect"),
    [
        ("def nope():", "not found"),
        ("    return 1", "matches 2 places"),
        ("", "old"),
    ],
)
def test_replace_in_file_errors(log: EventLog, old: str, expect: str) -> None:
    _, box, img = _box(log)
    img.files["/app/src/a.py"] = b"def a():\n    return 1\n\ndef b():\n    return 1\n" + b"#" * 10
    res = ToolDispatcher(log).dispatch(
        box, "replace_in_file", {"path": "src/a.py", "old": old, "new": "x"}
    )
    assert res.ok is False and res.box is box
    assert expect in res.output


def test_replace_in_file_missing_file(log: EventLog) -> None:
    _, box, _ = _box(log)
    res = ToolDispatcher(log).dispatch(
        box, "replace_in_file", {"path": "src/none.py", "old": "a", "new": "b"}
    )
    assert res.ok is False and res.box is box
    assert "No such file" in res.output


def test_replace_in_file_tolerates_aliases(log: EventLog) -> None:
    backend, box, img = _box(log)
    img.files["/app/src/a.py"] = b"x = 1\n"
    res = ToolDispatcher(log).dispatch(
        box, "replace_in_file", {"file_path": "src/a.py", "old_string": "1", "new_string": "2"}
    )
    assert res.ok
    assert backend.images[res.box.uuid].files["/app/src/a.py"] == b"x = 2\n"


# ------------------------------------------------------------------ implementer loop

T = "tests/test_fix.py"
TEST_A = "def test_a():\n    assert fix() == 1\n"
IMPL = "def fix():\n    return 1\n"


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
    log: EventLog,
    script: Sequence[ChatResult],
    *,
    files: dict[str, bytes] | None = None,
    max_tool_turns: int = 25,
) -> tuple[ScriptedLLM, Candidate]:
    backend = TddBackend({"src/fix.py": T})
    base = Box.from_ref(backend, "warden/fabric-base:latest", log)
    image = backend.images[base.uuid]
    image.test_results[T] = 1
    image.files.update(files or {})
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
    return [e.data for e in _events(log) if e.kind == "note" and e.data.get("implementer") == name]


def _user_texts(llm: ScriptedLLM) -> list[str]:
    msgs = llm.last_messages or []
    return [str(m["content"]) for m in msgs if m["role"] == "user"]


def test_refused_shrink_counts_a_turn_and_is_noted(log: EventLog) -> None:
    script = [turn(tool_call("write_file", path="src/exp.py", content=FRAGMENT)), finish_turn()]
    llm, cand = _run(log, script, files={"/app/src/exp.py": BIG.encode()})
    assert cand.tool_turns == 1
    (n,) = _notes(log, "refused_shrink")
    assert n["path"] == "src/exp.py"
    assert n["old_bytes"] == len(BIG.encode()) and n["new_bytes"] == len(FRAGMENT.encode())
    tool_msgs = [m for m in llm.last_messages or [] if m["role"] == "tool"]
    assert "would shrink" in tool_msgs[0]["content"]


def test_replace_in_file_on_a_test_file_cannot_drop_a_test(log: EventLog) -> None:
    both = "def test_old():\n    assert 1\n\ndef test_other():\n    assert 1\n"
    script = [
        turn(
            tool_call(
                "replace_in_file",
                path="tests/test_old.py",
                old="def test_old():\n    assert 1\n\n",
                new="",
            )
        ),
        finish_turn(),
    ]
    _, cand = _run(log, script, files={"/app/tests/test_old.py": both.encode()})
    assert cand.tool_turns == 1
    (n,) = _notes(log, "refused_write")
    assert n["removed"] == ["test_old"]


def test_replace_in_file_adding_a_test_counts_it(log: EventLog) -> None:
    old = "def test_old():\n    assert 1\n"
    script = [
        turn(
            tool_call(
                "replace_in_file",
                path="tests/test_old.py",
                old=old,
                new=old + "\n\ndef test_new():\n    assert 1\n",
            )
        ),
        finish_turn(),
    ]
    _, cand = _run(log, script, files={"/app/tests/test_old.py": old.encode()})
    assert cand.tests_added == ["tests/test_old.py::test_new"]
