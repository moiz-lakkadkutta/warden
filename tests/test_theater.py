"""Theater check on a real repo: node ids, per-test outcomes, errors are not failures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from warden.runlog.events import EventLog
from warden.sandbox.box import Box
from warden.verify.theater import parse_outcomes, theater_check

REPRO = "backend/tests/test_d.py::test_draft_excluded"
GUARD = "backend/tests/test_d.py::test_ar_unchanged"


class PytestStub:
    """Scripted backend: copy_in forks; any pytest run returns (stdout, exit)."""

    def __init__(self, stdout: str, exit_code: int) -> None:
        self.stdout, self.exit_code = stdout, exit_code
        self.runs: list[dict[str, Any]] = []

    def use(self, ref: str) -> str:
        return ref

    def run(
        self, image: Any, *, shell: str, cwd: str, keep: bool, files: Any = None, **_: Any
    ) -> Any:
        self.runs.append({"shell": shell, "cwd": cwd, "files": files, "keep": keep})
        if files is not None:
            return "", "", 0, f"{image}+t", f"{image}+t"
        return self.stdout, "", self.exit_code, None, ""

    def tag(self, image: Any, name: str) -> None:
        return None

    def uuid_of(self, image: Any) -> str:
        return str(image)


RA_OUT = f"""
FAILED {REPRO} - assert Decimal('10498.95') == Decimal('0')
PASSED {GUARD}
=========================== short test summary info ============================
PASSED {GUARD}
FAILED {REPRO} - assert Decimal('10498.95') == Decimal('0')
1 failed, 1 passed in 0.61s
"""


def _box(tmp_path: Path, stub: PytestStub) -> Box:
    return Box(stub, "base", EventLog(tmp_path / "runs", "t"))


def test_parse_outcomes_reads_ra_summary() -> None:
    assert parse_outcomes(RA_OUT) == {REPRO: "failed", GUARD: "passed"}
    assert parse_outcomes(f"ERROR {REPRO} - ImportError\n") == {REPRO: "error"}


def test_repro_fails_and_guard_passes_on_base_is_a_real_test(tmp_path: Path) -> None:
    stub = PytestStub(RA_OUT, 1)
    res = theater_check(
        _box(tmp_path, stub),
        {"backend/tests/test_d.py": b"def test_draft_excluded(): ..."},
        node_ids=[REPRO, GUARD],
        cwd="/src",
        wrap="with-services sh -c",
    )
    assert res.failed_on_base is True and res.inconclusive is False
    assert res.outcomes == {REPRO: "failed", GUARD: "passed"}
    copy, run = stub.runs
    assert copy["files"] and copy["cwd"] == "/src"
    assert run["cwd"] == "/src"
    assert run["shell"].startswith("with-services sh -c '")
    assert REPRO in run["shell"] and GUARD in run["shell"] and "-rA" in run["shell"]
    assert "FAIL on unfixed base" in res.label


def test_all_new_tests_pass_on_base_is_theater(tmp_path: Path) -> None:
    out = f"PASSED {REPRO}\nPASSED {GUARD}\n2 passed in 0.5s\n"
    res = theater_check(_box(tmp_path, PytestStub(out, 0)), {"f.py": b""}, node_ids=[REPRO, GUARD])
    assert res.failed_on_base is False and res.inconclusive is False
    assert "theater" in res.label


def test_collection_error_or_no_tests_is_inconclusive_not_a_failure(tmp_path: Path) -> None:
    for code, out in [(2, f"ERROR {REPRO} - ImportError\n"), (4, "no tests ran\n"), (5, "")]:
        res = theater_check(_box(tmp_path, PytestStub(out, code)), {"f.py": b""}, node_ids=[REPRO])
        assert res.failed_on_base is False, code
        assert res.inconclusive is True, code
        assert "inconclusive" in res.label


def test_failure_by_error_only_is_inconclusive(tmp_path: Path) -> None:
    """exit 1 with the new test ERRORing (fixture blew up), none FAILED: not proof."""
    out = f"ERROR {REPRO} - fixture 'x' not found\n1 error in 0.1s\n"
    res = theater_check(_box(tmp_path, PytestStub(out, 1)), {"f.py": b""}, node_ids=[REPRO])
    assert res.failed_on_base is False and res.inconclusive is True
