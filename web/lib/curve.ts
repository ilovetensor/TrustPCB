// A solver curve as the UI draws it. The backend's `curve` field is optional; anything not recognised is not drawn.
export interface Curve { f_ghz: number[]; s11_db: number[] }

/** Accepts {f_ghz, s11_db} or {freq_hz, s11_db}. Returns null for anything else, never a made-up curve. */
export function normaliseCurve(c: unknown): Curve | null {
  if (!c || typeof c !== "object") return null;
  const o = c as Record<string, unknown>;
  const s11 = o.s11_db;
  if (!Array.isArray(s11)) return null;
  if (Array.isArray(o.f_ghz) && o.f_ghz.length === s11.length) return { f_ghz: o.f_ghz as number[], s11_db: s11 as number[] };
  if (Array.isArray(o.freq_hz) && o.freq_hz.length === s11.length) return { f_ghz: (o.freq_hz as number[]).map((f) => f / 1e9), s11_db: s11 as number[] };
  return null;
}
