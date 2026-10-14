"""Lightning (queries + grep terms) → Tavily → repo context → Ultra → Plan with checked citations.

Inputs follow docs/warden-plan.md §6: issue text, `git ls-files` of the relevant dirs, grep hits
for identifiers the issue names, and Tavily results (title, url, 400-char snippet). Citations are
kept only if their URL came back from research, so every cited URL resolves.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel

from warden.config import Role
from warden.hunt.issue import IssueText
from warden.plan.research import Source
from warden.runlog.events import EventLog
from warden.schemas import Phase, Plan, ResearchQueries

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Layout:
    """Where the planner looks: repo root in the box, the dirs it lists, the paths it greps."""

    root: str = "/src"
    ls: tuple[str, ...] = ("backend/app", "backend/tests", "backend/alembic/versions")
    grep: tuple[str, ...] = ("backend",)


FABRIC_LAYOUT = Layout()


def generic_layout(root: str) -> Layout:
    """Any Python repo: list and grep every tracked .py file under `root`."""
    return Layout(root=root, ls=("*.py",), grep=("*.py",))


LS_LIMIT = 600
GREP_LINES_PER_TERM = 12
MAX_TERMS = 10
TOP_FILES = 2
SOURCE_LINES = 450
LIGHTNING_CONTEXT_CHARS = 12000

_IDENT = re.compile(
    r"`([A-Za-z_][\w.]*)`"  # anything in backticks
    r"|\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b"  # snake_case
    r"|\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b"  # CamelCase (two+ humps)
)

QUERY_SYSTEM = (
    "You prepare research for a code fix. You get the issue and the relevant code. Write up to 3 "
    "web search queries about the LIBRARY behaviour the fix depends on, naming the exact API the "
    "code uses (e.g. 'SQLAlchemy ColumnOperators.in_', 'PostgreSQL SELECT ... FOR UPDATE'). Also "
    "list up to 8 identifiers from the code (functions, constants, classes) that the fix will "
    "touch and that are worth grepping for. Never invent identifiers absent from the code."
)


class StructuredLLM(Protocol):
    def structured(
        self, role: Role, phase: Phase, system: str, user: str, schema: type[T]
    ) -> T: ...


def code_terms(text: str, *, extra: list[str] | None = None) -> list[str]:
    """Identifiers worth grepping for, in first-seen order (extra first), capped at MAX_TERMS."""
    found: list[str] = list(extra or [])
    for m in _IDENT.finditer(text):
        term = next(g for g in m.groups() if g)
        if term not in found:
            found.append(term)
    return found[:MAX_TERMS]


def context_command(
    terms: list[str],
    *,
    rev: str | None = None,
    with_files: bool = True,
    layout: Layout = FABRIC_LAYOUT,
) -> str:
    """One POSIX-sh command (the sandbox shell is dash) printing the file list then grep hits.

    With `rev`, read that revision straight from git (no checkout), so a closed issue can be
    replayed against the code as it was before the fix — not the already-fixed HEAD.
    """
    dirs = " ".join(shlex.quote(d) for d in layout.ls)
    where = " ".join(shlex.quote(g) for g in layout.grep)
    cd = f"cd {shlex.quote(layout.root)}"
    if rev is None:
        parts = [cd]
        if with_files:
            parts.append(f"git ls-files -- {dirs} | head -{LS_LIMIT}")
    else:
        parts = [cd, f"rev=$(git rev-parse --verify {shlex.quote(rev + '^{commit}')})"]
        if with_files:
            parts.append(f'git ls-tree -r --name-only "$rev" -- {dirs} | head -{LS_LIMIT}')
    for term in terms:
        q = shlex.quote(term)
        parts.append(f"echo @@GREP {q}")
        if rev is None:
            grep = f"git grep -n -I -F -e {q} -- {where}"
        else:
            grep = f'git grep -n -I -F -e {q} "$rev" -- {where} | sed "s|^$rev:||"'
        parts.append(f"{grep} | head -{GREP_LINES_PER_TERM}")
    return "; ".join(parts)


def top_source_files(context: str, *, k: int = TOP_FILES) -> list[str]:
    """Non-test files with the most grep hits (ties keep first-seen order)."""
    counts: dict[str, int] = {}
    for line in context.splitlines():
        path, sep, rest = line.partition(":")
        if not sep or not rest[:1].isdigit() or "/tests/" in f"/{path}" or not path.endswith(".py"):
            continue
        counts[path] = counts.get(path, 0) + 1
    return sorted(counts, key=lambda p: -counts[p])[:k]


def source_command(paths: list[str], *, rev: str | None = None, root: str = "/src") -> str:
    """Numbered source of `paths` (at `rev` if given), one `@@FILE path` block each."""
    parts = [f"cd {shlex.quote(root)}"]
    if rev is not None:
        parts.append(f"rev=$(git rev-parse --verify {shlex.quote(rev + '^{commit}')})")
    for path in paths:
        q = shlex.quote(path)
        show = f'git show "$rev":{q}' if rev is not None else f"cat {q}"
        parts.append(f"echo @@FILE {q}")
        parts.append(f"{show} | nl -ba -w6 -s': ' | head -{SOURCE_LINES}")
    return "; ".join(parts)


def gather_context(
    run: Callable[[str], str],
    terms: list[str],
    *,
    rev: str | None = None,
    with_files: bool = True,
    layout: Layout = FABRIC_LAYOUT,
) -> str:
    """File list + grep hits; with `with_files`, also the numbered source of the top hit files."""
    if not terms and not with_files:
        return ""
    ctx = run(context_command(terms, rev=rev, with_files=with_files, layout=layout))
    if not with_files:
        return ctx
    top = top_source_files(ctx)
    if top:
        ctx = f"{ctx}\n{run(source_command(top, rev=rev, root=layout.root))}"
    return ctx


def planner_user_message(issue: IssueText, context: str, sources: list[Source]) -> str:
    comments = "\n\n".join(f"--- comment\n{c}" for c in issue.comments)
    research = "\n".join(f"- [{s.title}]({s.url}): {s.snippet}" for s in sources) or "(none)"
    return (
        f"# Issue #{issue.number}: {issue.title}\n\n{issue.body}\n\n{comments}\n\n"
        "# Repository (git ls-files, then `@@GREP term` blocks of `path:line:text` hits)\n"
        f"{context}\n\n"
        "# Research results (you may cite ONLY these URLs)\n"
        f"{research}\n"
    )


def _url_key(url: str) -> str:
    return url.split("://", 1)[-1].rstrip("/").lower()


class ContextFn(Protocol):
    def __call__(self, terms: list[str], with_files: bool = True) -> str: ...


def sandbox_context(
    run: Callable[[str], str], *, rev: str | None = None, layout: Layout = FABRIC_LAYOUT
) -> ContextFn:
    """A ContextFn over a shell runner (live: `box.run(cmd, keep=False).stdout`)."""

    def ctx(terms: list[str], with_files: bool = True) -> str:
        return gather_context(run, terms, rev=rev, with_files=with_files, layout=layout)

    return ctx


def plan_issue(
    llm: StructuredLLM,
    issue: IssueText,
    *,
    context: ContextFn,
    search: Callable[[list[str]], list[Source]],
    log: EventLog,
    prompt: str,
) -> Plan:
    issue_text = f"{issue.title}\n\n{issue.body}"
    terms = code_terms(issue_text)
    log.emit("plan", "note", {"grep_terms": terms})
    repo_ctx = context(terms)

    rq = llm.structured(
        Role.TOOLS,
        "plan",
        QUERY_SYSTEM,
        f"{issue_text}\n\n# Code\n{repo_ctx[-LIGHTNING_CONTEXT_CHARS:]}",
        ResearchQueries,
    )
    extra = [t for t in rq.code_terms if t not in terms]
    if extra:
        log.emit("plan", "note", {"grep_terms_followup": extra})
        repo_ctx = f"{repo_ctx}\n{context(extra, with_files=False)}"
    sources = search(rq.queries)

    plan = llm.structured(
        Role.PLANNER, "plan", prompt, planner_user_message(issue, repo_ctx, sources), Plan
    )

    log.emit(
        "plan", "note", {"risk_class": list(plan.risk_class), "risk_rationale": plan.risk_rationale}
    )

    # Match on scheme-less, slash-trimmed URLs; keep the URL research actually returned.
    allowed = {_url_key(s.url): s.url for s in sources}
    kept = [
        c.model_copy(update={"url": allowed[_url_key(c.url)]})
        for c in plan.citations
        if _url_key(c.url) in allowed
    ]
    dropped = [c.url for c in plan.citations if _url_key(c.url) not in allowed]
    if dropped:
        log.emit("plan", "note", {"dropped_uncited_urls": dropped})
    return plan.model_copy(update={"issue": issue.number, "citations": kept})
