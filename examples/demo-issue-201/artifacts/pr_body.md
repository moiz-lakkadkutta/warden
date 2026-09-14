## Warden evidence — #201 double-posted voucher on concurrent invoice finalize

**Verdict: PASS**

| Check | Result |
|---|---|
| New tests on unfixed base | FAIL — as required |
| Full suite on candidate | 214 passed, 0 failed |
| Race probe | 8 threads → 1 effect(s) (was 3) |
| Gate | mypy clean · ruff clean · 1 alembic head · no openapi drift |
| Not verified | frontend, e-invoice path |
| Risk class | money, concurrency → **PENDING-HUMAN** |
| Cost | $1.42 · Nemotron-3-Ultra-550b-a55b 2 · nemotron-3-super-120b-a12b 11 · Nemotron-3_5-Lightning 37 |

**Plan citations**
- [with_for_update() emits SELECT … FOR UPDATE, serialising finalizers](https://docs.sqlalchemy.org/en/20/orm/queryguide/query.html#sqlalchemy.orm.Query.with_for_update)
