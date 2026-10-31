import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { bestRun, boardYRange, describeFreq, describeOps, findDips, measurementsOfSummary, pickStageJob, zFromGamma } from "../lib/sim-math.mjs";
import { allowedRoute } from "../lib/proxy-policy.mjs";

// The bundled real nRF52840 openEMS run (public/fixtures/nrf-sim, README.txt there).
const dir = new URL("../public/fixtures/nrf-sim/", import.meta.url);
const read = (f) => JSON.parse(fs.readFileSync(new URL(f, dir)));
const f32 = (f) => { const b = fs.readFileSync(new URL(f, dir)); return new Float32Array(b.buffer, b.byteOffset, b.byteLength / 4); };

test("nrf-sim sidecars match their binary sizes", () => {
  const field = read("field.json");
  for (const k of ["xy", "xz"]) assert.equal(fs.statSync(new URL(field[k].file, dir)).size, field[k].shape.reduce((a, b) => a * b, 1) * 4, k);
  const pulse = read("pulse.json");
  assert.equal(fs.statSync(new URL("pulse.bin", dir)).size, pulse.shape.reduce((a, b) => a * b, 1) * 4);
  const cur = read("current.json");
  assert.equal(fs.statSync(new URL("current.bin", dir)).size, cur.shape.reduce((a, b) => a * b, 1) * 4);
});

test("every file the lab asks the API for is an allowed proxy route", () => {
  for (const name of fs.readdirSync(dir).filter((f) => /\.(json|bin|png)$/.test(f) && !["scenario.json", "tune_matching.json", "compare_screen_confirm.json"].includes(f))) {
    assert.ok(allowedRoute("GET", `sim/abc123/files/${name}`), name);
  }
});

test("meta.json says scenario y is minus board y, and the field peak lands on the antenna in BOARD coordinates", () => {
  const meta = read("meta.json");
  assert.equal(meta.y_convention.scenario_y, "-board_y");
  const f = read("field.json").xy;
  const [, rows, cols] = f.shape;
  const buf = f32("field_xy.bin");
  let best = -1, at = 0;
  for (let i = 0; i < rows * cols; i++) { const m = Math.hypot(buf[i], buf[rows * cols + i]); if (m > best) { best = m; at = i; } }
  const r = Math.floor(at / cols), c = at % cols;
  const x = f.u_mm[0] + ((c + 0.5) / cols) * (f.u_mm[1] - f.u_mm[0]);
  const scenarioY = f.v_mm[0] + ((r + 0.5) / rows) * (f.v_mm[1] - f.v_mm[0]);
  const boardY = -scenarioY;
  const [x0, y0, x1, y1] = meta.setup.antenna_bbox;
  assert.ok(x > x0 - 6 && x < x1 + 6, `x ${x} vs antenna ${x0}..${x1}`);
  assert.ok(boardY > y0 - 6 && boardY < y1 + 6, `board y ${boardY} vs antenna ${y0}..${y1} (scenario y ${scenarioY} would be wrong)`);
  // read without the flip, the same point is farther from the antenna's middle
  const cy = (y0 + y1) / 2;
  assert.ok(Math.abs(boardY - cy) < Math.abs(scenarioY - cy), "the flipped y must be the closer one");
});

test("boardYRange negates and swaps the scenario v extent", () => {
  assert.deepEqual(boardYRange([-73.79, 29.98]), [-29.98, 73.79]);
});

test("surface current peaks on the antenna copper, in board coordinates", () => {
  const meta = read("meta.json"), c = read("current.json");
  const [rows, cols] = c.shape;
  const buf = f32("current.bin");
  let best = -1, at = 0;
  for (let i = 0; i < rows * cols; i++) if (buf[i] > best) { best = buf[i]; at = i; }
  const x = c.u_mm[0] + (((at % cols) + 0.5) / cols) * (c.u_mm[1] - c.u_mm[0]);
  const boardY = -(c.v_mm[0] + ((Math.floor(at / cols) + 0.5) / rows) * (c.v_mm[1] - c.v_mm[0]));
  const [x0, y0, x1, y1] = meta.setup.antenna_bbox;
  assert.ok(x > x0 - 3 && x < x1 + 3 && boardY > y0 - 3 && boardY < y1 + 3, `peak at ${x}, ${boardY}`);
});

test("findDips on the real S11 finds the solver's deepest dip", () => {
  const s11 = read("s11.json"), summary = read("summary.json");
  const dips = findDips({ f_ghz: s11.freq_hz.map((f) => f / 1e9), s11_db: s11.s11_db });
  const deepest = dips.reduce((m, d) => (d.db < m.db ? d : m));
  assert.ok(Math.abs(deepest.f_ghz * 1e9 - summary.sim.f_res) < 20e6);
  assert.ok(Math.abs(deepest.db - summary.sim.s11_min_db) < 0.6);
  assert.ok(dips.length >= 2, "more than one dip is reported");
});

test("impedance from the reflection coefficient matches zin.json", () => {
  const s11 = read("s11.json"), zin = read("zin.json");
  const z = zFromGamma(s11.s11_re, s11.s11_im, zin.z0_ohm ?? 50);
  for (const i of [0, 100, 200, 300, 400]) {
    assert.ok(Math.abs(z.z_re[i] - zin.z_re[i]) < 0.02 * Math.max(10, Math.abs(zin.z_re[i])) + 0.5, `re ${i}`);
    assert.ok(Math.abs(z.z_im[i] - zin.z_im[i]) < 0.02 * Math.max(10, Math.abs(zin.z_im[i])) + 0.5, `im ${i}`);
  }
});

test("measurements use the API's names and units", () => {
  const m = measurementsOfSummary(read("summary.json"));
  assert.equal(m.resonance.unit, "MHz");
  assert.ok(Math.abs(m.resonance.value - 1869.16) < 0.01);
  assert.equal(m.s11_dip_1.unit, "MHz");
  assert.ok(m.first_series_resonance.value > 1600 && m.first_series_resonance.value < 1800);
  assert.ok("antenna_only_band_worst_s11" in m && "as_built_band_worst_s11" in m);
});

test("what-if operations and frequencies are described plainly", () => {
  assert.equal(describeOps([{ op: "adjust_antenna_length", delta_mm: -6 }]), "antenna length −6.0 mm");
  assert.equal(describeOps([]), "");
  assert.match(describeFreq(2.44175e9, [2.4, 2.4835]), /inside/);
  assert.match(describeFreq(1.869e9, [2.4, 2.4835]), /outside/);
});

test("run choice: the latest confirm run with a field, else the latest run with one", () => {
  const job = (id, kind, files, status = "done") => ({ job_id: id, kind, files, status });
  const jobs = [job("a", "base", ["s11.json"]), job("b", "whatif", ["s11.json", "field.json", "field_xy.bin"]), job("c", "confirm", ["s11.json"]), job("d", "confirm", ["s11.json", "field.json", "field_xy.bin"], "running")];
  assert.equal(pickStageJob(jobs).job_id, "b");
  assert.equal(bestRun(jobs).job_id, "c");
  assert.equal(pickStageJob([job("x", "base", ["s11.json"])]), null);
});

test("run choice: the confirm of the board as drawn comes before a later confirm of a changed antenna", () => {
  const field = ["s11.json", "field.json", "field_xy.bin"];
  const job = (id, kind, ops = []) => ({ job_id: id, kind, files: field, status: "done", whatif_ops: ops });
  const jobs = [job("base", "confirm"), job("refined", "confirm", [{ op: "adjust_antenna_length", delta_mm: -4 }])];
  assert.equal(pickStageJob(jobs).job_id, "base");
  assert.equal(pickStageJob([job("only", "confirm", [{ op: "x" }]), job("screen", "base")]).job_id, "only");
});

test("a version is compared by its board as drawn: a confirm of a changed antenna is not that", () => {
  const job = (id, kind, ops = []) => ({ job_id: id, kind, files: ["s11.json"], status: "done", whatif_ops: ops });
  const jobs = [job("base", "base"), job("shorter", "whatif", [{ op: "adjust_antenna_length", delta_mm: -4 }]), job("confirm", "confirm"), job("confirm-shorter", "confirm", [{ op: "adjust_antenna_length", delta_mm: -4 }])];
  assert.equal(bestRun(jobs).job_id, "confirm");
  assert.equal(bestRun([job("base", "base"), job("confirm-shorter", "confirm", [{ op: "x" }])]).job_id, "base");
  assert.equal(bestRun([job("only", "confirm", [{ op: "x" }])]).job_id, "only", "nothing as drawn: fall back to what there is");
});
