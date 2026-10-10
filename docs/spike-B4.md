# Spike B4 — live gate (Fabric #196 candidate)

**Setup:** `spikes/b4_gate.py`, 2026-10-10. `run_gate` runs with profile `fabric` on the A6
candidate (`ca4365f8…`), from `/src`, with every step wrapped in `with-services sh -c`. This is the
same code Warden pushed as [Fabric-Inventory#220](https://github.com/moiz-lakkadkutta/Fabric-Inventory/pull/220).

## Done-criterion: the gate runs on the candidate box and produces the Verdict fields ✓

| Verdict field | Warden gate (Sandboxes) | Fabric CI on #220 (GitHub Actions) |
|---|---|---|
| suite | **1509 passed**, 0 failed (643 s) | **1509 passed** (845 s) |
| mypy_ok | ✓ (19.6 s) | not reached (the job stops at ruff) |
| ruff_ok | **✗** `ruff check` W292 ×2; `ruff format --check` would reformat 2 files | **✗** W292 ×2 |
| alembic_single_head | ✓ upgrade on an empty DB 3.6 s, 1 head | — |
| openapi_drift | none (dump 5.3 s + diff) | ✓ no drift |
| not verified | FE type codegen (no node in the image) | ✓ (CI has pnpm) |

`ok = False`. Warden's gate reaches the same verdict as the target repo's CI, with the same
failing check and the same test count, in 681 s against CI's 14 min. Had the gate been wired into
the pipeline, #220 would not have been opened with a red lint (and since `7e40557`,
`write_file` no longer produces the W292 at all).

1509 rather than 1787 (A4) is correct: the replay runs at `43f3851^`, an older commit with fewer
tests.

## Probe lesson

The first probe piped each command into `| tail` and every step reported exit 0, including
ruff's two errors. Profiles must not pipe, and `test_fabric_profile_matches_fabric_ci` asserts that.
