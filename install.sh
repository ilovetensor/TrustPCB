#!/bin/sh
# TrustPCB installer for macOS and Linux.
#   curl -fsSL https://raw.githubusercontent.com/ilovetensor/TrustPCB/main/install.sh | sh
# Clones (or updates) TrustPCB into $TRUSTPCB_DIR (default ~/trustpcb), installs uv if it is missing,
# and runs `npm run setup`. It never uses sudo and changes nothing outside that folder and uv's own install.
set -eu

REPO="${TRUSTPCB_REPO:-https://github.com/ilovetensor/TrustPCB.git}"
DIR="${TRUSTPCB_DIR:-$HOME/trustpcb}"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "git is not installed. Install it, then run this again."
command -v node >/dev/null 2>&1 || fail "Node.js 22 or newer is not installed (https://nodejs.org). Install it, then run this again."
major=$(node -p 'process.versions.node.split(".")[0]')
[ "$major" -ge 22 ] || fail "Node.js $major found; TrustPCB needs 22 or newer."

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; export PATH
  command -v uv >/dev/null 2>&1 || fail "uv was installed but is not on PATH. Open a new terminal and run this again."
fi

if [ -d "$DIR/.git" ]; then
  say "Updating $DIR"
  git -C "$DIR" pull --ff-only
else
  [ -e "$DIR" ] && fail "$DIR exists and is not a TrustPCB checkout. Set TRUSTPCB_DIR to another folder."
  say "Cloning TrustPCB into $DIR"
  git clone --depth 1 "$REPO" "$DIR"
fi

cd "$DIR"
say "Installing dependencies"
npm run setup

command -v kicad-cli >/dev/null 2>&1 || node scripts/check-kicad.mjs >/dev/null 2>&1 || \
  echo "Note: kicad-cli (KiCad 10) was not found. Install KiCad for rule checks: https://www.kicad.org/download/"
command -v docker >/dev/null 2>&1 || echo "Note: Docker was not found. RF simulation needs it; everything else works without it."

say "Done. Start TrustPCB with:"
echo "  cd \"$DIR\" && npm run dev"
echo "Then add a Meta or Baseten key under Settings."
