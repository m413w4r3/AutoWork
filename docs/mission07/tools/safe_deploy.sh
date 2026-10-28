#!/bin/sh
# Deploy only when the relay has no in-flight request; never force.
cd /home/nill/perso/work/AutoWork || exit 1
RP=$(pgrep -f "python3 relay.py" | head -1)
start=$(date +%s)
while :; do
  n=$(pgrep -P "$RP" | wc -l)
  if [ "$n" = 0 ]; then
    kill -STOP "$RP"
    sleep 1
    n2=$(pgrep -P "$RP" | wc -l)
    if [ "$n2" = 0 ]; then break; fi
    kill -CONT "$RP"
  fi
  [ $(( $(date +%s) - start )) -gt 3000 ] && { echo "TIMEOUT waiting for quiet relay"; exit 2; }
  sleep 3
done
echo "relay paused at $(date -u +%H:%M:%S)"
make up 2>&1 | tail -3
kill -CONT "$RP"
echo "relay resumed at $(date -u +%H:%M:%S)"
