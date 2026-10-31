#!/usr/bin/env bash
# The whole server suite, pytest -n 4 --dist worksteal, then the web tests (npm --prefix web test).
# Tests marked slow (solver benchmarks, full pipelines) are skipped unless --slow is given.
# Each test process is limited to a single math-library thread, so the parallel workers do not oversubscribe the CPU.
# It is the heavy full run: while working on one area, use scripts/test-related.sh instead.
#
#   scripts/test-all.sh [--slow] [--no-web] [pytest arguments]
#
# The web tests run only on a full run: they are skipped with --no-web or when pytest arguments select tests.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root/server"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1

slow=0
web=1
args=()
for arg in "$@"; do
  case "$arg" in
    --slow) slow=1 ;;
    --no-web) web=0 ;;
    *) args+=("$arg") ;;
  esac
done
[[ ${#args[@]} -gt 0 ]] && web=0
marker=(-m "not slow")
[[ $slow -eq 1 ]] && marker=()
status=0
uv run pytest -n 4 --dist worksteal -q "${marker[@]+"${marker[@]}"}" "${args[@]+"${args[@]}"}" || status=$?
if [[ $web -eq 1 && -f "$root/web/package.json" ]]; then
  echo "--- web tests"
  npm --prefix "$root/web" test --silent || status=$?
fi
exit $status
