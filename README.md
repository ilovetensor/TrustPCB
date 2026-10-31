# TrustPCB

**Others read your datasheets. TrustPCB tests your board.**

TrustPCB is an open-source inspection layer for circuit boards. You upload a KiCad project and say what the board must do. An AI agent, Meta's Muse Spark, then studies the board like a senior engineer: it looks at it, forms ideas about what could go wrong, runs real checks and electromagnetic simulations to test those ideas, and writes a report in which every claim is tied to a place on the board and to the evidence behind it.

Upload a new version later and TrustPCB rechecks it and shows what changed.

## The inspector rule

**TrustPCB never edits a design.** It reads your files, never changes them, and never produces modified KiCad or manufacturing files. Recommendations are written as steps for you to apply in your own tool. Your upload is copied before anything touches it, and the checks run on the copy.

## What it checks

Plain code does the measuring first, with no AI involved. Then the agent investigates.

**Baseline (KiCad):** design rule check (DRC) and electrical rule check (ERC) through `kicad-cli`, board statistics, layer images and a 3D board view.

**Layout analyzers** (they measure; they never judge):

| Analyzer | What it looks for |
|---|---|
| Function zones | Parts grouped by role (power, radio, digital and so on), with overlapping zones flagged |
| Noise proximity | Switching or RF parts close to sensitive parts |
| Track jitter, detours, sharp corners | Shaky routing, long detours against the straight line, corners sharper than 90 degrees |
| Border tracks | Copper close to the board edge, or running along it |
| Decoupling | Capacitor distance from the IC power pin it serves, and vias on the way |
| Antenna keep-out | Copper, parts and tracks inside an antenna clearance zone |
| RF path | Length, width changes, layer jumps and stubs along RF nets |
| Via count | Layer jumps along important connections |
| Return path | Fast signals that cross a gap in the ground copper beneath them |
| Track impedance | Closed-form estimate of track impedance from width, layer and stackup |

**RF lab (printed antennas):** TrustPCB cuts the antenna region out of your board and simulates it with [openEMS](https://www.openems.de/) in a Docker container. You get an S11 tuning graph, a Smith chart, a 3D radiation pattern, an animated field and a surface-current view, all drawn from real solver output. It can also test what-if changes to the antenna model and calculate matching parts. Nothing is drawn that the solver did not produce.

## How the agent investigates, with physics as the judge

The agent is Muse (`muse-spark-1.3`) running a tool loop over Meta's Messages API. It can only use the TrustPCB tools. It cannot read files, run commands or browse.

1. **Understand.** It reads the goal, the parts and nets, any datasheets you uploaded, and looks at rendered images of the board.
2. **Review.** It works through every analyzer rule group, recording a finding or dismissing the group with a specific reason, and takes close-up views of the board. The server will not let it leave this phase until every group is accounted for.
3. **Investigate.** It ranks suspects, then tests one at a time with simulations: a base run, then what-if changes (for example clearing copper near the antenna, or changing its length). A suspect ruled out stays ruled out.
4. **Confirm.** The best result is re-run at higher quality. Quick screening runs are used only to compare a change with the base run.
5. **Report.** It writes a summary and ranked actions.

Muse is not trusted to produce numbers:

- Tools produce numbers; Muse produces judgment. Every number with a unit in a finding is checked by the server against tool evidence. Anything it cannot match is rejected.
- Every simulation passes automatic trust checks (energy decay, passivity, mesh density, port setup). A result that fails them cannot support a finding.
- An antenna is judged by its S11 across the band as built, not by its deepest dip.
- Budgets are enforced by the server (time, tool calls, images, solver time, tokens). Repeat requests return the stored result, and ideas that stop making progress are closed. A review always ends with a report, even if the agent fails.

## Honest labels

Everything TrustPCB shows carries one of four labels:

| Label | Meaning |
|---|---|
| **Measured** | A tool measured it on your board geometry (for example a distance, a count, a KiCad check result). |
| **Simulated** | An openEMS run produced it, and it passed the trust checks. |
| **Engineering opinion** | Muse's judgment without a measurement behind it. Opinion cannot be rated critical. |
| **Not tested** | It was not checked, and the reason is stated (for example Docker was missing, or the antenna type is not supported). |

Assumptions, such as a default board stackup, are listed in the report rather than hidden.

## Architecture

```text
Next.js UI  (2D board, 3D scene, RF charts, investigation trail, report)
      |   HTTP commands + SSE events
FastAPI app  (one local Python process, SQLite, no queue service)
  |- Intake        unpack, validate, version the upload
  |- Pipeline      kicad-cli + layout analyzers (plain code, no AI)
  |- Review        phases, budgets, stop rules, finding and number checks
  |- Runner        one per review, bound to a profile
  |     |- muse_api   Muse Spark 1.3 over the Messages API (real reviews)
  |     |- scripted   deterministic, calls no model (tests and demos)
  |     '- TrustPCB tools (the only tools the agent has)
  |- Simulation queue ---> Docker container: openEMS (worker/run_scenario.py)
  '- SQLite + artifact folder (.trustpcb/)
```

The web app runs on port 3118 and the backend on port 8118, both bound to `127.0.0.1`.

## Requirements

- Node.js 22 or newer
- [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv installs the Python it needs)
- [KiCad](https://www.kicad.org/) 9 or newer, for `kicad-cli`. It is found automatically in the standard install locations, or set `KICAD_CLI_PATH`.
- Docker, only for RF simulation. Without it every other feature works and RF shows as **Not tested**.
- A Muse API key, only for real agent reviews (see below).
- Chrome or Chromium, only for PDF export of reports (set `TRUSTPCB_BROWSER` to point at it). The HTML report needs nothing extra.

## Quick start

```sh
git clone <this repository> trustpcb
cd trustpcb

# 1. Install backend and web dependencies
npm run setup

# 2. Build the openEMS solver image (needed for RF simulation; takes a while)
docker build -f docker/ems/Dockerfile -t trustpcb-ems:dev .

# 3. Configure
cp .env.example .env     # then edit .env

# 4. Start the backend (8118) and the web app (3118)
npm run dev
```

Open http://127.0.0.1:3118 (the launcher opens it for you; set `TRUSTPCB_NO_OPEN=1` to stop that).

The image name defaults to `trustpcb-ems:dev`; override it with `TRUSTPCB_EMS_IMAGE`. The solver code in `worker/` is mounted into the container at run time, so changing it needs no rebuild.

To check KiCad discovery on its own: `node scripts/check-kicad.mjs`.

### Connecting Muse

1. Put your key in `.env`:

   ```sh
   MODEL_API_KEY=your-key-here
   TRUSTPCB_RUNNER=muse_api
   ```

   `.env` is git-ignored. The key is read only from the environment and is the only credential the Muse runner sends. The runner uses standard Muse models only and refuses any `-contributor` model, because those may use content for training.

2. Check the connection before a real review:

   ```sh
   uv run --project server python scripts/muse-smoke.py
   ```

   It makes a handful of short requests: one tool call, and one tool that returns a small image. If it reports that the model cannot see images returned from tools, run it with `--mode user_message` and set `TRUSTPCB_MUSE_IMAGE_MODE=user_message`.

When `TRUSTPCB_RUNNER` is not set (a fresh clone), or is set to `scripted`, the app uses the scripted runner. It follows a fixed script and calls no model, so it is useful for trying the interface and for tests, but it does not review your board.

Each review is bound to one runner profile, shown in the UI and stored with the review. There is no automatic fallback from one runner to another. Token spend is recorded per review.

### Sample boards

| Folder | What it is |
|---|---|
| `samples/nrf52840-node/` | An nRF52840 Bluetooth node (KiCad project with schematic) with an antenna keep-out; the integration tests run it through the RF lab |
| `samples/patch-2g5/` | A 2.5 GHz patch antenna on FR-4 (`geometry.json` holds the design values it was generated from) |

Zip a sample folder (or drag the folder in) on the upload screen.

## Tests

```sh
bash scripts/test-related.sh server/trustpcb/pcb/model.py   # tests for the area that owns a path (pytest -n 2)
bash scripts/test-all.sh                                    # everything except tests marked slow (pytest -n 4)
bash scripts/test-all.sh --slow                             # include solver benchmarks and full pipelines
npm run typecheck                                           # web type check
npm --prefix web test                                       # web unit tests
```

No test calls a live model. The slow tests use Docker and the solver image.

## Limits

- **Printed antennas only.** Chip antennas, modules, and antennas on metal or inside a case are not simulated. They are reported as **Not tested** with the reason.
- **Simulated, not measured.** Simulation results are predictions from a model of your board. They are not lab measurements. Treat an antenna as unverified until it is measured on a real board. Run-to-run S11 depth varies by about 0.5 dB because threading changes where the solver stops.
- **Default stackup assumptions.** If the board file has no stackup, TrustPCB assumes FR-4 (permittivity 4.5, loss tangent 0.02, 35 um copper) and lists that as an assumption. Confirm the real stackup on the goal form for results you intend to rely on.
- **KiCad boards only.** Other formats are not imported.
- **Local use only. Do not expose it to the internet.** The backend binds to loopback, requires a per-launch token and checks the `Host` and `Origin` headers, but it has no accounts and no hosted-use hardening. Run it on your own machine, with your own boards.
- **Your board data goes to Meta.** In a Muse review, rendered board images, part and net data and text from your files are sent to the Muse API. Do not upload a board you are not allowed to share.
- **Network use at build time.** The web app loads its fonts through `next/font/google`, which downloads them when the web app builds or starts.

## Configuration

All settings are optional environment variables; see `.env.example` for the full list. The common ones:

| Variable | Purpose |
|---|---|
| `MODEL_API_KEY` | Muse key for real reviews |
| `TRUSTPCB_RUNNER` | `muse_api` or `scripted` |
| `TRUSTPCB_MUSE_EFFORT` | `low`, `medium` (default), `high` or `xhigh` |
| `TRUSTPCB_DATA_DIR` | Where projects, uploads and the database live (default `.trustpcb/`) |
| `KICAD_CLI_PATH` | Path to `kicad-cli` if it is not found automatically |
| `TRUSTPCB_EMS_IMAGE` | Solver image name (default `trustpcb-ems:dev`) |

## Repository layout

```text
server/trustpcb/   backend: api, storage, sessions, intake/, pcb/, analyzers/, rf/, review/, prompts/, runners/
worker/            run_scenario.py, runs inside the solver container with no app imports
docker/ems/        pinned openEMS image build
web/               Next.js app (2D board, 3D scene, RF lab, report)
samples/           demo boards
scripts/           launcher, setup, KiCad check, Muse smoke test, test runners
docs/              specs and the hackathon disclosure
```

The product spec is in `docs/specs/`. The hackathon disclosure is in `docs/HACKATHON.md`.

## Licensing

TrustPCB is released under the [MIT License](LICENSE).

KiCad (`kicad-cli`, GPL-3.0-or-later) and openEMS (GPL-3.0) are run only as separate programs. TrustPCB does not import, link to or redistribute them, and their licences do not apply to TrustPCB's own code. If you build or distribute the openEMS Docker image, you are responsible for meeting the GPL terms for that image.

All other licences, including MPL-2.0 for the resvg renderer, are listed in [NOTICE](NOTICE). The backend and web shell started from the author's earlier MIT project, earlier project.
