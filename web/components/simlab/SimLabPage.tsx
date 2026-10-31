"use client";
import "../rf/rf.css";
import "../pages/pages.css";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { getBoardBundle, getReview, normaliseBoard, type BoardBundle, type ReviewMeta } from "../../lib/api";
import { devFixtureAvailable, devFixtureSource, isBoardKind, listBoardRuns, liveBoardSource, type BoardSimKind, type BoardSimRun, type BoardSimSource } from "../../lib/board-sims";
import { NRF_BOARD } from "../../lib/sims";
import { Icon, Panel, TopBar } from "../ui";
import { SimLabView } from "./SimLab";

/** Poll while a run is being solved so it appears, advances and finishes on this page without a reload. */
function useBoardRuns(reviewId: string) {
  const [runs, setRuns] = useState<BoardSimRun[] | null>(null);
  const [source, setSource] = useState<BoardSimSource | null>(null);
  const [error, setError] = useState<string | null>(null);
  const working = runs?.some((r) => r.status === "queued" || r.status === "running") ?? false;
  useEffect(() => {
    let live = true;
    const load = () => listBoardRuns(reviewId)
      .then(async (rows) => { const s = await liveBoardSource(reviewId, rows); if (live) { setRuns(rows); setSource(s); setError(null); } })
      .catch((e) => { if (live) setError(e instanceof Error ? e.message : "Could not load the simulations."); });
    void load();
    const id = setInterval(load, working ? 3000 : 20000);
    return () => { live = false; clearInterval(id); };
  }, [reviewId, working]);
  return { source, error };
}

function Shell({ reviewId, meta, children }: { reviewId: string; meta: ReviewMeta | null; children: React.ReactNode }) {
  return (
    <div className="pg">
      <TopBar current="projects" />
      <main id="main" tabIndex={-1} className="pg-main">
        <div className="pg-head">
          <div>
            <h1 className="heading">Simulation lab</h1>
            <p className="pg-sub">{meta ? `${meta.project_name}, ${meta.version_label}` : ""}</p>
          </div>
          <span className="grow" />
          <Link className="btn" href={`/reviews/${reviewId}`}><Icon name="back" size={14} /> Back to review</Link>
        </div>
        {children}
      </main>
    </div>
  );
}

/** Why a review has no run of these kinds: the extent chosen at the start says it. */
function whyNone(meta: ReviewMeta | null) {
  const extent = (meta as (ReviewMeta & { extent?: string[] }) | null)?.extent;
  if (Array.isArray(extent)) {
    const sims = (["power", "thermal", "signal"] as const).filter((k) => extent.includes(k));
    return sims.length ? "The review chose these checks but has not produced a result yet, or the simulation was not possible on this board." : "This review did not include power, heat or signal checks. Start a new version with them chosen to get them.";
  }
  return "This review has no power, heat or signal simulation. Choose them when you start a review or upload a new version.";
}

function Live({ reviewId, kind, run, refId }: { reviewId: string; kind: BoardSimKind | null; run: string | null; refId: string | null }) {
  const [meta, setMeta] = useState<ReviewMeta | null>(null);
  const [bundle, setBundle] = useState<BoardBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { source, error: simError } = useBoardRuns(reviewId);
  useEffect(() => {
    let live = true;
    getReview(reviewId).then(async (m) => {
      if (!live) return;
      setMeta(m);
      try { const b = await getBoardBundle(m.version_id); if (live) setBundle(b); } catch (e) { if (live) setError(e instanceof Error ? e.message : "Could not load the board."); }
    }).catch((e) => { if (live) setError(e instanceof Error ? e.message : "Could not load the review."); });
    return () => { live = false; };
  }, [reviewId]);
  if (source && source.runs.length > 0) {
    return (
      <>
        <SimLabView reviewId={reviewId} source={source} bundle={bundle} title={meta ? `${meta.project_name}, ${meta.version_label}` : ""} initialKind={kind} initialRun={run} initialRef={refId} backHref={`/reviews/${reviewId}`} />
        {error && <p className="chat-err" role="alert" style={{ position: "fixed", bottom: 12, left: 12, zIndex: 20 }}>{error}</p>}
      </>
    );
  }
  return (
    <Shell reviewId={reviewId} meta={meta}>
      {(error || simError) && <p className="chat-err" role="alert">{error ?? simError}</p>}
      {source && source.runs.length === 0 && (
        <Panel className="card" title="No simulation of these kinds in this review" actions={<span className="stamp">Not tested</span>}>
          <div style={{ padding: 16 }}>
            <p style={{ margin: "0 0 10px" }}>{whyNone(meta)}</p>
            <p style={{ margin: "16px 0 0", display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}><Link href={`/reviews/${reviewId}/rf`} className="btn sm"><Icon name="wave" size={13} /> Open the RF lab</Link></p>
          </div>
        </Panel>
      )}
      {!source && !error && !simError && <p className="dim">Loading the simulations.</p>}
    </Shell>
  );
}

/** Development only: runs solved in the browser on the bundled sample board. The route answers only when the fixture door is open. */
function DevFixture({ reviewId, kind, run, refId }: { reviewId: string; kind: BoardSimKind | null; run: string | null; refId: string | null }) {
  const [source, setSource] = useState<BoardSimSource | null>(null);
  const [bundle, setBundle] = useState<BoardBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    fetch(NRF_BOARD.board).then((r) => r.json()).then(async (b) => {
      const board = normaliseBoard(b);
      if (live) setBundle({ board, glbUrl: NRF_BOARD.glb, scene: null, zones: [] });
      const s = await devFixtureSource(board);
      if (live) setSource(s);
    }).catch(() => { if (live) setError("The dev fixture could not be built."); });
    return () => { live = false; };
  }, []);
  if (error) return <Shell reviewId={reviewId} meta={null}><p className="chat-err" role="alert">{error}</p></Shell>;
  if (!source) return <div className="rf"><div className="rf-loading idx">Solving the dev fixture in your browser</div></div>;
  return <SimLabView reviewId={reviewId} source={source} bundle={bundle} title="nRF52840 node, dev fixture" initialKind={kind} initialRun={run} initialRef={refId} notice={source.fixture?.note} backHref="/" sampleNote />;
}

function Inner({ reviewId }: { reviewId: string }) {
  const sp = useSearchParams();
  const k = sp.get("kind");
  const kind = isBoardKind(k) ? k : null;
  const props = { reviewId, kind, run: sp.get("run"), refId: sp.get("ref") };
  if (devFixtureAvailable && sp.get("fixture") === "1") return <DevFixture {...props} />;
  return <Live {...props} />;
}
export function SimLab({ reviewId }: { reviewId: string }) {
  return (<Suspense fallback={null}><Inner reviewId={reviewId} /></Suspense>);
}
