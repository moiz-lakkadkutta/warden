"""Planner (A5/B2): issue fetch, Tavily research, code context, Ultra plan, checked citations."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from warden.config import Role
from warden.hunt.issue import IssueText, fetch_issue
from warden.plan.planner import (
    code_terms,
    gather_context,
    plan_issue,
)
from warden.plan.research import Source, research
from warden.runlog.events import EventLog
from warden.schemas import Citation, Plan, ResearchQueries


def _log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "runs", "t")


def _events(log: EventLog) -> list[dict[str, Any]]:
    path = log.dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


# ------------------------------------------------------------------ issue


def test_fetch_issue_uses_gh_json_and_drops_comments_when_asked() -> None:
    seen: list[list[str]] = []

    def runner(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        payload = {
            "number": 196,
            "title": "Dashboard KPIs count DRAFT",
            "body": "sales_today moves",
            "comments": [{"author": {"login": "m"}, "body": "# Implementation plan"}],
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    issue = fetch_issue("o/r", 196, include_comments=False, runner=runner)
    assert seen[0][:4] == ["gh", "issue", "view", "196"]
    assert "-R" in seen[0] and "o/r" in seen[0]
    assert issue == IssueText(
        number=196, title="Dashboard KPIs count DRAFT", body="sales_today moves", comments=[]
    )

    with_comments = fetch_issue("o/r", 196, include_comments=True, runner=runner)
    assert with_comments.comments == ["# Implementation plan"]


def test_fetch_issue_raises_with_fix_on_gh_failure() -> None:
    def runner(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, "", "could not resolve to an Issue")

    with pytest.raises(RuntimeError, match="could not resolve"):
        fetch_issue("o/r", 9, include_comments=False, runner=runner)


# --------------------------------------------------------------- research


class FakeTavily:
    def __init__(self, results: dict[str, list[dict[str, str]]]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    def search(self, query: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"query": query, **kw})
        return {"results": self.results.get(query, [])}


def test_research_dedupes_trims_and_logs(tmp_path: Path) -> None:
    long = "x" * 1000
    tav = FakeTavily(
        {
            "q1": [{"title": "A", "url": "https://docs.sqlalchemy.org/a", "content": long}],
            "q2": [
                {"title": "A again", "url": "https://docs.sqlalchemy.org/a", "content": "dup"},
                {"title": "B", "url": "https://www.postgresql.org/b", "content": "short"},
            ],
        }
    )
    log = _log(tmp_path)
    sources = research(["q1", "q2"], client=tav, log=log, include_domains=["docs.sqlalchemy.org"])
    assert [s.url for s in sources] == [
        "https://docs.sqlalchemy.org/a",
        "https://www.postgresql.org/b",
    ]
    assert len(sources[0].snippet) == 400
    assert tav.calls[0]["search_depth"] == "advanced"
    assert tav.calls[0]["include_domains"] == ["docs.sqlalchemy.org"]
    kinds = [(e["kind"], e["data"].get("name")) for e in _events(log)]
    assert kinds.count(("tool", "tavily.search")) == 2


def test_research_survives_one_failed_query(tmp_path: Path) -> None:
    class Flaky(FakeTavily):
        def search(self, query: str, **kw: Any) -> dict[str, Any]:
            if query == "bad":
                raise RuntimeError("429")
            return super().search(query, **kw)

    tav = Flaky({"ok": [{"title": "T", "url": "https://u", "content": "c"}]})
    log = _log(tmp_path)
    sources = research(["bad", "ok"], client=tav, log=log)
    assert [s.url for s in sources] == ["https://u"]
    assert any(e["kind"] == "error" for e in _events(log))


# ----------------------------------------------------------- code context


def test_code_terms_pick_identifiers_from_issue_text() -> None:
    text = (
        "Dashboard `sales_today`, `sales_mtd` and gst_collected_mtd count DRAFT; "
        "see InvoiceLifecycleStatus."
    )
    terms = code_terms(text, extra=["dashboard_service"])
    for t in (
        "sales_today",
        "sales_mtd",
        "gst_collected_mtd",
        "InvoiceLifecycleStatus",
        "dashboard_service",
    ):
        assert t in terms
    assert "Dashboard" not in terms  # plain words are not grep terms


def test_gather_context_greps_in_one_posix_command_and_quotes_terms() -> None:
    cmds: list[str] = []

    def run(cmd: str) -> str:
        cmds.append(cmd)
        return (
            "backend/app/service/dashboard_service.py\n"
            "@@GREP sales_today\nbackend/app/x.py:3:sales_today"
        )

    ctx = gather_context(run, ["sales_today", "a'b"])
    assert len(cmds) == 2  # grep, then the source of the top hit file
    assert "git ls-files" in cmds[0] and "git grep" in cmds[0]
    assert "'a'\"'\"'b'" in cmds[0]  # shlex-quoted
    for cmd in cmds:
        assert "[[" not in cmd and "$RANDOM" not in cmd  # sandbox shell is dash
    assert "dashboard_service.py" in ctx


# ---------------------------------------------------------------- planner


class FakeLLM:
    """Pops queued pydantic objects from `structured()`; records role + prompts."""

    def __init__(self, outputs: list[BaseModel]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[Role, str, str, type[BaseModel]]] = []

    def structured(
        self, role: Role, phase: str, system: str, user: str, schema: type[BaseModel], **_: Any
    ) -> BaseModel:
        self.calls.append((role, system, user, schema))
        out = self.outputs.pop(0)
        assert isinstance(out, schema)
        return out


def _plan(citations: list[Citation]) -> Plan:
    return Plan(
        issue=196,
        summary="count only billed invoices",
        files_to_touch=["backend/app/service/dashboard_service.py"],
        tests_to_add=["backend/tests/test_dashboard_service.py::test_draft_does_not_move_kpis"],
        landing_order=["test", "fix"],
        risk_class=["money"],
        citations=citations,
        acceptance=["draft invoice moves no sales KPI"],
    )


def test_plan_issue_greps_first_then_lightning_sees_code_then_ultra(tmp_path: Path) -> None:
    issue = IssueText(number=196, title="KPIs count DRAFT", body="`sales_today` moves", comments=[])
    good = Citation(url="https://docs.sqlalchemy.org/in", claim="in_() semantics")
    invented = Citation(url="https://example.com/made-up", claim="hallucinated")
    llm = FakeLLM(
        [
            ResearchQueries(
                queries=["sqlalchemy ColumnOperators.in_"],
                code_terms=["sales_today", "_NON_CANCELLED_LIFECYCLES"],
            ),
            _plan([good, invented]),
        ]
    )
    contexts: list[tuple[list[str], bool]] = []

    def context(terms: list[str], with_files: bool = True) -> str:
        contexts.append((terms, with_files))
        return "backend/app/service/dashboard_service.py:199:  .in_(_NON_CANCELLED_LIFECYCLES)"

    def search(queries: list[str]) -> list[Source]:
        assert queries == ["sqlalchemy ColumnOperators.in_"]
        return [Source(title="SA", url="https://docs.sqlalchemy.org/in", snippet="in_ ...")]

    log = _log(tmp_path)
    plan = plan_issue(llm, issue, context=context, search=search, log=log, prompt="SYSTEM")

    # 1. grep from the issue text alone, with the file list + top-file source
    assert contexts[0] == (["sales_today"], True)
    # 2. Lightning sees that code
    assert [c[0] for c in llm.calls] == [Role.TOOLS, Role.PLANNER]
    assert "_NON_CANCELLED_LIFECYCLES" in llm.calls[0][2]
    # 3. only identifiers not already searched get a second, list-free grep
    assert contexts[1] == (["_NON_CANCELLED_LIFECYCLES"], False)
    planner_user = llm.calls[1][2]
    assert "https://docs.sqlalchemy.org/in" in planner_user
    assert "dashboard_service.py:199" in planner_user
    assert plan.issue == 196
    assert [c.url for c in plan.citations] == ["https://docs.sqlalchemy.org/in"]
    notes = [e for e in _events(log) if e["kind"] == "note"]
    assert any("example.com/made-up" in json.dumps(e["data"]) for e in notes)
    assert any(e["data"].get("grep_terms") == ["sales_today"] for e in notes)


def test_top_source_files_ranks_app_files_by_hits_and_skips_tests() -> None:
    from warden.plan.planner import top_source_files

    ctx = "\n".join(
        [
            "backend/app/a.py",
            "@@GREP x",
            "backend/app/service/dash.py:1:x",
            "backend/app/service/dash.py:9:x",
            "backend/tests/test_dash.py:3:x",
            "backend/tests/test_dash.py:4:x",
            "backend/tests/test_dash.py:5:x",
            "@@GREP y",
            "backend/app/models/sales.py:2:y",
        ]
    )
    assert top_source_files(ctx, k=2) == [
        "backend/app/service/dash.py",
        "backend/app/models/sales.py",
    ]


def test_gather_context_appends_numbered_source_of_top_files() -> None:
    cmds: list[str] = []

    def run(cmd: str) -> str:
        cmds.append(cmd)
        if len(cmds) == 1:
            return "backend/app/s.py\n@@GREP t\nbackend/app/s.py:4:t"
        return "@@FILE backend/app/s.py\n     1: import x"

    ctx = gather_context(run, ["t"], rev="abc^")
    assert len(cmds) == 2
    assert "git show" in cmds[1] and "backend/app/s.py" in cmds[1] and "nl" in cmds[1]
    assert "@@FILE backend/app/s.py" in ctx
    assert gather_context(lambda c: "", ["t"], with_files=False) == ""


def test_plan_issue_forces_issue_number(tmp_path: Path) -> None:
    issue = IssueText(number=7, title="t", body="b", comments=[])
    wrong = _plan([])
    llm = FakeLLM([ResearchQueries(queries=[], code_terms=[]), wrong])
    plan = plan_issue(
        llm,
        issue,
        context=lambda t, with_files=True: "",
        search=lambda q: [],
        log=_log(tmp_path),
        prompt="S",
    )
    assert plan.issue == 7


def test_context_command_can_read_an_older_revision_without_checkout() -> None:
    from warden.plan.planner import context_command

    head = context_command(["sales_today"])
    assert "git ls-files" in head and "git grep" in head

    old = context_command(["sales_today"], rev="43f3851^")
    assert "git rev-parse --verify '43f3851^^{commit}'" in old
    assert "git ls-tree -r --name-only" in old
    assert 'git grep -n -I -F -e sales_today "$rev"' in old
    assert "sed" in old  # strips the `<sha>:` prefix git grep adds for a revision
    assert "checkout" not in old


def test_citation_matching_ignores_scheme_and_trailing_slash_and_keeps_research_url(
    tmp_path: Path,
) -> None:
    issue = IssueText(number=1, title="t", body="b", comments=[])
    cited = Citation(url="https://docs.sqlalchemy.org/en/latest/core/sqlelement.html/", claim="c")
    llm = FakeLLM([ResearchQueries(queries=["q"], code_terms=[]), _plan([cited])])
    research_url = "http://docs.sqlalchemy.org/en/latest/core/sqlelement.html"
    plan = plan_issue(
        llm,
        issue,
        context=lambda t, with_files=True: "",
        search=lambda q: [Source(title="SA", url=research_url, snippet="s")],
        log=_log(tmp_path),
        prompt="S",
    )
    assert [c.url for c in plan.citations] == [research_url]
