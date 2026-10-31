"use client";
import { loadCurves, loadField, loadMeta, loadPattern, loadSummary, type Curves, type Fields, type SimJob, type SimMeta, type SimSource, type Summary } from "./sims";
import type { PatternData } from "./rf-data";
import { useAsync } from "./use-async";

export interface RunData { curves: Curves | null; summary: Summary | null; meta: SimMeta | null; fields: Fields | null | undefined; pattern: PatternData | null | undefined }

/** One run's files, loaded when asked for. `undefined` means still loading, `null` means the run has no such file. */
export function useRunData(source: SimSource | null, job: SimJob | null, wantField: boolean, wantPattern: boolean): RunData {
  const k = `${source?.id}:${job?.job_id}`;
  const ok = !!source && !!job;
  const curves = useAsync<Curves>(ok ? () => loadCurves(source!, job!) : null, `rc:${k}`).data ?? null;
  const summary = useAsync(ok ? () => loadSummary(source!, job!) : null, `rs:${k}`).data ?? null;
  const meta = useAsync(ok ? () => loadMeta(source!, job!) : null, `rm:${k}`).data ?? null;
  const fields = useAsync(ok && wantField ? () => loadField(source!, job!) : null, `rf:${k}:${wantField}`).data;
  const pattern = useAsync(ok && wantPattern ? () => loadPattern(source!, job!) : null, `rp:${k}:${wantPattern}`).data;
  return { curves, summary, meta, fields, pattern };
}

export { bestRun } from "./sim-math.mjs";
