#!/bin/bash
# Parallel unit tests (no PG): 8 independent pytest processes over round-robin file shards.
cd /home/nill/perso/work/AutoWork/backend || exit 1
export TYPST_BINARY=$HOME/.local/share/autowork-tools/typst PATH=$HOME/.local/share/autowork-tools/bin:$PATH FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst
unset TEST_POSTGRES_ADMIN_DSN
N=${N:-8}; rm -rf /tmp/fu; mkdir -p /tmp/fu
ls tests/test_*.py | grep -v test_confined_analysis_dispatch > /tmp/fu/files.txt
for i in $(seq 0 $((N-1))); do awk -v n=$N -v i=$i 'NR%n==i' /tmp/fu/files.txt > /tmp/fu/shard$i.txt; done
for i in $(seq 0 $((N-1))); do
  ( mkdir -p $HOME/.cache/m06tmp/fu$i; TMPDIR=$HOME/.cache/m06tmp/fu$i .venv/bin/python -m pytest $(cat /tmp/fu/shard$i.txt) -q -p no:cacheprovider > /tmp/fu/out$i.txt 2>&1; echo $? > /tmp/fu/rc$i ) &
done
wait
for i in $(seq 0 $((N-1))); do tail -1 /tmp/fu/out$i.txt; done
grep -h ^FAILED /tmp/fu/out*.txt | sed 's/ - .*//'
