# DX plan — §5b items that live in cli.py (colour, errors, phase log, console alias, meter wiring, init)

## 1. Goal
One new light module `warden/ui.py` (stdlib only: os, sys, time, dataclasses, typing) owning colour policy,
the error block and the streaming phase log; cli.py routes every stderr/exit path through it; demo.py
drives the phase log so `warden run --demo` shows Hunt/Plan/Fix/Verify/PR + Total; `console --run`
alias; `CostMeter.log` wired; init made prompt-free. Base = main 52b3047 (B1 merged: cost.py has `log`,
EventLog.close idempotent). No change to any existing public signature except additive kwargs.

## 2. Design
### a. Colour / animation policy (ui.py)
- `resolve(*, plain: bool, debug: bool = False, env: Mapping[str, str] = os.environ, stream: TextIO | None
  = None) -> UIConfig` (frozen dataclass `UIConfig(color: bool, animate: bool, debug: bool)`); `stream`
  defaults to sys.stderr at call time. `configure(cfg)` / `current()` hold one module-level config.
- Precedence, decided and documented in the docstring: `--plain` (flag beats env, per config precedence)
  > `FORCE_COLOR` non-empty → colour on > `NO_COLOR` non-empty → off > `stream.isatty()`. Empty values
  are "unset" for both (no-color.org / force-color.org: presence and non-empty, value ignored). FORCE_COLOR
  wins over NO_COLOR (Node/chalk convention; FORCE is the per-invocation opt-in, NO_COLOR the ambient one).
  `animate = not plain and stream.isatty()` — FORCE_COLOR never turns animation on.
- Today's `_color_ok()` bug: no `--plain` input, colour and animation conflated; replaced by ui, deleted.
- Global options via `@app.callback()`: `--plain` and `--debug`, applied before any command
  (`warden --plain run --demo`). `run --plain` stays (OR-ed with the global). `_mark()` uses ui.
- `paint(text, code)` wraps only when `current().color`. `PHASE_ANSI` = 256-colour approximations of
  render.py's PHASE_COLOURS (hunt 97, plan 31, fix 136, verify 29, pr 240); red 31 only in `fail`.
- `Typer(pretty_exceptions_enable=False)`: an unexpected exception prints the plain Python traceback
  (rich never imported). A catch-all needs a `main()` entry point (pyproject) → follow-up, not here.

### b. Error style
- `fail(msg: str, *, fix: str | None = None, run_id: str | None = None, code: int = 1, exc: BaseException
  | None = None) -> NoReturn`. Writes to stderr exactly: line 1 `msg` (red when colour on); line 2 `fix:
  <fix>` if given; line 3 `run_id <id>` if given — same wording confirm_pr already uses. Then
  `raise typer.Exit(code)`; if `current().debug` and `exc` given, `raise exc` instead (block first, then
  the standard traceback). Never a prefix, never stdout.
- Exit codes: 1 failure (PrOpenError, unknown run, refused overwrite, no runs), 2 usage/config (no
  --issue, missing keys), 3 not-implemented stubs, 4 cost cap (CI can tell "raise --max-usd" apart).
- Routing (all existing `_err(...); raise typer.Exit` pairs): run no-issue → fail("nothing to run", fix=
  "pass --issue N, or --demo to try Warden without credentials", run_id, 2); missing keys → fail("missing
  X, Y", fix="run `warden doctor`", run_id, 2); live stub → code 3 with fix "use --demo for now"; resume
  unknown → fail(f"no run {id} under {runs_dir}", fix=f"ls {runs_dir}"); resume stub → code 3; console
  no runs → fail("no runs yet", fix="warden run --demo"); init exists → see f. `_err` stays as the plain
  stderr writer (delegating to `ui.err`). doctor's ✗ summary is not an error block; unchanged.
- Top level in `run` (the only sanctioned catch): body in try/finally `log.close()`; `except
  CostCapExceededError as e: fail(str(e), run_id=log.run_id, code=4, exc=e)` (message verbatim; cost.py
  already emitted the cost_cap event); `except PrOpenError as e: fail(str(e), fix=e.fix, run_id=…, exc=e)`.
  Never `except RuntimeError` — `typer.Exit` subclasses it. doctor `--live`: add `except
  CostCapExceededError: raise` above its blanket `except Exception` (stop catching the cap to continue).

### c. PhaseLog (ui.py; stderr only)
- `PhaseLog(err: TextIO | None = None, *, animate: bool | None = None, color: bool | None = None, clock:
  Callable[[], float] = time.monotonic)`; None → sys.stderr / `current()` at call time. Methods:
  `phase(phase: Phase, text: str)`, `step(text: str)`, `cost(usd: float)`, `finish(text: str)`;
  property `spent_usd`. Elapsed = seconds since construction (run start), `MM:SS`, minutes unbounded.
- Phase line, exact: `f"{LABEL[phase]:<6} ▸ {mm:02d}:{ss:02d}  ${spent:.2f}  {text}"`, LABEL = Hunt, Plan,
  Fix, Verify, PR (`"PR     ▸"`). `$` is the running total at that moment. Only the label is painted.
  Sub-step: 4 spaces + text. `finish`: `f"Total  ▸ {mm:02d}:{ss:02d}  ${spent:.2f}  {text}"` (a summary
  row, not a sixth phase — ruling: "Total" is the §2.3 "total at the end").
- Animation (only when `animate`): `phase()` leaves its line open (no `\n`); `cost()` rewrites it with
  `\r` + fresh elapsed/$ while open; `step()`/`phase()`/`finish()` close it with `\n` first. Non-animate:
  `phase()` ends with `\n`, `cost()` prints nothing. Persistent bytes are identical in both modes apart
  from `\r` rewrites. No threads, no spinner, no `update()`.
- demo.py: `run_demo(s, log, *, dry_run=False, progress: PhaseLog | None = None)` (additive kwarg).
  Calls: hunt phase "8 parallel POST /invoices/{id}/finalize" + two steps (the existing notes), cost 0.12;
  plan "reading issue #201, 1 citation", cost 0.31; dry-run → finish("plan printed (--dry-run)") and
  return; fix "forking base → candidate", step "candidate checkpoint <uuid> after 7 tool turns", cost
  0.80; verify "theater probe on unfixed base + full suite", steps "theater: <label>" and "suite: 214
  passed", cost 0.15; pr "draft PR body written · PENDING-HUMAN", cost 0.04; finish("PASS · body in
  runs/<id>/artifacts/pr_body.md"). Per-phase costs sum to the existing 1.42 = result cost_usd.
- cli `run`: always constructs `PhaseLog()` (stderr) — `--json` only changes stdout; the evidence table
  print to stderr in non-json mode stays. Nothing else on stdout.

### d. `console --run` alias: `typer.Option("--run-id", "--run", …)` on the same parameter. No `--open`.
### e. Meter wiring: `LLM.__init__` sets `meter.log = log` when `meter.log is None` (2 lines in client.py);
doctor builds `CostMeter(cap_usd=0.50, log=log)`. Every live run's cap hit now lands in events.jsonl.
### f. `warden init` audit: already writes warden.toml with 0 questions; gaps: overwrite uses
`typer.confirm` (stdout prompt — violates "never prompt for anything else"), `**/pytest.ini` recursive
glob (slow on node_modules), no repo detection, pytest-only. Minimal changes: existing file without `--yes`
→ fail("warden.toml exists", fix="re-run with --yes to overwrite"); look only at root/backend for
pyproject.toml, pytest.ini, setup.cfg, tox.ini; package.json → "npm test", go.mod → "go test ./...",
Cargo.toml → "cargo test"; warn (stderr, no fail) when `.git` is absent. Still ≤ 5 questions (0).
### g. Startup: ui.py imports stdlib only; cli.py imports ui at top; everything else stays lazy.

## 3. Tests
CliRunner note: click 8.5 has no `mix_stderr` (removed in 8.2) — `runner.invoke()` already separates
`result.stdout` / `result.stderr`; assert them independently. Autouse fixture: chdir tmp_path, env
`WARDEN_RUNS_DIR`, and `NO_COLOR: None, FORCE_COLOR: None` (unset inherited shell values), `ui.configure`
reset. `tests/test_ui.py` (new, pure, StringIO + fake clock):
1. resolve precedence table: plain→False always; FORCE=1+NO=1→colour; NO=1+tty→off; tty only→on; non-tty→
   off; FORCE=""/NO="" ignored. 2. animate = not plain and tty; FORCE_COLOR alone leaves it off.
3. phase line exact bytes after cost(0.31) at clock 42 s: `"Plan   ▸ 00:42  $0.31  reading issue #201, 2
   citations\n"`. 4. step = 4-space indent; finish = `"Total  ▸ …"`; PR label pads to `"PR     ▸"`.
5. animate: cost() emits one `\r` rewrite, step() closes with `\n`; non-animate output has no `\r`.
6. color=True paints only the label (ESC before "Plan", none around "$0.31"); color=False → no ESC.
7. fail block exact: `"boom\nfix: do x\nrun_id r1\n"`, Exit code 4; msg-only variant is one line; no
   "ERROR"; stdout empty. 8. debug + exc → the exc propagates, block written first.
`tests/test_cli.py` (CliRunner): 9. `run --demo`: stderr has lines starting `Hunt   ▸`, `Plan   ▸`,
`Fix    ▸`, `Verify ▸`, `PR     ▸`, `Total  ▸` in order, Total ends `$1.42  PASS…`; stdout == "".
10. `run --demo --json`: every stdout line is JSON, last is result with verdict PASS; no ESC anywhere.
11. colour via env: FORCE_COLOR=1 → ESC in stderr; FORCE+NO → ESC; NO only → none; `--plain run --demo`
and `run --demo --plain` with FORCE → none. 12. `console --run <id>` and `--run-id` both print the
index.html path to stdout. 13. `run` (no flags) → exit 2, stderr has "fix: pass --issue", "run_id", no
"ERROR". 14. resume unknown → exit 1 with fix line. 15. console with empty runs dir → exit 1, fix line.
16. init: pyproject → `test_cmd = "pytest -q"`; package.json → "npm test"; existing file → exit 1 + "--yes"
fix; `--yes` overwrites; no `.git` → warning on stderr, exit 0. 17. monkeypatch `warden.demo.run_demo` to
raise CostCapExceededError(0.5, 0.5, "fix"): `run --demo` → exit 4, stderr line 1 == "cost cap $0.50
reached at Fix; stopping. Raise with --max-usd", then run_id; `--debug run --demo` → result.exception is
that error. 18. subprocess `python -c "import sys, warden.cli; …"` prints `[]` for sys.modules ∩ {openai,
rich, pydantic, pydantic_settings, httpx, contree_sdk, contree_client, tavily, warden.config,
warden.schemas, warden.demo, warden.runlog, warden.llm}. 19. `LLM(Settings(), CostMeter(cap_usd=0.0),
EventLog(tmp))` → `meter.log is log`; `charge(<planner model from settings>, 1_000_000, 0, "fix")` raises;
events.jsonl has kind cost_cap phase fix; str(exc) verbatim. 20. `doctor` with NO_COLOR → no ESC, stdout "".
Existing subprocess tests stay.

## 4. Done-when
`uv run ruff check . && uv run ruff format --check . && uv run mypy warden && uv run pytest -q` green
(60 + ~24 tests), then one line:
`uv run warden run --demo 2>&1 >/dev/null | grep -cE '^(Hunt|Plan|Fix|Verify|PR) +▸' | grep -qx 5 && uv run
warden run --demo --json 2>/dev/null | python -c 'import sys,json;[json.loads(l) for l in sys.stdin]' &&
FORCE_COLOR=1 uv run warden run --demo 2>&1 >/dev/null | grep -q $'\e' && FORCE_COLOR=1 NO_COLOR=1 uv run
warden --plain run --demo 2>&1 >/dev/null | grep -vq $'\e' && time uv run warden --help >/dev/null`
(< 100 ms; baseline 79 ms). Commit `feat(DX): ui.py — colour policy, fail(), PhaseLog; console --run;
meter log wiring; prompt-free init`. README unchanged (hero flags unchanged).

## 5. Risks
- Global-callback options must precede the subcommand; `run --plain` kept so old muscle memory works.
- Module-level ui state leaks between CliRunner invocations → autouse reset; developer shell NO_COLOR /
  FORCE_COLOR leak into tests → explicit `None` in env. `.env`/warden.toml in cwd → chdir tmp_path.
- Running-$ on a phase line is the total *at phase start* in non-TTY mode (by design; Total is final).
- `typer.Exit` is a RuntimeError; a future broad except in cli.py would swallow exits — comment it.
- demo notes + steps are written twice (events vs stderr); B6 may fold them into one helper.
- Wall-clock timing assertion is not in pytest (CI flake); import-set test (18) is the deterministic proxy.

## 6. Size
ui.py ≈ 130 lines; cli.py ±70; demo.py +15; client.py +2; tests ≈ 230. ~2 h. Allowed files only.
