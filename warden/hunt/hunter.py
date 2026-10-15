"""Orchestrate the probe axes against a forked, seeded app; load the known-issues baseline first.

Lands in Phase C5 of docs/warden-plan.md. The Hunter boots a fork of the base image, runs an
axis's probes (currently IDOR / cross-tenant access), reproduces each hit before trusting it, and
labels re-finds of register entries as regressions. The HTTP work lives in the axis module and runs
*inside* the sandbox; the orchestration here is pure given a :class:`Prober`, so tests drive it with
canned runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from warden.hunt.axes.idor import CONFIRM_THRESHOLD, IdorRun, findings_from_runs
from warden.hunt.baseline import Baseline
from warden.schemas import Axis, Finding

if TYPE_CHECKING:
    from warden.runlog.events import EventLog

SUPPORTED_AXES: frozenset[Axis] = frozenset({"idor"})


class Prober(Protocol):
    """Produces one :class:`IdorRun` per call; each call is a fresh fork of the seeded base."""

    def probe(self) -> IdorRun: ...


@dataclass
class HuntReport:
    axis: Axis
    confirmed: list[Finding] = field(default_factory=list)
    plausible: list[Finding] = field(default_factory=list)
    runs: int = 0
    endpoints_enumerated: int = 0
    tenants: int = 0
    probe_error: str = ""
    attacker: dict[str, object] = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return bool(self.confirmed)


def run_hunt(
    axis: Axis,
    prober: Prober,
    *,
    baseline: Baseline,
    log: EventLog,
    reproduce: int = CONFIRM_THRESHOLD,
) -> HuntReport:
    """Run `axis`'s probes `reproduce` times, fold into findings, and label regressions.

    A finding is confirmed (and will be filed) only when it reproduces in every one of the
    `reproduce` runs; anything less is plausible and logged, never filed (runbook §3.4).
    """
    if axis not in SUPPORTED_AXES:
        raise ValueError(f"axis {axis!r} is not implemented; supported: {sorted(SUPPORTED_AXES)}")
    runs: list[IdorRun] = []
    for i in range(max(reproduce, 1)):
        run = prober.probe()
        runs.append(run)
        log.emit(
            "hunt",
            "note",
            {
                "probe_run": i + 1,
                "vulnerable": [o.probe for o in run.observations if o.vulnerable],
                "endpoints": run.endpoints_enumerated,
                "tenants": run.tenants,
                "error": run.error,
            },
        )
        if run.error:
            break
    endpoints = max((r.endpoints_enumerated for r in runs), default=0)
    tenants = max((r.tenants for r in runs), default=0)
    probe_error = next((r.error for r in runs if r.error), "")
    attacker = next((r.attacker for r in runs if r.attacker), {})
    confirmed, plausible = findings_from_runs(runs, threshold=reproduce)
    for finding in (*confirmed, *plausible):
        finding.regression_of = baseline.regression_id(finding)
    for finding in confirmed:
        log.emit(
            "hunt",
            "note",
            {
                "finding": finding.title,
                "severity": finding.severity,
                "root_cause_key": finding.root_cause_key,
                "reproduced_times": finding.reproduced_times,
                "regression_of": finding.regression_of,
            },
        )
    return HuntReport(
        axis=axis,
        confirmed=confirmed,
        plausible=plausible,
        runs=len(runs),
        endpoints_enumerated=endpoints,
        tenants=tenants,
        probe_error=probe_error,
        attacker=attacker,
    )


# ---------------------------------------------------------------- live sandbox prober


FABRIC_REGISTER = "docs/reviews/flows/12-bug-register.md"
FABRIC_PENTEST = "docs/reviews/pentest/02-authz-idor.md"


def fabric_baseline(repo_clone: Path) -> Baseline:
    """Load Fabric's known-issues register from a local clone (``/Users/moizp/fabric`` locally)."""
    from warden.hunt.baseline import load_baseline

    return load_baseline(repo_clone / FABRIC_REGISTER, extra=(repo_clone / FABRIC_PENTEST,))
