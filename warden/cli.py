"""warden — hunt → plan → fix → verify → PR, on Nebius Sandboxes with Nemotron.

DX rules (clig.dev / Heroku): first output < 100 ms; `--help` and `doctor` never touch the
network; stdout is data, stderr is progress; `--json` streams JSONL events; every prompt has a flag.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer

from warden import ui
from warden.ui import fail

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
    return "unknown", "unknown -q"


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
) -> None:
    """Hunt → plan → fix → verify → PR for one issue. Prints run_id first (see `warden resume`)."""
    from warden.config import load_settings
    from warden.integrate.pr import PrOpenError
    from warden.llm.cost import CostCapExceededError
    from warden.runlog.events import EventLog, stdout_stream

    if plain:
        cfg = ui.current()
        ui.configure(ui.UIConfig(color=False, animate=False, debug=cfg.debug))
    s = load_settings()
    if max_usd is not None:
        s.max_usd_per_issue = max_usd
    log = EventLog(s.runs_dir, json_stream=stdout_stream() if json_out else None)
    _err(f"run_id {log.run_id}   (resume with: warden resume {log.run_id})")
    progress = ui.PhaseLog()  # stderr; --json only changes stdout

    # The only sanctioned catches: they translate to the error block + exit code, never continue.
    try:
        if demo:
            import warden.demo

            result = warden.demo.run_demo(s, log, dry_run=dry_run, progress=progress)
            log.result(**result)
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
        fail(
            "live pipeline lands in Phase B (see docs/warden-plan.md §5)",
            fix="use --demo for now",
            run_id=log.run_id,
            code=3,
        )
    except CostCapExceededError as e:  # cost.py already emitted the cost_cap event
        fail(str(e), run_id=log.run_id, code=4, exc=e)
    except PrOpenError as e:
        fail(str(e), fix=e.fix, run_id=log.run_id, exc=e)
    finally:
        log.close()


@app.command()
def resume(run_id: Annotated[str, typer.Argument()]) -> None:
    """Continue an interrupted run from its last completed phase."""
    from warden.config import load_settings
    from warden.runlog.events import EventLog

    s = load_settings()
    events = EventLog.read(s.runs_dir, run_id)
    if not events:
        fail(f"no run {run_id} under {s.runs_dir}", fix=f"ls {s.runs_dir}")
    last = events[-1].phase
    fail(
        f"{run_id}: {len(events)} events, last phase {last}; resume lands in Phase B6",
        fix="re-run: warden run --issue N",
        run_id=run_id,
        code=3,
    )


@app.command()
def console(
    run_id: Annotated[
        str | None, typer.Option("--run-id", "--run", help="Render one run to static HTML.")
    ] = None,
    static: Annotated[
        bool, typer.Option(help="Write runs/<id>/index.html instead of serving.")
    ] = True,
) -> None:
    """Render a run's timeline, branch tree and evidence table."""
    from warden.config import load_settings
    from warden.runlog.render import render_run

    s = load_settings()
    if run_id is None:
        runs = (
            sorted(p.name for p in s.runs_dir.iterdir() if p.is_dir())
            if s.runs_dir.exists()
            else []
        )
        if not runs:
            fail("no runs yet", fix="warden run --demo")
        run_id = runs[-1]
    out = render_run(s.runs_dir, run_id)
    print(out)


if __name__ == "__main__":
    app()
