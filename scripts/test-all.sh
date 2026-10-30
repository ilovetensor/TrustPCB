#!/usr/bin/env bash
# Orchestrator only: the whole server suite, pytest -n 4 --dist worksteal.
# Tests marked slow (solver benchmarks, full pipelines) are skipped unless --slow is given.
# Workers run tests in parallel, so each one is limited to a single math-library thread.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root/server"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1

slow=0
args=()
for arg in "$@"; do
  if [[ "$arg" == "--slow" ]]; then slow=1; else args+=("$arg"); fi
done
marker=(-m "not slow")
[[ $slow -eq 1 ]] && marker=()
exec uv run pytest -n 4 --dist worksteal -q "${marker[@]+"${marker[@]}"}" "${args[@]+"${args[@]}"}"
