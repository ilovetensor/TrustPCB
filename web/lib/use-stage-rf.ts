"use client";
import { useEffect, useMemo, useState } from "react";
import { FIXTURE_MODE } from "./api";
import { listSims, liveSource, loadStageRf, nrfSampleSource, pickStageJob, type SimJob, type SimSource, type StageRf } from "./sims";

/**
 * A review's solver runs for the workspace: the list (refreshed as sim events arrive) and the field and
 * surface current of the run the board stage draws. Fixture replays use the bundled real nRF run.
 */
export function useReviewSims(reviewId: string, nEvents: number, working: boolean) {
  const [source, setSource] = useState<SimSource | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let live = true;
    if (FIXTURE_MODE) { nrfSampleSource().then((s) => { if (live) setSource(s); }).catch(() => setError(true)); return () => { live = false; }; }
    const load = () => listSims(reviewId).then((jobs) => { if (live) { setSource(liveSource(reviewId, jobs)); setError(false); } }).catch(() => { if (live) setError(true); });
    void load();
    const id = working ? setInterval(load, 4000) : undefined;
    return () => { live = false; if (id) clearInterval(id); };
  }, [reviewId, nEvents, working]);
  const jobs: SimJob[] = source?.jobs ?? [];
  const stageJob = useMemo(() => pickStageJob(jobs), [jobs]);
  const [rf, setRf] = useState<StageRf | null>(null);
  const key = stageJob ? `${stageJob.job_id}:${stageJob.files.length}` : "";
  useEffect(() => {
    if (!source || !stageJob) { setRf(null); return; }
    let live = true;
    loadStageRf(source, stageJob).then((r) => { if (live) setRf(r.fieldXY || r.current ? r : null); }).catch(() => { if (live) setRf(null); });
    return () => { live = false; };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  return { source, jobs, rf, error };
}
