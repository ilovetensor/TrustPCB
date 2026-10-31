"use client";
import "./report.css";
import Link from "next/link";
import { useEffect, useState } from "react";
import { FIXTURE_MODE, getReview, listRevisions, reportUrl, type ReviewMeta, type RevisionRow } from "../../lib/api";
import { Icon, Logo } from "../ui";
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
    document.documentElement.dataset.theme = "paper";
    document.body.style.background = "var(--stage)";
    return () => { delete document.documentElement.dataset.theme; document.body.style.background = ""; };
  }, []);
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
    <div className="rp rp-live" data-theme="paper">
      <div className="rp-bar no-print">
        <Link href="/projects" aria-label="Projects"><Logo size={18} /></Link>
        <span className="idx">/ REPORT {rev != null ? `R${rev}` : ""}</span>
        {meta && <span className="idx">/ {meta.project_name.toUpperCase()} {meta.version_label.toUpperCase()}</span>}
        <span style={{ flex: 1 }} />
        {revs && revs.length > 1 && (
          <div className="seg" role="group" aria-label="Report revision">
            {revs.map((r) => (<button key={r.id} aria-pressed={rev === r.number} onClick={() => setRev(r.number)} title={r.cause}>r{r.number}</button>))}
          </div>
        )}
        {rev != null && <a className="btn sm" href={reportUrl(reviewId, rev, "html")} target="_blank" rel="noreferrer">Open</a>}
        {rev != null && <button className="btn sm" onClick={pdf}>PDF</button>}
        <Link className="btn sm" href={`/reviews/${reviewId}`}>Workspace</Link>
      </div>
      {pdfError && <p className="chat-err no-print" role="alert" style={{ margin: "8px 16px" }}>{pdfError}</p>}
      {error && <div className="rp-page" style={{ paddingTop: 40 }}><p className="chat-err" role="alert">{error}</p></div>}
      {!error && revs && revs.length === 0 && (
        <div className="rp-page" style={{ paddingTop: 60 }}>
          <span className="idx">NO REPORT YET</span>
          <p className="serif" style={{ fontSize: 24 }}>{meta && (meta.status === "running" || meta.status === "queued") ? "The review is still running. The report is written when it ends." : "This review did not produce a report."}</p>
          <Link className="btn" href={`/reviews/${reviewId}`}>Back to the workspace <Icon name="arrow" size={13} /></Link>
        </div>
      )}
      {!revs && !error && <div className="rp-page" style={{ paddingTop: 60 }}><span className="idx">LOADING REPORT</span></div>}
      {rev != null && (
        <div className="rp-frame-wrap">
          {frameState === "loading" && <span className="idx rp-frame-note">ASSEMBLING REPORT</span>}
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
