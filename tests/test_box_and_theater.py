from __future__ import annotations

from pathlib import Path

import pytest

from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.sandbox.fake import FakeBackend
from warden.verify.theater import theater_check


def _log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "runs", "t")


def test_fork_semantics(tmp_path: Path) -> None:
    b = FakeBackend()
    base = Box.from_ref(b, "ubuntu:latest", _log(tmp_path))
    ck = base.run("echo seed > /tmp/r").box
    assert ck is not None
    a = ck.run("echo A >> /tmp/r").box
    bb = ck.run("echo B >> /tmp/r").box
    assert a is not None and bb is not None
    assert a.read_file("/tmp/r") == "seed\nA\n"
    assert bb.read_file("/tmp/r") == "seed\nB\n"
    assert ck.read_file("/tmp/r") == "seed\n"  # parent untouched


def test_theater_check_detects_real_and_fake_tests(tmp_path: Path) -> None:
    b = FakeBackend()
    log = _log(tmp_path)
    base = Box.from_ref(b, "warden/fabric-base:latest", log)
    new = {"tests/test_new.py": b"..."}
    b.images[base.uuid].test_results["tests/test_new.py"] = 1  # fails on base → real test
    assert theater_check(base, new).failed_on_base is True
    b.images[base.uuid].test_results["tests/test_new.py"] = 0  # passes on base → theater
    assert theater_check(base, new).failed_on_base is False


def test_events_are_logged(tmp_path: Path) -> None:
    b = FakeBackend()
    log = _log(tmp_path)
    Box.from_ref(b, "x", log).run("true")
    log.close()
    events = EventLog.read(tmp_path / "runs", "t")
    assert events and events[-1].kind == "sandbox"


class _StubImage:
    """Mimics contree_sdk lazy images: run() prepares, wait() executes, returns the result."""

    def __init__(self, uuid: str = "base", calls: list[dict[str, object]] | None = None) -> None:
        self.uuid = uuid
        self.calls = calls if calls is not None else []
        self.stdout = ""
        self.stderr = ""
        self.exit_code = 0

    def run(self, **kwargs: object) -> _StubImage:
        self.calls.append(kwargs)
        child = _StubImage(f"child{len(self.calls)}", self.calls)
        child.stdout, child.exit_code = "hi\n", 0
        return child

    def wait(self) -> _StubImage:
        return self


def test_contree_backend_forwards_timeout_and_disposable() -> None:
    from warden.sandbox.box import ContreeBackend

    backend = ContreeBackend.__new__(ContreeBackend)
    base = _StubImage()
    out, _err, code, child, child_uuid = backend.run(
        base, shell="echo hi", cwd="/app", env=None, timeout=42, files=None, keep=True
    )
    assert (out, code, child_uuid) == ("hi\n", 0, "child1")
    assert child is not None
    assert base.calls[0]["timeout"] == 42
    assert base.calls[0]["disposable"] is False

    _, _, _, child2, uuid2 = backend.run(
        base, shell="true", cwd="/app", env=None, timeout=5, files=None, keep=False
    )
    assert base.calls[1]["disposable"] is True
    assert child2 is None
    assert uuid2 == ""


def test_contree_backend_builds_iam_config_with_project(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    seen: dict[str, object] = {}

    class FakeIAMAuth:
        def __init__(self, **kw: object) -> None:
            seen.update(kw)

    class FakeConfig:
        def __init__(self, auth: object) -> None:
            seen["auth"] = auth

    class FakeSync:
        def __init__(self, config: object) -> None:
            seen["config"] = config

    mods = {
        "contree_sdk": types.SimpleNamespace(ContreeSync=FakeSync),
        "contree_sdk.auth": types.SimpleNamespace(IAMAuth=FakeIAMAuth),
        "contree_sdk.config": types.SimpleNamespace(ContreeConfig=FakeConfig),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)

    from warden.sandbox.box import ContreeBackend

    ContreeBackend("k", "https://sb", "proj-1")
    assert seen["token"] == "k"
    assert seen["project_id"] == "proj-1"
    assert seen["base_url"] == "https://sb"
    assert isinstance(seen["config"], FakeConfig)


def test_contree_backend_resolves_tags_strictly_so_events_carry_uuids() -> None:
    from types import SimpleNamespace

    from warden.sandbox.box import ContreeBackend

    calls: list[tuple[str, dict[str, object]]] = []

    def use(ref: str, **kw: object) -> str:
        calls.append((ref, kw))
        return "img"

    backend = ContreeBackend.__new__(ContreeBackend)
    backend._sdk = SimpleNamespace(images=SimpleNamespace(use=use, oci=lambda ref: "oci"))
    assert backend.use("warden/fabric-base:latest") == "img"
    assert calls == [("warden/fabric-base:latest", {"strict": True})]
    assert backend.use("docker://ghcr.io/x/y:1") == "oci"


def test_contree_timeout_is_exit_124_not_an_exception() -> None:
    from warden.sandbox.box import TIMEOUT_EXIT, ContreeBackend

    class OperationTimedOutError(Exception):  # stands in for contree_sdk's (never imported here)
        pass

    class _Image:
        def run(self, **kw: object) -> _Image:
            return self

        def wait(self) -> None:
            raise OperationTimedOutError("op has timed out")

    backend = ContreeBackend.__new__(ContreeBackend)  # no SDK client needed for run()
    for keep in (False, True):
        out = backend.run(
            _Image(), shell="sleep 99", cwd="/", env=None, timeout=10, files=None, keep=keep
        )
        assert out == ("", "warden: command timed out after 10 s", TIMEOUT_EXIT, None, "")


def test_other_contree_errors_still_raise() -> None:
    from warden.sandbox.box import ContreeBackend

    class _Image:
        def run(self, **kw: object) -> _Image:
            return self

        def wait(self) -> None:
            raise ValueError("boom")

    backend = ContreeBackend.__new__(ContreeBackend)
    with pytest.raises(ValueError):
        backend.run(_Image(), shell="x", cwd="/", env=None, timeout=1, files=None, keep=False)
