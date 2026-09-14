from pathlib import Path

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
