"""warden — hunt → plan → fix → verify → PR, on Nebius Sandboxes with Nemotron.

DX rules (clig.dev / Heroku): first output < 100 ms; `--help` and `doctor` never touch the
network; stdout is data, stderr is progress; `--json` streams JSONL events; every prompt has a flag.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from warden import ui
from warden.ui import fail

if TYPE_CHECKING:
    from warden.runlog.events import EventLog

app = typer.Typer(
    name="warden",
    help="Autonomous hunt → plan → fix → verify → PR agent on Nebius Token Factory Sandboxes.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,  # unexpected errors: plain traceback, rich never imported
)

PHASES = ("hunt", "plan", "fix", "verify", "pr")

# NOTE: `typer.Exit` subclasses RuntimeError — never `except RuntimeError` / `except Exception`
# around command bodies, or exits (and the cost cap) get swallowed.


@app.callback()
def main(
    plain: Annotated[bool, typer.Option("--plain", help="No colour or animation.")] = False,
    debug: Annotated[
        bool, typer.Option("--debug", help="Show tracebacks after the error block.")
    ] = False,
) -> None:
    """Global options; they must precede the subcommand: `warden --plain run --demo`."""
    ui.configure(ui.resolve(plain=plain, debug=debug))


def _err(msg: str) -> None:
    ui.err(msg)


def _mark(ok: bool) -> str:
    return ui.paint("✓", "32") if ok else ui.paint("✗", ui.RED)


@contextlib.contextmanager
def _guarded(log: EventLog) -> Iterator[None]:
    """The only sanctioned catches around the machine: each becomes the error block + exit code.

    Cost cap / provider budget (402) → 4, PR open → 1, not-ready → 3. Nothing else is caught
    (see the NOTE above): `typer.Exit` and real bugs propagate. The log is closed on every path.
    """
    from warden.integrate.pr import PrOpenError
    from warden.llm.client import ProviderBudgetError
    from warden.llm.cost import CostCapExceededError
    from warden.pipeline import NotReadyError

    try:
        yield
    except CostCapExceededError as e:  # cost.py already emitted the cost_cap event
        fail(str(e), run_id=log.run_id, code=4, exc=e)
    except ProviderBudgetError as e:  # client.py already emitted the error event
        fail(str(e), fix=f"{e.fix} {log.run_id}", run_id=log.run_id, code=4, exc=e)
    except PrOpenError as e:
        fail(str(e), fix=e.fix, run_id=log.run_id, exc=e)
    except NotReadyError as e:
        fail(str(e), fix=e.fix, run_id=log.run_id, code=3, exc=e)
    finally:
        log.close()


def _plain_override(plain: bool) -> None:
    if plain:
        cfg = ui.current()
        ui.configure(ui.UIConfig(color=False, animate=False, debug=cfg.debug))


# --------------------------------------------------------------------------- doctor
@app.command()
def doctor(
    live: Annotated[
        bool, typer.Option(help="Also call each model once (costs a few cents).")
    ] = False,
) -> None:
    """Check local tools, credentials and (optionally) model reachability. No network by default."""
    from warden.config import load_settings

    print("warden doctor", file=sys.stderr)
    s = load_settings()
    checks: list[tuple[str, bool, str]] = []

    checks.append(
        ("Python ≥ 3.12", sys.version_info >= (3, 12), "install with: uv python install 3.12")
    )
    checks.append(("git", shutil.which("git") is not None, "install git"))
    checks.append(
        ("gh (GitHub CLI)", shutil.which("gh") is not None, "brew install gh && gh auth login")
    )
    checks.append(
        (
            "NEBIUS_API_KEY",
            bool(s.nebius_api_key),
            "get a key at tokenfactory.nebius.com → put it in .env",
        )
    )
    checks.append(
        (
            "CONTREE_BASE_URL (Sandboxes)",
            bool(s.contree_base_url),
            "request Sandboxes beta access; set CONTREE_BASE_URL",
        )
    )
    checks.append(("NEBIUS_PROJECT_ID", bool(s.nebius_project_id), "copy from the Nebius console"))
    checks.append(
        ("TAVILY_API_KEY", bool(s.tavily_api_key), "free key at tavily.com; needed for cited plans")
    )
    checks.append(
        (
            f"WARDEN_TARGET_REPO ({s.target_repo or 'unset'})",
            s.target_repo.count("/") == 1,
            "set WARDEN_TARGET_REPO=owner/name in .env",
        )
    )
    checks.append(
        (
            f"WARDEN_WORKDIR ({s.workdir})",
            s.workdir.startswith("/"),
            "set WARDEN_WORKDIR to the absolute repo root inside the base image",
        )
    )
    from warden.verify.gate import PROFILES

    checks.append(
        (
            f"WARDEN_GATE_PROFILE ({s.gate_profile})",
            s.gate_profile in PROFILES,
            f"set WARDEN_GATE_PROFILE to one of: {', '.join(PROFILES)}",
        )
    )
    checks.append(
        (
            f"WARDEN_SPEC_CHECK ({'on' if s.spec_check else 'off'})",
            True,
            "set WARDEN_SPEC_CHECK=0 to skip the verifier's spec review (one Ultra call per issue)",
        )
    )
    checks.append((".env not tracked by git", not _env_tracked(), "git rm --cached .env"))

    if live:
        checks.extend(_live_model_checks(s))

    width = max(len(c[0]) for c in checks) + 2
    all_ok = True
    for name, ok, fix in checks:
        all_ok &= ok
        line = f"  {_mark(ok)} {name.ljust(width)}"
        if not ok:
            line += f"→ {fix}"
        _err(line)
    _err("")
    _err("Model routing:")
    for role, model in s.routing_table():
        _err(f"  {role.ljust(12)} {model}")
    raise typer.Exit(code=0 if all_ok else 1)


def _env_tracked() -> bool:
    import subprocess

    if shutil.which("git") is None or not Path(".git").exists():
        return False
    res = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], capture_output=True, text=True
    )
    return res.returncode == 0


def _live_model_checks(s: object) -> list[tuple[str, bool, str]]:
    from warden.config import Role, Settings
    from warden.llm.client import LLM
    from warden.llm.cost import CostCapExceededError, CostMeter
    from warden.runlog.events import EventLog

    assert isinstance(s, Settings)
    out: list[tuple[str, bool, str]] = []
    log = EventLog(s.runs_dir, "doctor")
    llm = LLM(s, CostMeter(cap_usd=0.50, log=log), log)
    try:
        for role in (Role.PLANNER, Role.IMPLEMENTER, Role.TOOLS):
            try:
                r = llm.chat(
                    role, "plan", [{"role": "user", "content": "Reply with the single word: ok"}]
                )
                out.append(
                    (f"model {s.model_for(role)}", bool(r.content), "check model id / quota")
                )
            except CostCapExceededError:
                raise  # never caught to continue
            except Exception as exc:  # noqa: BLE001
                out.append((f"model {s.model_for(role)}", False, str(exc)[:80]))
    finally:
        log.close()
    return out


# ------------------------------------------------------------------------------ init
@app.command()
def init(
    repo: Annotated[Path, typer.Argument(help="Path to the target repository.")] = Path("."),
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Accept detected defaults.")] = False,
) -> None:
    """Detect the repo's test runner and write warden.toml. Never prompts (0 questions)."""
    repo = repo.resolve()
    app_dir = "backend" if (repo / "backend").is_dir() else "."
    runner, test_cmd = _detect_test_cmd(repo, app_dir)
    cfg = repo / "warden.toml"
    if cfg.exists() and not yes:
        fail(f"{cfg} exists", fix="re-run with --yes to overwrite")
    if not (repo / ".git").exists():
        _err(f"warning: {repo} has no .git — Warden opens PRs from a git checkout")
    if runner == "unknown":
        _err("could not detect a test runner; set test_cmd in warden.toml")
    cfg.write_text(
        "\n".join(
            [
                "# warden.toml — project settings",
                "# precedence: flags > env > this file > ~/.config/warden/config.toml",
                f'test_cmd = "{test_cmd}"',
                f'app_dir = "{app_dir}"',
                'base_image = "warden/fabric-base:latest"',
                "max_usd_per_issue = 3.0",
                "max_forks = 4",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _err(f"wrote {cfg} (test runner: {runner}, app dir: {app_dir})")


def _detect_test_cmd(repo: Path, app_dir: str) -> tuple[str, str]:
    """(runner, command). Looks only at the root and app_dir — no recursive globs."""
    roots = [repo] if app_dir == "." else [repo, repo / app_dir]
    for root in roots:
        if any(
            (root / f).exists() for f in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
        ):
            return "pytest", "pytest -q"
    for root in roots:
        if (root / "package.json").exists():
            return "npm", "npm test"
        if (root / "go.mod").exists():
            return "go", "go test ./..."
        if (root / "Cargo.toml").exists():
            return "cargo", "cargo test"
    return "unknown", ""


# ------------------------------------------------------------------------------- run
@app.command()
def run(
    issue: Annotated[int | None, typer.Option(help="GitHub issue number to fix.")] = None,
    demo: Annotated[
        bool,
        typer.Option(help="Run against the bundled demo with the fake sandbox (no keys needed)."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Stop after Plan and print it.")
    ] = False,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Stream JSONL events to stdout; final line is the result."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Open the PR without asking.")] = False,
    max_usd: Annotated[float | None, typer.Option(help="Override the per-issue cost cap.")] = None,
    plain: Annotated[bool, typer.Option(help="No colour or animation.")] = False,
    run_id: Annotated[str | None, typer.Option("--run-id", hidden=True)] = None,
    at: Annotated[
        str | None,
        typer.Option(
            "--at",
            help="Replay a closed issue at this commit (e.g. the fix's parent): plan, fix and "
            "verify run on that code, issue comments are hidden, the PR targets warden/base-<sha>.",
        ),
    ] = None,
) -> None:
    """Hunt → plan → fix → verify → PR for one issue. Prints run_id first (see `warden resume`)."""
    from warden.config import load_settings
    from warden.runlog.events import EventLog, stdout_stream

    _plain_override(plain)
    s = load_settings()
    if max_usd is not None:
        s.max_usd_per_issue = max_usd
    if run_id is not None and (s.runs_dir / run_id / "run.json").exists():
        fail(
            f"run {run_id} already exists",
            fix=f"warden resume {run_id}, or pick another --run-id",
            run_id=run_id,
            code=2,
        )
    log = EventLog(s.runs_dir, run_id, json_stream=stdout_stream() if json_out else None)
    _err(f"run_id {log.run_id}   (resume with: warden resume {log.run_id})")
    progress = ui.PhaseLog()  # stderr; --json only changes stdout

    with _guarded(log):
        if demo:
            import warden.demo

            result = warden.demo.run_demo(
                s, log, dry_run=dry_run, progress=progress, yes=yes, json_out=json_out
            )
            if not json_out:
                _err(result["evidence"])
            raise typer.Exit(0)

        if issue is None:
            fail(
                "nothing to run",
                fix="pass --issue N, or --demo to try Warden without credentials",
                run_id=log.run_id,
                code=2,
            )
        missing = s.missing_for("NEBIUS_API_KEY", "CONTREE_BASE_URL", "NEBIUS_PROJECT_ID")
        if missing:
            fail(
                f"missing {', '.join(missing)}",
                fix="run `warden doctor`",
                run_id=log.run_id,
                code=2,
            )
        from warden.pipeline import live_steps, run_pipeline

        result = run_pipeline(
            s,
            log,
            live_steps(),
            start_after=None,
            json_out=json_out,
            yes=yes,
            progress=progress,
            stop_after="plan" if dry_run else None,
            requested_issue=issue,
            at_rev=at,
        )
        if not json_out:
            _err(result["evidence"])


@app.command("eval")
def eval_swe(
    n: Annotated[int, typer.Option(help="How many SWE-rebench tasks to sample.")] = 10,
    seed: Annotated[int, typer.Option(help="Sampling seed (fixed for a reportable run).")] = 7,
    budget: Annotated[
        float, typer.Option(help="Stop before a task that could take total spend over this (USD).")
    ] = 5.0,
    max_usd: Annotated[float | None, typer.Option(help="Override the per-task cost cap.")] = None,
    out: Annotated[
        Path, typer.Option(help="Directory for tasks.jsonl, task_ids.txt, results.csv.")
    ] = Path("evals/swe_rebench"),
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Start without asking.")] = False,
) -> None:
    """SWE-rebench on Nebius preloaded images: plan → fix → verify → hidden tests → results.csv.

    Tasks already in results.csv are skipped, so re-running the same command resumes."""
    from warden.config import load_settings
    from warden.eval import swe_rebench as swe
    from warden.runlog.events import EventLog

    s = load_settings()
    if max_usd is not None:
        s.max_usd_per_issue = max_usd
    missing = s.missing_for("NEBIUS_API_KEY", "CONTREE_BASE_URL", "NEBIUS_PROJECT_ID")
    if missing:
        fail(f"missing {', '.join(missing)}", fix="run `warden doctor`", code=2)
    tasks_path = out / "tasks.jsonl"
    if tasks_path.exists():
        tasks = swe.load_tasks(tasks_path)
    else:
        _err(f"sampling {n} tasks from {swe.DATASET} ({swe.SPLIT}, seed {seed})")
        tasks = swe.fetch_sample(tasks_path, n, seed=seed)
        (out / "task_ids.txt").write_text("\n".join(t.instance_id for t in tasks) + "\n")
    csv_path = out / "results.csv"
    todo = [t for t in tasks if t.instance_id not in swe.done_ids(csv_path)]
    _err(
        f"{len(todo)} of {len(tasks)} tasks to run · budget ${budget:.2f} · "
        f"cap ${s.max_usd_per_issue:.2f}/task · results → {csv_path}"
    )
    if not todo:
        raise typer.Exit(0)
    if not yes and not typer.confirm("Start (this spends Token Factory credit)?", err=True):
        raise typer.Exit(1)

    def show(row: swe.Row) -> None:
        mark = "✓" if row.resolved else "✗"
        extra = f"  {row.error}" if row.error else ""
        _err(
            f"{mark} {row.task_id}  {row.verdict}  ${row.usd:.2f}  {row.seconds}s"
            f"{'  FALSE PASS' if row.false_pass else ''}{extra}"
        )

    log = EventLog(s.runs_dir, "eval")
    with _guarded(log):
        rows = swe.run_eval(todo, s, csv_path=csv_path, budget_usd=budget, on_row=show)
    resolved = sum(r.resolved for r in rows)
    _err(f"ran {len(rows)} · resolved {resolved} · spent ${sum(r.usd for r in rows):.2f}")


@app.command()
def resume(
    run_id: Annotated[str, typer.Argument(help="The run_id printed by `warden run`.")],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Stream JSONL events to stdout; final line is the result."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Open the PR without asking.")] = False,
    plain: Annotated[bool, typer.Option(help="No colour or animation.")] = False,
) -> None:
    """Continue an interrupted run from its last completed phase (artifacts decide, not events)."""
    from warden.config import load_settings
    from warden.pipeline import completed_phases, live_steps, run_pipeline
    from warden.runlog.events import EventLog, stdout_stream

    _plain_override(plain)
    s = load_settings()
    run_dir = s.runs_dir / run_id
    if not (run_dir / "events.jsonl").exists():
        fail(f"no run {run_id} under {s.runs_dir}", fix=f"ls {s.runs_dir}", run_id=run_id)
    if not (run_dir / "run.json").exists():
        fail(f"run {run_id} predates resume", fix="re-run: warden run --demo", run_id=run_id)
    done = completed_phases(run_dir)
    if "pr" in done:
        fail(
            f"run {run_id} already complete",
            fix=f"warden console --run {run_id}",
            run_id=run_id,
            code=2,
        )
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    mode = manifest.get("mode")
    log = EventLog(s.runs_dir, run_id, json_stream=stdout_stream() if json_out else None)
    _err(f"run_id {run_id}   (resuming after {done[-1] if done else 'start'})")
    progress = ui.PhaseLog()

    with _guarded(log):
        if mode == "demo":
            import warden.demo

            steps = warden.demo.demo_steps()
        else:
            steps = live_steps()
        result = run_pipeline(
            s,
            log,
            steps,
            start_after=done[-1] if done else None,
            json_out=json_out,
            yes=yes,
            progress=progress,
            requested_issue=manifest.get("issue"),
        )
        if not json_out:
            _err(result["evidence"])


@app.command()
def console(
    run_ids: Annotated[
        list[str] | None,
        typer.Argument(help="Run ids to render (default: the latest run).", show_default=False),
    ] = None,
    run_id: Annotated[
        str | None, typer.Option("--run-id", "--run", help="Render one run to static HTML.")
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Write a deployable site here: index.html listing the runs, <id>/index.html "
            "per run with its events.jsonl, cost.jsonl, pr_body.md and timeline.gif.",
        ),
    ] = None,
    all_runs: Annotated[bool, typer.Option("--all", help="Every run under runs/.")] = False,
    gif: Annotated[
        bool, typer.Option(help="Also render runs/<id>/timeline.gif (needs the gif extra).")
    ] = True,
    static: Annotated[
        bool, typer.Option(help="Write runs/<id>/index.html instead of serving.")
    ] = True,
) -> None:
    """Render a run's timeline, branch tree and evidence table (stdout: the page written)."""
    from warden.config import load_settings
    from warden.runlog.site import list_runs

    s = load_settings()
    known = list_runs(s.runs_dir)
    ids = list(run_ids or []) + ([run_id] if run_id else [])
    if all_runs:
        ids = known
    if not known and not ids:
        fail("no runs yet", fix="warden run --demo")
    if not ids:
        ids = known[-1:]
    missing = [i for i in ids if i not in known]
    if missing:
        fail(f"no run {missing[0]} under {s.runs_dir}", fix=f"ls {s.runs_dir}", code=2)

    if out is not None:
        from warden.runlog.site import build_site

        for warning in build_site(s.runs_dir, ids, out, gif=gif):
            _err(f"warning: {warning}")
        _err(f"wrote {len(ids)} run page(s) to {out}")
        print(out / "index.html")
        return

    from warden.runlog.gif import GifUnavailableError
    from warden.runlog.render import render_run
    from warden.runlog.site import write_gif

    for rid in ids:
        has_gif = False
        if gif:
            try:
                _err(f"wrote {write_gif(s.runs_dir, rid)}")
                has_gif = True
            except GifUnavailableError as exc:
                _err(f"warning: {exc}; skipped timeline.gif → {exc.fix}")
        print(render_run(s.runs_dir, rid, has_gif=has_gif))


if __name__ == "__main__":
    app()
