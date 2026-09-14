"""examples/demo-issue-201 is a recorded run; the demo steps must reproduce it offline."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from warden.config import Settings
from warden.demo import demo_steps
from warden.pipeline import run_pipeline
from warden.runlog.events import EventLog
from warden.runlog.render import render_run

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE = EXAMPLES / "demo-issue-201"
RUN_ID = EXAMPLE.name


def normalise(run_dir: Path) -> dict[str, Any]:
    """The run tree minus what legitimately differs between recordings."""
    events = []
    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        if line:
            ev = json.loads(line)
            del ev["ts"]
            if ev["ms"] is not None:
                ev["ms"] = 0
            events.append(ev)
    cost = []
    for line in (run_dir / "cost.jsonl").read_text(encoding="utf-8").splitlines():
        if line:
            row = json.loads(line)
            del row["ts"]
            cost.append(row)
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    del result["run_id"]
    artifacts = {
        p.name: p.read_text(encoding="utf-8") for p in sorted((run_dir / "artifacts").iterdir())
    }
    return {
        "run.json": json.loads((run_dir / "run.json").read_text(encoding="utf-8")),
        "events": events,
        "cost": cost,
        "result": result,
        "artifacts": artifacts,
    }


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in list(os.environ):
        if key.startswith("WARDEN_"):
            monkeypatch.delenv(key)
    return Settings(_env_file=None, WARDEN_RUNS_DIR=str(tmp_path / "runs"))


def test_replay_matches_example(settings: Settings) -> None:
    log = EventLog(settings.runs_dir, RUN_ID)
    run_pipeline(
        settings, log, demo_steps(), start_after=None, json_out=False, yes=False, progress=None
    )
    log.close()
    assert normalise(settings.runs_dir / RUN_ID) == normalise(EXAMPLE)


def test_resume_replay_matches_example(settings: Settings) -> None:
    log = EventLog(settings.runs_dir, RUN_ID)
    with pytest.raises(RuntimeError, match="simulated crash after plan"):
        run_pipeline(
            settings,
            log,
            demo_steps(crash_after="plan"),
            start_after=None,
            json_out=False,
            yes=False,
            progress=None,
        )
    log.close()
    log2 = EventLog(settings.runs_dir, RUN_ID)
    run_pipeline(
        settings, log2, demo_steps(), start_after="plan", json_out=False, yes=False, progress=None
    )
    log2.close()
    got, want = normalise(settings.runs_dir / RUN_ID), normalise(EXAMPLE)
    assert got["artifacts"] == want["artifacts"]
    assert got["result"].pop("resumed_from") == "plan"
    assert want["result"].pop("resumed_from") is None
    assert got["result"] == want["result"]


def test_example_has_no_secrets() -> None:
    needles = ["sk-", "ghp_", "tvly-", "Bearer ", "API_KEY="]
    for var in ("NEBIUS_API_KEY", "TAVILY_API_KEY", "GITHUB_TOKEN"):
        value = os.environ.get(var, "")
        if value:
            needles.append(value)
    files = [p for p in EXAMPLES.rglob("*") if p.is_file()]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, f"{path.relative_to(EXAMPLES)} contains {needle[:6]}…"


def test_example_renders(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    shutil.copytree(EXAMPLE, runs / RUN_ID)
    out = render_run(runs, RUN_ID)
    html = out.read_text(encoding="utf-8")
    assert out.name == "index.html"
    for row in ("New tests on unfixed base", "Full suite on candidate", "Race probe", "Cost"):
        assert row in html
    assert "PENDING-HUMAN" in html and "$1.42" in html
