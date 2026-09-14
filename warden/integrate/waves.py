"""files_to_touch + function names → conflict graph → waves (≤1 migration per wave).

Pure and offline: no sandbox, no LLM, no config import (the CLI passes ``settings.max_forks``).
Two plans conflict when they touch the same normalised file, unless both reference that file
only through ``path::function`` selectors that are disjoint and all verified via ``functions``.
Waves are assigned by deterministic greedy first-fit over plans sorted by issue number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from warden.schemas import Plan, Wave

MIGRATION_MARKERS: tuple[str, ...] = ("alembic/versions/", "migrations/")

_SELECTOR_SEP = "::"
_NO_VALUE = "—"


def _normalise(entry: str) -> tuple[str, str | None]:
    """Split a ``files_to_touch`` entry into (normalised path, selector or None)."""
    path, sep, name = entry.partition(_SELECTOR_SEP)
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path, (name or None) if sep else None


def is_migration(path: str) -> bool:
    """True iff any migration marker is a substring of the normalised path (conservative)."""
    norm, _ = _normalise(path)
    return any(marker in norm for marker in MIGRATION_MARKERS)


def plan_is_migration(plan: Plan) -> bool:
    return any(is_migration(p) for p in plan.files_to_touch)


def _touched(plan: Plan) -> dict[str, set[str] | None]:
    """path → set of selector names, or None when the plan lists the bare path anywhere."""
    out: dict[str, set[str] | None] = {}
    for entry in plan.files_to_touch:
        path, name = _normalise(entry)
        if name is None:
            out[path] = None
            continue
        current = out.get(path, set())
        if current is not None:
            current.add(name)
            out[path] = current
    return out


def conflicts(a: Plan, b: Plan, functions: Mapping[str, set[str]] | None = None) -> bool:
    """Conflict iff a shared path, unless both sides use disjoint, verified selectors."""
    ta, tb = _touched(a), _touched(b)
    for path in ta.keys() & tb.keys():
        sa, sb = ta[path], tb[path]
        if functions is None or path not in functions or sa is None or sb is None:
            return True
        known = functions[path]
        if not (sa <= known and sb <= known):
            return True
        if sa & sb:
            return True
    return False


def plan_waves(
    plans: Sequence[Plan],
    *,
    functions: Mapping[str, set[str]] | None = None,
    max_wave_size: int = 4,
) -> list[Wave]:
    """Greedy first-fit: lowest wave with no conflict, room left, and ≤1 migration."""
    if max_wave_size < 1:
        raise ValueError(f"max_wave_size must be >= 1, got {max_wave_size}")
    seen: set[int] = set()
    for plan in plans:
        if plan.issue in seen:
            raise ValueError(f"duplicate issue #{plan.issue} in plans")
        seen.add(plan.issue)

    waves: list[list[Plan]] = []
    has_migration: list[bool] = []
    for plan in sorted(plans, key=lambda p: p.issue):
        migration = plan_is_migration(plan)
        for i, members in enumerate(waves):
            if len(members) >= max_wave_size or (migration and has_migration[i]):
                continue
            if any(conflicts(plan, m, functions) for m in members):
                continue
            members.append(plan)
            has_migration[i] = has_migration[i] or migration
            break
        else:
            waves.append([plan])
            has_migration.append(migration)

    return [
        Wave(number=n, issues=[p.issue for p in members])
        for n, members in enumerate(waves, start=1)
    ]


def render_waves(waves: Sequence[Wave]) -> str:
    """Fixed-width plain-text table (no ANSI); ``"no waves\\n"`` for empty input."""
    if not waves:
        return "no waves\n"
    rows = [
        (
            str(w.number),
            " ".join(f"#{i}" for i in w.issues) or _NO_VALUE,
            (w.integration_image_uuid or "")[:8] or _NO_VALUE,
            " ".join(f"#{i}" for i in w.merged) or _NO_VALUE,
        )
        for w in waves
    ]
    header = ("Wave", "Issues", "Image", "Merged")
    widths = [max(len(r[c]) for r in (header, *rows)) + 2 for c in range(3)]
    lines = [
        "".join(cell.ljust(widths[c]) for c, cell in enumerate(r[:3])) + r[3]
        for r in (header, *rows)
    ]
    return "\n".join(lines) + "\n"
