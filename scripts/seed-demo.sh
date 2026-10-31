#!/usr/bin/env bash
# Prepare a read-only demo from a finished data directory.
#
#   scripts/seed-demo.sh [--allow-any-runner] <finished-data-dir> [target-dir]
#
# Copies the data directory of finished reviews (the one TRUSTPCB_DATA_DIR pointed at, default .trustpcb/) to target-dir
# (default .trustpcb-demo/ in the repository root), then prints the command that serves it read-only:
#
#   TRUSTPCB_DEMO_READONLY=1 TRUSTPCB_DATA_DIR=<target-dir> npm run dev
#
# The stored service keys (secrets.json) are never copied. The source is never changed. The database is copied with SQLite's online backup, so a source that is open elsewhere still copies
# consistently. A source with a review or simulation still running is refused: the demo would show it as interrupted.
# A source with a review that did not run on Muse is refused too, because a public demo stands for work done with Meta models.
# --allow-any-runner accepts it for a private demo; each report still names the runner and model that wrote it.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

any_runner=0
if [[ "${1:-}" == "--allow-any-runner" ]]; then
  any_runner=1
  shift
fi
if [[ $# -lt 1 || $# -gt 2 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && exit 0 || exit 2
fi
source_dir="$(cd "$1" 2>/dev/null && pwd)" || { echo "seed-demo: $1 is not a directory" >&2; exit 1; }
target_arg="${2:-$root/.trustpcb-demo}"
mkdir -p "$target_arg"
target_dir="$(cd "$target_arg" && pwd)"
database="$source_dir/trustpcb.sqlite3"

[[ -f "$database" ]] || { echo "seed-demo: $source_dir has no trustpcb.sqlite3, so it is not a TrustPCB data directory" >&2; exit 1; }
[[ "$source_dir" != "$target_dir" ]] || { echo "seed-demo: source and target are the same directory" >&2; exit 1; }
if [[ -n "$(ls -A "$target_dir")" ]]; then
  echo "seed-demo: $target_dir is not empty. Remove it or choose another target." >&2
  exit 1
fi

python3 - "$database" "$any_runner" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
reviews = db.execute("SELECT count(*) FROM reviews WHERE status IN ('queued','running')").fetchone()[0]
try:
    jobs = db.execute("SELECT count(*) FROM sim_jobs WHERE status IN ('queued','running')").fetchone()[0]
except sqlite3.Error:
    jobs = 0
finished = db.execute("SELECT count(*) FROM reviews").fetchone()[0]
if reviews or jobs:
    sys.exit(f"seed-demo: {reviews} review(s) and {jobs} simulation(s) are still running in that data directory. Wait for them to finish.")
if not finished:
    sys.exit("seed-demo: that data directory has no reviews to show.")
other = db.execute("SELECT count(*) FROM reviews WHERE json_extract(profile, '$.runner') IS NOT 'muse_api'").fetchone()[0]
if other and sys.argv[2] != "1":
    sys.exit(f"seed-demo: {other} review(s) did not run on Muse. Use a Muse data directory, or pass --allow-any-runner for a private demo.")
print(f"{finished} review(s) found.")
PY

# everything except the live database files and the stored service keys (secrets.json, and its temporary files), then a consistent copy of the database
rsync -a --exclude 'trustpcb.sqlite3' --exclude 'trustpcb.sqlite3-wal' --exclude 'trustpcb.sqlite3-shm' --exclude 'secrets.json' --exclude '.secrets-*.tmp' "$source_dir"/ "$target_dir"/
python3 - "$database" "$target_dir/trustpcb.sqlite3" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
PY
chmod -R u+rwX "$target_dir"

echo "Demo data copied to $target_dir"
echo "Serve it read-only with:"
echo "  TRUSTPCB_DEMO_READONLY=1 TRUSTPCB_DATA_DIR='$target_dir' npm run dev"
