#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ -z "${TEST_POSTGRES_ADMIN_DSN:-}" ]]; then
  printf '%s\n' \
    'TEST_POSTGRES_ADMIN_DSN doit désigner le PostgreSQL local de test.' \
    'Le script refuse de démarrer ou supprimer un conteneur PostgreSQL.' >&2
  exit 2
fi

run_root="${WORKFLOW_MEASUREMENT_DIR:-$repository_root/var/workflow-results/measurements/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$run_root"
failed_runs=0

for profile in fast full; do
  for iteration in 1 2 3; do
    run_dir="$run_root/$profile-$iteration"
    mkdir -p "$run_dir"
    if [[ "$profile" == fast ]]; then
      target=test-workflow
    else
      target=test-workflow-full
    fi
    printf '\n=== %s exécution %s/3 ===\n' "$profile" "$iteration"
    if WORKFLOW_RESULTS_DIR="$run_dir/results" \
      WORKFLOW_PYTEST_TIME_CMD="/usr/bin/time -v -o $run_dir/pytest-time.txt" \
      COMPOSE_MODELS=off \
      /usr/bin/time -v -o "$run_dir/make-time.txt" \
      make -C "$repository_root" "$target" 2>&1 | tee "$run_dir/output.log"; then
      run_status=0
    else
      run_status=$?
      failed_runs=$((failed_runs + 1))
    fi
    printf '%s\n' "$run_status" > "$run_dir/exit-status.txt"
    printf 'Code de sortie du run : %s\n' "$run_status"
  done
done

for profile in fast full; do
  elapsed=()
  rss=()
  statuses=()
  for iteration in 1 2 3; do
    run_dir="$run_root/$profile-$iteration"
    elapsed+=("$(awk -F': ' '/Elapsed \(wall clock\) time/ { n = split($2, t, ":"); if (n == 2) print t[1] * 60 + t[2]; else print t[1] * 3600 + t[2] * 60 + t[3] }' "$run_dir/make-time.txt")")
    rss+=("$(awk -F': ' '/Maximum resident set size \(kbytes\)/ { print $2 }' "$run_dir/pytest-time.txt")")
    statuses+=("$(cat "$run_dir/exit-status.txt")")
  done
  awk -v t1="${elapsed[0]}" -v t2="${elapsed[1]}" -v t3="${elapsed[2]}" \
    -v r1="${rss[0]}" -v r2="${rss[1]}" -v r3="${rss[2]}" \
    'BEGIN {
      t[1] = t1; t[2] = t2; t[3] = t3;
      for (i = 1; i <= 3; i++) for (j = i + 1; j <= 3; j++) if (t[i] > t[j]) { tmp = t[i]; t[i] = t[j]; t[j] = tmp }
      max_rss = r1; if (r2 > max_rss) max_rss = r2; if (r3 > max_rss) max_rss = r3;
      printf "%s total Make (s), min/médiane/max: %.2f / %.2f / %.2f; RSS pytest maximal: %d KiB\n", profile, t[1], t[2], t[3], max_rss;
    }' profile="$profile"
  printf '%s codes de sortie: %s, %s, %s\n' "$profile" "${statuses[0]}" "${statuses[1]}" "${statuses[2]}"
done

printf '\nSetup de la fixture session PostgreSQL/Alembic (secondes, min / médiane / max) :\n'
setup_times=()
for profile in fast full; do
  if [[ "$profile" == fast ]]; then
    first_database_test='tests/workflow_emulator/test_w2_inclusion_exclusion.py::'
  else
    first_database_test='tests/workflow_emulator/test_w1_progressive_abc.py::'
  fi
  for iteration in 1 2 3; do
    run_dir="$run_root/$profile-$iteration"
    setup_times+=("$(awk -v target="$first_database_test" '
      /[0-9.]+s setup[[:space:]]+tests\/workflow_emulator\// && index($0, target) {
        duration = $1; sub(/s$/, "", duration); print duration; exit
      }' "$run_dir/output.log")")
  done
done
awk -v s1="${setup_times[0]}" -v s2="${setup_times[1]}" -v s3="${setup_times[2]}" \
  -v s4="${setup_times[3]}" -v s5="${setup_times[4]}" -v s6="${setup_times[5]}" \
  'BEGIN {
    s[1] = s1; s[2] = s2; s[3] = s3; s[4] = s4; s[5] = s5; s[6] = s6;
    for (i = 1; i <= 6; i++) for (j = i + 1; j <= 6; j++) if (s[i] > s[j]) { tmp = s[i]; s[i] = s[j]; s[j] = tmp }
    printf "%.2f / %.2f / %.2f\n", s[1], (s[3] + s[4]) / 2, s[6];
  }'

printf '\nDurées par scénario (secondes, min / médiane / max sur les runs disponibles) :\n'
awk '
  /[0-9.]+s call[[:space:]]+tests\/workflow_emulator\// {
    duration = $1; sub(/s$/, "", duration)
    node = $0; sub(/^.*s call[[:space:]]+/, "", node)
    count[node]++; values[node SUBSEP count[node]] = duration
  }
  END {
    for (node in count) {
      n = count[node]
      for (i = 1; i <= n; i++) for (j = i + 1; j <= n; j++) {
        if (values[node SUBSEP i] > values[node SUBSEP j]) {
          tmp = values[node SUBSEP i]; values[node SUBSEP i] = values[node SUBSEP j]; values[node SUBSEP j] = tmp
        }
      }
      if (n % 2) median = values[node SUBSEP ((n + 1) / 2)]
      else median = (values[node SUBSEP (n / 2)] + values[node SUBSEP (n / 2 + 1)]) / 2
      printf "%s\t%.2f / %.2f / %.2f (%d runs)\n", node, values[node SUBSEP 1], median, values[node SUBSEP n], n
    }
  }' \
  "$run_root"/fast-{1,2,3}/output.log "$run_root"/full-{1,2,3}/output.log | sort

printf '\nRésultats détaillés : %s\n' "$run_root"
printf '%s\n' 'Chaque journal output.log contient --durations=0; pytest-time.txt donne son RSS maximal.'
printf '%s\n' 'Les profils sont chacun exécutés trois fois; les rapports JUnit et traces restent par exécution.'
if [[ "$failed_runs" -gt 0 ]]; then
  printf '%s run(s) de profil ont échoué; les mesures sont conservées mais ne prouvent pas un profil vert.\n' "$failed_runs" >&2
  exit 1
fi
