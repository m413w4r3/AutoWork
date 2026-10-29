#!/bin/sh
# Deploy only after relay-status.json reports no active API requests.
cd /home/nill/perso/work/AutoWork || exit 1
RELAY_WORKDIR=${RELAY_WORKDIR:-/home/nill/perso/work/AutoWork/.autowork-runs/m07}
STATUS="$RELAY_WORKDIR/relay-status.json"

snapshot() {
  python3 - "$STATUS" <<'PY'
import datetime
import json
import pathlib
import sys

try:
    status = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    heartbeat = datetime.datetime.fromisoformat(status["heartbeat_ts"])
    age = max(0, int((datetime.datetime.now(datetime.timezone.utc) - heartbeat).total_seconds()))
    pid = int(status["pid"])
    idle = not status.get("in_flight")
    print(pid, int(idle), age)
except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
    print(0, 0, 999999)
PY
}

start=$(date +%s)
RP=0
while :; do
  set -- $(snapshot)
  pid=$1
  idle=$2
  age=$3
  if [ "$pid" -le 1 ] || [ "$age" -gt 20 ]; then
    echo "ERROR: missing or stale relay-status.json ($STATUS)"
    exit 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "ERROR: relay pid $pid from status is not running"
    exit 1
  fi
  if [ "$idle" = 1 ]; then
    kill -STOP "$pid" || exit 1
    sleep 1
    set -- $(snapshot)
    if [ "$1" = "$pid" ] && [ "$2" = 1 ]; then
      RP=$pid
      break
    fi
    kill -CONT "$pid" 2>/dev/null || exit 1
  fi
  [ $(( $(date +%s) - start )) -gt 3000 ] && { echo "TIMEOUT waiting for quiet relay"; exit 2; }
  sleep 3
done

resume_relay() {
  if [ "$RP" -gt 1 ]; then
    kill -CONT "$RP" 2>/dev/null || true
    RP=0
  fi
}
trap resume_relay EXIT HUP INT TERM

echo "relay paused at $(date -u +%H:%M:%S)"
make up 2>&1 | tail -3
resume_relay
trap - EXIT HUP INT TERM
echo "relay resumed at $(date -u +%H:%M:%S)"
