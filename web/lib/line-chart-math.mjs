// Range and clipping for impedance curves. A TDR of a line with an open end ramps to kilo-ohms; the lines worth reading
// are a few tens of ohms, so the axis follows the line's own section values and the open end is marked instead of drawn to the top.

/** The y range of an impedance chart from the line's section values (`values`) and its target: from 0 up to 1.4 times the larger of the two, rounded up to 25 ohm,
 * never below 100 and never above 200. With no numbers it is 0 to 150. */
export function impedanceRange(values, target, tolPct = 0) {
  const nums = (values ?? []).filter((v) => Number.isFinite(v) && v > 0);
  const top = Math.max(0, ...nums, Number.isFinite(target) ? target * (1 + (tolPct || 0) / 100) : 0);
  if (!(top > 0)) return [0, 150];
  const hi = Math.ceil((top * 1.4) / 25) * 25;
  return [0, Math.min(200, Math.max(100, hi))];
}

/** The curve with every value held inside [lo, hi], and the x where it first leaves the range (the open end) or null.
 * `ys` keeps the real values for the tooltip; `shown` is what is drawn. */
export function clipCurve(xs, ys, lo, hi) {
  const shown = ys.map((y) => (Number.isFinite(y) ? Math.min(hi, Math.max(lo, y)) : y));
  let exitX = null, exitIndex = -1;
  for (let i = 0; i < ys.length; i++) if (Number.isFinite(ys[i]) && ys[i] > hi) { exitIndex = i; break; }
  if (exitIndex >= 0) {
    if (exitIndex > 0 && Number.isFinite(ys[exitIndex - 1])) {
      const k = (hi - ys[exitIndex - 1]) / (ys[exitIndex] - ys[exitIndex - 1] || 1);
      exitX = xs[exitIndex - 1] + (xs[exitIndex] - xs[exitIndex - 1]) * Math.min(1, Math.max(0, k));
    } else exitX = xs[exitIndex];
  }
  return { shown, exitX, peak: ys.reduce((m, y) => (Number.isFinite(y) && y > m ? y : m), -Infinity) };
}
