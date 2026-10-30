# TrustPCB

An open-source inspection layer for circuit boards. Upload a KiCad board, say what it must do, and get a marked-up board, real simulations, and tested recommendations.

TrustPCB never edits a design. Everything it shows is labelled `Measured`, `Simulated`, `Engineering opinion`, or `Not tested`.

Status: early development. See `docs/specs/` for the product spec and the shared contracts.

## Run it

Needs Node 20+, [uv](https://docs.astral.sh/uv/), and `kicad-cli` 10 for board checks. Docker is needed only for RF simulation.

```sh
npm run setup   # install backend and web dependencies
npm run dev     # backend on :8118, web on :3118
```

Data lives in `.trustpcb/` (override with `TRUSTPCB_DATA_DIR`). Copy `.env.example` to `.env` for local settings.

## Tests

```sh
bash scripts/test-related.sh server/trustpcb/storage.py   # tests for the lane that owns a path (pytest -n 2)
bash scripts/test-all.sh                                   # everything except tests marked slow (pytest -n 4)
bash scripts/test-all.sh --slow                            # include slow tests
```

## Credits

Starts from the backend and web shell of Dreamblox (MIT), our earlier project. KiCad and openEMS run as separate programs; see `NOTICE`.

Released under the MIT License.
