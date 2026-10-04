"""Tavily search(advanced, include_domains) → Source objects the planner may cite.

The planner may only cite URLs that came back from here, so every citation in a Plan resolves.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from pydantic import BaseModel

from warden.runlog.events import EventLog

# Official docs for the target stack (FastAPI + SQLAlchemy 2 + Postgres + Pydantic + Alembic).
DOC_DOMAINS = [
    "docs.sqlalchemy.org",
    "fastapi.tiangolo.com",
    "www.postgresql.org",
    "docs.pydantic.dev",
    "alembic.sqlalchemy.org",
    "docs.python.org",
]
SNIPPET_CHARS = 400


class Source(BaseModel):
    title: str
    url: str
    snippet: str


class SearchClient(Protocol):
    def search(self, query: str, **kwargs: Any) -> dict[str, Any]: ...


def tavily_client(api_key: str) -> SearchClient:
    from tavily import TavilyClient

    client: SearchClient = TavilyClient(api_key=api_key)
    return client


def research(
    queries: list[str],
    *,
    client: SearchClient,
    log: EventLog,
    include_domains: list[str] | None = None,
    max_results: int = 3,
) -> list[Source]:
    """One advanced search per query; results deduped by URL in first-seen order. A failed query
    is logged as an `error` event and skipped — a plan without one doc lookup is still a plan."""
    seen: dict[str, Source] = {}
    for query in queries:
        t0 = time.perf_counter()
        try:
            resp = client.search(
                query,
                search_depth="advanced",
                include_domains=include_domains or DOC_DOMAINS,
                max_results=max_results,
            )
        except Exception as err:  # noqa: BLE001 — any client/network failure skips this query
            log.emit("plan", "error", {"name": "tavily.search", "query": query, "error": str(err)})
            continue
        results = resp.get("results", [])
        log.emit(
            "plan",
            "tool",
            {"name": "tavily.search", "query": query, "urls": [r.get("url") for r in results]},
            ms=int((time.perf_counter() - t0) * 1000),
        )
        for r in results:
            url = str(r.get("url", ""))
            if url and url not in seen:
                seen[url] = Source(
                    title=str(r.get("title", "")),
                    url=url,
                    snippet=str(r.get("content", ""))[:SNIPPET_CHARS],
                )
    return list(seen.values())
