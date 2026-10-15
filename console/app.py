"""Read-only console server (E3).

Serves a directory laid out like `warden console --out site` writes it (and like runs/):

    <root>/index.html                     the run list
    <root>/<run_id>/index.html            one page per run (timeline, branch tree, evidence)
    <root>/<run_id>/events.jsonl          RunEvent lines
    <root>/<run_id>/cost.jsonl            one line per model call
    <root>/<run_id>/artifacts/pr_body.md  the evidence table

Routes (all GET/HEAD; there is no write endpoint):

    /healthz                    liveness, run count, image build (commit sha)
    /api/runs                   run ids with event counts, time span and cost
    /api/runs/{id}              one run's summary
    /api/runs/{id}/events       its events (?phase=verify&kind=sandbox filter)
    /api/runs/{id}/cost         its cost lines
    /                           the static site

Only fastapi + the stdlib, so the container does not need the warden package.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "site"
BUILD_FILE = Path(__file__).resolve().parent / "BUILD"  # written by the Dockerfile (commit sha)


def build_id() -> str:
    try:
        return BUILD_FILE.read_text(encoding="utf-8").strip() or "dev"
    except OSError:
        return "dev"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    """Parsed lines of a JSONL file; blank or malformed lines are skipped, not fatal."""
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


class RunStore:
    """Read-only view of the run directories under one root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name
            for p in self.root.iterdir()
            if RUN_ID.match(p.name) and (p / "events.jsonl").is_file()
        )

    def run_dir(self, run_id: str) -> Path:
        # The regex is the path-traversal guard: an id can never contain "/" or "..".
        if not RUN_ID.match(run_id) or not (self.root / run_id / "events.jsonl").is_file():
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        return self.root / run_id

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return _jsonl(self.run_dir(run_id) / "events.jsonl")

    def cost(self, run_id: str) -> list[dict[str, Any]]:
        return _jsonl(self.run_dir(run_id) / "cost.jsonl")

    def summary(self, run_id: str) -> dict[str, Any]:
        d = self.run_dir(run_id)
        events = self.events(run_id)
        cost = self.cost(run_id)
        stamps = [str(e["ts"]) for e in events if e.get("ts")]
        usd = sum(float(c.get("usd") or 0) for c in cost)
        phases: list[str] = []
        for e in events:
            p = e.get("phase")
            if isinstance(p, str) and p not in phases:
                phases.append(p)
        return {
            "id": run_id,
            "href": f"/{run_id}/",
            "events": len(events),
            "phases": phases,
            "started": min(stamps) if stamps else None,
            "ended": max(stamps) if stamps else None,
            "model_calls": len(cost),
            "cost_usd": round(usd, 6),
            "has_page": (d / "index.html").is_file(),
            "has_gif": (d / "timeline.gif").is_file(),
            "has_pr_body": (d / "artifacts" / "pr_body.md").is_file(),
        }


def create_app(root: Path = DEFAULT_ROOT) -> FastAPI:
    store = RunStore(root)
    app = FastAPI(
        title="warden console",
        summary="Read-only run console for Warden (hunt → plan → fix → verify → PR).",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def read_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "read-only console"}, status_code=405)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "public, max-age=60")
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "runs": len(store.ids()), "build": build_id()}

    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        return [store.summary(i) for i in store.ids()]

    @app.get("/api/runs/{run_id}")
    def run(run_id: str) -> dict[str, Any]:
        return store.summary(run_id)

    @app.get("/api/runs/{run_id}/events")
    def events(
        run_id: str, phase: str | None = None, kind: str | None = None
    ) -> list[dict[str, Any]]:
        rows = store.events(run_id)
        if phase is not None:
            rows = [e for e in rows if e.get("phase") == phase]
        if kind is not None:
            rows = [e for e in rows if e.get("kind") == kind]
        return rows

    @app.get("/api/runs/{run_id}/cost")
    def cost(run_id: str) -> list[dict[str, Any]]:
        return store.cost(run_id)

    if root.is_dir():
        # html=True: "/" and "/<id>/" serve their index.html. Mounted last so /api wins.
        app.mount("/", StaticFiles(directory=root, html=True), name="site")
    return app


app = create_app()
