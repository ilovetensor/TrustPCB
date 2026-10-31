import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { headlineOf, screenVsConfirm, changeText, versionChangeOf, changeByCode } from "../lib/story.mjs";
import { acceptedPct, acceptedFromCurve, acceptedOf, fixedDipsMhz, isFixedDip, sameGeometry, fmtPct } from "../lib/sim-math.mjs";

// The investigation of a real finished Standard review (screen runs, then confirm runs, then a refine round). Numbers are the solver's.
const snap = JSON.parse(fs.readFileSync(new URL("./data/real_investigation.json", import.meta.url)));

test("screen against confirm: what screen predicted and what confirm measured, per round", () => {
  const story = screenVsConfirm(snap);
  assert.equal(story.length, 2, "first attempt and refine round 1");
  const first = story[0];
  assert.equal(first.round, 0);
  const change = first.rows.find((r) => r.label === "With the change");
  assert.ok(change.screen < -10, "the screen predicted a pass");
  assert.ok(change.confirm > -5, "confirm measured far less");
  assert.ok(change.screen < change.confirm, "screen overstated");
  assert.equal(first.rows[0].label, "As built");
  assert.equal(story[1].round, 1);
});

test("a snapshot without a partial win has no headline, and a headline is read from the snapshot first", () => {
  assert.equal(headlineOf(snap), null);
  assert.equal(headlineOf(null), null);
  const best = { band_worst_s11_db: -6.1, band_mean_s11_db: -6.2, accepted_power_worst_pct: 75.5, accepted_power_mean_pct: 76, ops: [], parts: [{ ref: "L1", was: "2.2 nH (fitted)", now: "0.82 pF", position: "series" }] };
  const h = headlineOf({ headline: { best, base: { band_worst_s11_db: -0.5, accepted_power_worst_pct: 10.9 }, gain_db: 5.6, finding: "F-009", target_db: -10 } });
  assert.equal(h.gainDb, 5.6);
  assert.equal(h.partsText, "L1 2.2 nH to 0.82 pF (series)");
  assert.equal(h.targetDb, -10);
});

test("a confirmation that says improved_short_of_target is a headline too", () => {
  const conf = { outcome: "improved_short_of_target", best: { band_worst_s11_db: -4, parts: [] , ops: [{ op: "adjust_antenna_length", delta_mm: -4 }] }, base: { band_worst_s11_db: -0.5 }, best_gain_db: 3.5 };
  const h = headlineOf({ investigations: [{ code: "I1", outcome: { finding: "F-009" }, confirmation: null, refine_rounds: [{ round: 1, confirmation: conf }] }] });
  assert.equal(h.investigation, "I1");
  assert.equal(h.gainDb, 3.5);
  assert.equal(h.partsText, "shorten the antenna by 4 mm");
  assert.equal(changeText(conf.best), "shorten the antenna by 4 mm");
});

test("accepted power is 100 x (1 - |S11|^2): -0.5 dB is about 11 %, -6.1 dB about 75 %", () => {
  assert.ok(Math.abs(acceptedPct(-0.5) - 10.9) < 0.1);
  assert.ok(Math.abs(acceptedPct(-6.1) - 75.5) < 0.1);
  assert.equal(acceptedPct(0), 0);
  assert.equal(acceptedPct(NaN), null);
  assert.equal(acceptedPct(3), 0, "S11 above 0 dB accepts nothing");
});

test("accepted power over a band comes from the real curve only", () => {
  const c = JSON.parse(fs.readFileSync(new URL("../public/fixtures/nrf-sim/s11_as_built.json", import.meta.url)));
  const curve = { f_ghz: c.freq_hz.map((f) => f / 1e9), s11_db: c.s11_db };
  const a = acceptedFromCurve(curve, [2.4, 2.4835]);
  assert.ok(a.worst < a.mean + 1e-9 && a.worst > 5 && a.worst < 20, `worst ${a.worst}`);
  assert.equal(acceptedFromCurve(curve, [10, 11]), null, "no point in the band: nothing is invented");
});

test("measurements carry accepted power with prefixes; as built wins as the headline", () => {
  const m = (v) => ({ value: v, unit: "%" });
  const a = acceptedOf({ as_built_band_worst_accepted_power_pct: m(11), as_built_band_mean_accepted_power_pct: m(12), antenna_only_band_worst_accepted_power_pct: m(18) });
  assert.deepEqual(a.asBuilt, { worst: 11, mean: 12 });
  assert.deepEqual(a.antenna, { worst: 18, mean: null });
  assert.equal(a.headline, a.asBuilt);
  assert.equal(acceptedOf({}).headline, null);
  assert.deepEqual(acceptedOf({ band_worst_accepted_power_pct: m(40) }).asBuilt, { worst: 40, mean: null });
});

test("fixed dips: frequencies that stayed put, matched within 12 MHz", () => {
  const fixed = fixedDipsMhz([{ measurements: { fixed_dip_1: { value: 2074.3, unit: "MHz" } } }, { measurements: { fixed_dip_1: { value: 2074.8, unit: "MHz" }, s11_dip_1: { value: 1, unit: "MHz" } } }]);
  assert.deepEqual(fixed, [2074.3], "the same dip seen twice is one");
  assert.equal(isFixedDip(2.0743, fixed), true);
  assert.equal(isFixedDip(2.2, fixed), false);
});

test("same geometry is the part of the job hash before the first dash", () => {
  assert.equal(sameGeometry("abc123", "abc123-1a2b3c4d"), true);
  assert.equal(sameGeometry("abc123", "def456-1a2b3c4d"), false);
  assert.equal(sameGeometry(null, "abc"), null);
  assert.equal(fmtPct(10.9), "11");
  assert.equal(fmtPct(4.44), "4.4");
});

test("a recheck's headline is the version change, and its own partial win rides inside it", () => {
  const vc = { kind: "version_change", from_version: 1, to_version: 2, from: { band_worst_s11_db: -0.5 }, to: { band_worst_s11_db: -5.78 }, gain_db: 5.28, direction: "improved", held: true, partial_win: null };
  assert.equal(versionChangeOf({ headline: vc }).gain_db, 5.28);
  assert.equal(versionChangeOf({ headline: vc, version_change: vc }).to_version, 2);
  assert.equal(headlineOf({ headline: vc }), null, "no partial win inside");
  const best = { band_worst_s11_db: -6.1, accepted_power_worst_pct: 75.5, ops: [], parts: [] };
  const h = headlineOf({ headline: { ...vc, partial_win: { best, base: { band_worst_s11_db: -5.78 }, gain_db: 3.1 } } });
  assert.equal(h.gainDb, 3.1);
  assert.equal(versionChangeOf({ headline: null }), null, "a snapshot from before the field existed has none, and none is invented");
  assert.equal(versionChangeOf(null), null);
});

test("each finding code has its change state from the version changes", () => {
  const out = changeByCode({ version_changes: { still_present: [{ code: "F-001", change: "still_present" }], changed: [{ code: "F-015", change: "improved", changes: ["worst in-band S11 as built -0.5 dB -> -5.78 dB"] }], new: [{ code: "F-020" }], fixed: [{ code: "F-002" }] } });
  assert.deepEqual(Object.keys(out).sort(), ["F-001", "F-015", "F-020"]);
  assert.equal(out["F-015"].change, "improved");
  assert.equal(out["F-020"].change, "new");
  assert.deepEqual(changeByCode(null), {});
});
