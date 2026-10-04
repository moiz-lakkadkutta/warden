#!/bin/sh
# with-services: start Postgres + Redis if they are not up, run "$@", then stop them cleanly so a
# kept sandbox checkpoint captures a consistent data directory. POSIX sh: the sandbox shell is dash.
set -u
started_pg=0
# Sandboxes' kernel has no SysV IPC; libshmshim emulates the one segment Postgres needs.
shim=""
[ -f /usr/local/lib/libshmshim.so ] && shim="LD_PRELOAD=/usr/local/lib/libshmshim.so"
if ! pg_isready -q -h 127.0.0.1 2>/dev/null; then
  runuser -u postgres -- env $shim pg_ctl -D "$PGDATA" -l /tmp/postgres.log -w -t 60 \
    -o "-c listen_addresses=127.0.0.1 -c fsync=off -c synchronous_commit=off -c full_page_writes=off" \
    start >/dev/null || { cat /tmp/postgres.log >&2; exit 70; }
  started_pg=1
fi
started_redis=0
if ! redis-cli -h 127.0.0.1 ping >/dev/null 2>&1; then
  redis-server --bind 127.0.0.1 --daemonize yes --save "" --appendonly no >/dev/null
  started_redis=1
fi
rc=0
if [ $# -gt 0 ]; then
  "$@"
  rc=$?
fi
[ "$started_redis" = 1 ] && redis-cli -h 127.0.0.1 shutdown nosave >/dev/null 2>&1
[ "$started_pg" = 1 ] && runuser -u postgres -- pg_ctl -D "$PGDATA" -m fast -w stop >/dev/null
exit "$rc"
