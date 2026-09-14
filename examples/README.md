# examples/

Recorded Warden runs that CI replays offline. Each directory is one `runs/<id>/` as the CLI
writes it: `run.json` (manifest), `events.jsonl` + `cost.jsonl` (the log), `artifacts/` (one
marker per phase: issue.json → plan.json → candidate.json → verdict.json → pr.json, plus
pr_body.md), `result.json` and the rendered `index.html`.

## demo-issue-201

`warden run --demo` on the fake sandbox. `tests/test_replay.py` re-runs the demo steps through
the pipeline machine and compares the tree modulo run_id and timestamps (event `ms` is zeroed;
`index.html` is only rendered, not compared). A diff here is a real change to the demo, the
machine or the evidence table — re-record it on purpose:

```bash
# from the repo root, with no NEBIUS_*/TAVILY_*/GITHUB_* keys exported and no .env in cwd
rm -rf examples/demo-issue-201
WARDEN_RUNS_DIR=examples uv run warden run --demo --json --run-id demo-issue-201 > /dev/null \
  && WARDEN_RUNS_DIR=examples uv run warden console --run demo-issue-201
```

Nothing under `examples/` may contain a key; `test_example_has_no_secrets` checks.
