from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.test_swe_rebench import ROW
from warden.cli import app
from warden.eval import shard
from warden.eval import swe_rebench as swe


def test_parse_shard() -> None:
    assert shard.parse_shard("0/5") == (0, 5)
    assert shard.parse_shard(" 4 / 5 ") == (4, 5)
    for bad in ("5/5", "1/0", "a/b", "1", "-1/3"):
        with pytest.raises(ValueError):
            shard.parse_shard(bad)


def test_shards_partition_the_task_list() -> None:
    items = list(range(50))
    parts = [shard.take(items, i, 5) for i in range(5)]
    assert sorted(x for p in parts for x in p) == items
    assert all(len(p) == 10 for p in parts)
    assert parts[1][:3] == [1, 6, 11]


def test_csv_name() -> None:
    assert shard.csv_name(None) == "results.csv"
    assert shard.csv_name((3, 5)) == "results.shard-3.csv"


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "NEBIUS_API_KEY": "k",
        "CONTREE_BASE_URL": "https://x",
        "NEBIUS_PROJECT_ID": "p",
    }.items():
        monkeypatch.setenv(k, v)


def test_cli_eval_shard_runs_its_slice_into_its_own_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    out = tmp_path / "e1"
    out.mkdir()
    ids = [f"o__r-{i}" for i in range(7)]
    (out / "tasks.jsonl").write_text(
        "".join(json.dumps({**ROW, "instance_id": t}) + "\n" for t in ids)
    )
    (out / "excluded.txt").write_text("o__r-4\tbase already passes\n")
    seen: dict[str, Any] = {}

    def fake_run_eval(tasks: list[swe.Task], s: Any, *, csv_path: Path, **kw: Any) -> list[Any]:
        seen["ids"] = [t.instance_id for t in tasks]
        seen["csv"] = csv_path
        return []

    monkeypatch.setattr(swe, "run_eval", fake_run_eval)
    res = CliRunner().invoke(app, ["eval", "--out", str(out), "--shard", "1/3", "--yes"])
    assert res.exit_code == 0, res.output
    assert seen["ids"] == ["o__r-1"]  # 1, 4, 7… with o__r-4 excluded
    assert seen["csv"] == out / "results.shard-1.csv"


def test_cli_eval_shard_needs_a_task_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    res = CliRunner().invoke(app, ["eval", "--out", str(tmp_path), "--shard", "0/2", "--yes"])
    assert res.exit_code == 2
