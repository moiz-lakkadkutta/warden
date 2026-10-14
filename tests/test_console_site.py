"""E3 console site + timeline GIF, replayed from fixture events.jsonl (no sandbox, no models)."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from warden import ui
from warden.runlog.events import EventLog
from warden.runlog.gif import frame_states, position, render_gif, segments
from warden.runlog.render import build_tree, event_text, load_run, render_view
from warden.runlog.site import build_site, list_runs, render_index
from warden.schemas import RunEvent

T0 = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
PR_BODY = """## Warden evidence — #196 KPIs count <DRAFT> invoices

**Verdict: PASS**

| Check | Result |
|---|---|
| New tests on unfixed base | FAIL — as required |
| Full suite on candidate | 1508 passed, 0 failed |
| Risk class | money → **PENDING-HUMAN** |
"""


def _ev(sec: float, phase: str, kind: str, data: dict[str, Any], **kw: Any) -> RunEvent:
    return RunEvent(ts=T0 + timedelta(seconds=sec), phase=phase, kind=kind, data=data, **kw)  # type: ignore[arg-type]


def live_shape() -> list[RunEvent]:
    """The shape of the live #196 replay: wrapped pytest commands, UUID checkpoints, theater
    probe run on a child of its checkout node."""
    w = "with-services sh -c '{}'"
    return [
        _ev(0, "hunt", "note", {"text": "hunt done → artifacts/issue.json", "phase_done": "hunt"}),
        _ev(1, "plan", "note", {"grep_terms": ["sales_today", "gst_collected_mtd"]}),
        _ev(2, "plan", "tool", {"name": "tavily.search", "query": "sqlalchemy in_", "urls": ["u"]}),
        _ev(
            3,
            "plan",
            "llm",
            {
                "role": "planner",
                "model": "nvidia/Nemotron-3-Ultra",
                "prompt_tokens": 900,
                "completion_tokens": 120,
            },
            ms=4000,
            usd=0.02,
        ),
        _ev(
            10,
            "fix",
            "sandbox",
            {"shell": "true", "exit_code": 0, "from": "base-0000-uuid", "to": "cand-1111-uuid"},
            ms=500,
        ),
        _ev(
            11,
            "fix",
            "tool",
            {
                "name": "read_file",
                "args": {"path": "app.py", "start_line": 1},
                "ok": False,
                "exit_code": None,
                "error": "no such file",
            },
            ms=200,
        ),
        _ev(
            12,
            "fix",
            "llm",
            {
                "role": "implementer",
                "model": "nvidia/nemotron-3-super",
                "prompt_tokens": 5000,
                "completion_tokens": 300,
            },
            ms=9000,
            usd=0.11,
        ),
        _ev(
            30,
            "fix",
            "sandbox",
            {
                "shell": w.format("cd backend && pytest -q tests"),
                "exit_code": 0,
                "from": "cand-1111-uuid",
                "to": "",
            },
            ms=6000,
        ),
        _ev(
            40,
            "verify",
            "sandbox",
            {
                "shell": "git checkout -q --detach 'abc^'",
                "exit_code": 0,
                "from": "base-0000-uuid",
                "to": "thea-2222-uuid",
            },
            ms=900,
        ),
        _ev(
            41,
            "verify",
            "sandbox",
            {"shell": "true", "exit_code": 0, "from": "thea-2222-uuid", "to": "thea-3333-uuid"},
            ms=300,
        ),
        _ev(
            42,
            "verify",
            "sandbox",
            {
                "shell": w.format("pytest -q -rA tests/test_new.py"),
                "exit_code": 1,
                "from": "thea-3333-uuid",
                "to": "",
            },
            ms=4000,
        ),
        _ev(
            50,
            "pr",
            "tool",
            {
                "name": "gh",
                "args": ["pr", "create", "--draft", "--repo", "o/r"],
                "exit_code": 0,
                "from": "",
                "to": "",
            },
        ),
    ]


def _write_run(runs: Path, run_id: str, events: list[RunEvent], *, mode: str = "live") -> None:
    d = runs / run_id
    (d / "artifacts").mkdir(parents=True)
    (d / "events.jsonl").write_text("".join(ev.model_dump_json() + "\n" for ev in events))
    (d / "cost.jsonl").write_text("")
    (d / "run.json").write_text(json.dumps({"mode": mode, "issue": 196}))
    (d / "result.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "issue": 196,
                "verdict": "PASS",
                "cost_usd": 0.13,
                "pr_url": "https://github.com/o/r/pull/221",
            }
        )
    )
    (d / "artifacts" / "issue.json").write_text(
        json.dumps({"issue": 196, "title": "KPIs count <DRAFT> invoices"})
    )
    (d / "artifacts" / "pr_body.md").write_text(PR_BODY)
    (d / "artifacts" / "verdict.json").write_text(
        json.dumps(
            {
                "decision": "PASS",
                "reasons": ["full suite green (1508 passed)"],
                "not_verified": ["frontend codegen"],
            }
        )
    )
    (d / "artifacts" / "gate.txt").write_text("SECRET-ish gate output\n")


# --- event lines -----------------------------------------------------------------------------


def test_event_text_covers_live_kinds() -> None:
    evs = live_shape()
    assert event_text(evs[1]) == "grep_terms=sales_today gst_collected_mtd"
    assert event_text(evs[2]) == 'tavily.search "sqlalchemy in_" → 1 url'
    assert "planner · Nemotron-3-Ultra · 900+120 tok · $0.0200" in event_text(evs[3])
    assert event_text(evs[4]) == "sandbox base-000 → cand-111 · true → exit 0 · 500 ms"
    assert event_text(evs[5]) == "read_file(path=app.py, start_line=1) · no such file"
    assert event_text(evs[11]) == "gh(pr create --draft --repo o/r) → exit 0"
    err = _ev(0, "verify", "error", {"task": "x", "error": "Operation timed out"})
    assert event_text(err) == "Operation timed out"


def test_log_rows_have_offsets_and_failed_tools_marked() -> None:
    page = render_view(load_run_from(live_shape()))
    assert "<span class='t'>+00:50</span>" in page
    assert "<div class='ev warn'>" in page


def load_run_from(events: list[RunEvent]) -> Any:
    from warden.runlog.render import RunView

    return RunView("r", events, None, PR_BODY, "KPIs <DRAFT>", None)


# --- branch tree on the live shape -----------------------------------------------------------


def test_tree_finds_wrapped_pytest_and_theater_child() -> None:
    tree = build_tree(live_shape())
    assert (tree.base, tree.candidate, tree.theater) == (
        "base-0000-uuid",
        "cand-1111-uuid",
        "thea-2222-uuid",
    )
    cand = tree.test_run("cand-1111-uuid")
    assert cand is not None and cand.exit_code == 0
    probe = tree.test_run("thea-2222-uuid")  # ran on thea-3333, a child of the checkout
    assert probe is not None and probe.exit_code == 1
    page = render_view(load_run_from(live_shape()))
    assert "failed on unfixed base — as required" in page
    assert "not run" not in page


def test_pytest_match_is_a_word() -> None:
    evs = [
        _ev(0, "fix", "sandbox", {"shell": "true", "exit_code": 0, "from": "a", "to": "b"}),
        _ev(
            1, "fix", "sandbox", {"shell": "cat pytest.ini", "exit_code": 0, "from": "b", "to": ""}
        ),
        _ev(2, "fix", "sandbox", {"shell": "mypytest", "exit_code": 0, "from": "b", "to": ""}),
    ]
    assert build_tree(evs).test_run("b") is None


# --- GIF layout (pure) -----------------------------------------------------------------------


def test_segments_cover_the_bar_and_keep_idle_phases_visible() -> None:
    segs = segments(live_shape())
    assert [s.phase for s in segs] == ["hunt", "plan", "fix", "verify", "pr"]
    assert segs[0].x0 == 0 and segs[-1].x1 == pytest.approx(1.0)
    assert all(s.x1 - s.x0 >= 0.07 - 1e-9 for s in segs)
    fix = next(s for s in segs if s.phase == "fix")
    assert fix.x1 - fix.x0 == max(s.x1 - s.x0 for s in segs)  # longest phase, widest segment
    empty = segments([])
    assert [round(s.x1 - s.x0, 6) for s in empty] == [0.2] * 5


def test_positions_stay_inside_their_phase() -> None:
    evs = live_shape()
    segs = segments(evs)
    for ev in evs:
        seg = next(s for s in segs if s.phase == ev.phase)
        assert seg.x0 <= position(ev, segs) <= seg.x1


def test_frame_states_accumulate_to_the_whole_run() -> None:
    evs = live_shape()
    states = frame_states(evs, 20)
    assert len(states) == 20
    counts = [len(s.seen) for s in states]
    assert counts == sorted(counts)
    assert counts[-1] == len(evs)
    assert states[-1].usd == pytest.approx(0.13)
    assert states[-1].phase_usd["fix"] == pytest.approx(0.11)
    assert states[0].elapsed_ms <= states[-1].elapsed_ms


# --- GIF rendering ---------------------------------------------------------------------------


def test_render_gif_frames_hold_and_determinism(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    a = render_gif(
        live_shape(),
        tmp_path / "a.gif",
        run_id="r",
        title="t",
        verdict="PASS",
        frames=12,
        hold_ms=2500,
    )
    b = render_gif(
        live_shape(),
        tmp_path / "b.gif",
        run_id="r",
        title="t",
        verdict="PASS",
        frames=12,
        hold_ms=2500,
    )
    data = a.read_bytes()
    assert data[:6] == b"GIF89a"
    assert data == b.read_bytes()  # reproducible from the log alone
    with pil.open(a) as im:
        assert im.size == (960, 420)
        assert im.n_frames == 12
        im.seek(11)
        assert im.info["duration"] == 2500


def test_render_gif_without_pillow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from warden.runlog.gif import GifUnavailableError

    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(GifUnavailableError):
        render_gif(live_shape(), tmp_path / "x.gif", run_id="r")


# --- site ------------------------------------------------------------------------------------


def test_build_site_writes_pages_and_only_linked_files(tmp_path: Path) -> None:
    runs, out = tmp_path / "runs", tmp_path / "site"
    _write_run(runs, "20261010-000000-aaaa", live_shape())
    _write_run(runs, "20260914-000000-bbbb", live_shape(), mode="demo")
    assert list_runs(runs) == ["20260914-000000-bbbb", "20261010-000000-aaaa"]
    assert build_site(runs, list_runs(runs), out, gif=False) == []

    run = out / "20261010-000000-aaaa"
    assert sorted(p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file()) == [
        "artifacts/pr_body.md",
        "cost.jsonl",
        "events.jsonl",
        "index.html",
    ]  # gate.txt, verdict.json etc. stay local
    assert (out / ".nojekyll").exists()

    page = (run / "index.html").read_text()
    assert '<a href="../index.html">all runs</a>' in page
    assert "timeline.gif" not in page
    assert "KPIs count &lt;DRAFT&gt; invoices" in page and "<DRAFT>" not in page
    assert 'href="https://github.com/o/r/pull/221"' in page
    assert "<h2>Verifier</h2>" in page and "frontend codegen" in page
    assert "<script" not in page and "src=" not in page  # one file, nothing fetched

    index = (out / "index.html").read_text()
    assert index.index("20261010-000000-aaaa") < index.index("20260914-000000-bbbb")
    assert 'href="20261010-000000-aaaa/index.html"' in index
    assert "demo · fake sandbox" in index and "live · Nebius" in index
    assert 'data-verdict="PENDING-HUMAN"' in index
    assert "<script" not in index and "src=" not in index


def test_build_site_with_gif(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    runs, out = tmp_path / "runs", tmp_path / "site"
    _write_run(runs, "r1", live_shape())
    assert build_site(runs, ["r1"], out) == []
    assert (runs / "r1" / "timeline.gif").read_bytes()[:6] == b"GIF89a"
    assert (out / "r1" / "timeline.gif").exists()
    assert '<a href="timeline.gif">timeline.gif</a>' in (out / "r1" / "index.html").read_text()


def test_build_site_warns_once_without_pillow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "PIL", None)
    runs, out = tmp_path / "runs", tmp_path / "site"
    _write_run(runs, "r1", live_shape())
    _write_run(runs, "r2", live_shape())
    warnings = build_site(runs, ["r1", "r2"], out)
    assert len(warnings) == 1 and "uv sync --extra gif" in warnings[0]
    assert (out / "r2" / "index.html").exists()


def test_index_empty_state() -> None:
    assert "no runs yet — <code>warden run --demo</code>" in render_index([])


def test_load_run_tolerates_missing_artifacts(tmp_path: Path) -> None:
    log = EventLog(tmp_path, "bare")
    log.note("hunt", "hi")
    log.close()
    view = load_run(tmp_path, "bare")
    assert (view.title, view.verdict, view.result, view.mode) == (None, None, None, None)
    assert view.label == "RUNNING"


# --- CLI -------------------------------------------------------------------------------------


@pytest.fixture
def cli_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WARDEN_RUNS_DIR", str(tmp_path / "runs"))
    ui.configure(ui.UIConfig(color=False, animate=False, debug=False))
    _write_run(tmp_path / "runs", "r1", live_shape())
    _write_run(tmp_path / "runs", "r2", live_shape())
    return tmp_path / "runs"


def _invoke(*args: str) -> Any:
    from warden.cli import app

    return CliRunner().invoke(app, list(args))


def test_cli_console_out_builds_site(cli_runs: Path, tmp_path: Path) -> None:
    res = _invoke("console", "r1", "--out", "site", "--no-gif")
    assert res.exit_code == 0, res.stderr
    assert res.stdout.strip() == str(Path("site") / "index.html")
    assert (tmp_path / "site" / "r1" / "index.html").exists()
    assert not (tmp_path / "site" / "r2").exists()
    assert "wrote 1 run page(s)" in res.stderr


def test_cli_console_all_runs(cli_runs: Path, tmp_path: Path) -> None:
    res = _invoke("console", "--all", "--out", "site", "--no-gif")
    assert res.exit_code == 0, res.stderr
    assert {p.name for p in (tmp_path / "site").iterdir() if p.is_dir()} == {"r1", "r2"}


def test_cli_console_default_latest_writes_gif(cli_runs: Path) -> None:
    pytest.importorskip("PIL")
    res = _invoke("console")
    assert res.exit_code == 0, res.stderr
    assert res.stdout.strip().endswith("r2/index.html")
    assert (cli_runs / "r2" / "timeline.gif").exists()
    assert "timeline.gif" in res.stderr


def test_cli_console_unknown_run(cli_runs: Path) -> None:
    res = _invoke("console", "nope", "--out", "site")
    assert res.exit_code == 2
    assert "no run nope" in res.stderr
