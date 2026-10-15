"""The IDOR probe harness that runs *inside* the sandbox, and the boot wrapper around it.

``PROBE_PY`` is a self-contained **stdlib-only** Python program (no Fabric imports, no pip
installs) copied into the box and run under Fabric's venv python. It boots nothing itself — the
dash wrapper in :func:`boot_command` starts uvicorn against ``fabric_demo`` and waits for ``/ready``
first — then it signs up two throwaway tenants, enumerates id-taking GET endpoints and every
``firm_id``-taking POST endpoint from ``/openapi.json``, and probes:

* cross-org reads: create a row in tenant B, try to read it by id with tenant A's token (expect
  404 — a 200 is a cross-org leak);
* cross-firm writes: with tenant A's token, POST each ``firm_id``-taking create endpoint carrying
  tenant B's ``firm_id`` (expect 422 "Firm … not found in this organization" — a 201 persists a
  cross-tenant row, the SEC-1 / AUTHZ-2 class).

It prints one JSON object between ``MARKER`` lines on stdout; :func:`parse_probe_output` reads it
into an :class:`~warden.hunt.axes.idor.IdorRun`. Kept as a string (not an imported module) so the
sandbox code is never type-checked against Warden's own deps.
"""

from __future__ import annotations

import json

from warden.hunt.axes.idor import IdorRun, Observation

MARKER = "__WARDEN_IDOR_RESULT__"

# Default boot settings for the Fabric base image (docs/status.md "Infra state" + recon):
# dev mode keeps /openapi.json live and relaxes the signup rate limit; the app role is fabric_app.
DEFAULT_DB_URL = "postgresql+asyncpg://fabric_app:fabric_app_dev@127.0.0.1:5432/fabric_demo"


# Superuser (dev warns, not refuses) used to create + migrate the scratch DB for a replay.
# The base image's superuser is `postgres` (local trust auth — no password); there is no `fabric`.
SCRATCH_DB = "warden_replay"
SUPERUSER_URL = "postgresql+asyncpg://postgres@127.0.0.1:5432/" + SCRATCH_DB
_ADMIN_PSQL = "postgresql://postgres@127.0.0.1:5432/postgres"


def boot_command(
    *,
    db_url: str = DEFAULT_DB_URL,
    port: int = 8000,
    probe_path: str = "/tmp/warden_idor_probe.py",
    ready_timeout: int = 120,
    at_rev: str | None = None,
    workdir: str = "/src",
) -> str:
    """A dash (POSIX sh) command: boot uvicorn, wait for /ready, run the probe.

    Default: boot against the seeded ``fabric_demo``. With ``at_rev`` (a historical replay), check
    that commit out at ``workdir`` (``/app`` is ``workdir/backend``), create a fresh scratch DB,
    ``alembic upgrade head`` to *that commit's* schema, and boot against it — signup auto-seeds the
    catalogue, so no demo data is needed and the newer seeded DB is left untouched. The installed
    venv is reused (deps are unchanged across the range). No bash-isms (the image shell is dash).
    """
    if at_rev is None:
        prep = f'export DATABASE_URL="{db_url}"'
    else:
        prep = f"""cd {workdir}
git checkout -q --detach {at_rev}
cd /app
export DATABASE_URL="{SUPERUSER_URL}"
export MIGRATION_DATABASE_URL="{SUPERUSER_URL}"
echo "warden: creating scratch DB {SCRATCH_DB} and migrating to {at_rev}" >&2
psql "{_ADMIN_PSQL}" -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS {SCRATCH_DB}" -c "CREATE DATABASE {SCRATCH_DB}"
alembic upgrade head"""
    return f"""
set -e
cd /app
export ENVIRONMENT=dev
export JWT_SECRET="warden-dev-probe-secret-0123456789abcdef"
export SIGNUP_RATE_LIMIT_MAX_REQUESTS=1000
export MFA_REQUIRED_FOR_ADMIN=false
{prep}
uvicorn main:app --host 127.0.0.1 --port {port} >/tmp/warden_uvicorn.log 2>&1 &
UV=$!
READY=0
i=0
while [ $i -lt {ready_timeout} ]; do
  code=$(curl -s -o /dev/null -w "%{{http_code}}" http://127.0.0.1:{port}/ready 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then READY=1; break; fi
  i=$((i+1))
  sleep 1
done
if [ "$READY" != "1" ]; then
  echo "{MARKER}"
  echo '{{"error":"uvicorn did not become ready","observations":[],"endpoints_enumerated":0,"tenants":0}}'
  echo "{MARKER}"
  echo "----- uvicorn.log tail -----" >&2
  tail -n 40 /tmp/warden_uvicorn.log >&2 || true
  kill $UV 2>/dev/null || true
  exit 0
fi
python {probe_path} || python3 {probe_path}
kill $UV 2>/dev/null || true
""".strip()


def parse_probe_output(stdout: str) -> IdorRun:
    """Pull the JSON object printed between the MARKER lines into an :class:`IdorRun`."""
    start = stdout.find(MARKER)
    if start == -1:
        return IdorRun(error="probe produced no result marker (boot or probe crashed)")
    rest = stdout[start + len(MARKER) :]
    end = rest.find(MARKER)
    blob = (rest if end == -1 else rest[:end]).strip()
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as e:
        return IdorRun(error=f"probe result was not valid JSON: {e}")
    observations = [Observation.model_validate(o) for o in data.get("observations", [])]
    attacker = data.get("attacker") or {}
    return IdorRun(
        observations=observations,
        endpoints_enumerated=int(data.get("endpoints_enumerated", 0)),
        tenants=int(data.get("tenants", 0)),
        error=str(data.get("error", "")),
        attacker=attacker if isinstance(attacker, dict) else {},
    )


# The in-sandbox probe. Stdlib only; {MARKER} is substituted at build time (see probe_py()).
_PROBE_TEMPLATE = r"""
import json, sys, uuid
import urllib.request, urllib.error

BASE = "http://127.0.0.1:8000"
MARKER = "__MARKER__"
PW = "WardenProbe123!"

def req(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    if method == "POST":
        # Fabric's idempotency middleware returns 400 on a mutating POST with no key.
        r.add_header("Idempotency-Key", str(uuid.uuid4()))
    try:
        resp = urllib.request.urlopen(r, timeout=30)
        raw = resp.read()
        headers = {k.lower(): v for k, v in resp.headers.items()}
        status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        headers = {k.lower(): v for k, v in (e.headers or {}).items()}
        status = e.code
    except Exception as e:  # connection refused, timeout, ...
        return -1, {"error": str(e)}, {}
    try:
        parsed = json.loads(raw) if raw else None
    except Exception:
        parsed = {"raw": raw.decode("utf-8", "replace")[:400]}
    return status, parsed, headers

def decode_jwt(token):
    # Decode the JWT payload (middle segment) without verifying — evidence about the caller only.
    import base64
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        claims = json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}
    keep = ("sub", "role", "roles", "org_id", "firm_id", "is_owner", "permissions", "scope")
    out = {}
    for k in keep:
        if k in claims:
            v = claims[k]
            if k == "permissions" and isinstance(v, list):
                v = len(v)  # a count, not the whole catalogue
            out[k] = v
    return out

def request_id(headers, body):
    for h in ("x-request-id", "x-correlation-id", "request-id"):
        if h in headers:
            return headers[h]
    if isinstance(body, dict):
        for k in ("request_id", "requestId", "correlation_id"):
            if body.get(k):
                return str(body[k])
    return None

def body_text(body):
    if isinstance(body, dict):
        return json.dumps(body)
    return str(body)

def signup(tag):
    body = {
        "email": "warden-%s-%s@example.com" % (tag, uuid.uuid4().hex[:8]),
        "password": PW,
        "org_name": "Warden Probe %s %s" % (tag, uuid.uuid4().hex[:6]),
        "firm_name": "Firm %s" % tag,
        "state_code": "MH",
    }
    st, data, _ = req("POST", "/auth/signup", body=body)
    if st not in (200, 201) or not isinstance(data, dict) or "access_token" not in data:
        raise RuntimeError("signup %s -> %s: %s" % (tag, st, body_text(data)))
    return data

def resolve(schema, spec):
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 10:
        name = schema["$ref"].split("/")[-1]
        schema = spec.get("components", {}).get("schemas", {}).get(name, {})
        seen += 1
    return schema if isinstance(schema, dict) else {}

def dummy(p, spec):
    p = resolve(p, spec)
    if p.get("enum"):
        return p["enum"][0]
    t = p.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "string":
        if p.get("format") == "uuid":
            return str(uuid.uuid4())
        if p.get("format") in ("date", "date-time"):
            return "2026-01-01T00:00:00Z" if p["format"] == "date-time" else "2026-01-01"
        return "WARDEN-PROBE"
    if t in ("integer", "number"):
        return 1
    if t == "boolean":
        return True
    if t == "array":
        return []
    if t == "object":
        return {}
    return "WARDEN-PROBE"

def synth(schema, spec, firm_id):
    schema = resolve(schema, spec)
    props = schema.get("properties", {})
    out = {}
    for name in schema.get("required", []):
        out[name] = dummy(props.get(name, {}), spec)
    out["firm_id"] = firm_id
    return out, props

def has_firm_id(schema, spec):
    schema = resolve(schema, spec)
    return "firm_id" in schema.get("properties", {})

def repro_prefix():
    return (
        "# two throwaway tenants; signup returns access_token, org_id, firm_id\n"
        "A=$(curl -s -XPOST %s/auth/signup -H 'content-type: application/json' "
        "-d '{\"email\":\"a@ex.com\",\"password\":\"%s\",\"org_name\":\"Org A\","
        "\"firm_name\":\"Firm A\",\"state_code\":\"MH\"}')\n"
        "B=$(curl -s -XPOST %s/auth/signup -H 'content-type: application/json' "
        "-d '{\"email\":\"b@ex.com\",\"password\":\"%s\",\"org_name\":\"Org B\","
        "\"firm_name\":\"Firm B\",\"state_code\":\"MH\"}')\n"
        "A_TOKEN=$(printf '%%s' \"$A\" | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"access_token\"])')\n"
        "B_FIRM=$(printf '%%s' \"$B\" | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"firm_id\"])')\n"
        % (BASE, PW, BASE, PW)
    )

def write_repro(method, path, body):
    b = json.dumps(body).replace("WILL_BE_B_FIRM", "$B_FIRM")
    return (
        repro_prefix()
        + "# org A writes to %s carrying org B's firm_id\n" % path
        + "curl -i -X%s %s%s -H \"authorization: Bearer $A_TOKEN\" "
        "-H 'content-type: application/json' -d '%s'\n"
        "# EXPECT 422 \"Firm ... not found in this organization\"; a 201 is the cross-tenant bug."
        % (method, BASE, path, b)
    )

def read_repro(path_tmpl, coll):
    return (
        repro_prefix()
        + "B_TOKEN=$(printf '%%s' \"$B\" | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"access_token\"])')\n"
        "# org B creates a %s, then org A tries to read it by id\n" % coll
        + "ID=$(curl -s -XPOST %s/%s -H \"authorization: Bearer $B_TOKEN\" "
        "-H 'content-type: application/json' -d '{\"code\":\"BONLY\",\"name\":\"b\"}' "
        "| python3 -c 'import sys,json;print(json.load(sys.stdin).get(\"id\",\"\"))')\n"
        "curl -i %s%s -H \"authorization: Bearer $A_TOKEN\"\n"
        "# EXPECT 404; a 200 leaks org B's row to org A."
        % (BASE, coll, BASE, path_tmpl.replace("{id}", "$ID"))
    )

obs = []

def add(kind, method, path, status, headers, body, vulnerable, expected, actual, repro):
    obs.append({
        "probe": "%s %s %s" % (kind, method, path),
        "kind": kind,
        "method": method,
        "path": path,
        "status": status,
        "vulnerable": vulnerable,
        "request_id": request_id(headers, body),
        "repro": repro,
        "expected": expected,
        "actual": actual,
    })

attacker = {}

def main():
    a = signup("A")
    b = signup("B")
    a_tok = a["access_token"]
    b_tok = b["access_token"]
    b_firm = b["firm_id"]
    # Evidence about the attacking token: a freshly-signed-up Owner. Decoded JWT claims show its
    # own role and firm scope — a reader can judge what a firm-scoped check on the token would see.
    attacker.update(decode_jwt(a_tok))
    attacker["signup_firm_id"] = a.get("firm_id")
    attacker["signup_org_id"] = a.get("org_id")

    sto, spec, _ = req("GET", "/openapi.json", token=a_tok)
    if sto != 200 or not isinstance(spec, dict):
        print(MARKER)
        print(json.dumps({"error": "could not fetch /openapi.json (%s)" % sto,
                          "observations": obs, "endpoints_enumerated": 0, "tenants": 2}))
        print(MARKER)
        return
    paths = spec.get("paths", {})
    get_by_id = [p for p in paths if "{" in p and "get" in paths[p]]

    # ---- cross-org read: create in B, read by id with A's token ----
    for coll, singular, idkey in (("parties", "party", "party_id"), ("items", "item", "item_id")):
        make = {"code": "BONLY-" + uuid.uuid4().hex[:6], "name": "b-only"}
        if coll == "parties":
            make["is_customer"] = True
        else:
            make["item_type"] = "RAW"
            make["primary_uom"] = "PIECE"
        cst, cdata, _ = req("POST", "/" + coll, token=b_tok, body=make)
        rid = None
        if isinstance(cdata, dict):
            rid = cdata.get("id") or cdata.get(idkey)
        if cst in (200, 201) and rid:
            gst, gdata, gh = req("GET", "/%s/%s" % (coll, rid), token=a_tok)
            leaked = gst == 200
            add("cross_org_read", "GET", "/%s/{id}" % coll, gst, gh, gdata, leaked,
                "404/422 - org A must not read org B's %s by id" % singular,
                ("200 LEAKED org B's row to org A" if leaked else "HTTP %s (isolation holds)" % gst),
                read_repro("/%s/{id}" % coll, coll))

    # ---- cross-firm write: every firm_id-taking POST, with org B's firm_id ----
    swept = 0
    for path in sorted(paths):
        if "{" in path:
            continue
        op = paths[path].get("post")
        if not op:
            continue
        rb = (op.get("requestBody") or {}).get("content", {}).get("application/json", {})
        schema = rb.get("schema")
        if not schema or not has_firm_id(schema, spec):
            continue
        swept += 1
        body, props = synth(schema, spec, "WILL_BE_B_FIRM")
        # parties needs >=1 role flag; satisfy common business rules so we reach the firm guard.
        if "is_customer" in props:
            body["is_customer"] = True
        body_live = dict(body)
        body_live["firm_id"] = b_firm
        st, data, hh = req("POST", path, token=a_tok, body=body_live)
        vuln = st in (200, 201)
        guard = isinstance(data, dict) and "not found in this organization" in body_text(data).lower()
        persisted_org = None
        if vuln and isinstance(data, dict):
            persisted_org = data.get("org_id")
        if vuln:
            actual = (
                "%s - the request was ACCEPTED at the HTTP API and the row persisted with "
                "org_id=%s (the caller's org, from the token) but firm_id=%s (org B's firm). "
                "The attacking token is a freshly-signed-up Owner whose own JWT firm_id=%s, so the "
                "body firm_id was never validated against the org on the create path."
                % (st, persisted_org, b_firm, attacker.get("firm_id"))
            )
        elif guard:
            actual = "422 rejected: a firm_id outside the caller's org is refused"
        elif st == 422:
            actual = "422 request validation (synthetic body missing required FKs; not reached)"
        else:
            actual = "HTTP %s" % st
        add("cross_firm_write", "POST", path, st, hh, data, vuln,
            "422 - a create carrying a firm_id outside the caller's org must be rejected",
            actual, write_repro("POST", path, body))

    print(MARKER)
    print(json.dumps({
        "observations": obs,
        "endpoints_enumerated": len(get_by_id),
        "tenants": 2,
        "error": "",
        "attacker": attacker,
        "debug": {"firm_id_posts_swept": swept, "total_paths": len(paths)},
    }))
    print(MARKER)

try:
    main()
except Exception as e:
    print(MARKER)
    print(json.dumps({"error": "probe crashed: %s" % e, "observations": obs,
                     "endpoints_enumerated": 0, "tenants": 0}))
    print(MARKER)
"""


def probe_py() -> str:
    """The in-sandbox probe source with the result marker substituted in."""
    return _PROBE_TEMPLATE.replace("__MARKER__", MARKER)
