"""Terminal output policy: colour, the error block and the streaming phase log.

stdlib + typer.Exit only; nothing heavier — this module is on the `warden --help` path.
Everything here writes to stderr; stdout is data (see docs/ux-brief.md §2.3).

Colour precedence (first match wins):
  1. `--plain`                      → colour off, animation off (flags beat env)
  2. `FORCE_COLOR` set, non-empty   → colour on   (per-invocation opt-in; force-color.org)
  3. `NO_COLOR` set, non-empty      → colour off  (ambient opt-out; no-color.org)
  4. `stream.isatty()`              → colour on iff a terminal
Empty values count as unset for both variables (presence and non-emptiness matter, the value
does not). FORCE_COLOR beats NO_COLOR, following the Node/chalk convention.
Animation is `not plain and stream.isatty()` — FORCE_COLOR never turns animation on.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, NoReturn, TextIO

import typer

Phase = Literal["hunt", "plan", "fix", "verify", "pr"]

LABEL: dict[str, str] = {
    "hunt": "Hunt",
    "plan": "Plan",
    "fix": "Fix",
    "verify": "Verify",
    "pr": "PR",
}
# 256-colour approximations of render.py's PHASE_COLOURS (legible on light and dark terminals).
PHASE_ANSI: dict[str, str] = {
    "hunt": "38;5;97",
    "plan": "38;5;31",
    "fix": "38;5;136",
    "verify": "38;5;29",
    "pr": "38;5;240",
}
RED = "31"
RESET = "\033[0m"


@dataclass(frozen=True)
class UIConfig:
    color: bool
    animate: bool
    debug: bool


_current = UIConfig(color=False, animate=False, debug=False)


def resolve(
    *,
    plain: bool,
    debug: bool = False,
    env: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> UIConfig:
    """Decide colour/animation for this process. See the module docstring for precedence."""
    env = os.environ if env is None else env
    stream = sys.stderr if stream is None else stream
    tty = bool(stream.isatty())
    if plain:
        color = False
    elif env.get("FORCE_COLOR", ""):
        color = True
    elif env.get("NO_COLOR", ""):
        color = False
    else:
        color = tty
    return UIConfig(color=color, animate=not plain and tty, debug=debug)


def configure(cfg: UIConfig) -> None:
    global _current
    _current = cfg


def current() -> UIConfig:
    return _current


def paint(text: str, code: str, *, color: bool | None = None) -> str:
    """Wrap `text` in an SGR sequence when colour is on; `code` is the SGR parameter string."""
    on = current().color if color is None else color
    return f"\033[{code}m{text}{RESET}" if on else text


def err(msg: str = "") -> None:
    """Plain stderr line (progress, warnings). Never stdout."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def fail(
    msg: str,
    *,
    fix: str | None = None,
    run_id: str | None = None,
    code: int = 1,
    exc: BaseException | None = None,
) -> NoReturn:
    """The error block: human sentence, then `fix: …`, then `run_id …`. No prefix, no stdout.

    Exit codes: 1 failure, 2 usage/config, 3 not implemented, 4 cost cap.
    With `--debug` and an `exc`, the block is written first and the exception propagates so the
    plain Python traceback follows it.
    """
    out = sys.stderr
    out.write(paint(msg, RED) + "\n")
    if fix:
        out.write(f"fix: {fix}\n")
    if run_id:
        out.write(f"run_id {run_id}\n")
    out.flush()
    if exc is not None and current().debug:
        raise exc
    raise typer.Exit(code)


class PhaseLog:
    """Streaming phase log on stderr: one line per phase with elapsed time and running cost.

        Plan   ▸ 00:42  $0.31  reading issue #201, 2 citations
            sub-step
        Total  ▸ 03:10  $1.42  PASS

    With `animate`, the phase line stays open and `cost()` rewrites it in place (`\\r`); the
    persistent bytes are the same in both modes. No threads, no spinner.
    """

    def __init__(
        self,
        err: TextIO | None = None,
        *,
        animate: bool | None = None,
        color: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._err = err
        self._animate = animate
        self._color = color
        self._clock = clock
        self._t0 = clock()
        self._spent = 0.0
        self._open: tuple[str, str] | None = None  # (phase, text) of the un-terminated line

    # ------------------------------------------------------------ resolution at call time
    @property
    def _out(self) -> TextIO:
        return sys.stderr if self._err is None else self._err

    @property
    def _animated(self) -> bool:
        return current().animate if self._animate is None else self._animate

    @property
    def _coloured(self) -> bool:
        return current().color if self._color is None else self._color

    @property
    def spent_usd(self) -> float:
        return self._spent

    # ------------------------------------------------------------ formatting
    def _elapsed(self) -> str:
        secs = max(0, int(self._clock() - self._t0))
        return f"{secs // 60:02d}:{secs % 60:02d}"

    def _line(self, label: str, text: str, code: str | None) -> str:
        head = paint(label, code, color=self._coloured) if code is not None else label
        head += " " * (6 - len(label))
        return f"{head} ▸ {self._elapsed()}  ${self._spent:.2f}  {text}"

    def _close_open(self) -> None:
        if self._open is not None:
            self._out.write("\n")
            self._open = None

    # ------------------------------------------------------------ API
    def phase(self, phase: Phase, text: str) -> None:
        self._close_open()
        self._out.write(self._line(LABEL[phase], text, PHASE_ANSI[phase]))
        if self._animated:
            self._open = (phase, text)
        else:
            self._out.write("\n")
        self._out.flush()

    def step(self, text: str) -> None:
        self._close_open()
        self._out.write(f"    {text}\n")
        self._out.flush()

    def cost(self, usd: float) -> None:
        self._spent = round(self._spent + usd, 6)
        if self._animated and self._open is not None:
            phase, text = self._open
            self._out.write("\r" + self._line(LABEL[phase], text, PHASE_ANSI[phase]))
            self._out.flush()

    def finish(self, text: str) -> None:
        self._close_open()
        self._out.write(self._line("Total", text, None) + "\n")
        self._out.flush()
