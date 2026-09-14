import json
import subprocess
import sys
from pathlib import Path


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
