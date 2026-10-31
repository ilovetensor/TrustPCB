import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { buildGoal, depthNote, parseBand } from "../lib/goal.mjs";

test("a board with a radio sends the band and the S11 target", () => {
  const g = buildGoal({ purpose: " Sensor ", band: "2.4 to 2.4835 GHz", target: "-10", focus: "", wireless: true });
  assert.deepEqual(g, { purpose: "Sensor", band_hz: [2.4e9, 2.4835e9], target_s11_db: -10 });
});

test("a board with no radio never sends a band or a target, even when the form holds the default", () => {
  const g = buildGoal({ purpose: "USB LDO", band: "2.4 to 2.4835 GHz", target: "-10", focus: "power path", wireless: false });
  assert.deepEqual(g, { purpose: "USB LDO", focus: "power path" });
  assert.ok(!("band_hz" in g) && !("target_s11_db" in g));
});

test("before detection the goal keeps what was typed", () => {
  assert.equal(buildGoal({ target: "-12" }).target_s11_db, -12);
});

test("parseBand reads GHz and MHz and refuses a reversed range", () => {
  assert.deepEqual(parseBand("868 to 928 MHz"), [868e6, 928e6]);
  assert.equal(parseBand("3 to 2 GHz"), undefined);
  assert.equal(parseBand(""), undefined);
});

test("depth notes say nothing is simulated when there is no radio, and keep the time limit", () => {
  const std = "Up to 60 min. One investigation with simulation when the RF lab is installed.";
  assert.equal(depthNote(std, "standard", false), "Up to 60 min. No radio found, so nothing is simulated.");
  assert.equal(depthNote(std, "standard", true), std);
  assert.equal(depthNote(std, "standard", undefined), std);
  const quick = "About 6 min. KiCad checks and an open review. No simulation.";
  assert.equal(depthNote(quick, "quick", false), quick);
});

test("the upload page hides the RF fields from the detection result and builds its goal in one place", () => {
  const src = readFileSync(new URL("../components/pages/Upload.tsx", import.meta.url), "utf8");
  assert.match(src, /const noRadio = !!d && !d\.wireless;/);
  assert.match(src, /noRadio\s*\?[\s\S]*data-testid="no-radio-note"[\s\S]*htmlFor="g2"[\s\S]*htmlFor="g4"/);
  assert.match(src, /buildGoal\(\{ purpose, band, target, focus, wireless: version\.detection\?\.wireless \}\)/);
});
