import hashlib
import inspect
import json
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import warden.fix.implementer as impl_module
from tests.fakes import (
    RecordingProgress,
    ScriptedLLM,
    TddBackend,
    finish_turn,
    text_turn,
    tool_call,
    turn,
)
from warden.config import Role, Settings
from warden.fix.implementer import (
    ALL_TOOLS,
    FINISH_TOOL,
    PROMPT_PATH,
    LLMLike,
    Progress,
    is_test_path,
    prompt_hash,
    removed_tests,
    run_implementer,
)
from warden.fix.tools import TOOL_SCHEMAS, ToolDispatcher
from warden.llm.client import LLM, ChatResult
from warden.llm.cost import CostCapExceededError, CostMeter, usd_for
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeImage
from warden.schemas import Candidate, Plan

T = "tests/test_fix.py"
TEST_A = "def test_a():\n    assert fix() == 1\n"
IMPL = "def fix():\n    return 1\n"
PYTEST = f"pytest -q {T}"


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "runs", "t")
    yield log
    log.close()


@dataclass
class Env:
    backend: TddBackend
    base: Box
    image: FakeImage
    plan: Plan
    dispatcher: ToolDispatcher
    log: EventLog


def _setup(log: EventLog) -> Env:
    backend = TddBackend({"src/fix.py": T})
    base = Box.from_ref(backend, "warden/fabric-base:latest", log)
    image = backend.images[base.uuid]
    image.test_results[T] = 1
    plan = Plan(
        issue=7,
        summary="fix() must return 1",
        files_to_touch=["src/fix.py"],
        tests_to_add=[T],
        landing_order=["src/fix.py"],
        risk_class=["none"],
        citations=[],
        acceptance=["test_fix passes"],
    )
    return Env(backend, base, image, plan, ToolDispatcher(log), log)


def _run(
    env: Env,
    script: Sequence[ChatResult | BaseException],
    *,
    max_tool_turns: int = 25,
    progress: RecordingProgress | None = None,
) -> tuple[ScriptedLLM, Candidate]:
    llm = ScriptedLLM(script, log=env.log)
    cand = run_implementer(
        llm,
        env.base,
        env.plan,
        env.log,
        dispatcher=env.dispatcher,
        max_tool_turns=max_tool_turns,
        progress=progress,
    )
    return llm, cand


def _events(log: EventLog) -> list[Any]:
    log.close()
    return EventLog.read(log.dir.parent, log.run_id)


def _notes(log: EventLog) -> list[dict[str, Any]]:
    return [e.data for e in _events(log) if e.kind == "note" and "implementer" in e.data]


def _note(log: EventLog, name: str) -> dict[str, Any]:
    found = [n for n in _notes(log) if n["implementer"] == name]
    assert len(found) == 1, f"expected one {name} note, got {found}"
    return found[0]


def happy_script() -> list[ChatResult]:
    return [
        turn(tool_call("write_file", path=T, content=TEST_A)),
        turn(tool_call("run_shell", command=PYTEST)),
        turn(tool_call("write_file", path="src/fix.py", content=IMPL)),
        turn(tool_call("run_shell", command=PYTEST)),
        finish_turn(done=True, tests_added=[T]),
    ]


# ------------------------------------------------------------------ 1. happy path


def test_happy_path_candidate(log: EventLog) -> None:
    env = _setup(log)
    _, cand = _run(env, happy_script())
    assert cand.issue == 7
    assert cand.image_uuid != env.base.uuid
    assert cand.tool_turns == 4 == env.dispatcher.calls
    assert cand.tests_added == [T]
    assert cand.cost_usd == pytest.approx(5 * usd_for("", 1000, 100))
    final = env.backend.images[cand.image_uuid].files
    assert final[f"/app/{T}"] == TEST_A.encode()
    assert final["/app/src/fix.py"] == IMPL.encode()
    assert f"/app/{T}" not in env.image.files and "/app/src/fix.py" not in env.image.files


def test_happy_path_messages(log: EventLog) -> None:
    env = _setup(log)
    llm, _ = _run(env, happy_script())
    assert len(llm.calls) == 5
    assert all(c.role is Role.IMPLEMENTER and c.phase == "fix" for c in llm.calls)
    assert all(c.tools == ALL_TOOLS for c in llm.calls)
    msgs = llm.calls[4].messages
    assert [m["role"] for m in msgs] == ["system", "user"] + ["assistant", "tool"] * 4
    assert "## Available tools" in msgs[0]["content"]
    assert "Budget: 25 tool turns" in msgs[0]["content"]
    assert "finish" in msgs[0]["content"]
    assert "#7" in msgs[1]["content"] and "1. test_fix passes" in msgs[1]["content"]
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert tool_msgs[0]["content"].startswith("wrote ")
    assert tool_msgs[1]["content"].endswith("[exit 1]")
    assert tool_msgs[3]["content"].endswith("[exit 0]")
    for assistant, tool in zip(msgs[2::2], msgs[3::2], strict=True):
        calls = assistant["tool_calls"]
        assert len(calls) == 1
        call = calls[0]
        assert call["type"] == "function"
        assert isinstance(call["function"]["arguments"], str)
        json.loads(call["function"]["arguments"])
        assert call["id"] == tool["tool_call_id"]
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "write_file"
    assert json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"])["path"] == T


def test_events(log: EventLog) -> None:
    env = _setup(log)
    _run(env, happy_script())
    events = _events(log)
    kinds = [e.kind for e in events if e.kind in {"llm", "tool"}]
    assert kinds == ["llm", "tool"] * 4 + ["llm"]
    start = events[0]
    assert start.kind == "note" and start.data["implementer"] == "start"
    expected = hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()[:12]
    assert start.data["prompt_hash"] == expected == prompt_hash(PROMPT_PATH.read_text())
    assert start.data["issue"] == 7 and start.data["base"] == env.base.uuid
    assert start.data["max_tool_turns"] == 25
    llm_events = [e for e in events if e.kind == "llm"]
    assert set(llm_events[0].data) == {
        "role",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "tool_calls",
    }
    assert all(e.phase == "fix" for e in events)
    final = _note(log, "final_tests")
    assert final["tests"] == [T] and final["exit_code"] == 0
    assert "passed" in final["tail"]
    done = _note(log, "done")
    assert done["report"]["done"] is True and done["report"]["tests_added"] == [T]
    assert done["tool_turns"] == 4 and done["tests_added"] == [T]
    assert done["image_uuid"] != env.base.uuid


# ------------------------------------------------------------------ 4. budget


def test_budget_exhaustion(log: EventLog) -> None:
    env = _setup(log)
    script = [turn(tool_call("read_file", path="REF")) for _ in range(25)]
    llm, cand = _run(env, script)
    assert cand.tool_turns == 25 == env.dispatcher.calls
    assert cand.image_uuid == env.base.uuid
    assert cand.tests_added == []
    assert len(llm.calls) == 25
    assert len([e for e in _events(log) if e.kind == "llm"]) == 25
    assert _note(log, "budget")["tool_turns"] == 25
    assert _note(log, "blocked")["report"] is None


def test_budget_mid_turn(log: EventLog) -> None:
    env = _setup(log)
    script = [
        turn(
            tool_call("list_files", path="."),
            tool_call("list_files", path="."),
            tool_call("list_files", path="."),
        )
    ]
    llm, cand = _run(env, script, max_tool_turns=2)
    assert env.dispatcher.calls == 2
    assert cand.tool_turns == 2
    assert llm.last_messages is not None
    tool_msgs = [m for m in llm.last_messages if m["role"] == "tool"]
    assert len(tool_msgs) == 3
    assert tool_msgs[0]["content"] == "./REF\n" == tool_msgs[1]["content"]
    assert "budget exhausted" in tool_msgs[2]["content"]
    assert tool_msgs[2]["tool_call_id"] == llm.last_messages[2]["tool_calls"][2]["id"]
    assert _note(log, "budget")["tool_turns"] == 2


# ------------------------------------------------------------------ 5. cost cap


def test_cost_cap_propagates(log: EventLog) -> None:
    env = _setup(log)
    script: list[ChatResult | BaseException] = [
        turn(tool_call("write_file", path=T, content=TEST_A)),
        turn(tool_call("run_shell", command=PYTEST)),
        CostCapExceededError(3.01, 3.0, "fix"),
    ]
    with pytest.raises(CostCapExceededError):
        _run(env, script)
    assert env.dispatcher.calls == 2
    names = {n["implementer"] for n in _notes(log)}
    assert names == {"start"}
    assert "except" not in inspect.getsource(impl_module)


# ------------------------------------------------------------------ 6. never delete a test


def test_refuses_test_deletion(log: EventLog) -> None:
    env = _setup(log)
    both = TEST_A + "\n\ndef test_b():\n    assert fix() != 2\n"
    only_b = "def test_b():\n    assert fix() != 2\n"
    script = [
        turn(tool_call("write_file", path=T, content=both)),
        turn(tool_call("write_file", path=T, content=only_b)),
        finish_turn(done=False, unmet=["test_fix passes"]),
    ]
    llm, cand = _run(env, script)
    refused = llm.calls[2].messages[-1]
    assert refused["role"] == "tool"
    assert refused["tool_call_id"] == llm.calls[2].messages[-2]["tool_calls"][0]["id"]
    assert refused["content"].startswith("refused:")
    assert "test_a" in refused["content"] and T in refused["content"]
    assert env.dispatcher.calls == 1
    assert cand.tool_turns == 2
    tool_events = [e for e in _events(log) if e.kind == "tool"]
    assert tool_events[0].data["to"] == cand.image_uuid
    note = _note(log, "refused_write")
    assert note["path"] == T and note["removed"] == ["test_a"]
    assert b"def test_a" in env.backend.images[cand.image_uuid].files[f"/app/{T}"]
    assert cand.tests_added == [T]
    assert _note(log, "blocked")["report"]["unmet"] == ["test_fix passes"]


def test_existing_test_file_edit_allowed(log: EventLog) -> None:
    env = _setup(log)
    env.image.files["/app/tests/test_old.py"] = b"def test_z():\n    pass\n"
    grown = "def test_z():\n    pass\n\n\ndef test_y():\n    pass\n"
    script = [turn(tool_call("write_file", path="tests/test_old.py", content=grown)), finish_turn()]
    llm, cand = _run(env, script)
    assert env.dispatcher.calls == 1
    assert llm.calls[1].messages[-1]["content"].startswith("wrote ")
    assert cand.tests_added == []
    assert cand.tool_turns == 1
    assert env.backend.images[cand.image_uuid].files["/app/tests/test_old.py"] == grown.encode()
    assert not [n for n in _notes(log) if n["implementer"] == "final_tests"]


# ------------------------------------------------------------------ 7. prose routing


def test_prose_routed_via_lightning(log: EventLog) -> None:
    env = _setup(log)
    prose = "let me list tests"
    script = [text_turn(prose), turn(tool_call("list_files", path="tests")), finish_turn()]
    llm, cand = _run(env, script)
    assert [c.role for c in llm.calls] == [Role.IMPLEMENTER, Role.TOOLS, Role.IMPLEMENTER]
    routed = llm.calls[1]
    assert routed.tools == ALL_TOOLS
    assert [m["role"] for m in routed.messages] == ["system", "user"]
    assert routed.messages[1]["content"] == prose
    assistant = llm.calls[2].messages[2]
    assert assistant["role"] == "assistant" and assistant["content"] == prose
    assert assistant["tool_calls"][0]["function"]["name"] == "list_files"
    assert llm.calls[2].messages[3]["role"] == "tool"
    assert env.dispatcher.calls == 1
    assert cand.tool_turns == 1


def test_unroutable_prose_nudges(log: EventLog) -> None:
    env = _setup(log)
    script = [text_turn("I think the bug is in fix()"), text_turn("no action"), finish_turn()]
    llm, cand = _run(env, script)
    assert [c.role for c in llm.calls] == [Role.IMPLEMENTER, Role.TOOLS, Role.IMPLEMENTER]
    assert cand.tool_turns == 1
    assert env.dispatcher.calls == 0
    msgs = llm.calls[2].messages
    assert msgs[2] == {"role": "assistant", "content": "I think the bug is in fix()"}
    assert msgs[3]["role"] == "user" and "tool call" in msgs[3]["content"]
    assert _note(log, "unroutable")
    assert cand.cost_usd == pytest.approx(3 * usd_for("", 1000, 100))


# ------------------------------------------------------------------ 8. finish handling


def test_invalid_finish_feeds_errors_back(log: EventLog) -> None:
    env = _setup(log)
    script = [turn(tool_call("finish", done="maybe")), finish_turn()]
    llm, cand = _run(env, script)
    msg = llm.calls[1].messages[-1]
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == llm.calls[1].messages[-2]["tool_calls"][0]["id"]
    assert msg["content"].startswith("invalid arguments for finish")
    assert "done" in msg["content"] and "https://" not in msg["content"]
    assert cand.tool_turns == 1
    assert env.dispatcher.calls == 0
    assert _note(log, "done")["report"]["done"] is True


def test_tdd_order_warn_only(log: EventLog) -> None:
    env = _setup(log)
    script = [turn(tool_call("write_file", path="src/fix.py", content=IMPL)), finish_turn()]
    _, cand = _run(env, script)
    assert isinstance(cand, Candidate)
    note = _note(log, "tdd_order")
    assert note["first_write"] == "src/fix.py" and note["test_first"] is False
    assert _note(log, "done")


def test_tdd_order_happy(log: EventLog) -> None:
    env = _setup(log)
    _run(env, happy_script())
    note = _note(log, "tdd_order")
    assert note["first_write"] == T and note["test_first"] is True


# ------------------------------------------------------------------ 9. progress + protocols


def test_progress_hooks(log: EventLog) -> None:
    env = _setup(log)
    progress = RecordingProgress()
    _run(env, happy_script(), progress=progress)
    assert len(progress.steps) == 4
    assert progress.steps[0] == f"write_file {T} → exit 0"
    assert progress.steps[1] == f"run_shell {PYTEST} → exit 1"
    assert progress.steps[3] == f"run_shell {PYTEST} → exit 0"
    assert progress.costs == [usd_for("", 1000, 100)] * 5
    other = _setup(log)
    _, cand = _run(other, happy_script(), progress=None)
    assert cand.tool_turns == 4


def test_protocols(log: EventLog) -> None:
    from warden import ui

    assert isinstance(ScriptedLLM([]), LLMLike)
    assert isinstance(LLM(Settings(), CostMeter(1.0), log), LLMLike)
    assert isinstance(ui.PhaseLog(StringIO()), Progress)
    assert isinstance(RecordingProgress(), Progress)


# ------------------------------------------------------------------ 10. schema + helpers


def test_finish_tool_schema() -> None:
    assert FINISH_TOOL["type"] == "function"
    fn = FINISH_TOOL["function"]
    assert fn["name"] == "finish" and fn["description"]
    params = fn["parameters"]
    assert params["type"] == "object" and params["additionalProperties"] is False
    assert set(params["properties"]) == {"done", "tests_added", "unmet", "notes"}
    assert params["required"] == list(params["properties"])
    assert "title" not in params
    for prop in params["properties"].values():
        assert "title" not in prop and "default" not in prop
        assert prop["description"]
    assert ALL_TOOLS == [*TOOL_SCHEMAS, FINISH_TOOL]
    assert [t["function"]["name"] for t in ALL_TOOLS][-1] == "finish"


def test_no_contree_import() -> None:
    assert "contree" not in inspect.getsource(impl_module)
    assert not [m for m in sys.modules if m.startswith("contree")]
    assert "warden.ui" not in inspect.getsource(impl_module)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_x.py", True),
        ("./tests/x.py", True),
        ("/app/tests/x.py", True),
        ("src/pkg/tests/helpers.py", True),
        ("src/test_x.py", True),
        ("src/x_test.py", True),
        ("spec/planned_check.py", True),
        ("src/x.py", False),
        ("src/testing.py", False),
        ("", False),
    ],
)
def test_is_test_path(path: str, expected: bool) -> None:
    plan = Plan(
        issue=1,
        summary="s",
        files_to_touch=[],
        tests_to_add=["./spec/planned_check.py"],
        landing_order=[],
        risk_class=[],
        citations=[],
        acceptance=[],
    )
    assert is_test_path(path, plan) is expected


def test_removed_tests() -> None:
    before = (
        "import x\n\n"
        "def test_a():\n    pass\n\n"
        "async def test_b():\n    pass\n\n"
        "class TestC:\n    def test_c(self):\n        pass\n"
    )
    assert removed_tests(before, before) == []
    assert removed_tests(before, before + "\ndef test_d():\n    pass\n") == []
    assert removed_tests(before, "def test_a():\n    pass\n") == ["test_b", "test_c"]
    assert removed_tests(before, "") == ["test_a", "test_b", "test_c"]
    assert removed_tests(before, before.replace("test_a", "test_a_renamed")) == ["test_a"]
    assert removed_tests("x = test_a\n", "") == []
    assert removed_tests("", "def test_new(): pass\n") == []


def test_prompt_hash() -> None:
    assert prompt_hash("abc") == hashlib.sha256(b"abc").hexdigest()[:12]
    assert len(prompt_hash("")) == 12
    assert PROMPT_PATH.is_file() and PROMPT_PATH.name == "implementer.md"
