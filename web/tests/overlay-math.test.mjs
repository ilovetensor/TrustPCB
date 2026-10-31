import { test } from "node:test";
import assert from "node:assert/strict";
import { cellAt, colorize, decodeRaster, diffOverlay, fmtNum, fmtVal, legendLabels, legendTicks, mapsOf, sampleAt, scalePos, sharedRange, statsOf } from "../lib/overlay-math.mjs";
import { buildLut } from "../lib/ramp.mjs";
import { HEAT_STOPS, lutFor } from "../lib/sim-ramps.mjs";

const ov = (extra = {}) => ({ id: "a", label: "Voltage drop", unit: "mV", layer: "F.Cu", x0: 10, y0: 20, dx: 0.5, dy: 0.5, nx: 4, ny: 2, vmin: 0, vmax: 8, scale: "sequential", ...extra });
const data = Float32Array.from([1, 2, NaN, 4, 5, 6, 7, NaN]);

test("a board point finds its cell, and outside the raster finds none", () => {
  assert.equal(cellAt(ov(), 10.1, 20.1), 0);
  assert.equal(cellAt(ov(), 12.1, 20.1), -1);
  assert.equal(cellAt(ov(), 11.2, 20.6), 6);
  assert.equal(cellAt(ov(), 9.9, 20.1), -1);
  assert.equal(cellAt(ov(), 10.1, 21.1), -1);
});
test("sampling returns the value, and null for NaN and for outside", () => {
  assert.equal(sampleAt(ov(), data, 10.6, 20.2), 2);
  assert.equal(sampleAt(ov(), data, 11.1, 20.2), null);
  assert.equal(sampleAt(ov(), data, 0, 0), null);
});
test("stats ignore NaN and report where the maximum is", () => {
  const s = statsOf(ov(), data);
  assert.equal(s.count, 6);
  assert.equal(s.min, 1); assert.equal(s.max, 7);
  assert.ok(Math.abs(s.mean - 25 / 6) < 1e-9);
  assert.deepEqual(s.maxAt, [11.25, 20.75]);
  assert.equal(statsOf(ov(), new Float32Array(8).fill(NaN)).max, null);
});
test("a difference is after minus before on one grid, symmetric around zero, and empty where only one run has copper", () => {
  const b = Float32Array.from([4, 4, NaN, 4, 4, 4, 4, NaN]);
  const a = Float32Array.from([3, 6, NaN, 4, 4, 4, 1, 9]);
  const d = diffOverlay(ov(), b, ov({ id: "b" }), a);
  assert.deepEqual([...d.data.slice(0, 2)], [-1, 2]);
  assert.ok(Number.isNaN(d.data[2]));
  assert.equal(d.data[6], -3);
  assert.ok(Number.isNaN(d.data[7]), "copper that exists only after has no change to show");
  assert.equal(d.ov.scale, "diverging");
  assert.equal(d.ov.vmax, 3); assert.equal(d.ov.vmin, -3);
  assert.equal(diffOverlay(ov(), b, ov({ nx: 5 }), a), null);
  // a run with no change at all still has a usable scale
  const same = diffOverlay(ov(), b, ov({ id: "b" }), b);
  assert.ok(same.ov.vmax > 0 && same.ov.vmin === -same.ov.vmax);
});
test("a few sharp cells do not set the colour range of a change map", () => {
  const n = 1000, b = new Float32Array(n).fill(1), a = new Float32Array(n).fill(1.1);
  a[0] = 50;
  const g = { id: "x", label: "L", unit: "u", layer: "F.Cu", x0: 0, y0: 0, dx: 1, dy: 1, nx: n, ny: 1, vmin: 0, vmax: 1 };
  const d = diffOverlay(g, b, g, a);
  assert.ok(d.ov.vmax < 1, `${d.ov.vmax}`);
  assert.ok(d.data[0] > 40, "the sharp cell keeps its true value for the readout");
});
test("legend labels: both ends and round numbers clear of them, zero in the middle of a diverging scale", () => {
  const l = legendLabels({ vmin: 25, vmax: 37, scale: "sequential" });
  assert.equal(l[0].v, 25); assert.equal(l[l.length - 1].v, 37);
  assert.ok(l.slice(1, -1).every((t) => t.at > 0.14 && t.at < 0.86));
  assert.deepEqual(legendLabels({ vmin: -3, vmax: 2, scale: "diverging" }), [{ v: -3, at: 0 }, { v: 0, at: 0.5 }, { v: 3, at: 1 }]);
});
test("legend ticks are round numbers inside the range", () => {
  assert.deepEqual(legendTicks(0, 8, 4), [0, 2, 4, 6, 8]);
  assert.deepEqual(legendTicks(25, 37, 4), [25, 30, 35]);
  assert.ok(legendTicks(-50, 50, 4).includes(0));
  assert.ok(legendTicks(0.26, 0.31, 4).every((v) => v >= 0.26 && v <= 0.31));
  assert.deepEqual(legendTicks(3, 3), [3]);
});
test("numbers read well: three digits, true minus, units tight where they should be", () => {
  assert.equal(fmtNum(1.184), "1.18");
  assert.equal(fmtNum(0.444), "0.444");
  assert.equal(fmtNum(85.98), "86");
  assert.equal(fmtNum(-49.04), "−49");
  assert.equal(fmtNum(1234.5), "1235");
  assert.equal(fmtNum(0), "0");
  assert.equal(fmtNum(NaN), "n/a");
  assert.equal(fmtVal(3.4, "mV"), "3.4 mV");
  assert.equal(fmtVal(12, "%"), "12%");
  assert.equal(fmtVal(37.2, "°C"), "37.2°C");
});
test("scale position: sequential from vmin to vmax, diverging with zero in the middle", () => {
  assert.equal(scalePos(ov(), 0), 0); assert.equal(scalePos(ov(), 4), 0.5); assert.equal(scalePos(ov(), 99), 1);
  const dv = { vmin: -10, vmax: 20, scale: "diverging" };
  assert.equal(scalePos(dv, 0), 0.5); assert.equal(scalePos(dv, 20), 1); assert.equal(scalePos(dv, -20), 0);
});
test("colours: values map through the table, NaN is transparent", () => {
  const lut = buildLut(HEAT_STOPS);
  const px = colorize(ov(), data, lut, 0.9);
  assert.equal(px.length, 4 * 8);
  assert.equal(px[2 * 4 + 3], 0);
  assert.equal(px[0 * 4 + 3], Math.round(255 * 0.9));
  const lo = colorize(ov(), Float32Array.from([0, 0, 0, 0, 0, 0, 0, 8]), lut, 1);
  assert.deepEqual([...lo.slice(0, 3)], [...lut.slice(0, 3)]);
  assert.deepEqual([...lo.slice(28, 31)], [...lut.slice(255 * 4, 255 * 4 + 3)]);
  assert.equal(lutFor(ov()), lutFor(ov()));
  assert.notEqual(lutFor({ scale: "diverging" }), lutFor({ scale: "sequential" }));
});
test("maps group an overlay's layers and put the top copper first", () => {
  const maps = mapsOf([ov({ id: "d_B", layer: "B.Cu" }), ov({ id: "j_F", label: "Current density", unit: "A/mm²", layer: "F.Cu" }), ov({ id: "d_F", layer: "F.Cu" })]);
  assert.equal(maps.length, 2);
  assert.equal(maps[0].label, "Voltage drop");
  assert.deepEqual(maps[0].layers.map((l) => l.layer), ["F.Cu", "B.Cu"]);
  assert.deepEqual(mapsOf([ov({ layer: "board" })])[0].layers.map((l) => l.layer), ["board"]);
  assert.deepEqual(mapsOf(null), []);
});
test("before and after share one colour range", () => {
  assert.deepEqual(sharedRange(ov({ vmin: 1, vmax: 5 }), ov({ vmin: 0, vmax: 4 })), { vmin: 0, vmax: 5 });
  assert.deepEqual(sharedRange(ov({ scale: "diverging", vmin: -3, vmax: 3 }), ov({ scale: "diverging", vmin: -5, vmax: 4 })), { vmin: -5, vmax: 5 });
  assert.equal(sharedRange(null, ov()), null);
});
test("a raster decodes little endian float32, and a wrong size is refused", () => {
  const buf = new ArrayBuffer(8 * 4);
  const dv = new DataView(buf);
  [1, 2, NaN, 4, 5, 6, 7, NaN].forEach((v, i) => dv.setFloat32(i * 4, v, true));
  const out = decodeRaster(buf, ov());
  assert.equal(out[3], 4); assert.ok(Number.isNaN(out[2]));
  assert.equal(decodeRaster(new ArrayBuffer(12), ov()), null);
  assert.equal(decodeRaster(null, ov()), null);
});
