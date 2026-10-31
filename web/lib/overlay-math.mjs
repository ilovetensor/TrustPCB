// Pure helpers for simulation overlays: a float32 raster over the board in board millimetres (x right, y down).
// No browser, no React: unit tested. The cell (i, j) covers x0 + i*dx to x0 + (i+1)*dx and y0 + j*dy to y0 + (j+1)*dy.
// A cell with no value (no copper, off the board) is NaN.

/** The area of the board an overlay covers, as [x0, y0, x1, y1]. */
export const overlayBox = (ov) => [ov.x0, ov.y0, ov.x0 + ov.nx * ov.dx, ov.y0 + ov.ny * ov.dy];

/** The cell index of a board point, or -1 when it lies outside the raster. */
export function cellAt(ov, x, y) {
  const i = Math.floor((x - ov.x0) / ov.dx), j = Math.floor((y - ov.y0) / ov.dy);
  if (!(i >= 0 && i < ov.nx && j >= 0 && j < ov.ny)) return -1;
  return j * ov.nx + i;
}

/** The value under a board point, or null where the raster has no value (outside it, or NaN). */
export function sampleAt(ov, data, x, y) {
  const k = cellAt(ov, x, y);
  if (k < 0) return null;
  const v = data[k];
  return Number.isFinite(v) ? v : null;
}

/** Minimum, maximum, mean and count of the cells that have a value, and where the largest one is (board mm). */
export function statsOf(ov, data) {
  let min = Infinity, max = -Infinity, sum = 0, n = 0, at = -1;
  for (let k = 0; k < data.length; k++) {
    const v = data[k];
    if (!Number.isFinite(v)) continue;
    if (v < min) min = v;
    if (v > max) { max = v; at = k; }
    sum += v; n++;
  }
  if (!n) return { min: null, max: null, mean: null, count: 0, maxAt: null };
  const i = at % ov.nx, j = Math.floor(at / ov.nx);
  return { min, max, mean: sum / n, count: n, maxAt: [ov.x0 + (i + 0.5) * ov.dx, ov.y0 + (j + 0.5) * ov.dy] };
}

/** True when two overlays share one grid, so one can be subtracted from the other. */
export const gridsMatch = (a, b) => a.nx === b.nx && a.ny === b.ny && Math.abs(a.x0 - b.x0) < 1e-6 && Math.abs(a.y0 - b.y0) < 1e-6 && Math.abs(a.dx - b.dx) < 1e-9 && Math.abs(a.dy - b.dy) < 1e-9;

/**
 * after minus before as a new overlay on the same grid, drawn on a diverging scale around zero. Null when the grids differ.
 * A cell that has a value in only one of the two runs has no change to show (new copper, say), so it is left empty.
 * The scale ends at the 99.5th percentile of the changes, so a few sharp cells do not wash out the rest; the readout still gives the true value.
 */
export function diffOverlay(before, dBefore, after, dAfter) {
  if (!gridsMatch(before, after) || dBefore.length !== dAfter.length) return null;
  const out = new Float32Array(dAfter.length);
  const mags = [];
  for (let k = 0; k < out.length; k++) {
    const a = dAfter[k], b = dBefore[k];
    const v = Number.isFinite(a) && Number.isFinite(b) ? a - b : NaN;
    out[k] = v;
    if (Number.isFinite(v)) mags.push(Math.abs(v));
  }
  mags.sort((x, y) => x - y);
  const lim = mags.length ? mags[Math.min(mags.length - 1, Math.floor(0.995 * mags.length))] || mags[mags.length - 1] : 0;
  const span = lim || 1;
  return { ov: { ...after, id: `${after.id}:change`, label: `${after.label}, change`, vmin: -span, vmax: span, scale: "diverging" }, data: out };
}

/** Rounds a span to a step of 1, 2 or 5 times a power of ten. */
function niceStep(span, n) {
  const raw = span / Math.max(1, n);
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

/** Tick values for a legend between vmin and vmax: round numbers, at most about n of them. */
export function legendTicks(vmin, vmax, n = 4) {
  if (!(vmax > vmin)) return [vmin];
  const step = niceStep(vmax - vmin, n);
  const out = [];
  for (let v = Math.ceil(vmin / step - 1e-9) * step; v <= vmax + step * 1e-9; v += step) out.push(+v.toPrecision(12));
  return out;
}

/** A readable number: three significant digits, no trailing noise, a true minus sign. */
export function fmtNum(v, digits = 3) {
  if (v == null || !Number.isFinite(v)) return "n/a";
  if (v === 0) return "0";
  const a = Math.abs(v);
  const s = a >= 1000 ? String(Math.round(a)) : a >= 100 ? a.toFixed(0) : a >= 10 ? a.toFixed(Math.max(0, digits - 2)) : a >= 1 ? a.toFixed(Math.max(0, digits - 1)) : a.toPrecision(digits);
  const t = s.includes(".") && !s.includes("e") ? s.replace(/0+$/, "").replace(/\.$/, "") : s;
  return (v < 0 ? "−" : "") + t;
}

/** The value with its unit: "3.4 mV". A percent sign and a degree sign sit tight to the number. */
export const fmtVal = (v, unit) => (v == null || !Number.isFinite(v) ? "n/a" : `${fmtNum(v)}${unit ? (unit === "%" || unit.startsWith("°") ? "" : " ") + unit : ""}`);

/** Position of a value on the colour scale, 0 to 1. A diverging scale puts zero in the middle. */
export function scalePos(ov, v) {
  if (ov.scale === "diverging") {
    const lim = Math.max(Math.abs(ov.vmin), Math.abs(ov.vmax)) || 1;
    return Math.min(1, Math.max(0, 0.5 + 0.5 * (v / lim)));
  }
  const span = ov.vmax - ov.vmin;
  return span > 0 ? Math.min(1, Math.max(0, (v - ov.vmin) / span)) : 0;
}

/**
 * RGBA pixels for a raster: each value through a 256-entry lookup table (RGBA, from buildLut), transparent where there is no value.
 * `alpha` is the opacity of cells that have one, 0 to 1.
 */
export function colorize(ov, data, lut, alpha = 1) {
  const out = new Uint8ClampedArray(ov.nx * ov.ny * 4);
  const a = Math.round(255 * alpha);
  for (let k = 0; k < data.length; k++) {
    const v = data[k];
    if (!Number.isFinite(v)) continue;
    const idx = Math.round(scalePos(ov, v) * 255) * 4;
    out[k * 4] = lut[idx]; out[k * 4 + 1] = lut[idx + 1]; out[k * 4 + 2] = lut[idx + 2]; out[k * 4 + 3] = a;
  }
  return out;
}

/**
 * Groups a run's overlays into the maps a viewer can switch between: one map per label and unit, with one raster per copper layer
 * (or one for the whole board). Order follows the first appearance. Layers are sorted top copper first.
 */
export function mapsOf(overlays) {
  const maps = new Map();
  for (const ov of overlays ?? []) {
    const key = `${ov.label}\u0000${ov.unit}`;
    if (!maps.has(key)) maps.set(key, { key, label: ov.label, unit: ov.unit, scale: ov.scale ?? "sequential", layers: [] });
    maps.get(key).layers.push(ov);
  }
  const rank = (l) => (l === "board" ? -2 : l === "F.Cu" ? -1 : l === "B.Cu" ? 99 : l.startsWith("In") ? parseInt(l.replace(/\D/g, ""), 10) || 50 : 60);
  for (const m of maps.values()) m.layers.sort((a, b) => rank(a.layer) - rank(b.layer));
  return [...maps.values()];
}

/** A shared colour range for two runs of one map, so before and after are drawn on the same scale. */
export function sharedRange(a, b) {
  if (!a || !b) return null;
  if (a.scale === "diverging") { const lim = Math.max(Math.abs(a.vmin), Math.abs(a.vmax), Math.abs(b.vmin), Math.abs(b.vmax)); return { vmin: -lim, vmax: lim }; }
  return { vmin: Math.min(a.vmin, b.vmin), vmax: Math.max(a.vmax, b.vmax) };
}

/** Decodes a float32 little-endian raster; null when the byte length does not match the grid. */
export function decodeRaster(buffer, ov) {
  if (!buffer || buffer.byteLength !== ov.nx * ov.ny * 4) return null;
  const view = new DataView(buffer);
  const out = new Float32Array(ov.nx * ov.ny);
  for (let k = 0; k < out.length; k++) out[k] = view.getFloat32(k * 4, true);
  return out;
}

/**
 * The labels under a legend: the ends of the scale and the round numbers between them that sit clear of the ends. A diverging scale is labelled at
 * both ends and at zero. Each label has its position, 0 to 1.
 */
export function legendLabels(ov, n = 4) {
  const lo = ov.vmin, hi = ov.vmax;
  if (ov.scale === "diverging") {
    const lim = Math.max(Math.abs(lo), Math.abs(hi));
    return [{ v: -lim, at: 0 }, { v: 0, at: 0.5 }, { v: lim, at: 1 }];
  }
  const inner = legendTicks(lo, hi, n).map((v) => ({ v, at: scalePos(ov, v) })).filter((t) => t.at > 0.14 && t.at < 0.86);
  return [{ v: lo, at: 0 }, ...inner, { v: hi, at: 1 }];
}
