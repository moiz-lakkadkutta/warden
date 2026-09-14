import json
import subprocess
import sys
from pathlib import Path

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
