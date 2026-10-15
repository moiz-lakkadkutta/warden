"""E3: the read-only console server, offline via TestClient."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from console.app import DEFAULT_ROOT, create_app  # noqa: E402

RID = "20261010-111217-7c89"
OTHER = "20261014-084347-9feb"


def _site(tmp: Path) -> Path:
    root = tmp / "site"
    (root / RID / "artifacts").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>warden console</title>runs")
    (root / RID / "index.html").write_text("<!doctype html><div id='tree'>base → candidate</div>")
    events = [
        {"ts": "2026-10-10T11:12:19Z", "phase": "hunt", "kind": "note", "data": {}},
        {"ts": "2026-10-10T11:20:00Z", "phase": "verify", "kind": "sandbox", "data": {}},
        {"ts": "2026-10-10T11:21:00Z", "phase": "verify", "kind": "llm", "data": {}},
    ]
    lines = [json.dumps(e) for e in events]
    (root / RID / "events.jsonl").write_text("\n".join(lines[:1] + ["not json", ""] + lines[1:]))
    (root / RID / "cost.jsonl").write_text(
        json.dumps({"usd": 0.25, "role": "planner"}) + "\n" + json.dumps({"usd": 0.5}) + "\n"
    )
    (root / RID / "artifacts" / "pr_body.md").write_text("| Check | Result |")
    (root / OTHER).mkdir()
    (root / OTHER / "events.jsonl").write_text("")
    (root / "notarun").mkdir()  # no events.jsonl, not listed
    return root


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(_site(tmp_path)))


def test_health(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "runs": 2, "build": "dev"}


def test_index_and_run_page_are_served(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "warden console" in r.text
    r = client.get(f"/{RID}/")
    assert r.status_code == 200 and "base → candidate" in r.text
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/{RID}/artifacts/pr_body.md").text == "| Check | Result |"


def test_runs_list_summarises_each_run(client: TestClient) -> None:
    runs = client.get("/api/runs").json()
    assert [r["id"] for r in runs] == [RID, OTHER]
    first = runs[0]
    assert first["events"] == 3  # the malformed and blank lines are skipped
    assert first["phases"] == ["hunt", "verify"]
    assert first["started"] == "2026-10-10T11:12:19Z"
    assert first["ended"] == "2026-10-10T11:21:00Z"
    assert first["cost_usd"] == 0.75 and first["model_calls"] == 2
    assert first["has_page"] and first["has_pr_body"] and not first["has_gif"]
    assert first["href"] == f"/{RID}/"
    assert runs[1]["events"] == 0 and runs[1]["started"] is None


def test_events_filter_by_phase_and_kind(client: TestClient) -> None:
    assert len(client.get(f"/api/runs/{RID}/events").json()) == 3
    verify = client.get(f"/api/runs/{RID}/events", params={"phase": "verify"}).json()
    assert [e["kind"] for e in verify] == ["sandbox", "llm"]
    llm = client.get(f"/api/runs/{RID}/events", params={"phase": "verify", "kind": "llm"})
    assert len(llm.json()) == 1
    assert len(client.get(f"/api/runs/{RID}/cost").json()) == 2


@pytest.mark.parametrize("bad", ["nope", "20261010-111217-zzzz", "20991231-000000-0000", ".."])
def test_unknown_or_malformed_run_is_404(client: TestClient, bad: str) -> None:
    assert client.get(f"/api/runs/{bad}/events").status_code == 404


def test_path_traversal_is_not_served(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("key")
    assert client.get("/../secret.txt").status_code == 404
    assert client.get("/%2e%2e/secret.txt").status_code == 404


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_no_write_methods(client: TestClient, method: str) -> None:
    for path in ("/api/runs", f"/api/runs/{RID}/events", f"/{RID}/events.jsonl", "/"):
        r = client.request(method.upper(), path)
        assert r.status_code == 405, (method, path)


def test_missing_root_still_answers_health(tmp_path: Path) -> None:
    c = TestClient(create_app(tmp_path / "absent"))
    assert c.get("/healthz").json()["runs"] == 0
    assert c.get("/api/runs").json() == []


def test_committed_site_is_servable() -> None:
    """The image bakes in site/: every committed run must list and render."""
    c = TestClient(create_app(DEFAULT_ROOT))
    runs = c.get("/api/runs").json()
    assert runs, "site/ has no runs"
    for r in runs:
        assert r["has_page"], r["id"]
        assert c.get(r["href"]).status_code == 200
