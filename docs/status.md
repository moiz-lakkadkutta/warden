# Warden — status (2026-10-06, session 2)

## Phase A spike: done (A3–A7 live on Nebius)

| Step | Commit | Result |
|---|---|---|
| A3 hello sandbox | `76345ed` | run/fork ≈ 1 s; fork isolation verified; ContreeBackend fixed for IAM auth |
| A4 fabric-base | `2bd9d55` | Fabric suite **1787 passed** in a sandbox, the same as CI (774 s vs 893 s). Kernel has no SysV IPC → `ops/shmshim.c` LD_PRELOAD shim |
| A5 first plan | `8943441` | Ultra + Tavily + sandbox code context; root cause found once source was in context; [docs/spike-A5.md](spike-A5.md) |
| A6 first fix | `f1dbc1a` | #196 fixed in a fork at the pre-fix commit: same status set as the human fix, 3 new tests green, **$0.15**, 18 turns; [docs/spike-A6.md](spike-A6.md) |
| A7 theater check | this commit | repro test **FAILS on the unfixed base** (`10498.95 == 0`), passes on the candidate; guards pass on both; [docs/spike-A7.png](spike-A7.png) |

Along the way, B2/B3/C1 pieces landed:
- B2 planner: research, grep-first context, pre-fix replay, citation filter, preserve-scope rule.
- B3 implementer: paged `read_file`, `grep -E`, workdir + service wrap, node-id tests.
- C1: a theater check that treats errors as inconclusive, never as proof.

Gate on main: ruff ✓ format ✓ mypy --strict ✓ `241 passed`. Spend so far ≈ **$1** on Token Factory (A5 $0.12, A6 four runs $0.76, probes).

## Open items

- **Push the rebuilt image (Moiz):** `ops/build-base.sh … --push` (needs your GHCR login). The
  pushed image predates the shim; Sandboxes uses a patched layer tagged `warden/fabric-base:latest`
  until then.
- `.env` sets `WARDEN_MAX_TOOL_TURNS`; the new default is 40 (A6 needed 18, run 3 ran out at 25).
- Image DB is migrated at `8cfc493`; replays at older commits run against a newer schema. It was
  fine for #196; a migration-touching issue needs `alembic downgrade` to the replay commit's head.
- Implementer didn't run the failing test before implementing (protocol step 2); the theater
  check now covers that, but it's worth a nudge.
- Planner/implementer missed the human plan's "PAID still counts" test.

## Next

1. **B6-live / Phase B exit:** wire fix + verify(theater + gate) + draft PR into `warden run`;
   add `--at <rev>` for replays.
2. **B4-live:** the gate (suite, mypy, ruff, alembic heads, openapi) on the candidate, in the
   workdir with the service wrap.
3. **B2 criterion:** plans on 3 closed issues name the right files (#196 ✓; pick two more from #212).
