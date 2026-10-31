import { test } from "node:test";
import assert from "node:assert/strict";
import { evidenceLink, simKindName, simStatusText, describeBoardOps, defaultCount, isBoardRow, limitShare, metricChange, normaliseRun, pairOf, panelModel, pickRun, seriesOf, sourcesFor, trustOf, usesDefault } from "../lib/board-sim-model.mjs";

const run = (over) => normaliseRun({ run_id: "r", kind: "power", status: "done", result: { metrics: {} }, ...over });

test("RF jobs are not board runs, power heat and signal runs are", () => {
  assert.equal(isBoardRow({ kind: "base" }), false);
  assert.equal(isBoardRow({ kind: "confirm" }), false);
  assert.equal(isBoardRow({ kind: "power" }), true);
  assert.equal(isBoardRow({ kind: "whatif", sim_kind: "thermal" }), true);
});

test("a row becomes a run without inventing anything", () => {
  const r = normaliseRun({ job_id: "j1", sim_kind: "signal", kind: "whatif", status: "done", whatif_ops: [{ op: "widen_track", net: "A", width_mm: 0.4 }] });
  assert.equal(r.run_id, "j1"); assert.equal(r.kind, "signal"); assert.equal(r.result, null); assert.equal(r.waiting, null);
  assert.equal(r.whatif.length, 1); assert.equal(r.base_run, null); assert.equal(r.ref, null);
});

test("waiting for the solver is read from the phase, the step or a waiting object", () => {
  assert.deepEqual(normaliseRun({ run_id: "a", kind: "power", status: "queued", phase: "waiting", position: 2, ahead: 1 }).waiting, { position: 2, ahead: 1 });
  assert.deepEqual(normaliseRun({ run_id: "a", kind: "power", status: "queued", step: "waiting" }).waiting, { position: null, ahead: null });
  assert.deepEqual(normaliseRun({ run_id: "a", kind: "power", status: "queued", waiting: { position: 3, ahead: 3 } }).waiting, { position: 3, ahead: 3 });
  assert.equal(normaliseRun({ run_id: "a", kind: "power", status: "done", phase: "waiting" }).waiting, null);
});

test("what-ifs are described in words", () => {
  assert.equal(describeBoardOps([{ op: "widen_track", net: "3V3", width_mm: 0.9 }]), "Widen 3V3 to 0.9 mm");
  assert.equal(describeBoardOps([{ op: "set_current", ref: "U3", amps: 0.08 }]), "Set U3 to 80 mA");
  assert.equal(describeBoardOps([{ op: "set_part_power", ref: "U2", watts: 0.04 }]), "Set U2 to 40 mW");
  assert.equal(describeBoardOps([{ op: "add_via", net: "GND", at: [1, 2] }, { op: "remove_gap" }]), "Add a via on GND, Close a gap in the plane");
  assert.equal(describeBoardOps([]), "");
});

test("before and after: a what-if pairs with its base, a base with the newest what-if of its kind", () => {
  const base = run({ run_id: "b" }), w1 = run({ run_id: "w1", whatif: [{ op: "widen_track" }], base_run: "b" }), w2 = run({ run_id: "w2", whatif: [{ op: "widen_track" }], base_run: "b" });
  const other = run({ run_id: "t", kind: "thermal" });
  const all = [base, w1, w2, other];
  assert.equal(pairOf(all, w1).before.run_id, "b");
  assert.equal(pairOf(all, base).after.run_id, "w2");
  assert.equal(pairOf(all, other), null);
  assert.equal(pairOf(all, run({ run_id: "q", status: "running", result: null })), null);
  const orphan = run({ run_id: "o", whatif: [{ op: "x" }] });
  assert.equal(pairOf([base, orphan], orphan).before.run_id, "b");
});

test("the first run shown is the newest finished base run of the kind", () => {
  const a = run({ run_id: "a" }), w = run({ run_id: "w", whatif: [{ op: "x" }], base_run: "a" }), t = run({ run_id: "t", kind: "thermal" }), live = run({ run_id: "l", status: "running", result: null });
  assert.equal(pickRun([a, w, t, live], { kind: "power" }).run_id, "a");
  assert.equal(pickRun([a, w, t, live], { run: "w" }).run_id, "w");
  assert.equal(pickRun([a, w, t, live], { ref: "zz" }).run_id, "t");
  assert.equal(pickRun([live], {}).run_id, "l");
  assert.equal(pickRun([], {}), null);
});

test("metrics go to the panel that shows them", () => {
  const m = panelModel({ metrics: {
    worst_drop: { value: 1, unit: "mV", label: "Worst" }, "load.U3": { value: 2, unit: "mV", label: "U3" }, "part:U2": { value: 80, unit: "C", label: "U2" },
    "segment.1": { value: 50, unit: "ohm", label: "s" }, x: { value: 1, unit: "", label: "x", group: "load" }, "weird.k": { value: 1, unit: "", label: "w" },
  } });
  assert.deepEqual(m.headline.map((r) => r.key), ["worst_drop"]);
  assert.deepEqual(m.load.map((r) => r.key), ["load.U3", "x"]);
  assert.deepEqual(m.part.map((r) => r.key), ["part:U2"]);
  assert.equal(m.segment.length, 1); assert.equal(m.other.length, 1);
  assert.deepEqual(panelModel(null).headline, []);
});

test("series come from the result and from a metric that carries one", () => {
  const s = seriesOf({ series: [{ id: "a" }], metrics: { tdr: { label: "T", series: { x: [1], y: [2], x_unit: "ns", y_unit: "ohm" } } } });
  assert.deepEqual(s.map((x) => x.id), ["a", "tdr"]);
  assert.deepEqual(seriesOf(null), []);
});

test("defaults are counted and tied to the part they belong to", () => {
  const a = [{ name: "U3 current", source: "user" }, { name: "U2 dissipation", source: "default" }, { name: "U2 thermal resistance", source: "datasheet" }, { name: "Air temperature", source: "default" }];
  assert.equal(defaultCount(a), 2);
  assert.deepEqual(sourcesFor("U2", a), ["default", "datasheet"]);
  assert.equal(usesDefault("U2", a), true); assert.equal(usesDefault("U3", a), false); assert.equal(usesDefault("U", a), false); assert.equal(usesDefault(null, a), false);
});

test("trust: ok only when it says ok and no check failed", () => {
  assert.equal(trustOf({ trust: { status: "ok", checks: [{ check: "a", ok: true }] } }).ok, true);
  assert.equal(trustOf({ trust: { status: "ok", checks: [{ check: "a", ok: false }] } }).ok, false);
  assert.equal(trustOf({ trust: { status: "failed", checks: [] } }).ok, false);
  assert.equal(trustOf(null).ok, false);
  assert.equal(trustOf({ trust: { status: "failed", checks: [{ check: "z", ok: false }] } }).failed.length, 1);
});

test("limits and changes", () => {
  assert.equal(limitShare(33, 66), 50); assert.equal(limitShare(1, null), null);
  const a = { metrics: { w: { value: 4, unit: "mV" } } }, b = { metrics: { w: { value: 3, unit: "mV" } } };
  assert.deepEqual(metricChange(a, b, "w"), { before: 4, after: 3, delta: -1, unit: "mV" });
  assert.equal(metricChange(a, b, "q"), null); assert.equal(metricChange(null, b, "w"), null);
});

test("progress lines: the kind, the wait for the solver, the step and the share done", () => {
  assert.equal(simKindName("power"), "Power"); assert.equal(simKindName("thermal"), "Heat"); assert.equal(simKindName("signal"), "Signal"); assert.equal(simKindName("whatif"), "Antenna");
  assert.equal(simStatusText({ waiting: { position: 2, ahead: 1 } }), "Waiting for the solver, 1 ahead");
  assert.equal(simStatusText({ waiting: { position: 3, ahead: 0 } }), "Waiting for the solver, place 3");
  assert.equal(simStatusText({ waiting: { position: null, ahead: null } }), "Waiting for the solver");
  assert.equal(simStatusText({ step: "solving", pct: 0.42, status: "running" }), "Solving 42%");
  assert.equal(simStatusText({ step: "queued", pct: 0, status: "queued" }), "Queued 0%");
  assert.equal(simStatusText({ status: "running", pct: 0.1 }), "Solving 10%");
});

test("evidence links to the lab that shows its simulation", () => {
  assert.deepEqual(evidenceLink("r1", { source_tool: "run_board_sim", values: { sim_kind: "power", run_id: "ab12" } }), { href: "/reviews/r1/sim?kind=power&run=ab12", label: "Open in the simulation lab" });
  assert.equal(evidenceLink("r1", { source_tool: "run_board_sim", values: { kind: "thermal", ref: "R4" } }).href, "/reviews/r1/sim?kind=thermal&ref=R4");
  assert.equal(evidenceLink("r1", { source_tool: "compare_sims", values: {} }).href, "/reviews/r1/sim");
  assert.deepEqual(evidenceLink("r1", { source_tool: "run_simulation", values: { job_id: "j9" } }), { href: "/reviews/r1/rf?job=j9", label: "Open in the RF lab" });
  assert.equal(evidenceLink("r1", { source_tool: "measure", values: { width: { value: 1, unit: "mm" } } }), null);
  assert.equal(evidenceLink("r1", null), null);
  assert.equal(evidenceLink("r 1", { values: { sim_kind: "signal", run_id: "a/b" } }).href, "/reviews/r 1/sim?kind=signal&run=a%2Fb");
});
