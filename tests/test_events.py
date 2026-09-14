import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from warden.runlog.events import EventLog, phase_spans
from warden.schemas import RunEvent


def test_emit_round_trips_through_read(tmp_path: Path) -> None:
    log = EventLog(tmp_path, "t")
    a = log.emit("hunt", "note", {"text": "x"})
    b = log.emit("fix", "llm", {"model": "m"}, ms=12, usd=0.25)
    log.close()
    got = EventLog.read(tmp_path, "t")
    assert [(e.phase, e.kind, e.data, e.ms, e.usd) for e in got] == [
        (a.phase, a.kind, a.data, a.ms, a.usd),
        (b.phase, b.kind, b.data, b.ms, b.usd),
    ]


def test_usd_event_writes_cost_jsonl(tmp_path: Path) -> None:
    log = EventLog(tmp_path, "t")
    log.emit("plan", "llm", {"model": "m"}, usd=0.5)
    log.emit("plan", "note", {"text": "free"}, usd=None)
    log.close()
    lines = [
        json.loads(line)
        for line in (tmp_path / "t" / "cost.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(lines) == 1
    assert lines[0]["phase"] == "plan"
    assert lines[0]["usd"] == 0.5


def test_json_stream_mirrors_events_and_result(tmp_path: Path) -> None:
    stream = io.StringIO()
    log = EventLog(tmp_path, "t", json_stream=stream)
    log.emit("hunt", "note", {"text": "a"})
    log.emit("plan", "note", {"text": "b"})
    log.result(verdict="PASS")
    log.close()
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 3
    assert lines[0]["kind"] == "note" and lines[1]["phase"] == "plan"
    assert lines[2]["type"] == "result"
    assert lines[2]["run_id"] == "t"
    assert lines[2]["verdict"] == "PASS"


def test_read_result_none_when_missing(tmp_path: Path) -> None:
    log = EventLog(tmp_path, "t")
    assert EventLog.read_result(tmp_path, "t") is None
    log.result(verdict="PASS", cost_usd=1.0)
    log.close()
    res = EventLog.read_result(tmp_path, "t")
    assert res is not None
    assert res["run_id"] == "t"
    assert res["verdict"] == "PASS"


def test_phase_spans_uses_last_ms() -> None:
    t0 = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    events = [
        RunEvent(ts=t0, phase="fix", kind="note", data={"text": "start"}),
        RunEvent(ts=t0 + timedelta(seconds=2), phase="fix", kind="sandbox", data={}, ms=500),
    ]
    spans = phase_spans(events)
    assert set(spans) == {"fix"}
    start, end = spans["fix"]
    assert start == t0
    assert end == t0 + timedelta(seconds=2.5)


def test_phase_spans_without_ms_ends_at_last_ts() -> None:
    t0 = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
    events = [
        RunEvent(ts=t0, phase="hunt", kind="note", data={}),
        RunEvent(ts=t0 + timedelta(seconds=1), phase="hunt", kind="note", data={}),
    ]
    assert phase_spans(events)["hunt"] == (t0, t0 + timedelta(seconds=1))


def test_close_is_idempotent(tmp_path: Path) -> None:
    log = EventLog(tmp_path, "t")
    log.close()
    log.close()
