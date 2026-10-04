"""A4 spike: import warden/fabric-base from GHCR into Sandboxes, run Fabric's suite in it.

Done-criterion: in-sandbox `pytest -q` passed count == reference (Fabric CI @ 8cfc493: 1787).
"""

import json
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from contree_sdk import ContreeSync  # noqa: E402
from contree_sdk.auth import IAMAuth  # noqa: E402
from contree_sdk.config import ContreeConfig  # noqa: E402

REF = "docker://ghcr.io/moiz-lakkadkutta/warden/fabric-base:8cfc493"
TAG = "warden/fabric-base:latest"
t: dict[str, float] = {}


def clock(name, fn):  # type: ignore[no-untyped-def]
    t0 = time.perf_counter()
    out = fn()
    t[name] = round(time.perf_counter() - t0, 1)
    print(f"{name}: {t[name]}s", file=sys.stderr, flush=True)
    return out


OPS = Path(__file__).resolve().parents[1] / "ops"
sdk = ContreeSync(ContreeConfig(auth=IAMAuth()))
base = clock("oci_import", lambda: sdk.images.oci(REF))
print("imported uuid:", base.uuid, file=sys.stderr, flush=True)

# The sandbox kernel has no SysV IPC: layer the shmshim + updated with-services on top, as a
# kept run (child checkpoint), so no image rebuild/push is needed to iterate.
img = clock(
    "patch_layer",
    lambda: base.run(
        shell="chmod 0755 /usr/local/bin/with-services /usr/local/lib/libshmshim.so && "
        "ls -l /usr/local/lib/libshmshim.so",
        cwd="/",
        files={
            "/usr/local/lib/libshmshim.so": (OPS / "libshmshim.so").read_bytes(),
            "/usr/local/bin/with-services": (OPS / "entrypoint.sh").read_bytes(),
        },
        disposable=False,
    ).wait(),
)
print("patch rc", img.exit_code, img.stdout, img.stderr[-500:], file=sys.stderr, flush=True)
clock("tag_as", lambda: img.tag_as(TAG))

probe = clock(
    "probe",
    lambda: img.run(
        shell="pwd; echo PATH=$PATH; which pytest alembic pg_ctl redis-server; "
        "echo DB=$DATABASE_URL; with-services psql -h 127.0.0.1 -U postgres -Atc "
        "'select datname from pg_database order by 1'",
        cwd="/app",
    ).wait(),
)
print(
    "probe rc",
    probe.exit_code,
    "\n",
    probe.stdout,
    probe.stderr[-2000:],
    file=sys.stderr,
    flush=True,
)

suite = clock(
    "pytest_full",
    lambda: img.run(
        shell="with-services pytest -q -p no:cacheprovider 2>&1 | tail -60",
        cwd="/app",
        timeout=3600,
    ).wait(),
)
out = suite.stdout if isinstance(suite.stdout, str) else suite.stdout.decode()
summary = [ln for ln in out.splitlines() if re.search(r"\d+ (passed|failed|error)", ln)]
counts = (
    {
        k: int(v)
        for v, k in re.findall(
            r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)", summary[-1]
        )
    }
    if summary
    else {}
)
print(out[-6000:], file=sys.stderr)
print(
    json.dumps(
        {
            "image_uuid": str(img.uuid),
            "base_uuid": str(base.uuid),
            "summary": summary[-1:],
            "counts": counts,
            "reference_passed": 1787,
            "match": counts.get("passed") == 1787,
            "seconds": t,
        },
        indent=1,
    )
)
