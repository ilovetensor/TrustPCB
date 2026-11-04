# Contributing to TrustPCB

Thanks for helping. TrustPCB is an open-source inspection layer for KiCad boards. This page says how to set the project up, how to test a change, and what a good pull request looks like. By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## The rules of the project

These matter more than style. A change that breaks one will not be merged.

1. **TrustPCB never edits a design.** It reads files and copies them before any check runs. It never writes a modified KiCad file or a manufacturing file (Gerber, drill, pick-and-place and so on). Recommendations are text for the user to apply in their own tool. What-ifs happen inside simulation, on a model, never on the user's files.
2. **Findings trace to evidence.** Every claim ties to a place on the board (a target id such as `ref:U1` or `net:VDD`) and to the tool result behind it. Every number with a unit in a finding must match tool evidence; the server rejects numbers it cannot match.
3. **Honest labels.** A result carries one of four labels: **Measured** (a tool measured it on the board geometry; only KiCad and the layout analyzers measure), **Simulated** (an openEMS run produced it and it passed the trust checks), **Engineering opinion** (judgment with no measurement behind it; never rated critical) and **Not tested** (with the reason). A simulated number is never called measured.
4. **Analyzers measure and never judge.** Deciding what a measurement means is the review agent's job.
5. **No secrets and no private boards.** Never commit keys, `.env`, or a board you may not share. Tests never call a live model and never need a key.

## Set up

You need Node.js 22 or newer and [uv](https://docs.astral.sh/uv/) (it installs Python 3.12). KiCad 10 (`kicad-cli`) is optional for most work: without it the tests that need it skip themselves. Docker is only needed for RF simulation work. A Muse API key is optional: the default **scripted** runner needs no key and calls no model, so you can run the app and all the tests without one.

```sh
git clone <your fork of this repository> trustpcb
cd trustpcb
npm run setup          # backend (uv sync) and web (npm install) dependencies
cp .env.example .env   # optional; every setting has a default
npm run dev            # backend on 8118, web app on 3118
```

The README has the full quick start, including how to build the solver image and connect Muse.

## Run the tests

Run the tests for the area you changed, not everything, while you work:

```sh
bash scripts/test-related.sh server/trustpcb/analyzers/track_angle.py   # runs the tests that own that path (pytest -n 2)
npm --prefix web test                                                   # web unit tests
npm run typecheck                                                       # web type check
npm run test:release                                                    # release script tests
npm run check:versions                                                  # every version field agrees
```

Before you open a pull request, run the same backend command CI runs. From the `server` folder:

```sh
uv run pytest -n 2 -m "not slow" -q -rs
```

Tests marked `slow` use Docker and the solver image and are not run by CI; run them with `bash scripts/test-all.sh --slow` if you changed the simulation code. Tests that need `kicad-cli` skip themselves when it is missing, and `-rs` lists each skip with its reason. CI does not install KiCad, so those tests are skipped there; run them on your machine if your change touches KiCad handling.

## Branches, commits and pull requests

- Branch from `main`: `feat/short-name`, `fix/short-name` or `docs/short-name`.
- Commit messages and pull request titles use the history's style: `type: what changed`, in lower case and the present tense, for example `feat: flag connectors whose opening a cable cannot reach` or `fix: say why chat is locked while a review runs`. Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`.
- Keep a pull request to one change. Add or update tests with the code. Update the README or `docs/` when behaviour the user sees changes.
- Fill in the pull request template. CI must pass: the **CI result** check covers backend tests, web type check and tests, and the version check. A docs-only change skips the heavy jobs.
- Do not edit version numbers or `CHANGELOG.md` by hand. The release workflow does both (below). Put notes in the pull request description instead.

### How releases work (what the labels do)

When a pull request is merged to `main`, a GitHub Action creates a release. The pull request's labels pick the version (X.Y.Z):

| Label on the pull request | Bump | Example |
|---|---|---|
| `major` | the first number | 1.2.3 becomes 2.0.0 |
| `minor` | the middle number | 1.2.3 becomes 1.3.0 |
| anything else, or no label | the last number | 1.2.3 becomes 1.2.4 |

If both labels are present, `major` wins. The pull request title becomes the changelog line (`feat:` goes under Added, `fix:` under Fixed, the rest under Changed), so write it for a reader of the changelog. Maintainers apply the label before merging. The details are in [docs/RELEASING.md](docs/RELEASING.md).

## What makes a good analyzer or finding

An analyzer is one module in `server/trustpcb/analyzers/` with `analyze(board, ctx) -> list[Candidate]` (see `track_angle.py` for a small one), its name added to `ANALYZER_NAMES` in `analyzers/base.py`, and tests in `server/tests/analyzers/`. The step-by-step list and the Candidate shape are under [Adding an analyzer](#adding-an-analyzer) below. A good one:

- **Measures one thing and says what.** Values come with units (mm, deg, ohm) and the limit it compared against.
- **Points at the board.** Every candidate lists target ids that resolve with `Board.resolve`, and marks that let the app draw the place.
- **Runs without extra input.** It works without part roles or a goal, using best-effort heuristics, and uses them when they exist.
- **Has a threshold you can defend.** Cite a datasheet, an application note or a standard in the module docstring or the pull request. Explain how to tell a real problem from a false alarm.
- **Is tested on both sides.** A small synthetic board where it fires, one where it stays quiet, and, where it makes sense, a run on a bundled sample board. Tests must not take long.
- **Does not judge.** It sets `severity_hint` as a hint only. It does not write advice.

A good finding (changes to review logic and prompts) names the place, states the measured or simulated number with its label and evidence id, says what would change if it were fixed, and says what was not tested. If you cannot trace it to evidence, it is an Engineering opinion and cannot be critical.

## Other things

- Code style follows `.editorconfig` and the surrounding code. CI lints Python only for syntax errors and undefined names.
- Test data: use the boards in `samples/`. Do not add boards you do not own or may not share.
- Docs and interface text use plain, short sentences.
- Questions are welcome as issues or discussions once the repository has them enabled.

## For maintainers

When the repository is created, follow the setup list in [docs/RELEASING.md](docs/RELEASING.md): create the labels, allow the release commit through branch protection, turn on private vulnerability reporting, and fill in the contact method in the Code of Conduct.

## Adding an analyzer

1. Create `server/trustpcb/analyzers/<name>.py` with `RULE = "<name>"` and `analyze(board, ctx) -> list[dict]`. Use `Emitter(RULE).add(title, measurements, targets, marks, severity)`; `track_angle.py` is a small example. Measurements use `meas(value, unit)`; marks use `mark(type, targets, ...)`.
2. Add the name to `ANALYZER_NAMES` in `analyzers/base.py` (this is also the run order) and any thresholds to `DEFAULTS` there, read with `ctx.t("key")`.
3. If the rule is about layout and the review should judge it in phase 3, add it to `LAYOUT_RULES` in `review/budgets.py`. Rule groups are formed from each candidate's `rule`, so nothing else is needed for the review to see it.
4. Add a short note on what it looks for to the table in the README.
5. Add tests in `server/tests/analyzers/`: a small board where it fires, one where it stays quiet, and a run on a sample board where that makes sense.
6. Run `bash scripts/test-related.sh server/trustpcb/analyzers/<name>.py`.

An analyzer measures; it never judges. It sets `severity_hint` as a hint only, and one analyzer failing does not stop the others.

## Evidence, findings and candidates

- **Evidence** (`review/evidence.py`) is a stored row with a display code (E-nnn) and a type: `candidate`, `measurement`, `simulation`, `confirm`, `comparison`, `calculation`, `placement`, `reference`. Values are stored as `{measurements: {name: {value, unit}}, ...}`.
- **Finding** (`review/findings.py`) is a row with a code (F-nnn), severity, class, text fields, marks (where on the board), evidence ids, assumptions and not-tested notes. Every change goes through `FindingEngine.prepare`, which:
  - sets the class from the evidence (the model cannot choose it);
  - refuses opinion rated critical;
  - checks every number with a unit against evidence (`number_check.py`);
  - checks every mark resolves on the board;
  - refuses wording that claims the board was edited;
  - refuses once (`lint.py`, then lets the same sentence stand, so a run never jams) a causal sentence with no evidence code, a do-not-populate statement that names one of several parts or guesses why, "keep the width constant" beside a width for the target impedance, and a severity other than the analyzers' worst hint with no `severity_reason`.
- The server also refuses to dismiss an RF `netclass_width` result whose class width gives about the target impedance (`classwidth.py`) and writes the class width and the width for the target into the finding that covers it. `read_schematic` with kind `state` returns the schematic's no-connect markers, do-not-populate flags and pin names (`analyzers/schematic.py`), and parity results carry what the schematic already has.
- **Fingerprints** (`fingerprint.py`) keep a finding's identity when coordinates shift, so a new version can be compared finding by finding (`compare.py`, `change.py`).
- **Candidates** are the analyzers' and KiCad's raw results: `{id, rule, title, source, measurements, targets, marks, severity_hint, ...}`. Targets are board ids such as `ref:C5`, `pad:U1:3`, `net:GND` or `point:x,y`, resolved by `Board.resolve`.
