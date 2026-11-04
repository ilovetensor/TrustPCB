# TrustPCB installer for Windows (PowerShell).
#   irm https://raw.githubusercontent.com/ilovetensor/trustpcb/main/install.ps1 | iex
# Clones (or updates) TrustPCB into $env:TRUSTPCB_DIR (default ~\trustpcb), installs uv if it is missing,
# and runs `npm run setup`. It needs no administrator rights.
$ErrorActionPreference = 'Stop'

$Repo = if ($env:TRUSTPCB_REPO) { $env:TRUSTPCB_REPO } else { 'https://github.com/ilovetensor/trustpcb.git' }
$Dir = if ($env:TRUSTPCB_DIR) { $env:TRUSTPCB_DIR } else { Join-Path $HOME 'trustpcb' }

function Say($text) { Write-Host $text -ForegroundColor White }
function Fail($text) { Write-Host "Error: $text" -ForegroundColor Red; exit 1 }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail 'git is not installed (https://git-scm.com). Install it, then run this again.' }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { Fail 'Node.js 22 or newer is not installed (https://nodejs.org). Install it, then run this again.' }
$major = [int](node -p 'process.versions.node.split(".")[0]')
if ($major -lt 22) { Fail "Node.js $major found; TrustPCB needs 22 or newer." }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say 'Installing uv (Python package manager)'
  powershell -ExecutionPolicy ByPass -c 'irm https://astral.sh/uv/install.ps1 | iex'
  $env:Path = "$HOME\.local\bin;$env:Path"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail 'uv was installed but is not on PATH. Open a new terminal and run this again.' }
}

if (Test-Path (Join-Path $Dir '.git')) {
  Say "Updating $Dir"
  git -C $Dir pull --ff-only
} else {
  if (Test-Path $Dir) { Fail "$Dir exists and is not a TrustPCB checkout. Set TRUSTPCB_DIR to another folder." }
  Say "Cloning TrustPCB into $Dir"
  git clone --depth 1 $Repo $Dir
}

Set-Location $Dir
Say 'Installing dependencies'
npm run setup
if ($LASTEXITCODE -ne 0) { Fail 'npm run setup failed; see the output above.' }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Write-Host 'Note: Docker was not found. RF simulation needs it; everything else works without it.' }
Say 'Done. Start TrustPCB with:'
Write-Host "  cd `"$Dir`"; npm run dev"
Write-Host 'Then add a Meta or Baseten key under Settings.'
