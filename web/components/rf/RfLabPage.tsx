"use client";
import "./rf.css";
import "../pages/pages.css";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";
import { FIXTURE_MODE, getBoardBundle, getReview, getSnapshot, listRevisions, normaliseBoard, type BoardBundle, type ReviewMeta } from "../../lib/api";
import { headlineOf, versionChangeOf, type Headline, type VersionChange } from "../../lib/story.mjs";
import { liveSource, listSims, NRF_BOARD, nrfSampleSource, type SimJob, type SimSource } from "../../lib/sims";
import { Icon, Panel, TopBar } from "../ui";
import { RfLabView, type Band } from "./RfLab";

/** Poll while the solver is working so a run appears, advances and finishes on this page without a reload. */
function useSims(reviewId: string, enabled: boolean) {
  const [jobs, setJobs] = useState<SimJob[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const working = jobs?.some((j) => j.status === "queued" || j.status === "running") ?? false;
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    const load = () => listSims(reviewId).then((j) => { if (live) { setJobs(j); setError(null); } }).catch((e) => { if (live) setError(e instanceof Error ? e.message : "Could not load the simulations."); });
    void load();
    const id = setInterval(load, working ? 3000 : 20000);
    return () => { live = false; clearInterval(id); };
  }, [reviewId, enabled, working]);
  return { jobs, error };
}

function Shell({ reviewId, meta, children, sample }: { reviewId: string; meta: ReviewMeta | null; children: React.ReactNode; sample?: boolean }) {
  return (
    <div className="pg">
      <TopBar current="projects" />
      <main className="pg-main">
        <div className="pg-head">
          <div>
            <Link href={`/reviews/${reviewId}`} className="idx">REVIEW {reviewId.slice(0, 6).toUpperCase()} /</Link>
            <h1 className="serif">RF lab</h1>
            <p className="pg-sub">{meta ? `${meta.project_name}, ${meta.version_label}` : sample ? "Bundled sample" : ""}</p>
          </div>
          <span className="grow" />
          <Link className="btn" href={`/reviews/${reviewId}`}><Icon name="board" size={13} /> Workspace</Link>
        </div>
        {children}
      </main>
    </div>
  );
}

function RfSample({ reviewId, job }: { reviewId: string; job: string | null }) {
  const [source, setSource] = useState<SimSource | null>(null);
  const [bundle, setBundle] = useState<BoardBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    nrfSampleSource().then(setSource).catch(() => setError("The bundled sample could not be loaded."));
    fetch(NRF_BOARD.board).then((r) => r.json()).then((b) => setBundle({ board: normaliseBoard(b), glbUrl: NRF_BOARD.glb, scene: null, zones: [] })).catch(() => setError("The bundled board could not be loaded."));
  }, []);
  if (error) return <Shell reviewId={reviewId} meta={null} sample><p className="chat-err" role="alert">{error}</p></Shell>;
  return <RfLabView reviewId={reviewId} sample initialJob={job} source={source ?? { id: "loading", jobs: [], fileUrl: () => "" }} bundle={source ? bundle : null} title="nRF52840 node, meander antenna" goalBand={{ lo_ghz: 2.4, hi_ghz: 2.4835, target_db: -10 }} notice={source?.sample?.note} />;
}

function RfLive({ reviewId, job }: { reviewId: string; job: string | null }) {
  const [meta, setMeta] = useState<ReviewMeta | null>(null);
  const [bundle, setBundle] = useState<BoardBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { jobs, error: simError } = useSims(reviewId, true);
  const [headline, setHeadline] = useState<Headline | null>(null);
  const [versionChange, setVersionChange] = useState<VersionChange | null>(null);
  // the partial win lives in the report snapshot; no report yet means no headline
  useEffect(() => {
    let live = true;
    listRevisions(reviewId).then((r) => (r.length ? getSnapshot(reviewId, r[r.length - 1].number) : null)).then((snap) => { if (live) { setHeadline(headlineOf(snap)); setVersionChange(versionChangeOf(snap)); } }).catch(() => { if (live) setHeadline(null); });
    return () => { live = false; };
  }, [reviewId]);
  useEffect(() => {
    let live = true;
    getReview(reviewId).then(async (m) => {
      if (!live) return;
      setMeta(m);
      try { const b = await getBoardBundle(m.version_id); if (live) setBundle(b); } catch (e) { if (live) setError(e instanceof Error ? e.message : "Could not load the board."); }
    }).catch((e) => { if (live) setError(e instanceof Error ? e.message : "Could not load the review."); });
    return () => { live = false; };
  }, [reviewId]);
  const source = useMemo(() => (jobs ? liveSource(reviewId, jobs) : null), [reviewId, jobs]);
  const gb = meta?.goal?.band_hz;
  const goalBand: Band | null = gb ? { lo_ghz: gb[0] / 1e9, hi_ghz: gb[1] / 1e9, target_db: meta?.goal?.target_s11_db ?? -10 } : null;

  if (source && source.jobs.length > 0) {
    return (
      <>
        <RfLabView reviewId={reviewId} source={source} bundle={bundle} initialJob={job} title={meta ? `${meta.project_name}, ${meta.version_label}` : ""} goalBand={goalBand} headline={headline} versionChange={versionChange} />
        {error && <p className="chat-err" role="alert" style={{ position: "fixed", bottom: 12, left: 12, zIndex: 20 }}>{error}</p>}
      </>
    );
  }
  return (
    <Shell reviewId={reviewId} meta={meta}>
      {(error || simError) && <p className="chat-err" role="alert">{error ?? simError}</p>}
      {source && source.jobs.length === 0 && (
        <Panel className="card" idx="NOT TESTED" title="No simulation result for this review" delay={0}>
          <div style={{ padding: 16 }}>
            <p style={{ margin: "0 0 10px" }}>Nothing in this review was simulated, so there is no resonance, S11 curve, field or pattern to show. Nothing here is estimated.</p>
            {meta?.rf?.reason && <p className="dim" style={{ margin: "0 0 10px" }}>{meta.rf.reason}</p>}
            <p className="dim" style={{ margin: 0 }}>Quick reviews run no simulation. Standard and Deep reviews simulate the antenna when the RF lab is installed.</p>
            <p style={{ margin: "14px 0 0" }}><Link href={`/reviews/${reviewId}/rf?sample=1`} className="signal">View a bundled sample simulation</Link> <span className="dim">(a different board, labelled as a sample)</span></p>
          </div>
        </Panel>
      )}
      {!source && !error && !simError && <p className="dim">Loading the simulations.</p>}
    </Shell>
  );
}

function Inner({ reviewId }: { reviewId: string }) {
  const sp = useSearchParams();
  if (FIXTURE_MODE || sp.get("sample") === "1") return <RfSample reviewId={reviewId} job={sp.get("job")} />;
  return <RfLive reviewId={reviewId} job={sp.get("job")} />;
}
export function RfLab({ reviewId }: { reviewId: string }) {
  return (<Suspense fallback={null}><Inner reviewId={reviewId} /></Suspense>);
}
