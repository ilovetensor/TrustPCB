import type { Finding, Mark } from "./types";
import type { FindingMeta } from "../components/board2d/Board2D";

export const findingNumber = (id: string) => parseInt(id.replace(/\D+/g, ""), 10) || 0;
export const findingLabel = (id: string) => `F-${String(findingNumber(id)).padStart(3, "0")}`;

export function markValue(marks: Mark[]): string | undefined {
  const m = marks.find((x) => x.value);
  return m?.value ? `${m.value.value} ${m.value.unit}` : undefined;
}

export function buildMeta(findings: Finding[]): Record<string, FindingMeta> {
  const out: Record<string, FindingMeta> = {};
  for (const f of findings) out[f.id] = { n: findingNumber(f.id), title: f.title, severity: f.severity, value: markValue(f.marks) };
  return out;
}

export const fmtClock = (s: number) => {
  const m = Math.floor(Math.max(0, s) / 60);
  const r = Math.floor(Math.max(0, s) % 60);
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
};
