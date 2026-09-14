"""Render the evidence table — the single artefact judges and reviewers read.

The console shows the same table; the PR embeds it. Rows are claims with the exact command and
result. An explicit "Not verified" row is always present.
"""

from __future__ import annotations

from warden.schemas import Plan, Verdict

PENDING_LABEL = "PENDING-HUMAN"


def evidence_table(
    issue: int,
    title: str,
    plan: Plan,
    verdict: Verdict,
    *,
    cost_usd: float,
    calls_by_model: dict[str, int],
    log_url: str | None = None,
) -> str:
    link = f" [↗]({log_url})" if log_url else ""
    theater = (
        "FAIL — as required" if verdict.theater_failed_on_base else "PASSED — tests are theater"
    )
    suite = ", ".join(f"{v} {k}" for k, v in verdict.suite.items()) or "not run"
    race = (
        f"{verdict.race.threads} threads → {verdict.race.effects_on_candidate} effect(s)"
        + (
            f" (was {verdict.race.effects_on_base})"
            if verdict.race.effects_on_base is not None
            else ""
        )
        if verdict.race
        else "n/a"
    )
    gate = " · ".join(
        [
            "mypy clean" if verdict.mypy_ok else "mypy FAIL",
            "ruff clean" if verdict.ruff_ok else "ruff FAIL",
            "1 alembic head" if verdict.alembic_single_head else "alembic heads ≠ 1",
            "no openapi drift" if not verdict.openapi_drift else "openapi DRIFT",
        ]
    )
    risk = ", ".join(plan.risk_class) or "none"
    risk_cell = f"{risk} → **{PENDING_LABEL}**" if plan.needs_human else risk
    not_verified = ", ".join(verdict.not_verified) or "—"
    calls = " · ".join(f"{m.split('/')[-1]} {n}" for m, n in calls_by_model.items())

    lines = [
        f"## Warden evidence — #{issue} {title}",
        "",
        f"**Verdict: {verdict.decision.replace('_', ' ')}**",
        "",
        "| Check | Result |",
        "|---|---|",
        f"| New tests on unfixed base | {theater}{link} |",
        f"| Full suite on candidate | {suite}{link} |",
        f"| Race probe | {race} |",
        f"| Gate | {gate} |",
        f"| Not verified | {not_verified} |",
        f"| Risk class | {risk_cell} |",
        f"| Cost | ${cost_usd:.2f} · {calls} |",
    ]
    if verdict.concerns:
        lines += ["", "**Concerns**", *[f"- {c}" for c in verdict.concerns]]
    if plan.citations:
        lines += ["", "**Plan citations**", *[f"- [{c.claim}]({c.url})" for c in plan.citations]]
    return "\n".join(lines) + "\n"
