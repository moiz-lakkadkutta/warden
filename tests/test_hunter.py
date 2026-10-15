"""Offline tests for the Hunter (C5) and Filer (C6): baseline, IDOR axis, probe parsing,
orchestration, filing. Nothing here touches Contree, Token Factory or GitHub."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from warden.hunt.axes.idor import (
    IdorRun,
    Observation,
    SandboxProber,
    findings_from_runs,
)
from warden.hunt.axes.probe_script import MARKER, boot_command, parse_probe_output, probe_py
from warden.hunt.baseline import Baseline, load_baseline
from warden.hunt.filer import (
    attacker_section,
    file_findings,
    format_body,
    group_findings,
    issue_title,
)
from warden.hunt.hunter import HuntReport, run_hunt
from warden.runlog.events import EventLog
from warden.schemas import Finding

# --------------------------------------------------------------------------- helpers


def obs(
    kind: str, path: str, *, status: int, vulnerable: bool, rid: str | None = None
) -> Observation:
    return Observation(
        probe=f"{kind} POST {path}",
        kind=kind,  # type: ignore[arg-type]
        method="POST" if kind == "cross_firm_write" else "GET",
        path=path,
        status=status,
        vulnerable=vulnerable,
        request_id=rid,
        repro=f"curl -X POST {path}",
        expected="422",
        actual=f"HTTP {status}",
    )


def finding(key: str, *, axis: str = "idor", title: str = "t", repro: str = "r") -> Finding:
    return Finding(
        axis=axis,  # type: ignore[arg-type]
        severity="P1",
        title=title,
        repro=repro,
        expected="e",
        actual="a",
        root_cause_key=key,
    )


@dataclass
class _FakeRun:
    stdout: str
    stderr: str = ""
    exit_code: int = 0


class _FakeBox:
    """Minimal Box stand-in: records the probe run and returns canned stdout."""

    def __init__(self, stdout: str) -> None:
        self._stdout = stdout
        self.calls: list[dict[str, Any]] = []

    def run(self, shell: str, **kw: Any) -> _FakeRun:
        self.calls.append({"shell": shell, **kw})
        return _FakeRun(self._stdout)


class _FakeProber:
    def __init__(self, runs: list[IdorRun]) -> None:
        self._runs = list(runs)

    def probe(self) -> IdorRun:
        return self._runs.pop(0)


class FakeRunner:
    def __init__(self, *results: subprocess.CompletedProcess[str]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return (
            self._results.pop(0) if self._results else subprocess.CompletedProcess(argv, 0, "", "")
        )


def _cp(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


@pytest.fixture
def log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "runs", "hunt-test")


# --------------------------------------------------------------------------- baseline


def test_load_baseline_parses_ids(tmp_path: Path) -> None:
    reg = tmp_path / "register.md"
    reg.write_text(
        "| **SEC-1** | all writes | Cross-org firm-spoof: POST /parties with foreign firm_id |\n"
        "## AUTHZ-2 — Cross-firm write\nPOST /items takes firm_id from the body, no check.\n"
        "| GL-1 | GL | Purchase cycle posts nothing |\n",
        encoding="utf-8",
    )
    base = load_baseline(reg)
    assert {"SEC-1", "AUTHZ-2", "GL-1"} <= base.ids
    assert base.path == reg


def test_missing_register_is_not_fatal(tmp_path: Path) -> None:
    base = load_baseline(tmp_path / "nope.md")
    assert base.ids == frozenset()
    assert base.path is None


def test_regression_id_matches_endpoint_and_axis(tmp_path: Path) -> None:
    reg = tmp_path / "r.md"
    reg.write_text(
        "**SEC-1** Cross-org firm-spoof on writes: POST /parties with a foreign firm_id persists.\n"
        "**GL-1** Purchase cycle posts nothing to the GL; voucher missing.\n",
        encoding="utf-8",
    )
    base = load_baseline(reg)
    f = finding(
        "idor:cross_firm_write",
        title="cross firm write: POST /parties",
        repro="POST /parties firm_id",
    )
    assert base.regression_id(f) == "SEC-1"
    # A money-axis finding must not match the IDOR entry.
    assert base.regression_id(finding("x", axis="money", title="novel", repro="/widgets")) is None


def test_empty_baseline_labels_nothing() -> None:
    assert Baseline().regression_id(finding("k")) is None


# ------------------------------------------------------------------------- idor axis


def test_findings_from_runs_confirms_on_threshold() -> None:
    runs = [
        IdorRun(
            observations=[
                obs("cross_firm_write", "/accounting/vouchers", status=201, vulnerable=True)
            ]
        ),
        IdorRun(
            observations=[
                obs("cross_firm_write", "/accounting/vouchers", status=201, vulnerable=True)
            ]
        ),
    ]
    confirmed, plausible = findings_from_runs(runs, threshold=2)
    assert len(confirmed) == 1 and not plausible
    f = confirmed[0]
    assert f.axis == "idor" and f.confirmed and f.reproduced_times == 2
    assert f.root_cause_key == "idor:cross_firm_write:/accounting/vouchers" and f.severity == "P1"
    assert f.impact


def test_findings_from_runs_plausible_below_threshold() -> None:
    runs = [
        IdorRun(observations=[obs("cross_firm_write", "/x", status=201, vulnerable=True)]),
        IdorRun(observations=[obs("cross_firm_write", "/x", status=422, vulnerable=False)]),
    ]
    confirmed, plausible = findings_from_runs(runs, threshold=2)
    assert not confirmed and len(plausible) == 1 and plausible[0].reproduced_times == 1


def test_cross_org_read_is_p0() -> None:
    runs = [
        IdorRun(observations=[obs("cross_org_read", "/parties/{id}", status=200, vulnerable=True)])
    ] * 2
    confirmed, _ = findings_from_runs(runs, threshold=2)
    assert confirmed[0].severity == "P0"
    assert confirmed[0].root_cause_key == "idor:cross_org_read:/parties/{id}"


def test_guard_holds_yields_no_finding() -> None:
    runs = [
        IdorRun(observations=[obs("cross_firm_write", "/parties", status=422, vulnerable=False)])
    ] * 2
    confirmed, plausible = findings_from_runs(runs, threshold=2)
    assert not confirmed and not plausible


# ----------------------------------------------------------------------- probe script


def test_parse_probe_output_roundtrip() -> None:
    payload = {
        "observations": [
            obs("cross_firm_write", "/items", status=201, vulnerable=True).model_dump()
        ],
        "endpoints_enumerated": 18,
        "tenants": 2,
        "error": "",
    }
    stdout = f"boot noise\n{MARKER}\n{json.dumps(payload)}\n{MARKER}\ntrailing\n"
    run = parse_probe_output(stdout)
    assert run.endpoints_enumerated == 18 and run.tenants == 2 and not run.error
    assert run.observations[0].vulnerable


def test_parse_probe_output_no_marker_is_error() -> None:
    assert "no result marker" in parse_probe_output("uvicorn crashed").error


def test_parse_probe_output_bad_json_is_error() -> None:
    assert "not valid JSON" in parse_probe_output(f"{MARKER}\n{{bad\n{MARKER}").error


def test_boot_command_is_dash_safe() -> None:
    cmd = boot_command()
    assert "fabric_demo" in cmd and "/ready" in cmd and MARKER in cmd
    assert "uvicorn main:app" in cmd
    # no bash-isms
    assert "[[" not in cmd and "$RANDOM" not in cmd


def test_boot_command_replay_migrates_scratch_db() -> None:
    cmd = boot_command(at_rev="d9c37cb^", workdir="/src")
    assert "git checkout -q --detach d9c37cb^" in cmd
    assert "cd /src" in cmd
    assert "CREATE DATABASE warden_replay" in cmd and "DROP DATABASE IF EXISTS warden_replay" in cmd
    assert "alembic upgrade head" in cmd
    # the app boots against the scratch DB, not the seeded demo
    assert "warden_replay" in cmd and "fabric_demo" not in cmd


def test_boot_command_default_uses_seeded_demo() -> None:
    cmd = boot_command()
    assert "fabric_demo" in cmd
    assert "alembic upgrade head" not in cmd and "git checkout" not in cmd


def test_probe_py_has_marker_substituted() -> None:
    src = probe_py()
    assert "__MARKER__" not in src and MARKER in src
    compile(src, "probe.py", "exec")  # it is syntactically valid Python


# --------------------------------------------------------------------- sandbox prober


def test_sandbox_prober_runs_and_parses() -> None:
    payload = {"observations": [], "endpoints_enumerated": 5, "tenants": 2, "error": ""}
    box = _FakeBox(f"{MARKER}\n{json.dumps(payload)}\n{MARKER}\n")
    run = SandboxProber(
        box, EventLog(Path("/tmp/wh"), "p"), shell_wrap="with-services sh -c"
    ).probe()  # type: ignore[arg-type]
    assert run.endpoints_enumerated == 5
    call = box.calls[0]
    assert call["shell"].startswith("with-services sh -c ") and call["keep"] is False
    assert "/tmp/warden_idor_probe.py" in call["files"]


# -------------------------------------------------------------------------- hunter


def test_run_hunt_confirms_and_labels_regression(log: EventLog) -> None:
    v = obs("cross_firm_write", "/accounting/vouchers", status=201, vulnerable=True)
    prober = _FakeProber([IdorRun(observations=[v]), IdorRun(observations=[v])])
    base = Baseline(by_id={"SEC-1": "cross-firm POST /accounting/vouchers firm_id"})
    report = run_hunt("idor", prober, baseline=base, log=log, reproduce=2)
    assert isinstance(report, HuntReport) and report.found
    assert report.runs == 2 and report.confirmed[0].regression_of == "SEC-1"


def test_run_hunt_short_circuits_on_probe_error(log: EventLog) -> None:
    prober = _FakeProber([IdorRun(error="uvicorn did not become ready")])
    report = run_hunt("idor", prober, baseline=Baseline(), log=log, reproduce=2)
    assert report.runs == 1 and report.probe_error and not report.confirmed


def test_run_hunt_rejects_unknown_axis(log: EventLog) -> None:
    with pytest.raises(ValueError, match="not implemented"):
        run_hunt("race", _FakeProber([]), baseline=Baseline(), log=log)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- filer


def test_group_findings_by_root_cause() -> None:
    fs = [finding("a"), finding("b"), finding("a")]
    groups = group_findings(fs)
    assert list(groups) == ["a", "b"] and len(groups["a"]) == 2


def test_format_body_has_strict_sections() -> None:
    f = finding("idor:cross_firm_write", title="cross firm write: POST /items", repro="curl ...")
    f.request_id = "req-123"
    f.impact = "cross-tenant write"
    body = format_body([f])
    for section in ("## Repro", "## Expected vs actual", "## Impact", "**Severity:** P1"):
        assert section in body
    assert "req-123" in body and "curl ..." in body


def test_issue_title_marks_regression() -> None:
    f = finding("k", title="cross firm write")
    f.regression_of = "SEC-1"
    assert issue_title([f]).startswith("[P1] [regression SEC-1]")


def test_grouped_title_prefers_regression_head_and_prefix() -> None:
    plain = finding("idor:cross_firm_write", title="cross firm write: POST /cost-centres")
    reg = finding("idor:cross_firm_write", title="cross firm write: POST /parties")
    reg.regression_of = "SEC-1"
    # even though the plain finding is first, the title uses the regression-labelled one
    title = issue_title([plain, reg], prefix="[replay] ")
    assert title == "[replay] [P1] [regression SEC-1] cross firm write: POST /parties"


def test_format_body_preamble_is_prepended() -> None:
    body = format_body([finding("k")], preamble="> already fixed on main")
    assert body.startswith("> already fixed on main")


def test_attacker_section_renders_claims() -> None:
    sec = attacker_section({"role": "OWNER", "firm_id": None, "org_id": "abc"})
    assert "decoded JWT" in sec
    assert "`role`: `OWNER`" in sec and "`firm_id`: `None`" in sec


def test_attacker_section_empty_is_blank() -> None:
    assert attacker_section({}) == ""


def test_format_body_includes_evidence() -> None:
    body = format_body([finding("k")], evidence="## Attacker token (decoded JWT)\n- `role`: `X`")
    assert "## Attacker token (decoded JWT)" in body


def test_per_resource_grouping_files_one_issue_each() -> None:
    # findings_from_runs keys each resource separately, so the filer makes one issue per resource
    parties = finding("idor:cross_firm_write:/parties", title="cross firm write: POST /parties")
    items = finding("idor:cross_firm_write:/items", title="cross firm write: POST /items")
    groups = group_findings([parties, items])
    assert len(groups) == 2


def test_file_findings_dry_run_opens_nothing() -> None:
    runner = FakeRunner()
    filed = file_findings([finding("k")], "o/r", no_file=True, runner=runner)
    assert filed[0].dry_run and filed[0].number is None and not runner.calls


def test_file_findings_creates_label_then_issue() -> None:
    runner = FakeRunner(
        _cp(0),  # label create
        _cp(0, stdout="https://github.com/o/r/issues/7\n"),  # issue create
    )
    filed = file_findings([finding("k")], "o/r", runner=runner)
    assert runner.calls[0][:3] == ["gh", "label", "create"]
    assert runner.calls[1][:3] == ["gh", "issue", "create"]
    assert filed[0].number == 7 and filed[0].url.endswith("/issues/7")  # type: ignore[union-attr]


def test_file_findings_raises_on_gh_failure() -> None:
    runner = FakeRunner(_cp(0), _cp(1, stderr="gh: boom"))
    with pytest.raises(RuntimeError, match="gh issue create failed"):
        file_findings([finding("k")], "o/r", runner=runner)


# ----------------------------------------------------------------------------- CLI

_CREDS = {
    "NEBIUS_API_KEY": "k",
    "CONTREE_BASE_URL": "https://c",
    "NEBIUS_PROJECT_ID": "p",
}


def _invoke_hunt(tmp_path: Path, *args: str) -> Any:
    from typer.testing import CliRunner

    from warden.cli import app

    env = {**_CREDS, "WARDEN_RUNS_DIR": str(tmp_path / "runs")}
    return CliRunner().invoke(app, ["hunt", *args], env=env)


def test_hunt_rejects_unknown_axis(tmp_path: Path) -> None:
    res = _invoke_hunt(tmp_path, "--axis", "bogus")
    assert res.exit_code == 2


def test_hunt_dry_run_files_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import warden.hunt.axes.idor as idor_mod
    import warden.sandbox.box as box_mod

    v = obs("cross_firm_write", "/accounting/vouchers", status=201, vulnerable=True)
    runs = [IdorRun(observations=[v], endpoints_enumerated=12, tenants=2) for _ in range(2)]

    monkeypatch.setattr(box_mod, "open_base", lambda *a, **k: object())

    class _Prober:
        def __init__(self, *a: Any, **k: Any) -> None:
            self._runs = list(runs)

        def probe(self) -> IdorRun:
            return self._runs.pop(0)

    monkeypatch.setattr(idor_mod, "SandboxProber", _Prober)
    # a real gh must never run from the hunt command
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp(1, stderr="no gh in tests"))

    res = _invoke_hunt(tmp_path, "--no-file", "--register", str(tmp_path / "no-clone"))
    assert res.exit_code == 0, res.output
    assert "1 confirmed" in res.output and "would file" in res.output
