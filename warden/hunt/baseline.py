"""Known-issues baseline: re-finds of already-registered bugs are labelled regressions.

The Hunter loads this first (docs/warden-plan.md C5). The register lives in the *target* repo
(Fabric: ``docs/reviews/flows/12-bug-register.md`` and the ``docs/reviews/pentest/`` slices), so a
finding that matches an entry already on record is a regression, not a novel discovery.

Matching is deliberately shallow and deterministic: register entries are keyed by their ids
(``SEC-1``, ``AUTHZ-2``, ``GL-1`` …); a finding matches the entry whose text shares the most of the
finding's endpoint tokens and an axis keyword. Nothing here calls a model or the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from warden.schemas import Axis, Finding

# A register id: SEC-1, AUTHZ-2, GL-1, PROC-1, AUTH-1 … (2–6 uppercase letters, dash, digits).
_ID = re.compile(r"\b([A-Z]{2,6}-\d+)\b")
# Endpoint tokens inside a finding's repro/title, e.g. /parties, /items, /auth/login.
_ENDPOINT = re.compile(r"/[a-z][a-z0-9_]*(?:/[a-z][a-z0-9_{}]*)*")
# Axis → the register keywords that mark an entry as belonging to that axis.
_AXIS_KEYWORDS: dict[Axis, tuple[str, ...]] = {
    "idor": ("firm", "cross-org", "cross-firm", "tenant", "idor", "isolation", "org_id"),
    "race": ("race", "concurrent", "idempoten", "lost update", "row-lock"),
    "money": ("gl", "voucher", "ledger", "balance", "gst", "tax", "control", "sub-ledger"),
    "malformed": ("500", "unhandled", "malformed", "validation"),
    "flow": ("state machine", "status", "transition", "reversal"),
}


@dataclass(frozen=True)
class Baseline:
    """Register entries by id, plus the path they came from (``None`` = no register found)."""

    by_id: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self.by_id)

    def regression_id(self, finding: Finding) -> str | None:
        """The id of the register entry this finding re-finds, or ``None`` if it is novel.

        An entry qualifies only if it carries an axis keyword; among those, the one sharing the
        most endpoint tokens with the finding wins (ties break on the register's own order).
        """
        # Tokens come from the title and actual (not the curl repro, whose signup boilerplate
        # would match unrelated auth entries). The endpoint under test is in the title.
        haystack = f"{finding.title}\n{finding.actual}".lower()
        tokens = {m.group(0) for m in _ENDPOINT.finditer(haystack)}
        keywords = _AXIS_KEYWORDS.get(finding.axis, ())
        best: tuple[int, int] | None = None  # (score, -order) for max()
        best_id: str | None = None
        for order, (rid, text) in enumerate(self.by_id.items()):
            low = text.lower()
            if keywords and not any(k in low for k in keywords):
                continue  # only entries that belong to this axis are candidates
            score = sum(1 for t in tokens if t and t in low)
            if score == 0:
                continue  # an endpoint-token overlap is required — no axis-keyword freebie
            key = (score, -order)
            if best is None or key > best:
                best, best_id = key, rid
        return best_id


def load_baseline(register: Path, *, extra: tuple[Path, ...] = ()) -> Baseline:
    """Parse the register (and any ``extra`` review files) into a :class:`Baseline`.

    A missing register is not fatal — the Hunter still runs, just without regression labels.
    Each id maps to every line that mentions it (table rows, headings, bullets), lower-cased
    matching happens at query time.
    """
    sources = [register, *extra]
    by_id: dict[str, list[str]] = {}
    found_any = False
    for src in sources:
        try:
            text = src.read_text(encoding="utf-8")
        except OSError:
            continue
        found_any = True
        for line in text.splitlines():
            ids = _ID.findall(line)
            for rid in ids:
                by_id.setdefault(rid, []).append(line.strip())
    merged = {rid: "\n".join(lines) for rid, lines in by_id.items()}
    return Baseline(by_id=merged, path=register if found_any else None)
