"use client";
import "./report.css";
import Link from "next/link";
import { useEffect, useState } from "react";
import { FIXTURE_MODE, getReview, listRevisions, reportUrl, type ReviewMeta, type RevisionRow } from "../../lib/api";
import { Icon } from "../ui";
import { ReportFixture } from "./ReportFixture";

/**
 * The report is rendered by the server from a frozen snapshot (spec section 16), so what is on this page is
 * what the backend wrote. The page frames it: revision picker, open, print and PDF.
 */
function ReportLive({ reviewId }: { reviewId: string }) {
  const [meta, setMeta] = useState<ReviewMeta | null>(null);
  const [revs, setRevs] = useState<RevisionRow[] | null>(null);
  const [rev, setRev] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [frameState, setFrameState] = useState<"loading" | "ready" | "error">("loading");
  const [pdfError, setPdfError] = useState<string | null>(null);

  useEffect(() => {
    getReview(reviewId).then(setMeta).catch((e) => setError(e instanceof Error ? e.message : "Could not load the review."));
    listRevisions(reviewId).then((r) => { setRevs(r); if (r.length) setRev(r[r.length - 1].number); }).catch((e) => setError(e instanceof Error ? e.message : "Could not load the report."));
  }, [reviewId]);
  useEffect(() => { setFrameState("loading"); }, [rev]);
  const pdf = async () => {
    if (rev == null) return;
    setPdfError(null);
    try {
      const r = await fetch(reportUrl(reviewId, rev, "pdf"));
      if (!r.ok) throw new Error((await r.json().catch(() => ({ detail: `PDF failed (${r.status})` }))).detail);
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `trustpcb-report-r${rev}.pdf`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    } catch (e) { setPdfError(e instanceof Error ? e.message : "PDF failed."); }
  };

  return (
    <div className="rp rp-live">
      <div className="rp-bar no-print">
        <Link className="btn sm" href={`/reviews/${reviewId}`}><Icon name="back" size={13} /> Back to review</Link>
        <div className="rp-bar-title">
          <b>Report{rev != null ? ` r${rev}` : ""}</b>
          {meta && <span className="dim">{meta.project_name}, {meta.version_label}</span>}
        </div>
        <span style={{ flex: 1 }} />
        {revs && revs.length > 1 && (
          <div className="seg" role="group" aria-label="Report revision">
            {revs.map((r) => (<button key={r.id} aria-pressed={rev === r.number} onClick={() => setRev(r.number)} title={r.cause}>r{r.number}</button>))}
          </div>
        )}
        {rev != null && <a className="btn sm" href={reportUrl(reviewId, rev, "html")} target="_blank" rel="noreferrer"><Icon name="external" size={13} /> Open in a new tab</a>}
        {rev != null && <button className="btn sm primary" onClick={pdf}><Icon name="file" size={13} /> Save as PDF</button>}
      </div>
      {pdfError && <p className="chat-err no-print" role="alert" style={{ margin: "8px 16px" }}>{pdfError}</p>}
      {error && <div className="rp-page" style={{ paddingTop: 40 }}><p className="chat-err" role="alert">{error}</p></div>}
      {!error && revs && revs.length === 0 && (
        <div className="rp-page" style={{ paddingTop: 60 }}>
          <span className="idx">No report yet</span>
          <p className="serif" style={{ fontSize: 24 }}>{meta && (meta.status === "running" || meta.status === "queued") ? "The review is still running. The report is written when it ends." : "This review did not produce a report."}</p>
          <Link className="btn" href={`/reviews/${reviewId}`}><Icon name="back" size={14} /> Back to review</Link>
        </div>
      )}
      {!revs && !error && <div className="rp-page" style={{ paddingTop: 60 }}><span className="idx">Loading the report</span></div>}
      {rev != null && (
        <div className="rp-frame-wrap">
          {frameState === "loading" && <span className="idx rp-frame-note">Assembling the report</span>}
          <iframe
            key={rev} title={`Report r${rev}`} className="rp-frame" src={reportUrl(reviewId, rev, "html")}
            sandbox="allow-same-origin" onLoad={() => setFrameState("ready")} onError={() => setFrameState("error")}
          />
        </div>
      )}
    </div>
  );
}

export function Report({ reviewId }: { reviewId: string }) {
  return FIXTURE_MODE ? <ReportFixture reviewId={reviewId} /> : <ReportLive reviewId={reviewId} />;
}
