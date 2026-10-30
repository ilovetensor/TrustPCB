"use client";
import { useEffect, useRef, useState } from "react";
import type { ReviewState, TrailEntry } from "../../lib/review-reducer.mjs";
import { findingLabel, findingNumber } from "../../lib/review-view";
import { SevGlyph } from "../ui";
import { Meter } from "../ui";

type Filter = "all" | "thoughts" | "tests" | "results";

const STATUS: Record<string, string> = { pending: "QUEUED", testing: "TESTING", confirmed: "CONFIRMED", ruled_out: "RULED OUT", inconclusive: "INCONCLUSIVE" };

export function Trail({ state, onSelectFinding }: { state: ReviewState; onSelectFinding: (id: string) => void }) {
  const [filter, setFilter] = useState<Filter>("all");
  const scroller = useRef<HTMLDivElement>(null);
  const items = state.trail.filter((e) => {
    if (filter === "all") return true;
    if (filter === "thoughts") return e.kind === "thought" || e.kind === "phase";
    if (filter === "tests") return e.kind === "tool" || e.kind === "sim" || e.kind === "investigation" || e.kind === "suspect";
    return e.kind === "finding" || e.kind === "result";
  });
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [state.trail.length]);
  return (
    <div className="trail">
      <div className="trail-filter">
        <div className="seg" role="tablist" aria-label="Trail filter">
          {(["all", "thoughts", "tests", "results"] as Filter[]).map((f) => (
            <button key={f} role="tab" aria-selected={filter === f} onClick={() => setFilter(f)}>{f}</button>
          ))}
        </div>
      </div>
      <div className="trail-scroll" ref={scroller}>
        {items.length === 0 && (
          <p className="empty">The investigation trail fills in as the agent works. Each thought, test and result lands here in order.</p>
        )}
        <ol className="trail-list">
          {items.map((e) => (<TrailRow key={e.id} e={e} state={state} onSelectFinding={onSelectFinding} />))}
        </ol>
      </div>
    </div>
  );
}

function TrailRow({ e, state, onSelectFinding }: { e: TrailEntry; state: ReviewState; onSelectFinding: (id: string) => void }) {
  if (e.kind === "phase")
    return (
      <li className="tr tr-phase">
        <span className="idx">PHASE {String(e.phase ?? 0).padStart(2, "0")}</span>
        <b>{e.text}</b>
        <hr className="hl" />
      </li>
    );
  if (e.kind === "thought")
    return (<li className="tr tr-thought"><p>{e.text}</p></li>);
  if (e.kind === "tool")
    return (
      <li className="tr tr-tool">
        <span className={`tr-st ${e.state}`} aria-label={e.state} />
        <span className="mono tr-tn">{e.tool}</span>
        <span className="tr-tt">{e.text}</span>
        {e.result && <span className="tr-res mono">{e.result}</span>}
      </li>
    );
  if (e.kind === "sim") {
    const sim = e.simId ? state.sims[e.simId] : null;
    return (
      <li className="tr tr-sim">
        <div className="tr-simtop"><span className="idx">SIM {e.simId?.replace("sim-", "").toUpperCase()}</span><b>{e.text}</b><span className="idx">{e.state === "done" ? "DONE" : e.state === "queued" ? "QUEUED" : `${Math.round((e.pct ?? 0) * 100)}%`}</span></div>
        <Meter value={e.pct ?? 0} signal={e.state === "running"} />
        {sim?.change && <div className="mono tr-change">{sim.change}</div>}
        {sim?.summary && (
          <div className="tr-sum mono"><span>f0 <b>{sim.summary.f0_ghz.toFixed(3)}</b> GHz</span><span>S11 <b>{sim.summary.s11_db.toFixed(1)}</b> dB</span><span>BW <b>{sim.summary.bw_mhz}</b> MHz</span></div>
        )}
      </li>
    );
  }
  if (e.kind === "finding" && e.findingId)
    return (
      <li className="tr tr-find">
        <button onClick={() => onSelectFinding(e.findingId!)}>
          <SevGlyph sev={e.severity!} />
          <span className="idx">{findingLabel(e.findingId)}</span>
          <span>{state.findings[e.findingId]?.title ?? e.text}</span>
          <span className="idx">№{findingNumber(e.findingId)}</span>
        </button>
      </li>
    );
  if (e.kind === "investigation") {
    const inv = e.investigationId ? state.investigations[e.investigationId] : null;
    return (
      <li className="tr tr-inv">
        <span className="idx">INVESTIGATION {e.investigationId?.replace("inv-", "").padStart(2, "0")}</span>
        <p className="serif">{inv?.question ?? e.text}</p>
        <ul>
          {inv?.suspects.map((s, i) => (
            <li key={s.id} className={`sus ${s.status}`}>
              <span className="idx">S{i + 1}</span><span>{s.title}</span><span className="stamp">{STATUS[s.status] ?? s.status}</span>
            </li>
          ))}
        </ul>
      </li>
    );
  }
  if (e.kind === "suspect")
    return (<li className="tr tr-thought dim2"><p><span className="stamp">{STATUS[e.status ?? ""] ?? e.status}</span> {e.text}{e.detail ? `. ${e.detail}` : ""}</p></li>);
  return (
    <li className="tr tr-res2"><span className="idx">RESULT</span><b>{e.text}</b>{e.detail && <p>{e.detail}</p>}</li>
  );
}
