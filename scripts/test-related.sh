#!/usr/bin/env bash
# Run the tests related to the given paths with pytest -n 2.
#
#   scripts/test-related.sh server/trustpcb/pcb/model.py server/tests/pcb/test_model.py -- -k board
#
# A path under server/tests is run as is. Source paths map to the test folders that cover them:
#   server/trustpcb/rf/, worker/, docker/       tests/rf_model and tests/rf
#   server/trustpcb/runners/                    tests/foundation and tests/muse
#   server/trustpcb/{api,storage,sessions,events}.py   tests/foundation
#   server/trustpcb/intake/                     tests/pcb
#   server/trustpcb/prompts/                    tests/review
#   server/trustpcb/settings_store.py           tests/parts
#   server/trustpcb/<package>/                  tests/<package>   (pcb, analyzers, review, parts)
# Everything after "--" goes to pytest unchanged. The web app has its own tests: npm --prefix web test.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1

targets=()
passthrough=()
while [[ $# -gt 0 ]]; do
  arg="$1"; shift
  if [[ "$arg" == "--" ]]; then passthrough=("$@"); break; fi
  abs="$arg"; [[ "$abs" = /* ]] || abs="$(cd "$(dirname "$arg")" 2>/dev/null && pwd)/$(basename "$arg")"
  rel="${abs#"$root"/}"
  case "$rel" in
    server/tests/*) targets+=("${rel#server/}") ;;
    server/trustpcb/rf/*|worker/*|docker/*) targets+=("tests/rf_model" "tests/rf") ;;
    server/trustpcb/runners/*) targets+=("tests/foundation" "tests/muse") ;;
    server/trustpcb/api.py|server/trustpcb/storage.py|server/trustpcb/sessions.py|server/trustpcb/events.py) targets+=("tests/foundation") ;;
    server/trustpcb/intake/*) targets+=("tests/pcb") ;;
    server/trustpcb/settings_store.py) targets+=("tests/parts") ;;
    server/trustpcb/prompts/*) targets+=("tests/review") ;;
    server/trustpcb/*/*) package="${rel#server/trustpcb/}"; targets+=("tests/${package%%/*}") ;;
    web/*) echo "skipping $arg: web tests run with npm --prefix web test" >&2 ;;
    *) echo "skipping $arg: not under server/tests, server/trustpcb, worker or docker" >&2 ;;
  esac
done

cd "$root/server"
existing=()
for target in "${targets[@]:-}"; do
  [[ -z "$target" ]] && continue
  if [[ -e "$target" ]]; then existing+=("$target"); else echo "no tests yet at $target" >&2; fi
done
if [[ ${#existing[@]} -eq 0 ]]; then
  echo "usage: scripts/test-related.sh <paths> [-- pytest options]" >&2
  echo "nothing to run" >&2
  exit 0
fi
# Deduplicate while keeping order.
unique=($(printf '%s\n' "${existing[@]}" | awk '!seen[$0]++'))
exec uv run pytest -n 2 -q "${passthrough[@]+"${passthrough[@]}"}" "${unique[@]}"
