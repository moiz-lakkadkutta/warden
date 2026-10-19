from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from warden.cli import app
from warden.eval import report

OLD = "task_id,resolved,verdict,false_pass,theater_catch,tool_turns,usd,seconds,error\n"
NEW = (
    "task_id,resolved,verdict,false_pass,false_fail,unverified,theater_catch,tool_turns,usd,"
    "seconds,error\n"
)
BON = (
    "task_id,resolved,verdict,false_pass,false_fail,unverified,theater_catch,tool_turns,usd,"
    "seconds,error,candidates,chosen\n"
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def shards(tmp_path: Path) -> list[Path]:
    a = _write(
        tmp_path / "results.shard-0.csv",
        NEW
        + "a__a-1,1,PASS,0,0,0,0,10,0.1000,100,\n"
        + "b__b-2,0,PASS,1,0,0,0,20,0.2000,300,\n"
        + "c__c-3,1,FAIL,0,1,0,0,40,0.3000,200,\n",
    )
    b = _write(
        tmp_path / "results.shard-1.csv",
        BON
        + "d__d-4,1,FAIL,0,0,1,0,30,0.4000,500,,3,2\n"
        + "e__e-5,0,FAIL,0,0,0,1,35,0.0000,50,,3,\n"
        + "f__f-6,0,ERROR,0,0,0,0,0,0.5000,10,CostCapExceededError: cap\n",
    )
    return [a, b]


def test_merge_tolerates_old_new_and_best_of_n_columns(tmp_path: Path, shards: list[Path]) -> None:
    old = _write(tmp_path / "old.csv", OLD + "g__g-7,0,FAIL,0,0,12,0.0100,60,\n")
    rows = report.read_results([*shards, old])
    assert [r.task_id for r in rows] == ["a__a-1", "b__b-2", "c__c-3", "d__d-4", "e__e-5",
                                         "f__f-6", "g__g-7"]  # fmt: skip
    g = rows[-1]
    assert not g.false_fail and not g.unverified  # missing columns default to derived / 0
    assert rows[5].is_error


def test_duplicates_prefer_a_real_row_over_an_error_then_the_latest(tmp_path: Path) -> None:
    first = _write(tmp_path / "1.csv", NEW + "a__a-1,0,ERROR,0,0,0,0,0,0.1,5,boom\n")
    second = _write(tmp_path / "2.csv", NEW + "a__a-1,1,PASS,0,0,0,0,9,0.2,50,\n")
    third = _write(tmp_path / "3.csv", NEW + "a__a-1,0,ERROR,0,0,0,0,0,0.1,5,again\n")
    rows = report.read_results([first, second, third])
    assert len(rows) == 1 and rows[0].verdict == "PASS"
    later = _write(tmp_path / "4.csv", NEW + "a__a-1,0,FAIL,0,0,0,0,40,0.3,60,\n")
    assert report.read_results([second, later])[0].verdict == "FAIL"


def test_metrics(shards: list[Path]) -> None:
    m = report.compute(report.read_results(shards))
    assert m.n == 6 and m.resolved == 3
    assert m.passes == 2 and m.pass_resolved == 1 and m.false_pass == 1
    assert m.fails == 3 and m.false_fail == 1 and m.unverified == 1
    assert m.theater_catches == 1 and m.errors == 1
    assert m.usd_total == pytest.approx(1.5) and m.usd_mean == pytest.approx(0.25)
    assert m.median_seconds == 150
    assert m.mean_turns == pytest.approx((10 + 20 + 40 + 30 + 35) / 5)  # ERROR rows excluded
    lo, hi = m.resolved_ci
    assert 0.18 < lo < 0.19 and 0.81 < hi < 0.82  # Wilson 95% for 3/6


def test_excluded_tasks_are_reported_not_counted(shards: list[Path]) -> None:
    m = report.compute(report.read_results(shards), excluded={"f__f-6": "image missing"})
    assert m.n == 5 and m.errors == 0 and m.excluded == {"f__f-6": "image missing"}


def test_wilson_edges() -> None:
    assert report.wilson(0, 0) == (0.0, 0.0)
    lo, hi = report.wilson(0, 10)
    assert lo == 0.0 and 0.27 < hi < 0.28
    lo, hi = report.wilson(10, 10)
    assert 0.72 < lo < 0.73 and hi == pytest.approx(1.0)


def test_markdown_table(shards: list[Path]) -> None:
    md = report.markdown(
        report.compute(report.read_results(shards), excluded={"x__x-9": "base"}), label="E1"
    )
    lines = md.strip().splitlines()
    assert lines[0] == "| metric | E1 |"
    assert "| Resolved | **3 / 6** (50%, 95% CI 19–81%) |" in lines
    assert any(line.startswith("| Verifier PASS that hidden tests reject") and "1 of 2 PASS" in line
               for line in lines)  # fmt: skip
    assert "| Cost | **$0.25 / task**, $1.50 total |" in lines
    assert any("Excluded" in line and "1" in line for line in lines)


def test_clean_pass_precision_leaves_out_pass_with_concerns(tmp_path: Path) -> None:
    rows = report.read_results(
        [
            _write(
                tmp_path / "r.csv",
                NEW
                + "a__a-1,1,PASS,0,0,0,0,10,0.1,100,\n"
                + "b__b-2,0,PASS_WITH_CONCERNS,1,0,0,0,10,0.1,100,\n"
                + "c__c-3,0,PASS,1,0,0,0,10,0.1,100,\n",
            )
        ]
    )
    m = report.compute(rows)
    assert (m.passes, m.pass_resolved, m.clean_passes, m.clean_pass_resolved) == (3, 1, 2, 1)
    assert "| Clean PASS precision (PASS without concerns) | 1 of 2 PASS (50%) |" in (
        report.markdown(m)
    )


def test_replace_between_markers() -> None:
    text = "intro\n<!-- results:start -->\nold\n<!-- results:end -->\nouter\n"
    out = report.replace_block(text, "| new |\n")
    assert out == "intro\n<!-- results:start -->\n| new |\n<!-- results:end -->\nouter\n"
    with pytest.raises(ValueError, match="markers"):
        report.replace_block("no markers", "x")


def test_readme_has_markers_around_the_results_table() -> None:
    readme = Path(__file__).parents[1].joinpath("README.md").read_text(encoding="utf-8")
    start, end = readme.index(report.START), readme.index(report.END)
    assert "| metric |" in readme[start:end]


def test_cli_report_md_and_write_readme(tmp_path: Path, shards: list[Path]) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["report", *map(str, shards), "--md", "--label", "E1"])
    assert res.exit_code == 0, res.output
    assert "| Resolved | **3 / 6**" in res.stdout

    js = runner.invoke(app, ["report", *map(str, shards)])
    assert js.exit_code == 0 and '"resolved": 3' in js.stdout

    readme = _write(tmp_path / "README.md", f"a\n{report.START}\nold\n{report.END}\nb\n")
    w = runner.invoke(app, ["report", *map(str, shards), "--write-readme", "--readme", str(readme)])
    assert w.exit_code == 0, w.output
    text = readme.read_text()
    assert "old" not in text and "**3 / 6**" in text and text.endswith(f"{report.END}\nb\n")


def test_cli_report_picks_up_a_sibling_excluded_txt(tmp_path: Path, shards: list[Path]) -> None:
    (tmp_path / "excluded.txt").write_text("f__f-6\timage missing\n")
    res = CliRunner().invoke(app, ["report", *map(str, shards)])
    assert res.exit_code == 0 and '"n": 5' in res.stdout


def test_cli_report_takes_eval_directories(tmp_path: Path, shards: list[Path]) -> None:
    e1 = tmp_path / "e1"
    e1.mkdir()
    _write(e1 / "results.shard-0.csv", NEW + "a__a-1,0,FAIL,0,0,0,0,10,0.1000,100,\n")
    _write(e1 / "notes.csv", "not,a,results,file\n")  # only results*.csv is read
    res = CliRunner().invoke(
        app, ["report", str(tmp_path), "--md", "--label", "E2", "--baseline", str(e1)]
    )
    assert res.exit_code == 0, res.output
    assert "| Resolved | **0 / 1**" in res.stdout and "**3 / 6**" in res.stdout


def test_cli_report_refuses_a_baseline_glob_that_leaked(tmp_path: Path, shards: list[Path]) -> None:
    # `--baseline e1/*.csv` puts e1's 2nd..nth shard among the measured CSVs.
    e1 = tmp_path / "e1"
    e1.mkdir()
    first = _write(e1 / "results.shard-0.csv", NEW + "a__a-1,0,FAIL,0,0,0,0,10,0.1,100,\n")
    leaked = _write(e1 / "results.shard-1.csv", NEW + "b__b-2,0,FAIL,0,0,0,0,10,0.1,100,\n")
    res = CliRunner().invoke(
        app, ["report", *map(str, shards), str(leaked), "--md", "--baseline", str(first)]
    )
    assert res.exit_code == 2
    assert "more than one directory" in res.output
