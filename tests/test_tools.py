import inspect
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import warden.fix.tools as tools_module
from warden.fix.tools import TOOL_NAMES, TOOL_SCHEMAS, ToolDispatcher, ToolResult, as_message
from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend, FakeImage


@pytest.fixture
def log(tmp_path: Path) -> Iterator[EventLog]:
    log = EventLog(tmp_path / "runs", "t")
    yield log
    log.close()


def _setup(log: EventLog) -> tuple[FakeBackend, Box, FakeImage]:
    backend = FakeBackend()
    box = Box.from_ref(backend, "warden/fabric-base:latest", log)
    return backend, box, backend.images[box.uuid]


def _events(log: EventLog) -> list[Any]:
    log.close()
    return EventLog.read(log.dir.parent, log.run_id)


def test_tool_schemas_shape() -> None:
    assert TOOL_NAMES == ("list_files", "read_file", "grep", "write_file", "run_shell")
    assert len(TOOL_SCHEMAS) == 5
    assert [s["function"]["name"] for s in TOOL_SCHEMAS] == list(TOOL_NAMES)
    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["description"]
        assert "strict" not in fn
        params = fn["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False
        assert set(params["required"]) == set(params["properties"])
        assert "title" not in params
        for prop in params["properties"].values():
            assert prop["description"]
            assert "title" not in prop
            assert "default" not in prop


def test_list_files_happy(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.files["/app/src/b.py"] = b"b"
    img.files["/app/src/a.py"] = b"a"
    result = ToolDispatcher(log).dispatch(box, "list_files", {"path": "src"})
    assert result.ok and result.exit_code == 0
    assert result.output == "src/a.py\nsrc/b.py\n"
    assert result.box is box
    assert result.truncated is False


def test_read_file_happy_and_missing(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.files["/app/src/a.py"] = b"def foo():\n    return 1\n"
    d = ToolDispatcher(log)
    good = d.dispatch(box, "read_file", {"path": "src/a.py"})
    assert good.ok and good.exit_code == 0
    assert good.output == "def foo():\n    return 1\n"
    assert good.box is box
    bad = d.dispatch(box, "read_file", {"path": "src/missing.py"})
    assert bad.ok is False and bad.exit_code == 1
    assert "No such file" in bad.output
    assert bad.box is box


def test_grep_match_and_no_match(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.files["/app/src/a.py"] = b"import os\ndef foo():\n    pass\n"
    d = ToolDispatcher(log)
    hit = d.dispatch(box, "grep", {"pattern": "def foo", "path": "src"})
    assert hit.ok and hit.exit_code == 0
    assert hit.output == "src/a.py:2:def foo():\n"
    miss = d.dispatch(box, "grep", {"pattern": "nope", "path": "src"})
    assert miss.ok is True and miss.exit_code == 1
    assert miss.output == "(no matches)"
    assert miss.box is box


def test_write_file_forks(log: EventLog) -> None:
    backend, parent, _ = _setup(log)
    d = ToolDispatcher(log)
    res = d.dispatch(parent, "write_file", {"path": "src/new.py", "content": "x = 1\n"})
    assert res.ok and res.exit_code == 0
    assert res.output == "wrote 6 bytes to src/new.py"
    child = res.box
    assert child.uuid != parent.uuid
    assert d.dispatch(child, "read_file", {"path": "src/new.py"}).output == "x = 1\n"
    assert d.dispatch(parent, "read_file", {"path": "src/new.py"}).ok is False
    assert "/app/src/new.py" not in backend.images[parent.uuid].files
    assert backend.images[child.uuid].files["/app/src/new.py"] == b"x = 1\n"
    absolute = d.dispatch(parent, "write_file", {"path": "/etc/warden.cfg", "content": "k=v"})
    assert absolute.ok and absolute.box.uuid != parent.uuid
    assert backend.images[absolute.box.uuid].files["/etc/warden.cfg"] == b"k=v"


def test_run_shell_happy(log: EventLog) -> None:
    backend, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "run_shell", {"command": "echo hi"})
    assert res.ok and res.exit_code == 0
    assert res.output.startswith("hi")
    assert res.output.endswith("[exit 0]")
    assert "--- stderr ---" not in res.output
    assert res.box.uuid != box.uuid
    assert backend.timeouts[-1] == 600


def test_run_shell_nonzero(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.test_results["tests/t.py"] = 1
    res = ToolDispatcher(log).dispatch(box, "run_shell", {"command": "pytest tests/t.py"})
    assert res.ok is False and res.exit_code == 1
    assert "1 failed" in res.output
    assert res.output.endswith("[exit 1]")
    assert res.box.uuid != box.uuid


def test_truncation(log: EventLog) -> None:
    _, box, img = _setup(log)
    big = "".join(f"{i % 10}" for i in range(20_000))
    img.files["/app/big.txt"] = big.encode()
    img.files["/app/small.txt"] = b"tiny"
    d = ToolDispatcher(log, max_output=1000)
    res = d.dispatch(box, "read_file", {"path": "big.txt"})
    assert res.truncated is True
    marker = "\n…[truncated 19000 of 20000 chars]…\n"
    assert marker in res.output
    assert len(res.output) <= 1000 + len(marker)
    assert res.output.startswith(big[:500])
    assert res.output.endswith(big[-500:])
    small = d.dispatch(box, "read_file", {"path": "small.txt"})
    assert small.truncated is False and small.output == "tiny"


def test_truncate_function() -> None:
    assert tools_module.truncate("abc", 10) == ("abc", False)
    text, cut = tools_module.truncate("a" * 50 + "b" * 50, 20)
    assert cut is True
    assert text.startswith("a" * 10) and text.endswith("b" * 10)
    assert "[truncated 80 of 100 chars]" in text


def test_unknown_tool(log: EventLog) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "delete_everything", {})
    assert res.ok is False and res.exit_code is None
    assert res.name == "delete_everything"
    for name in TOOL_NAMES:
        assert name in res.output
    assert res.output.startswith("unknown tool delete_everything")
    assert res.box is box


@pytest.mark.parametrize(
    ("name", "args", "needle"),
    [
        ("read_file", {}, "path"),
        ("write_file", {"path": "a", "content": "b", "extra": 1}, "extra"),
        ("run_shell", {"command": "ls", "timeout": 9999}, "timeout"),
        ("read_file", "src/a.py", "invalid"),
        ("list_files", None, "invalid"),
    ],
)
def test_bad_args(log: EventLog, name: str, args: Any, needle: str) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, name, args)
    assert res.ok is False and res.exit_code is None
    assert res.output.startswith(f"invalid arguments for {name}")
    assert needle in res.output
    assert "https://" not in res.output
    assert res.box is box


def test_every_call_emits_tool_event(log: EventLog) -> None:
    _, box, img = _setup(log)
    img.files["/app/src/a.py"] = b"x\n"
    d = ToolDispatcher(log)
    long_cmd = "echo " + "y" * 300
    d.dispatch(box, "list_files", {"path": "src"})
    d.dispatch(box, "read_file", {"path": "src/a.py"})
    d.dispatch(box, "grep", {"pattern": "x", "path": "src"})
    d.dispatch(box, "write_file", {"path": "src/b.py", "content": "secret-content"})
    d.dispatch(box, "run_shell", {"command": long_cmd})
    d.dispatch(box, "nope", {})
    d.dispatch(box, "read_file", {})
    events = _events(log)
    tool = [e for e in events if e.kind == "tool"]
    assert len(tool) == 7
    assert [e.data["name"] for e in tool] == [
        "list_files",
        "read_file",
        "grep",
        "write_file",
        "run_shell",
        "nope",
        "read_file",
    ]
    for e in tool:
        assert e.phase == "fix"
        assert {"name", "args", "ok", "exit_code", "from", "to", "truncated"} <= set(e.data)
        assert e.ms is not None and e.ms >= 0
    assert [e.data["ok"] for e in tool] == [True, True, True, True, True, False, False]
    assert [e.data["exit_code"] for e in tool] == [0, 0, 0, 0, 0, None, None]
    write = tool[3].data
    assert write["args"] == {"path": "src/b.py", "bytes": 14}
    assert "secret-content" not in str(write)
    assert write["from"] == box.uuid and write["to"] != box.uuid
    shell = tool[4].data
    assert len(shell["args"]["command"]) <= 200
    assert shell["args"]["timeout"] == 600
    assert tool[5].data["error"].startswith("unknown tool")
    assert tool[6].data["error"].startswith("invalid arguments")
    assert tool[5].data["from"] == tool[5].data["to"] == box.uuid
    shell_idx = events.index(tool[4])
    assert events[shell_idx - 1].kind == "sandbox"
    assert events[shell_idx - 1].data["shell"].startswith("echo ")
    assert tool[0].data["args"] == {"path": "src"}


def test_calls_counter(log: EventLog) -> None:
    _, box, _ = _setup(log)
    d = ToolDispatcher(log)
    assert d.calls == 0
    d.dispatch(box, "list_files", {"path": "."})
    d.dispatch(box, "nope", {})
    d.dispatch(box, "read_file", {})
    d.dispatch(box, "run_shell", {"command": "false"})
    assert d.calls == 4


def test_as_message(log: EventLog) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "run_shell", {"command": "echo hi"})
    msg = as_message(res, "call_123")
    assert msg == {"role": "tool", "tool_call_id": "call_123", "content": res.output}


def test_tool_result_is_frozen(log: EventLog) -> None:
    _, box, _ = _setup(log)
    res = ToolDispatcher(log).dispatch(box, "list_files", {})
    assert isinstance(res, ToolResult)
    with pytest.raises(AttributeError):
        res.ok = False  # type: ignore[misc]


def test_shell_timeout_caps_and_phase(log: EventLog) -> None:
    backend, box, _ = _setup(log)
    d = ToolDispatcher(log, phase="verify", shell_timeout=30)
    d.dispatch(box, "run_shell", {"command": "echo hi", "timeout": 120})
    assert backend.timeouts[-1] == 30
    d.dispatch(box, "run_shell", {"command": "echo hi", "timeout": 5})
    assert backend.timeouts[-1] == 5
    events = _events(log)
    assert all(e.phase == "verify" for e in events if e.kind == "tool")


def test_shell_args_are_quoted(log: EventLog) -> None:
    backend, box, img = _setup(log)
    img.files["/app/src/my file.py"] = b"hello world\n"
    d = ToolDispatcher(log)
    res = d.dispatch(box, "read_file", {"path": "src/my file.py"})
    assert res.ok and res.output == "hello world\n"
    assert "'src/my file.py'" in backend.calls[-1]
    hit = d.dispatch(box, "grep", {"pattern": "hello world", "path": "src"})
    assert hit.ok and hit.output == "src/my file.py:1:hello world\n"


def test_no_contree_import() -> None:
    assert "contree" not in inspect.getsource(tools_module)
    assert not [m for m in sys.modules if m.startswith("contree")]
