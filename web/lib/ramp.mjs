// Colour ramps for simulation graphics. Perceptually ordered by luminance:
// blue-violet (low / negative) -> magenta -> vermilion -> amber-white (high / positive).

export const FIELD_STOPS = [
  [0.0, [7, 4, 31]],
  [0.16, [37, 12, 96]],
  [0.34, [98, 22, 150]],
  [0.52, [186, 36, 128]],
  [0.68, [240, 78, 56]],
  [0.84, [255, 166, 66]],
  [1.0, [255, 244, 214]],
];

// Signed ramp for instantaneous field: cool for negative phase, warm for positive.
export const SIGNED_STOPS = [
  [0.0, [124, 108, 255]],
  [0.18, [64, 52, 206]],
  [0.36, [26, 18, 104]],
  [0.5, [6, 4, 14]],
  [0.62, [150, 28, 130]],
  [0.78, [242, 84, 48]],
  [0.9, [255, 176, 72]],
  [1.0, [255, 246, 220]],
];

export function sample(stops, t) {
  const x = Math.min(1, Math.max(0, t));
  for (let i = 1; i < stops.length; i++) {
    if (x <= stops[i][0]) {
      const [t0, c0] = stops[i - 1];
      const [t1, c1] = stops[i];
      const k = (x - t0) / (t1 - t0 || 1);
      return [c0[0] + (c1[0] - c0[0]) * k, c0[1] + (c1[1] - c0[1]) * k, c0[2] + (c1[2] - c0[2]) * k];
    }
  }
  return stops[stops.length - 1][1];
}

/** 256-entry RGBA lookup table (Uint8) for textures and canvas. */
export function buildLut(stops, n = 256) {
  const out = new Uint8Array(n * 4);
  for (let i = 0; i < n; i++) {
    const [r, g, b] = sample(stops, i / (n - 1));
    out[i * 4] = Math.round(r);
    out[i * 4 + 1] = Math.round(g);
    out[i * 4 + 2] = Math.round(b);
    out[i * 4 + 3] = 255;
  }
  return out;
}

export function cssGradient(stops, dir = "to right") {
  return `linear-gradient(${dir}, ${stops.map(([t, c]) => `rgb(${c.join(",")}) ${Math.round(t * 100)}%`).join(", ")})`;
}

export function rgb(stops, t) {
  const [r, g, b] = sample(stops, t);
  return `rgb(${Math.round(r)},${Math.round(g)},${Math.round(b)})`;
}
