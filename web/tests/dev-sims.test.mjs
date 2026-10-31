import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { cg, microstrip, RHO_S_1OZ, solveCopper, solvePlate, tdr } from "../lib/dev-sims/engine.mjs";
import { buildFixtureRuns } from "../lib/dev-sims/runs.mjs";

// The dev fixtures are built from small solvers. Each is checked against a closed-form answer, as the real simulators must be.

test("a straight copper strip drops I x R: the benchmark for the voltage-drop solver", () => {
  const nx = 200, ny = 10, dx = 0.1;                         // 20 mm long, 1 mm wide
  const g = { x0: 0, y0: 0, dx, nx, ny };
  const mask = new Uint8Array(nx * ny).fill(1);
  const col = (i) => Array.from({ length: ny }, (_, j) => j * nx + i);
  const sol = solveCopper({ g, masks: [mask], sources: [col(0)], sinks: [[{ cells: col(nx - 1), amps: 1 }]] });
  const end = col(nx - 1).reduce((a, k) => a + sol.drop[0][k], 0) / ny;
  const exact = RHO_S_1OZ * ((nx - 1) / ny);                // the discrete strip: (nx - 1) squares of length over ny squares of width
  assert.ok(Math.abs(end - exact) / exact < 1e-5, `${end} against ${exact}`);
  assert.ok(Math.abs(end - (RHO_S_1OZ * 20) / 1) / ((RHO_S_1OZ * 20) / 1) < 0.01, "within 1 % of rho x length / width");
  // the current through any cross-section is the load: sheet current x width
  const mid = col(100).reduce((a, k) => a + sol.sheet[0][k] * dx, 0);
  assert.ok(Math.abs(mid - 1) < 0.02, `current through the middle ${mid}`);
  assert.equal(sol.lost, 0);
});

test("a copper island with no path to the source is left out and its load is reported lost", () => {
  const nx = 30, ny = 3, g = { x0: 0, y0: 0, dx: 0.1, nx, ny };
  const mask = new Uint8Array(nx * ny).fill(1);
  for (let j = 0; j < ny; j++) mask[j * nx + 15] = 0;       // a cut across the strip
  const col = (i) => Array.from({ length: ny }, (_, j) => j * nx + i);
  const sol = solveCopper({ g, masks: [mask], sources: [col(0)], sinks: [[{ cells: col(29), amps: 0.5 }]] });
  assert.equal(sol.lost, 0.5);
  assert.ok(Number.isNaN(sol.drop[0][col(29)[0]]));
});

test("two layers joined by vias add the via resistance", () => {
  const nx = 20, ny = 4, g = { x0: 0, y0: 0, dx: 0.1, nx, ny };
  const top = new Uint8Array(nx * ny), bot = new Uint8Array(nx * ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < 10; i++) top[j * nx + i] = 1;
  for (let j = 0; j < ny; j++) for (let i = 9; i < nx; i++) bot[j * nx + i] = 1;
  const col = (i) => Array.from({ length: ny }, (_, j) => j * nx + i);
  const vias = [{ x: 0.95, y: 0.2, r: 0.3, layers: [0, 1] }];
  const withVia = solveCopper({ g, masks: [top, bot], sources: [col(0), []], sinks: [[], [{ cells: col(19), amps: 0.1 }]], vias, viaOhm: 0.0 + 0.002 });
  assert.equal(withVia.lost, 0);
  assert.ok(withVia.drop[1][col(19)[0]] > withVia.drop[0][col(9)[0]], "the drop continues down the lower layer");
});

test("heat spreading conserves energy: heat lost to the air equals heat put in", () => {
  const g = { x0: 0, y0: 0, dx: 0.5, nx: 60, ny: 40 };
  const gSheet = new Float32Array(60 * 40).fill(0.014);
  const power = new Float64Array(60 * 40);
  for (let j = 18; j < 22; j++) for (let i = 28; i < 32; i++) power[j * 60 + i] = 0.2 / 16;
  const h = 26e-6;
  const { rise, residual } = solvePlate({ g, gSheet, h, power });
  assert.ok(residual < 1e-8);
  let lost = 0;
  for (const v of rise) lost += h * g.dx * g.dx * v;
  assert.ok(Math.abs(lost - 0.2) / 0.2 < 1e-6, `lost ${lost} W of 0.2 W`);
  const peak = Math.max(...rise), edge = rise[0];
  assert.ok(peak > edge && edge > 0, "hottest at the source, still warm at the board edge");
});

test("conjugate gradients solve a small symmetric system", () => {
  const A = [[4, 1, 0], [1, 3, 1], [0, 1, 2]];
  const apply = (x, y) => { for (let i = 0; i < 3; i++) y[i] = A[i][0] * x[0] + A[i][1] * x[1] + A[i][2] * x[2]; };
  const { x } = cg(3, apply, [1, 2, 3], [4, 3, 2]);
  for (let i = 0; i < 3; i++) assert.ok(Math.abs(A[i][0] * x[0] + A[i][1] * x[1] + A[i][2] * x[2] - [1, 2, 3][i]) < 1e-8);
});

test("microstrip: a track about twice as wide as the dielectric is about 50 ohm on FR-4", () => {
  const { z } = microstrip(3.04, 1.6, 4.4, 0.001);
  assert.ok(z > 49 && z < 52.5, `${z} ohm`);
  assert.ok(microstrip(0.3, 0.2, 4.3).z > microstrip(0.6, 0.2, 4.3).z, "a narrower track has a higher impedance");
});

test("time domain reflection: a uniform line is flat, and a step settles at the new impedance", () => {
  const flat = tdr([{ z: 50, delay_ps: 100 }, { z: 50, delay_ps: 100 }]);
  assert.ok(flat.z.every((v) => Math.abs(v - 50) < 1e-6));
  const step = tdr([{ z: 70, delay_ps: 400 }], { zSource: 50, zLoad: 70 });
  const at = (t) => step.z[step.t_ps.findIndex((x) => x >= t)];
  assert.ok(Math.abs(at(200) - 70) < 0.05, `plateau ${at(200)}`);
  assert.ok(at(0) > 55 && at(0) < 65, "the edge is half way up at the launch");
  // 50 -> 70 -> 50: sees 70 for the length of the section, then the return to 50 after the round trip
  const bump = tdr([{ z: 70, delay_ps: 100 }, { z: 50, delay_ps: 300 }], { zLoad: 50 });
  const b = (t) => bump.z[bump.t_ps.findIndex((x) => x >= t)];
  assert.ok(Math.abs(b(100) - 70) < 1.5 && b(400) < 56, `${b(100)} then ${b(400)}`);
});

test("the dev runs agree with themselves: balance, ranking, what-ifs that help", { timeout: 60000 }, () => {
  const board = JSON.parse(readFileSync(new URL("../public/fixtures/nrf/board.json", import.meta.url), "utf8"));
  const { runs, rasters } = buildFixtureRuns(board, { fine: false });
  const by = Object.fromEntries(runs.map((r) => [r.run_id, r]));
  for (const id of ["fx-p1", "fx-p2", "fx-t1", "fx-t2", "fx-s1", "fx-s2"]) {
    const r = by[id].result;
    assert.equal(r.status, "ok");
    assert.equal(r.trust.status, "ok", id);
    assert.ok(r.assumptions.length > 4);
    assert.ok(r.assumptions.every((a) => ["datasheet", "user", "default", "board"].includes(a.source)));
    for (const ov of r.overlays) {
      const data = rasters.get(`${id}/${ov.id}`);
      assert.equal(data.length, ov.nx * ov.ny, `${id}/${ov.id} grid size`);
      assert.ok(data.some(Number.isFinite), `${id}/${ov.id} has values`);
      assert.ok(ov.vmax > ov.vmin);
    }
  }
  // the loads add up to the supply current
  const loads = Object.values(by["fx-p1"].result.metrics).filter((m) => m.group === "load");
  assert.ok(Math.abs(loads.reduce((a, m) => a + m.current, 0) * 1000 - by["fx-p1"].result.metrics.supply_current.value) < 0.01);
  assert.ok(by["fx-p2"].result.metrics.worst_drop.value < by["fx-p1"].result.metrics.worst_drop.value, "wider copper drops less");
  assert.ok(by["fx-t2"].result.metrics.hottest_part.value < by["fx-t1"].result.metrics.hottest_part.value, "less heat is cooler");
  const seg = (r) => Object.values(r.result.metrics).filter((m) => m.group === "segment");
  assert.ok(Math.abs(seg(by["fx-s2"])[0].deviation) < Math.abs(seg(by["fx-s1"])[0].deviation), "a wider feed is closer to 50 ohm");
  assert.equal(by["fx-p3"].waiting.position, 2);
  assert.equal(by["fx-t3"].status, "running");
});
