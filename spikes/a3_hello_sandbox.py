"""A3 spike: hello sandbox + fork. Not part of the package; numbers go to FEEDBACK.md."""

import json
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from contree_sdk import ContreeSync  # noqa: E402
from contree_sdk.auth import IAMAuth  # noqa: E402
from contree_sdk.config import ContreeConfig  # noqa: E402

t: dict[str, float] = {}


def clock(name: str, fn):  # type: ignore[no-untyped-def]
    t0 = time.perf_counter()
    out = fn()
    t[name] = round(time.perf_counter() - t0, 2)
    print(f"{name}: {t[name]}s", file=sys.stderr, flush=True)
    return out


sdk = ContreeSync(ContreeConfig(auth=IAMAuth()))
info = clock("whoami", lambda: sdk.get_token_info())
print("whoami ok:", info.permissions["spawn"], file=sys.stderr)

try:
    img = clock("use_tag", lambda: sdk.images.use("ubuntu:latest", strict=True))
except Exception as e:  # noqa: BLE001
    print("use(tag) failed -> oci import:", type(e).__name__, e, file=sys.stderr)
    img = clock("oci_import", lambda: sdk.images.oci("docker://docker.io/library/ubuntu:latest"))

hi = clock("run_echo_disposable", lambda: img.run(shell="echo hi").wait())
print("echo:", hi.exit_code, repr(hi.stdout), file=sys.stderr)

c = clock("run_keep_c", lambda: img.run(shell="echo $RANDOM > /tmp/r", disposable=False).wait())
a = clock("fork_a", lambda: c.run(shell="echo A >> /tmp/r", disposable=False).wait())
b = clock("fork_b", lambda: c.run(shell="echo B >> /tmp/r", disposable=False).wait())
ra = clock("read_a", lambda: a.run(shell="cat /tmp/r").wait()).stdout
rb = clock("read_b", lambda: b.run(shell="cat /tmp/r").wait()).stdout
rc = clock("read_c", lambda: c.run(shell="cat /tmp/r").wait()).stdout

res = {
    "uuids": {"c": str(c.uuid), "a": str(a.uuid), "b": str(b.uuid)},
    "c": rc,
    "a": ra,
    "b": rb,
    "fork_ok": ra != rb and ra.startswith(rc.strip()) and rb.startswith(rc.strip()),
    "seconds": t,
}
print(json.dumps(res, indent=1, default=str))
