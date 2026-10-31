import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { buildLut, SIGNED_STOPS, FIELD_STOPS, sample } from "../lib/ramp.mjs";

const dir = new URL("../public/fixtures/patch-sim/", import.meta.url);
const read = (f) => JSON.parse(fs.readFileSync(new URL(f, dir)));

test("real solver sidecars match their binary sizes", () => {
  const field = read("field.json");
  for (const k of ["xy", "xz"]) {
    const n = field[k].shape.reduce((a, b) => a * b, 1) * 4;
    assert.equal(fs.statSync(new URL(field[k].file, dir)).size, n, k);
  }
  const pulse = read("pulse.json");
  // the bundled pulse is packed (uint8, gzip): the file is far smaller than float32, and decodes to exactly shape values
  assert.equal(pulse.dtype, "uint8");
  assert.equal(pulse.encoding, "gzip");
  assert.ok(fs.statSync(new URL("pulse.bin", dir)).size < pulse.shape.reduce((a, b) => a * b, 1));
  const cur = read("current.json");
  assert.equal(fs.statSync(new URL("current.bin", dir)).size, cur.shape.reduce((a, b) => a * b, 1) * 4);
});

test("s11 and zin share a frequency axis; pattern is 37 x 73", () => {
  const s11 = read("s11.json"), zin = read("zin.json"), pat = read("pattern.json");
  assert.equal(s11.freq_hz.length, s11.s11_db.length);
  assert.equal(zin.freq_hz.length, zin.z_re.length);
  assert.equal(pat.directivity_dbi.length, 37);
  assert.equal(pat.directivity_dbi[0].length, 73);
});

test("field extents are symmetric about the patch centre in scenario mm", () => {
  const f = read("field.json").xy;
  assert.ok(Math.abs(f.u_mm[0] + f.u_mm[1]) < 1e-6);
});

test("colour ramps are monotone in luminance and span dark to light", () => {
  const lum = ([r, g, b]) => 0.2126 * r + 0.7152 * g + 0.0722 * b;
  let prev = -1;
  for (let i = 0; i <= 20; i++) { const l = lum(sample(FIELD_STOPS, i / 20)); assert.ok(l >= prev - 1e-6); prev = l; }
  assert.equal(buildLut(SIGNED_STOPS).length, 256 * 4);
  assert.ok(lum(sample(SIGNED_STOPS, 0.5)) < lum(sample(SIGNED_STOPS, 0)));
});
