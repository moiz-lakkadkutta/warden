"""ui.py: colour policy, error block, streaming phase log. Pure — StringIO + fake clock."""

from __future__ import annotations

import ast
import io
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import typer

from warden import ui

ESC = "\x1b"


class _Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _reset_ui() -> Iterator[None]:
    ui.configure(ui.UIConfig(color=False, animate=False, debug=False))
    yield
    ui.configure(ui.UIConfig(color=False, animate=False, debug=False))


# ------------------------------------------------------------------ 1. resolve precedence
@pytest.mark.parametrize(
    ("plain", "env", "tty", "color"),
    [
        (True, {"FORCE_COLOR": "1"}, True, False),
        (True, {}, True, False),
        (False, {"FORCE_COLOR": "1", "NO_COLOR": "1"}, False, True),
        (False, {"NO_COLOR": "1"}, True, False),
        (False, {}, True, True),
        (False, {}, False, False),
        (False, {"FORCE_COLOR": ""}, False, False),
        (False, {"NO_COLOR": ""}, True, True),
    ],
)
def test_resolve_precedence(plain: bool, env: dict[str, str], tty: bool, color: bool) -> None:
    cfg = ui.resolve(plain=plain, env=env, stream=_Stream(tty))
    assert cfg.color is color


# ------------------------------------------------------------------ 2. animate
def test_animate_needs_tty_and_not_plain() -> None:
    assert ui.resolve(plain=False, env={}, stream=_Stream(True)).animate is True
    assert ui.resolve(plain=True, env={}, stream=_Stream(True)).animate is False
    assert ui.resolve(plain=False, env={}, stream=_Stream(False)).animate is False
    assert ui.resolve(plain=False, env={"FORCE_COLOR": "1"}, stream=_Stream(False)).animate is False
    assert ui.resolve(plain=False, env={}, stream=_Stream(True), debug=True).debug is True


# ------------------------------------------------------------------ 3/4. phase line bytes
def test_phase_line_exact() -> None:
    clock = _Clock(100.0)
    err = io.StringIO()
    log = ui.PhaseLog(err, animate=False, color=False, clock=clock)
    log.cost(0.31)
    clock.t = 142.0
    log.phase("plan", "reading issue #201, 2 citations")
    assert err.getvalue() == "Plan   ▸ 00:42  $0.31  reading issue #201, 2 citations\n"
    assert log.spent_usd == 0.31


def test_step_finish_and_pr_padding() -> None:
    clock = _Clock(0.0)
    err = io.StringIO()
    log = ui.PhaseLog(err, animate=False, color=False, clock=clock)
    log.phase("pr", "draft PR body written")
    log.step("labelled PENDING-HUMAN")
    clock.t = 61.0
    log.cost(1.42)
    log.finish("PASS")
    assert err.getvalue() == (
        "PR     ▸ 00:00  $0.00  draft PR body written\n"
        "    labelled PENDING-HUMAN\n"
        "Total  ▸ 01:01  $1.42  PASS\n"
    )


# ------------------------------------------------------------------ 5. animation
def test_animate_rewrites_with_cr() -> None:
    clock = _Clock(0.0)
    err = io.StringIO()
    log = ui.PhaseLog(err, animate=True, color=False, clock=clock)
    log.phase("hunt", "probing")
    assert err.getvalue() == "Hunt   ▸ 00:00  $0.00  probing"
    clock.t = 3.0
    log.cost(0.12)
    assert err.getvalue() == ("Hunt   ▸ 00:00  $0.00  probing\rHunt   ▸ 00:03  $0.12  probing")
    log.step("filed #201")
    assert err.getvalue().endswith("probing\n    filed #201\n")
    assert err.getvalue().count("\r") == 1

    # Non-animate: the phase line shows the total at phase start; cost() prints nothing.
    plain_err = io.StringIO()
    plain = ui.PhaseLog(plain_err, animate=False, color=False, clock=_Clock(0.0))
    plain.phase("hunt", "probing")
    plain.cost(0.12)
    plain.step("filed #201")
    assert "\r" not in plain_err.getvalue()
    assert plain_err.getvalue() == "Hunt   ▸ 00:00  $0.00  probing\n    filed #201\n"


# ------------------------------------------------------------------ 6. colour paints only label
def test_color_paints_only_label() -> None:
    err = io.StringIO()
    log = ui.PhaseLog(err, animate=False, color=True, clock=_Clock(0.0))
    log.cost(0.31)
    log.phase("plan", "x")
    out = err.getvalue()
    assert out.startswith(ESC)
    assert out.index("Plan") < out.index(ESC + "[0m") < out.index("▸")
    assert out.count(ESC) == 2
    assert "$0.31" in out and ESC not in out[out.index("▸") :]

    err2 = io.StringIO()
    ui.PhaseLog(err2, animate=False, color=False, clock=_Clock(0.0)).phase("plan", "x")
    assert ESC not in err2.getvalue()


# ------------------------------------------------------------------ 7. fail block
def test_fail_block_exact(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as ei:
        ui.fail("boom", fix="do x", run_id="r1", code=4)
    assert ei.value.exit_code == 4
    cap = capsys.readouterr()
    assert cap.err == "boom\nfix: do x\nrun_id r1\n"
    assert cap.out == ""

    with pytest.raises(typer.Exit) as ei:
        ui.fail("boom")
    assert ei.value.exit_code == 1
    cap = capsys.readouterr()
    assert cap.err == "boom\n"
    assert "ERROR" not in cap.err


# ------------------------------------------------------------------ 8. debug re-raises
def test_fail_debug_reraises(capsys: pytest.CaptureFixture[str]) -> None:
    ui.configure(ui.UIConfig(color=False, animate=False, debug=True))
    exc = ValueError("inner")
    with pytest.raises(ValueError) as ei:
        ui.fail("boom", fix="do x", exc=exc)
    assert ei.value is exc
    assert capsys.readouterr().err == "boom\nfix: do x\n"

    ui.configure(ui.UIConfig(color=False, animate=False, debug=False))
    with pytest.raises(typer.Exit):
        ui.fail("boom", exc=exc)


# ------------------------------------------------------------------ import contract
def test_ui_imports_only_stdlib_and_typer() -> None:
    """ui.py sits on the `warden --help` path: stdlib + typer only, nothing heavier."""
    tree = ast.parse(Path(ui.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None and node.level == 0
            names.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"typer"}
    assert names <= allowed, names - allowed
    assert "typer" in names  # typer.Exit is the one sanctioned non-stdlib import
