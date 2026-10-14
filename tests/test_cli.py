import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from warden import ui


def test_demo_run_json(tmp_path: Path) -> None:
    res = subprocess.run(
        [sys.executable, "-m", "warden.cli", "run", "--demo", "--json"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            "PATH": "",
            "WARDEN_RUNS_DIR": str(tmp_path / "runs"),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        },
    )
    assert res.returncode == 0, res.stderr
    lines = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    assert lines[-1]["type"] == "result"
    assert lines[-1]["verdict"] == "PASS"
    assert any(e.get("kind") == "sandbox" for e in lines[:-1])
    assert "run_id" in res.stderr


def test_help_is_fast_and_offline() -> None:
    res = subprocess.run(
        [sys.executable, "-m", "warden.cli", "--help"],
        capture_output=True,
        text=True,
        env={"PATH": "", "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert res.returncode == 0 and "doctor" in res.stdout


# ----------------------------------------------------------------------------- DX (CliRunner)
ESC = "\x1b"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WARDEN_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    ui.configure(ui.UIConfig(color=False, animate=False, debug=False))


def _invoke(
    *args: str, env: dict[str, str | None] | None = None, input: str | None = None
) -> Result:
    from warden.cli import app

    return CliRunner().invoke(app, list(args), env=env, input=input)


def _run_id(stderr: str) -> str:
    for line in stderr.splitlines():
        if line.startswith("run_id "):
            return line.split()[1]
    raise AssertionError(f"no run_id in {stderr!r}")


PHASE_PREFIXES = ("Hunt   ▸", "Plan   ▸", "Fix    ▸", "Verify ▸", "PR     ▸", "Total  ▸")


def test_demo_phase_log_on_stderr() -> None:
    res = _invoke("run", "--demo")
    assert res.exit_code == 0, res.stderr
    assert res.stdout == ""
    lines = res.stderr.splitlines()
    idx = [next(i for i, ln in enumerate(lines) if ln.startswith(p)) for p in PHASE_PREFIXES]
    assert idx == sorted(idx)
    total = lines[idx[-1]]
    assert "$1.42  PASS" in total
    assert lines[idx[1]].endswith("reading issue #201, 1 citation")  # singular for the fixture


def test_demo_json_stdout_is_jsonl() -> None:
    res = _invoke("run", "--demo", "--json")
    assert res.exit_code == 0, res.stderr
    lines = [json.loads(ln) for ln in res.stdout.splitlines() if ln.strip()]
    assert lines[-1]["type"] == "result" and lines[-1]["verdict"] == "PASS"
    assert ESC not in res.stdout and ESC not in res.stderr
    assert any(ln.startswith("Hunt   ▸") for ln in res.stderr.splitlines())


@pytest.mark.parametrize(
    ("args", "env", "esc"),
    [
        (("run", "--demo"), {"FORCE_COLOR": "1"}, True),
        (("run", "--demo"), {"FORCE_COLOR": "1", "NO_COLOR": "1"}, True),
        (("run", "--demo"), {"NO_COLOR": "1"}, False),
        (("run", "--demo"), {}, False),
        (("--plain", "run", "--demo"), {"FORCE_COLOR": "1"}, False),
        (("run", "--demo", "--plain"), {"FORCE_COLOR": "1"}, False),
    ],
)
def test_colour_env_precedence(args: tuple[str, ...], env: dict[str, str], esc: bool) -> None:
    res = _invoke(*args, env=env)
    assert res.exit_code == 0, res.stderr
    assert (ESC in res.stderr) is esc
    assert res.stdout == ""


@pytest.mark.parametrize("flag", ["--run", "--run-id"])
def test_console_run_alias(flag: str, tmp_path: Path) -> None:
    run_id = _run_id(_invoke("run", "--demo").stderr)
    res = _invoke("console", flag, run_id)
    assert res.exit_code == 0, res.stderr
    assert res.stdout.strip().endswith(f"{run_id}/index.html")
    assert Path(res.stdout.strip()).exists()


def test_run_without_issue_is_usage_error() -> None:
    res = _invoke("run")
    assert res.exit_code == 2
    assert "fix: pass --issue" in res.stderr
    assert "run_id" in res.stderr
    assert "ERROR" not in res.stderr
    assert res.stdout == ""


def test_resume_unknown_run() -> None:
    res = _invoke("resume", "nope")
    assert res.exit_code == 1
    assert "no run nope" in res.stderr
    assert "fix: ls " in res.stderr


def test_console_without_runs() -> None:
    res = _invoke("console")
    assert res.exit_code == 1
    assert "no runs yet" in res.stderr
    assert "fix: warden run --demo" in res.stderr


def test_init_detects_runner_and_never_prompts(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    res = _invoke("init", str(tmp_path))
    assert res.exit_code == 0, res.stderr
    assert 'test_cmd = "pytest -q"' in (tmp_path / "warden.toml").read_text()
    assert ".git" in res.stderr  # warning: not a git repo
    assert res.stdout == ""

    res = _invoke("init", str(tmp_path), input="y\n")
    assert res.exit_code == 1
    assert "warden.toml exists" in res.stderr and "--yes" in res.stderr
    assert res.stdout == ""

    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "package.json").write_text("{}")
    res = _invoke("init", str(tmp_path), "--yes")
    assert res.exit_code == 0, res.stderr
    assert 'test_cmd = "npm test"' in (tmp_path / "warden.toml").read_text()

    (tmp_path / ".git").mkdir()
    res = _invoke("init", str(tmp_path), "--yes")
    assert res.exit_code == 0 and ".git" not in res.stderr

    (tmp_path / "package.json").unlink()
    res = _invoke("init", str(tmp_path), "--yes")
    assert res.exit_code == 0, res.stderr
    assert 'test_cmd = ""' in (tmp_path / "warden.toml").read_text()
    assert "could not detect a test runner; set test_cmd in warden.toml" in res.stderr


def test_cost_cap_exits_4(monkeypatch: pytest.MonkeyPatch) -> None:
    import warden.demo
    from warden.llm.cost import CostCapExceededError

    def boom(*a: object, **k: object) -> None:
        raise CostCapExceededError(0.5, 0.5, "fix")

    monkeypatch.setattr(warden.demo, "run_demo", boom)
    res = _invoke("run", "--demo")
    assert res.exit_code == 4
    lines = res.stderr.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.startswith("cost cap"))
    assert lines[i] == "cost cap $0.50 reached at Fix; stopping. Raise with --max-usd"
    assert lines[i + 1].startswith("run_id ")
    assert res.stdout == ""

    res = _invoke("--debug", "run", "--demo")
    assert isinstance(res.exception, CostCapExceededError)
    assert "cost cap $0.50 reached at Fix" in res.stderr


def test_cli_import_is_light() -> None:
    heavy = {
        "openai",
        "rich",
        "pydantic",
        "pydantic_settings",
        "httpx",
        "contree_sdk",
        "contree_client",
        "tavily",
    }
    exact = {"warden.config", "warden.schemas", "warden.demo", "warden.runlog", "warden.llm"}
    code = (
        "import sys, warden.cli; print(sorted(m for m in sys.modules "
        f"if m.split('.')[0] in {heavy!r} or m in {exact!r}))"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PATH": "", "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "[]"


def test_llm_wires_meter_log(tmp_path: Path) -> None:
    from warden.config import Settings
    from warden.llm.client import LLM
    from warden.llm.cost import CostCapExceededError, CostMeter
    from warden.runlog.events import EventLog

    s = Settings()
    meter = CostMeter(cap_usd=0.0)
    log = EventLog(tmp_path / "runs")
    LLM(s, meter, log)
    assert meter.log is log
    with pytest.raises(CostCapExceededError) as ei:
        meter.charge(s.planner_model, 1_000_000, 0, "fix")
    assert str(ei.value) == "cost cap $0.00 reached at Fix; stopping. Raise with --max-usd"
    log.close()
    events = EventLog.read(tmp_path / "runs", log.run_id)
    assert any(e.kind == "cost_cap" and e.phase == "fix" for e in events)


def test_doctor_no_color() -> None:
    res = _invoke("doctor", env={"NO_COLOR": "1"})
    assert ESC not in res.stderr
    assert res.stdout == ""
    assert "Model routing" in res.stderr


def test_doctor_checks_warden_candidates() -> None:
    bad = _invoke("doctor", env={"WARDEN_CANDIDATES": "0"})
    assert "WARDEN_CANDIDATES (0)" in bad.stderr and "set WARDEN_CANDIDATES to 1-8" in bad.stderr
    good = _invoke("doctor", env={"WARDEN_CANDIDATES": "3"})
    line = next(ln for ln in good.stderr.splitlines() if "WARDEN_CANDIDATES (3)" in ln)
    assert "→" not in line


def test_eval_candidates_option_is_bounded() -> None:
    assert _invoke("eval", "--candidates", "0", "--yes").exit_code == 2
    assert "--candidates" in _invoke("eval", "--help").stdout


# ----------------------------------------------------------------------------- B6: resume


def test_resume_after_dry_run_completes(tmp_path: Path) -> None:
    run_id = _run_id(_invoke("run", "--demo", "--dry-run").stderr)
    res = _invoke("resume", run_id)
    assert res.exit_code == 0, res.stderr
    assert res.stdout == ""
    lines = res.stderr.splitlines()
    assert not any(ln.startswith("Hunt   ▸") for ln in lines)
    assert not any(ln.startswith("Plan   ▸") for ln in lines)
    assert any(ln.startswith("Fix    ▸") for ln in lines)
    total = next(ln for ln in lines if ln.startswith("Total  ▸"))
    assert "$1.42  PASS" in total
    assert lines[0] == f"run_id {run_id}   (resuming after plan)"
    result = json.loads((tmp_path / "runs" / run_id / "result.json").read_text())
    assert result["resumed_from"] == "plan" and result["verdict"] == "PASS"
    assert result["cost_usd"] == pytest.approx(1.42)
    res = _invoke("console", "--run", run_id)
    assert res.exit_code == 0, res.stderr
    assert Path(res.stdout.strip()).exists()


def test_resume_json_is_pure() -> None:
    run_id = _run_id(_invoke("run", "--demo", "--dry-run", "--json").stderr)
    res = _invoke("resume", run_id, "--json", env={"FORCE_COLOR": "1"})
    assert res.exit_code == 0, res.stderr
    lines = [json.loads(ln) for ln in res.stdout.splitlines() if ln.strip()]
    assert lines and lines[-1]["type"] == "result"
    assert lines[-1]["resumed_from"] == "plan" and lines[-1]["verdict"] == "PASS"
    assert {e["phase"] for e in lines[:-1]} == {"fix", "verify", "pr"}
    assert ESC not in res.stdout


def test_resume_complete_run_exit_2() -> None:
    run_id = _run_id(_invoke("run", "--demo").stderr)
    res = _invoke("resume", run_id)
    assert res.exit_code == 2
    assert f"run {run_id} already complete" in res.stderr
    assert f"fix: warden console --run {run_id}" in res.stderr
    assert res.stdout == ""


def test_resume_run_without_manifest(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "old"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text("")
    res = _invoke("resume", "old")
    assert res.exit_code == 1
    assert "run old predates resume" in res.stderr
    assert "fix: re-run: warden run --demo" in res.stderr


def test_run_live_exits_3_via_machine(tmp_path: Path) -> None:
    creds = {"NEBIUS_API_KEY": "x", "CONTREE_BASE_URL": "http://c", "NEBIUS_PROJECT_ID": "p"}
    res = _invoke("run", "--issue", "1", env=creds)
    assert res.exit_code == 3, res.stderr
    assert "live fix step" in res.stderr
    assert "fix: use --demo for now" in res.stderr
    run_id = _run_id(res.stderr)
    manifest = json.loads((tmp_path / "runs" / run_id / "run.json").read_text())
    assert manifest == {"mode": "live", "issue": 1}
    artifacts = tmp_path / "runs" / run_id / "artifacts"
    assert sorted(f.name for f in artifacts.iterdir()) == ["issue.json", "plan.json"]
    assert res.stdout == ""


def test_run_id_option(tmp_path: Path) -> None:
    res = _invoke("run", "--demo", "--json", "--run-id", "demo-x")
    assert res.exit_code == 0, res.stderr
    assert _run_id(res.stderr) == "demo-x"
    lines = [json.loads(ln) for ln in res.stdout.splitlines() if ln.strip()]
    assert lines[-1]["run_id"] == "demo-x"
    assert (tmp_path / "runs" / "demo-x" / "artifacts" / "pr.json").exists()
    help_out = _invoke("run", "--help").stdout
    assert "--run-id" not in help_out


def test_resume_live_keeps_manifest_and_exits_3(tmp_path: Path) -> None:
    creds = {"NEBIUS_API_KEY": "x", "CONTREE_BASE_URL": "http://c", "NEBIUS_PROJECT_ID": "p"}
    run_id = _run_id(_invoke("run", "--issue", "7", env=creds).stderr)
    manifest = tmp_path / "runs" / run_id / "run.json"
    assert json.loads(manifest.read_text()) == {"mode": "live", "issue": 7}
    res = _invoke("resume", run_id, env=creds)
    assert res.exit_code == 3, res.stderr
    assert "fix: use --demo for now" in res.stderr
    assert f"run_id {run_id}   (resuming after plan)" in res.stderr
    assert json.loads(manifest.read_text()) == {"mode": "live", "issue": 7}


def test_run_id_collision_exits_2(tmp_path: Path) -> None:
    res = _invoke("run", "--demo", "--run-id", "dup")
    assert res.exit_code == 0, res.stderr
    events = tmp_path / "runs" / "dup" / "events.jsonl"
    n = len(events.read_text().splitlines())
    res = _invoke("run", "--demo", "--run-id", "dup")
    assert res.exit_code == 2
    lines = res.stderr.splitlines()
    assert lines[0] == "run dup already exists"
    assert lines[1] == "fix: warden resume dup, or pick another --run-id"
    assert lines[2] == "run_id dup"
    assert res.stdout == ""
    assert len(events.read_text().splitlines()) == n


def test_resume_errors_carry_run_id() -> None:
    res = _invoke("resume", "nope")
    assert res.exit_code == 1 and "run_id nope" in res.stderr
    run_id = _run_id(_invoke("run", "--demo").stderr)
    res = _invoke("resume", run_id)
    assert res.exit_code == 2 and f"run_id {run_id}" in res.stderr


def test_run_at_records_the_replay_commit(tmp_path: Path) -> None:
    creds = {"NEBIUS_API_KEY": "x", "CONTREE_BASE_URL": "http://c", "NEBIUS_PROJECT_ID": "p"}
    res = _invoke("run", "--issue", "196", "--at", "43f3851^", env=creds)
    assert res.exit_code == 3, res.stderr  # offline fixture stops at fix
    run_id = _run_id(res.stderr)
    manifest = json.loads((tmp_path / "runs" / run_id / "run.json").read_text())
    assert manifest == {"mode": "live", "issue": 196, "at": "43f3851^"}
    assert "--at" in _invoke("run", "--help").stdout


def test_provider_budget_error_is_a_clean_exit_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import warden.pipeline as pipeline
    from warden.llm.client import ProviderBudgetError

    def plan(*_: object) -> object:
        raise ProviderBudgetError("Token Factory budget exhausted (HTTP 402)", fix="add funds")

    monkeypatch.setattr(pipeline, "_live_plan", plan)
    creds = {"NEBIUS_API_KEY": "x", "CONTREE_BASE_URL": "http://c", "NEBIUS_PROJECT_ID": "p"}
    res = _invoke("run", "--issue", "196", env=creds)
    assert res.exit_code == 4, res.stderr
    assert "Token Factory budget exhausted" in res.stderr
    assert "Traceback" not in res.stderr
    assert "warden resume" in res.stderr or "run_id" in res.stderr


# ------------------------------------------------------------- --no-pr / --stop-after / --repo
CREDS = {"NEBIUS_API_KEY": "x", "CONTREE_BASE_URL": "http://c", "NEBIUS_PROJECT_ID": "p"}


def test_no_pr_stops_after_verify(tmp_path: Path) -> None:
    res = _invoke("run", "--demo", "--no-pr")
    assert res.exit_code == 0, res.stderr
    artifacts = tmp_path / "runs" / _run_id(res.stderr) / "artifacts"
    assert (artifacts / "verdict.json").exists() and not (artifacts / "pr.json").exists()
    assert "stopped after verify" in res.stderr
    res = _invoke("run", "--demo", "--stop-after", "fix")
    assert res.exit_code == 0, res.stderr
    artifacts = tmp_path / "runs" / _run_id(res.stderr) / "artifacts"
    assert (artifacts / "candidate.json").exists() and not (artifacts / "verdict.json").exists()


@pytest.mark.parametrize(
    "args",
    [("--dry-run", "--no-pr"), ("--stop-after", "pr"), ("--no-pr", "--stop-after", "plan")],
)
def test_bad_or_conflicting_stops_are_usage_errors(args: tuple[str, ...]) -> None:
    res = _invoke("run", "--demo", *args)
    assert res.exit_code == 2, res.stderr
    assert res.stderr.splitlines()[1].startswith("fix: ")


def test_repo_must_be_owner_name() -> None:
    res = _invoke("run", "--issue", "1", "--repo", "icalendar", env=CREDS)
    assert res.exit_code == 2 and "is not owner/name" in res.stderr
    res = _invoke("run", "--issue", "1", "--install-cmd", "pip install .", env=CREDS)
    assert res.exit_code == 2 and "--install-cmd needs --repo" in res.stderr


def test_run_repo_builds_the_base_and_resume_reuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import warden.pipeline as pipeline
    import warden.sandbox.box as box_mod
    import warden.sandbox.external as external
    from tests.test_external import ICAL_PYPROJECT, SHA, RepoBackend

    backend = RepoBackend({"pyproject.toml": ICAL_PYPROJECT})
    monkeypatch.setattr(box_mod, "ContreeBackend", lambda *a: backend)
    monkeypatch.setattr(
        external, "remote_head", lambda repo: external.RepoHead(sha=SHA, branch="main")
    )
    seen: list[Any] = []

    def fix(ctx: pipeline.StepContext, _plan: object) -> object:
        seen.append(ctx.settings)
        raise pipeline.NotReadyError("offline fix", fix="none")

    monkeypatch.setattr(pipeline, "_live_fix", fix)
    res = _invoke(
        "run", "--issue", "1928", "--repo", "collective/icalendar", "--no-pr",
        "--test-cmd", "python -m pytest -x", env=CREDS,
    )  # fmt: skip
    assert res.exit_code == 3, res.stderr
    assert f"base: collective/icalendar@{SHA[:7]} (main)" in res.stderr
    run_id = _run_id(res.stderr)
    base = json.loads((tmp_path / "runs" / run_id / "base.json").read_text())
    assert base["repo"] == "collective/icalendar" and base["sha"] == SHA
    s = seen[-1]
    assert (s.target_repo, s.base_image, s.workdir, s.gate_profile, s.shell_wrap) == (
        "collective/icalendar", base["image"], "/src", "swe", "sh -c",
    )  # fmt: skip
    assert s.test_cmd == "python -m pytest -x" and s.base_branch == "main"
    assert (tmp_path / "runs" / ".bases.json").exists()

    n = len(backend.calls)
    res = _invoke("resume", run_id, env=CREDS)
    assert res.exit_code == 3, res.stderr
    assert seen[-1].target_repo == "collective/icalendar"
    assert seen[-1].base_image == base["image"] and seen[-1].test_cmd == "python -m pytest -x"
    assert len(backend.calls) == n  # resume never rebuilds

    res = _invoke("run", "--issue", "1928", "--repo", "collective/icalendar", env=CREDS)
    assert "(cached)" in res.stderr and len(backend.calls) == n


def test_run_repo_base_failure_exits_3_with_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import warden.sandbox.box as box_mod
    import warden.sandbox.external as external
    from tests.test_external import ICAL_PYPROJECT, SHA, RepoBackend

    backend = RepoBackend({"pyproject.toml": ICAL_PYPROJECT}, install_rc=1)
    monkeypatch.setattr(box_mod, "ContreeBackend", lambda *a: backend)
    monkeypatch.setattr(
        external, "remote_head", lambda repo: external.RepoHead(sha=SHA, branch="main")
    )
    res = _invoke("run", "--issue", "1", "--repo", "o/r", env=CREDS)
    assert res.exit_code == 3, res.stderr
    assert "install failed for o/r" in res.stderr and "fix: pass --install-cmd" in res.stderr
    assert "Traceback" not in res.stderr


def test_doctor_reports_the_new_settings(tmp_path: Path) -> None:
    res = _invoke("doctor", env={"WARDEN_PR_TEMPLATE": str(tmp_path / "missing.md")})
    assert "WARDEN_TEST_CMD" in res.stderr and "WARDEN_BASE_BRANCH (main)" in res.stderr
    line = next(ln for ln in res.stderr.splitlines() if "WARDEN_PR_TEMPLATE" in ln)
    assert "✗" in line and "point WARDEN_PR_TEMPLATE at a markdown file" in line
