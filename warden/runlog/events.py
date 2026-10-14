"""Append-only run log: runs/<run_id>/events.jsonl (+ cost.jsonl).

The console renders these files; the CLI's --json mode streams the same events to stdout.
"""

from __future__ import annotations

import copy
import json
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from warden.schemas import Phase, RunEvent


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


class EventLog:
    def __init__(
        self, runs_dir: Path, run_id: str | None = None, *, json_stream: TextIO | None = None
    ) -> None:
        self.run_id = run_id or new_run_id()
        self.dir = runs_dir / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "artifacts").mkdir(exist_ok=True)
        self._events = (self.dir / "events.jsonl").open("a", encoding="utf-8")
        self._cost = (self.dir / "cost.jsonl").open("a", encoding="utf-8")
        self._json_stream = json_stream
        self.tags: dict[str, Any] = {}
        self._artifacts = self.dir / "artifacts"

    def tagged(self, *, candidate: int) -> EventLog:
        """A view on the same files whose events carry `candidate: k` (best-of-N) and whose
        artifacts land in artifacts/candidate-<k>/, so N verifications never overwrite each
        other. The view shares the open files: close the parent, not the view."""
        view = copy.copy(self)
        view.tags = {**self.tags, "candidate": candidate}
        view._artifacts = self.dir / "artifacts" / f"candidate-{candidate}"
        view._artifacts.mkdir(parents=True, exist_ok=True)
        return view

    def emit(
        self,
        phase: Phase,
        kind: str,
        data: dict[str, Any] | None = None,
        *,
        ms: int | None = None,
        usd: float | None = None,
    ) -> RunEvent:
        payload = {**(data or {}), **self.tags} if self.tags else data or {}
        ev = RunEvent(phase=phase, kind=kind, data=payload, ms=ms, usd=usd)  # type: ignore[arg-type]
        line = ev.model_dump_json()
        self._events.write(line + "\n")
        self._events.flush()
        if usd is not None:
            self._cost.write(
                json.dumps({"ts": ev.ts.isoformat(), "phase": phase, "usd": usd, **ev.data}) + "\n"
            )
            self._cost.flush()
        if self._json_stream is not None:
            self._json_stream.write(line + "\n")
            self._json_stream.flush()
        return ev

    def note(self, phase: Phase, text: str) -> None:
        self.emit(phase, "note", {"text": text})

    def artifact_path(self, name: str) -> Path:
        """Where `artifact(name, …)` writes (artifacts/, or artifacts/candidate-<k>/ on a view)."""
        return self._artifacts / name

    def artifact(self, name: str, content: str | bytes) -> Path:
        path = self._artifacts / name
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)
        return path

    def result(self, **fields: Any) -> None:
        """Final line for --json consumers; always to stdout."""
        payload = {"type": "result", "run_id": self.run_id, **fields}
        (self.dir / "result.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if self._json_stream is not None:
            self._json_stream.write(json.dumps(payload) + "\n")
            self._json_stream.flush()

    def close(self) -> None:
        """Idempotent: closing twice is safe (the CLI closes on both the happy and error paths)."""
        for f in (self._events, self._cost):
            if not f.closed:
                f.close()

    @staticmethod
    def read(runs_dir: Path, run_id: str) -> list[RunEvent]:
        path = runs_dir / run_id / "events.jsonl"
        if not path.exists():
            return []
        return [
            RunEvent.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    @staticmethod
    def read_result(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
        path = runs_dir / run_id / "result.json"
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data


def phase_spans(events: list[RunEvent]) -> dict[Phase, tuple[datetime, datetime]]:
    """Per phase: (first ts, last ts + last event's ms). Phases with no events are omitted."""
    spans: dict[Phase, tuple[datetime, datetime]] = {}
    for ev in events:
        end = ev.ts + timedelta(milliseconds=ev.ms or 0)
        if ev.phase in spans:
            start, _ = spans[ev.phase]
            spans[ev.phase] = (min(start, ev.ts), end)
        else:
            spans[ev.phase] = (ev.ts, end)
    return spans


def stdout_stream() -> TextIO:
    return sys.stdout
