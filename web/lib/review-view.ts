import type { Finding, Mark } from "./types";
import type { FindingMeta } from "../components/board2d/Board2D";

type Coded = string | { id: string; code?: string };
const codeOf = (f: Coded) => (typeof f === "string" ? f : f.code ?? f.id);
/** Finding number from its display code (F-007 -> 7). Falls back to digits in the id. */
export const findingNumber = (f: Coded) => parseInt(codeOf(f).replace(/\D+/g, ""), 10) || 0;
export const findingLabel = (f: Coded) => {
  const c = codeOf(f);
  return /^F-\d+$/i.test(c) ? c.toUpperCase() : `F-${String(findingNumber(f)).padStart(3, "0")}`;
};

export function markValue(marks: Mark[]): string | undefined {
  const m = marks.find((x) => x.value);
  return m?.value ? `${m.value.value} ${m.value.unit}` : undefined;
}

export function buildMeta(findings: Finding[]): Record<string, FindingMeta> {
  const out: Record<string, FindingMeta> = {};
  for (const f of findings) out[f.id] = { n: findingNumber(f), title: f.title, severity: f.severity, value: markValue(f.marks ?? []) };
  return out;
}

export const fmtClock = (s: number) => {
  const m = Math.floor(Math.max(0, s) / 60);
  const r = Math.floor(Math.max(0, s) % 60);
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
};

const NAMES: Record<string, string> = { resonance: "deepest dip", f_res_hz: "deepest dip", s11_min: "S11", s11_min_db: "S11", bandwidth_hz: "BW", dmax_dbi: "directivity", efficiency: "eff", band_worst_s11_db: "band worst" };
/** Display rows for a simulation's measurements, in friendly units. Only what the solver reported. */
export function measurementLine(m: Record<string, { value: number; unit: string }>) {
  return Object.entries(m).slice(0, 4).map(([k, x]) => {
    if (k === "f_res_hz" || x.unit === "Hz") return { k: NAMES[k] ?? k.replace(/_/g, " "), v: (x.value / (x.value >= 1e9 ? 1e9 : 1e6)).toFixed(x.value >= 1e9 ? 3 : 1), u: x.value >= 1e9 ? "GHz" : "MHz" };
    return { k: NAMES[k] ?? k.replace(/_/g, " "), v: Number.isInteger(x.value) ? String(x.value) : x.value.toFixed(1), u: x.unit };
  });
}
