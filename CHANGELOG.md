# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases are created automatically when a pull request is merged to `main`; the release workflow moves the entries below into a dated version section and adds one line for the merged pull request. See [docs/RELEASING.md](docs/RELEASING.md). Nothing has been released yet: the first release will be 0.1.0 and will include everything listed under Unreleased.

## [Unreleased]

### Added

- Board intake for KiCad projects: upload a project, copy it before anything touches it, and keep each upload as a version.
- KiCad baseline through `kicad-cli`: design rule check, electrical rule check, board statistics, layer images and a 3D board view.
- Layout analyzers that measure and never judge: function zones, noise proximity, track jitter, detours and sharp corners, border tracks, decoupling, antenna keep-out, RF path, via count, return path and track impedance.
- RF lab for printed antennas: openEMS simulation in a Docker container, with an S11 tuning graph, a Smith chart, a 3D radiation pattern, an animated field and a surface-current view, all drawn from solver output.
- Review agent loop with the phases understand, review, investigate, confirm and report. Budgets and stop rules are enforced by the server, and every number in a finding is checked against tool evidence.
- Honest labels on everything shown: Measured, Simulated, Engineering opinion and Not tested.
- Version rechecks that show what changed between two uploads, with the earlier prediction and a note when a result comes from the same solver run.
- Muse Spark runner over the Messages API, and a deterministic scripted runner that calls no model and is the default on a fresh clone.
- Web app (Next.js) with a 2D board, a 3D scene, RF charts, the investigation trail, chat and a printable report with PDF export.
- Read-only demo mode that serves finished reviews and refuses every change.
- Sample boards: an nRF52840 Bluetooth node and a 2.5 GHz patch antenna.
- Continuous integration for pull requests, a version consistency check, and automatic releases on merge (see docs/RELEASING.md).
- Contributor documentation: CONTRIBUTING.md, a code of conduct, a security policy, issue templates and a pull request template.
