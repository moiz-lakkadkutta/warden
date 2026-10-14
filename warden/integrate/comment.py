"""The issue comment the GitHub Action posts after a run: the draft PR link, or why there is none,
and what the run cost. Built only from files under runs/<id>/, so it works after any exit:
a clean finish (result.json), a recorded failure (an `error` / `cost_cap` event), or a crash.

The comment never quotes the issue body back, and never contains a secret: run artefacts hold
neither.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from warden.integrate.pr import PENDING_LABEL
from warden.schemas import RunEvent

MARKER = "<!-- warden-run -->"


def _events(run_dir: Path) -> list[RunEvent]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    return [
        RunEvent.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _failure(events: list[RunEvent]) -> tuple[str, str | None] | None:
    """(sentence, fix) of the last recorded failure, if any."""
    for ev in reversed(events):
        if ev.kind == "error" and ev.data.get("text"):
            fix = ev.data.get("fix")
            return str(ev.data["text"]), str(fix) if fix else None
        if ev.kind == "cost_cap":
            spent = float(ev.data.get("spent_usd", 0.0))
            cap = float(ev.data.get("cap_usd", 0.0))
            return (
                f"cost cap reached in {ev.phase}: ${spent:.2f} of ${cap:.2f}",
                "raise the action's `max-usd` input and re-add the label",
            )
    return None


def issue_comment(run_dir: Path, *, run_id: str, exit_code: int, log_url: str | None = None) -> str:
    events = _events(run_dir)
    spent = round(sum(ev.usd or 0.0 for ev in events), 2)
    result = _json(run_dir / "result.json")
    pr = _json(run_dir / "artifacts" / "pr.json") or {}
    body_path = run_dir / "artifacts" / "pr_body.md"
    evidence = body_path.read_text(encoding="utf-8") if body_path.exists() else ""

    lines = [MARKER]
    pr_url = (result or {}).get("pr_url")
    verdict = str((result or {}).get("verdict") or "").replace("_", " ")
    show_evidence = False
    if pr_url:
        lines.append(f"**Warden opened a draft PR: {pr_url}** (verifier: {verdict}).")
        if pr.get("label") == PENDING_LABEL:
            lines.append(
                f"It is labelled `{PENDING_LABEL}`: the plan touches money, tax, schema or "
                "security, so a human decides whether it lands."
            )
    elif result is not None and verdict == "FAIL":
        lines.append(
            "**Warden did not open a PR: the verifier rejected the candidate fix (FAIL).** "
            "The evidence it used is below."
        )
        show_evidence = bool(evidence)
    elif result is not None and verdict:
        lines.append(
            f"**Warden finished (verifier: {verdict}) but opened no PR.** See the run log."
        )
        show_evidence = bool(evidence)
    else:
        failure = _failure(events)
        if failure is not None:
            text, fix = failure
            lines.append(f"**Warden stopped before opening a PR:** {text}")
            if fix:
                lines.append(f"Fix: {fix}")
        else:
            lines.append(
                f"**Warden stopped unexpectedly (exit {exit_code}) before opening a PR.** "
                "See the run log for the traceback."
            )
    lines += [
        "",
        f"Cost: **${spent:.2f}** · run `{run_id}`" + (f" · [log]({log_url})" if log_url else ""),
    ]
    if show_evidence:
        lines += [
            "",
            "<details><summary>Evidence</summary>",
            "",
            evidence.strip(),
            "",
            "</details>",
        ]
    lines += ["", "_Warden opens draft PRs only and never merges._"]
    return "\n".join(lines) + "\n"
