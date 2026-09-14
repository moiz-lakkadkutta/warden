"""D1: conflict graph → waves. Pure/offline; no sandbox, no LLM."""

from __future__ import annotations

import re

import pytest

from warden.integrate.waves import is_migration, plan_waves, render_waves
from warden.schemas import Plan, RiskClass, Wave


def mk(issue: int, *files: str, risk: tuple[RiskClass, ...] = ("none",)) -> Plan:
    return Plan(
        issue=issue,
        summary=f"fix #{issue}",
        files_to_touch=list(files),
        tests_to_add=[f"tests/test_{issue}.py"],
        landing_order=list(files),
        risk_class=list(risk),
        citations=[],
        acceptance=["it works"],
    )


FIX6: list[Plan] = [
    mk(101, "backend/app/services/invoices.py", "backend/tests/test_invoices.py"),
    mk(102, "backend/app/api/vouchers.py", "backend/tests/test_vouchers.py"),
    mk(103, "backend/app/services/invoices.py", "backend/app/services/tax.py"),
    mk(104, "backend/alembic/versions/0042_voucher_idx.py", "backend/app/models/voucher.py"),
    mk(105, "backend/alembic/versions/0043_invoice_status.py", "backend/app/models/invoice.py"),
    mk(106, "backend/app/api/payments.py"),
]


def issues(waves: list[Wave]) -> list[list[int]]:
    return [w.issues for w in waves]


def test_six_issue_fixture_matches_hand_plan() -> None:
    assert issues(plan_waves(FIX6)) == [[101, 102, 104, 106], [103, 105]]


def test_fixture_with_max_wave_size_three() -> None:
    assert issues(plan_waves(FIX6, max_wave_size=3)) == [[101, 102, 104], [103, 105, 106]]


def test_empty_input_returns_no_waves() -> None:
    assert plan_waves([]) == []


def test_all_conflicting_gives_one_issue_per_wave() -> None:
    plans = [mk(1, "a.py"), mk(2, "a.py"), mk(3, "a.py")]
    assert issues(plan_waves(plans)) == [[1], [2], [3]]


def test_two_migrations_forced_into_two_waves() -> None:
    plans = [mk(1, "alembic/versions/x.py"), mk(2, "db/migrations/0001.sql")]
    assert issues(plan_waves(plans)) == [[1], [2]]
    assert is_migration("alembic/versions/x.py")
    assert is_migration("db/migrations/0001.sql")
    assert not is_migration("app/migrate_helper.py")


def test_determinism_ignores_input_order() -> None:
    assert plan_waves(list(reversed(FIX6))) == plan_waves(FIX6)
    assert plan_waves(FIX6) == plan_waves(FIX6)


def test_max_wave_size_respected() -> None:
    plans = [mk(i, f"f{i}.py") for i in range(1, 6)]
    waves = plan_waves(plans, max_wave_size=2)
    assert [len(w.issues) for w in waves] == [2, 2, 1]
    assert all(len(w.issues) <= 2 for w in waves)
    with pytest.raises(ValueError):
        plan_waves(plans, max_wave_size=0)


def test_duplicate_issue_raises() -> None:
    with pytest.raises(ValueError):
        plan_waves([mk(7, "a.py"), mk(7, "b.py")])


def test_wave_defaults() -> None:
    waves = plan_waves(FIX6)
    assert [w.number for w in waves] == list(range(1, len(waves) + 1))
    for w in waves:
        assert w.integration_image_uuid is None
        assert w.merged == []


def test_selectors_disjoint_verified_do_not_conflict() -> None:
    plans = [mk(1, "a.py::foo"), mk(2, "a.py::bar")]
    waves = plan_waves(plans, functions={"a.py": {"foo", "bar"}})
    assert issues(waves) == [[1, 2]]


def test_selectors_fall_back_to_file_level() -> None:
    plans = [mk(1, "a.py::foo"), mk(2, "a.py::bar")]
    assert issues(plan_waves(plans, functions=None)) == [[1], [2]]
    assert issues(plan_waves(plans, functions={"a.py": {"foo"}})) == [[1], [2]]
    bare = [mk(1, "a.py"), mk(2, "a.py::foo")]
    assert issues(plan_waves(bare, functions={"a.py": {"foo"}})) == [[1], [2]]


def test_selector_path_normalisation() -> None:
    plans = [mk(1, "./a.py::foo"), mk(2, "a.py")]
    assert issues(plan_waves(plans, functions={"a.py": {"foo"}})) == [[1], [2]]
    assert is_migration("./alembic/versions/0001_x.py::upgrade")
    assert is_migration("alembic\\versions\\0001_x.py")
    migs = [mk(1, "alembic/versions/a.py::upgrade"), mk(2, "alembic/versions/b.py::upgrade")]
    assert issues(plan_waves(migs)) == [[1], [2]]


def test_render_waves_table() -> None:
    waves = plan_waves(FIX6)
    waves[1] = waves[1].model_copy(
        update={"integration_image_uuid": "ab12cd34-5678-90ab-cdef-1234567890ab", "merged": [103]}
    )
    out = render_waves(waves)
    lines = out.splitlines()
    assert lines[0].startswith("Wave")
    line1 = next(ln for ln in lines if ln.startswith("1"))
    assert "#101 #102 #104 #106" in line1
    assert "—" in line1
    line2 = next(ln for ln in lines if ln.startswith("2"))
    assert "ab12cd34" in line2
    assert "ab12cd34-" not in line2
    assert "#103" in line2
    assert out.endswith("\n")
    assert not re.search(r"\x1b\[", out)
    assert render_waves([]) == "no waves\n"
