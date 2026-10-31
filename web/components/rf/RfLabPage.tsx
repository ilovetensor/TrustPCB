"use client";
import "./rf.css";
import "../pages/pages.css";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { FIXTURE_MODE, getReview, getReviewEvents, type ReviewMeta } from "../../lib/api";
import { normaliseCurve } from "../../lib/curve";
import { initialReviewState, replayAll, type ReviewState } from "../../lib/review-reducer.mjs";
import { measurementLine } from "../../lib/review-view";
import { Icon, Panel, TopBar } from "../ui";
import { TuningGraph } from "./charts";
import { RfSample } from "./RfLab";

/**
 * The RF lab for a real review: what the backend actually simulated, taken from the review's own events.
 * No result means a plain Not tested. The bundled patch-antenna run is reachable only as a labelled sample.
 */
function RfLive({ reviewId }: { reviewId: string }) {
  const [meta, setMeta] = useState<ReviewMeta | null>(null);
  const [state, setState] = useState<ReviewState | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    getReview(reviewId).then(setMeta).catch((e) => setError(e instanceof Error ? e.message : "Could not load the review."));
    getReviewEvents(reviewId).then((ev) => setState(replayAll(ev, initialReviewState()))).catch((e) => setError(e instanceof Error ? e.message : "Could not load events."));
  }, [reviewId]);
  const goalBand = meta?.goal?.band_hz;
  const band = goalBand ? { lo_ghz: goalBand[0] / 1e9, hi_ghz: goalBand[1] / 1e9, target_db: meta?.goal?.target_s11_db ?? -10 } : null;
  const exps = state?.experiments ?? [];
  return (
    <div className="pg">
      <TopBar current="projects" />
      <main className="pg-main">
        <div className="pg-head">
          <div>
            <Link href={`/reviews/${reviewId}`} className="idx">REVIEW {reviewId.slice(0, 6).toUpperCase()} /</Link>
            <h1 className="serif">RF lab</h1>
            <p className="pg-sub">{meta ? `${meta.project_name}, ${meta.version_label}` : ""}</p>
          </div>
          <span className="grow" />
          <Link className="btn" href={`/reviews/${reviewId}`}><Icon name="board" size={13} /> Workspace</Link>
        </div>
        {error && <p className="chat-err" role="alert">{error}</p>}
        {state && exps.length === 0 && (
          <Panel className="card" idx="NOT TESTED" title="No simulation result for this review" delay={0}>
            <div style={{ padding: 16 }}>
              <p style={{ margin: "0 0 10px" }}>Nothing in this review was simulated, so there is no resonance, S11 curve, field or pattern to show. Nothing here is estimated.</p>
              {meta?.rf?.reason && <p className="dim" style={{ margin: "0 0 10px" }}>{meta.rf.reason}</p>}
              <p className="dim" style={{ margin: 0 }}>Quick reviews run no simulation. Standard and Deep reviews simulate the antenna when the RF lab is installed.</p>
              <p style={{ margin: "14px 0 0" }}><Link href={`/reviews/${reviewId}/rf?sample=1`} className="signal">View a bundled sample simulation</Link> <span className="dim">(a different board, labelled as a sample)</span></p>
            </div>
          </Panel>
        )}
        {exps.length > 0 && (
          <Panel className="card" idx="RUNS" title="Simulations in this review" delay={0}>
            <table className="rp-tbl" style={{ margin: "0 16px 16px", width: "calc(100% - 32px)" }}>
              <thead><tr><th>Run</th><th>Kind</th><th>Quality</th><th>Result</th><th>Trust</th></tr></thead>
              <tbody>{exps.map((e) => (
                <tr key={e.id}>
                  <td>{e.label}</td><td>{e.kind}</td><td>{e.quality}</td>
                  <td className="mono">{measurementLine(e.measurements).map((m) => `${m.k} ${m.v} ${m.u}`).join("  ")}</td>
                  <td><span className="stamp">{e.trust}</span></td>
                </tr>
              ))}</tbody>
            </table>
            {exps.map((e) => { const c = normaliseCurve(e.curve); return c ? <div key={e.id} style={{ padding: "0 16px 16px" }}><span className="idx">{e.label}</span><TuningGraph curves={[{ id: e.id, label: e.label, ...c }]} band={band} height={200} /></div> : null; })}
            <p className="idx" style={{ padding: "0 16px 16px", margin: 0 }}>FIELD, PATTERN AND CURRENT VIEWS NEED THE SOLVER OUTPUT FILES, WHICH THE API DOES NOT SERVE YET.</p>
          </Panel>
        )}
      </main>
    </div>
  );
}

function Inner({ reviewId }: { reviewId: string }) {
  const sp = useSearchParams();
  if (FIXTURE_MODE || sp.get("sample") === "1") return <RfSample reviewId={reviewId} />;
  return <RfLive reviewId={reviewId} />;
}
export function RfLab({ reviewId }: { reviewId: string }) {
  return (<Suspense fallback={null}><Inner reviewId={reviewId} /></Suspense>);
}
