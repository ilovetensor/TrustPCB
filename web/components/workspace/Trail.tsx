"use client";
import { useEffect, useRef, useState } from "react";
import type { ReviewState, TrailEntry } from "../../lib/review-reducer.mjs";
import { findingLabel, findingNumber } from "../../lib/review-view";
import { measurementLine } from "../../lib/review-view";
import { SevGlyph } from "../ui";
import { Meter } from "../ui";
import type { StoryRound, StoryTest } from "../../lib/story.mjs";
import { InvestigationBoard } from "./InvestigationBoard";

type Filter = "all" | "board" | "thoughts" | "tests" | "results";

const STATUS: Record<string, string> = { pending: "QUEUED", testing: "TESTING", confirmed: "CONFIRMED", ruled_out: "RULED OUT", inconclusive: "INCONCLUSIVE" };

export function Trail({ state, onSelectFinding, story = [], tests = [] }: { state: ReviewState; onSelectFinding: (id: string) => void; story?: StoryRound[]; tests?: StoryTest[] }) {
  const [filter, setFilter] = useState<Filter>("all");
  // a finished review opens on the board: it is the story, the log is the detail
  const opened = useRef(false);
  useEffect(() => { if (!opened.current && state.status === "complete" && state.investigationOrder.length > 0) { opened.current = true; setFilter("board"); } }, [state.status, state.investigationOrder.length]);
  const scroller = useRef<HTMLDivElement>(null);
  const items = state.trail.filter((e) => {
    if (filter === "all") return true;
    if (filter === "board") return false;
    if (filter === "thoughts") return e.kind === "thought" || e.kind === "phase";
    if (filter === "tests") return e.kind === "tool" || e.kind === "sim" || e.kind === "investigation" || e.kind === "suspect";
    return e.kind === "finding" || e.kind === "result";
  });
  useEffect(() => {
    const el = scroller.current;
    if (el && filter !== "board") el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [state.trail.length, filter]);
  useEffect(() => { if (filter === "board") scroller.current?.scrollTo({ top: 0 }); }, [filter]);
  return (
    <div className="trail">
      <div className="trail-filter">
        <div className="seg" role="tablist" aria-label="Trail filter">
          {(["all", "board", "thoughts", "tests", "results"] as Filter[]).map((f) => (
            <button key={f} role="tab" aria-selected={filter === f} onClick={() => setFilter(f)}>{f}</button>
          ))}
        </div>
      </div>
      <div className="trail-scroll" ref={scroller}>
        {filter === "board" && <InvestigationBoard state={state} story={story} tests={tests} />}
        {filter !== "board" && items.length === 0 && (
          <p className="empty">The investigation trail fills in as the agent works. Each thought, test and result lands here in order.</p>
        )}
        <ol className="trail-list">
          {items.map((e) => (<TrailRow key={e.id} e={e} state={state} onSelectFinding={onSelectFinding} />))}
        </ol>
      </div>
    </div>
  );
}

/** A refused tool call comes back as JSON with a message. Show the message. */
function errorText(raw: string) {
  try { const j = JSON.parse(raw); if (j && typeof j.message === "string") return j.message.slice(0, 200); } catch { /* plain text */ }
  return raw.slice(0, 200);
}

/** The agent's own words may carry **bold**. Nothing else is interpreted. */
function inlineMd(text: string) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) => (/^\*\*[^*]+\*\*$/.test(part) ? <b key={i}>{part.slice(2, -2)}</b> : part));
}

function TrailRow({ e, state, onSelectFinding }: { e: TrailEntry; state: ReviewState; onSelectFinding: (id: string) => void }) {
  if (e.kind === "phase")
    return (
      <li className="tr tr-phase">
        <span className="idx">PHASE {String(e.phase ?? 0).padStart(2, "0")}{e.substep === "refine" ? " / REFINE" : ""}</span>
        <b>{e.text}</b>
        <hr className="hl" />
      </li>
    );
  if (e.kind === "thought")
    return (<li className="tr tr-thought"><p style={{ whiteSpace: "pre-wrap" }}>{inlineMd(e.text)}</p></li>);
  if (e.kind === "tool" && e.state === "error")
    return (
      <li className="tr tr-tool">
        <span className="tr-st error" aria-label="error" />
        <span className="mono tr-tn">{e.tool}</span>
        <span className="tr-tt">{e.text}</span>
        {e.result && <span className="tr-res mono">{errorText(e.result)}</span>}
      </li>
    );
  if (e.kind === "tool")
    return (
      <li className="tr tr-tool">
        <span className={`tr-st ${e.state}`} aria-label={e.state} />
        <span className="mono tr-tn">{e.tool}</span>
        <span className="tr-tt">{e.text}</span>
      </li>
    );
  if (e.kind === "sim") {
    const sim = e.simId ? state.sims[e.simId] : null;
    return (
      <li className="tr tr-sim">
        <div className="tr-simtop"><span className="idx">SIM {sim?.kind ? sim.kind.toUpperCase() : "RUN"}</span><b>{e.text}</b><span className="idx">{e.state === "done" ? "DONE" : e.state === "queued" ? "QUEUED" : `${Math.round((e.pct ?? 0) * 100)}%`}</span></div>
        <Meter value={e.pct ?? 0} signal={e.state === "running"} />
        {sim?.change && <div className="mono tr-change">{sim.change}</div>}
        {sim?.measurements && Object.keys(sim.measurements).length > 0 && (
          <div className="tr-sum mono">{measurementLine(sim.measurements).map((m) => (<span key={m.k}>{m.k} <b>{m.v}</b> {m.u}</span>))}</div>
        )}
      </li>
    );
  }
  if (e.kind === "finding" && e.findingId)
    return (
      <li className="tr tr-find">
        <button onClick={() => onSelectFinding(e.findingId!)}>
          <SevGlyph sev={e.severity!} />
          <span className="idx">{findingLabel(state.findings[e.findingId] ?? e.findingId)}</span>
          <span>{state.findings[e.findingId]?.title ?? e.text}</span>
          {e.detail && <span className="stamp">{e.detail.replace(/_/g, " ")}</span>}
          <span className="idx">№{findingNumber(state.findings[e.findingId] ?? e.findingId)}</span>
        </button>
      </li>
    );
  if (e.kind === "investigation") {
    const inv = e.investigationId ? state.investigations[e.investigationId] : null;
    return (
      <li className="tr tr-inv">
        <span className="idx">INVESTIGATION {inv?.code ?? ""}</span>
        <p className="serif">{inv?.question ?? e.text}</p>
        <ul>
          {inv?.suspects.map((s, i) => (
            <li key={s.id} className={`sus ${s.status}`}>
              <span className="idx">{s.code ?? `S${i + 1}`}</span><span>{s.title}</span><span className="stamp">{STATUS[s.status] ?? s.status}</span>
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
