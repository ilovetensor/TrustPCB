import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import zlib from "node:zlib";
import { decodePulseFrames } from "../lib/pulse-codec.mjs";

const ab = (b) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);

test("a packed pulse decodes to the stored frames, within one step of the square-root byte", async () => {
  const shape = [3, 4, 5];
  const values = Float32Array.from({ length: 60 }, (_, i) => (i % 17) / 16 * ((i % 3) + 1) / 3);
  const q = Uint8Array.from(values, (v) => Math.round(255 * Math.sqrt(v)));
  const frames = await decodePulseFrames(ab(zlib.gzipSync(q)), { shape, dtype: "uint8", encoding: "gzip" });
  assert.equal(frames.length, 3);
  assert.equal(frames[0].length, 20);
  for (let i = 0; i < 60; i++) assert.ok(Math.abs(Math.sqrt(frames[Math.floor(i / 20)][i % 20]) - Math.sqrt(values[i])) <= 0.5 / 255 + 1e-6, `value ${i}`);
});

test("uncompressed uint8 and the solver's own float32 files still decode", async () => {
  const q = Uint8Array.from({ length: 12 }, (_, i) => i * 20);
  const u8 = await decodePulseFrames(ab(q), { shape: [2, 2, 3], dtype: "uint8" });
  assert.equal(u8[1][2], Math.fround((160 / 255) ** 2)); // byte 8 of 12
  const f = Float32Array.from({ length: 12 }, (_, i) => i / 12);
  const f32 = await decodePulseFrames(ab(new Uint8Array(f.buffer)), { shape: [2, 2, 3] });
  assert.deepEqual([...f32[1]], [...f.subarray(6)]);
});

test("bytes that do not fit the declared shape give null", async () => {
  assert.equal(await decodePulseFrames(ab(zlib.gzipSync(new Uint8Array(11))), { shape: [2, 2, 3], dtype: "uint8", encoding: "gzip" }), null);
  assert.equal(await decodePulseFrames(new ArrayBuffer(7), { shape: [2, 2, 3] }), null);
});

test("the bundled sample pulses are packed, complete and in range", async () => {
  for (const name of ["nrf-sim", "patch-sim"]) {
    const dir = new URL(`../public/fixtures/${name}/`, import.meta.url);
    const meta = JSON.parse(fs.readFileSync(new URL("pulse.json", dir)));
    const bytes = fs.readFileSync(new URL("pulse.bin", dir));
    const [fr, rows, cols] = meta.shape;
    assert.equal(meta.times_s.length, fr, name);
    const frames = await decodePulseFrames(ab(bytes), meta);
    assert.equal(frames.length, fr, name);
    let max = 0;
    for (const f of frames) { assert.equal(f.length, rows * cols); for (let i = 0; i < f.length; i += 7) max = Math.max(max, f[i]); }
    assert.ok(max > 0.5 && max <= 1, `${name} peak ${max}`);
    assert.ok(bytes.length < 1_000_000, `${name} is ${bytes.length} bytes`);
  }
});
