"use client";
import { useEffect, useRef, useState } from "react";
import { SEVERITY_ORDER, sortFindings, type ReviewState } from "../../lib/review-reducer.mjs";
import { findingLabel, findingNumber } from "../../lib/review-view";
import type { Evidence, Finding, Severity } from "../../lib/types";
import { ClassStamp, Icon, SevGlyph } from "../ui";
import { PartialWin } from "../rf/PartialWin";
import type { Headline } from "../../lib/story.mjs";
import type { Mark } from "../../lib/types";

const evidenceLine = (e: Evidence): { tool: string; text: string; vals: string } => {
  const v = (e.values ?? {}) as Record<string, any>;
  const meas = (v.measurements ?? (Object.values(v).every((x) => x && typeof x === "object" && "value" in x) ? v : {})) as Record<string, { value: number; unit: string }>;
  const vals = Object.entries(meas).slice(0, 4).map(([k, m]) => `${k.replace(/_/g, " ")} ${typeof m.value === "number" ? +m.value.toFixed(3) : m.value} ${m.unit}`).join(" / ");
  return { tool: e.source_tool, text: e.title ?? v.title ?? e.summary ?? "", vals };
};

export function FindingsPanel({ state, evidence, selectedId, onSelect, onAsk, onSetStatus, headline = null, onFindingCode }: {
  headline?: Headline | null; onFindingCode?: (code: string) => void;
  state: ReviewState; evidence: Record<string, Evidence>; selectedId: string | null; onSelect: (id: string | null) => void; onAsk: (id: string) => void;
  onSetStatus: (id: string, status: "dismissed" | "open", reason?: string) => void;
}) {
  const [sev, setSev] = useState<Severity | "all">("all");
  const [showDismissed, setShowDismissed] = useState(false);
  const all = state.findingOrder.map((id) => state.findings[id]).filter(Boolean);
  const live = all.filter((f) => f.status !== "dismissed" && f.status !== "superseded");
  const gone = all.filter((f) => f.status === "dismissed");
  const list = sortFindings(live).filter((f) => sev === "all" || f.severity === sev);
  const counts = Object.fromEntries(SEVERITY_ORDER.map((s) => [s, live.filter((f) => f.severity === s).length])) as Record<Severity, number>;
  const refs = useRef<Record<string, HTMLElement | null>>({});
  useEffect(() => { if (selectedId) refs.current[selectedId]?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [selectedId]);
  const c = state.candidates;
  return (
    <div className="fp">
      <div className="fp-tabs" role="tablist" aria-label="Severity filter">
        <button role="tab" aria-selected={sev === "all"} onClick={() => setSev("all")}><span className="num">{live.length}</span><span className="idx">ALL</span></button>
        {SEVERITY_ORDER.map((s) => (
          <button key={s} role="tab" aria-selected={sev === s} onClick={() => setSev(s)} style={{ opacity: counts[s] || sev === s ? 1 : 0.4 }} title={s}>
            <span className="num"><SevGlyph sev={s} size={10} /> {counts[s]}</span><span className="idx">{s.slice(0, 4)}</span>
          </button>
        ))}
      </div>
      {c.total > 0 && (
        <div className="fp-cand mono"><span className="idx">CANDIDATES</span><span>{c.reviewed}/{c.total} reviewed</span><div className="meter"><b style={{ width: `${(c.reviewed / c.total) * 100}%` }} /></div></div>
      )}
      <div className="fp-list">
        {headline && <PartialWin h={headline} compact onFinding={onFindingCode} />}
        {list.length === 0 && (
          <p className="empty">{all.length === 0 ? (state.status === "running" || state.status === "idle" ? "No findings yet. Each one appears here, with its numbered balloon on the board, the moment the agent records it." : "This review recorded no findings.") : "No findings at this level."}</p>
        )}
        {list.map((f) => (
          <FindingCard key={f.id} f={f} ev={(f.evidence ?? f.evidence_ids ?? []).map((i) => evidence[i]).filter(Boolean)} evCodes={f.evidence ?? []} sel={selectedId === f.id} onSelect={() => onSelect(selectedId === f.id ? null : f.id)} onAsk={() => onAsk(f.id)} onDismiss={(reason) => onSetStatus(f.id, "dismissed", reason)} setRef={(el) => { refs.current[f.id] = el; }} />
        ))}
        {gone.length > 0 && (
          <div className="fp-gone">
            <button className="btn sm ghost" onClick={() => setShowDismissed(!showDismissed)}>{showDismissed ? "Hide" : "Show"} {gone.length} dismissed</button>
            {showDismissed && gone.map((f) => (
              <div key={f.id} className="fp-gone-row">
                <span className="idx">{findingLabel(f)}</span><span className="fp-gone-t">{f.title}</span>
                <span className="dim">{f.dismissed_reason}</span>
                <button className="btn sm" onClick={() => onSetStatus(f.id, "open")}>Reopen</button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function FindingCard({ f, ev, evCodes, sel, onSelect, onAsk, onDismiss, setRef }: { f: Finding; ev: Evidence[]; evCodes: string[]; sel: boolean; onSelect: () => void; onAsk: () => void; onDismiss: (reason: string) => void; setRef: (el: HTMLElement | null) => void }) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  return (
    <article ref={setRef} className={`fc ${sel ? "sel" : ""}`} data-sev={f.severity}>
      <button className="fc-head" onClick={onSelect} aria-expanded={sel}>
        <span className={`fc-bal ${f.severity}`}>{findingNumber(f)}</span>
        <span className="fc-main">
          <span className="fc-meta"><span className="idx">{findingLabel(f)}</span><SevGlyph sev={f.severity} size={10} /><span className="idx">{f.severity}</span>{f.status === "needs_recheck" && <span className="stamp sig">Needs recheck</span>}</span>
          <span className="fc-title">{f.title}</span>
        </span>
        <Icon name="chevron" size={12} style={{ transform: sel ? "rotate(90deg)" : undefined, transition: "transform .2s" }} />
      </button>
      {sel && (
        <div className="fc-body rise">
          <div className="fc-stamps"><ClassStamp cls={f.class} />{f.source === "followup" && <span className="stamp">Found in follow-up</span>}</div>
          <MarksSummary marks={f.marks ?? []} />
          <Sec k="WHAT" t={f.what} />
          <Sec k="WHY IT MATTERS" t={f.why_it_matters} />
          <Sec k="RECOMMENDATION" t={f.recommendation} />
          {(ev.length > 0 || evCodes.length > 0) && (
            <div className="fc-ev">
              <span className="idx">EVIDENCE</span>
              {ev.length === 0 && <div className="fc-evrow"><span className="mono">{evCodes.join(", ")}</span></div>}
              {ev.map((e) => {
                const l = evidenceLine(e);
                return (
                  <div key={e.id} className="fc-evrow">
                    <span className="idx">{e.code ?? e.id}</span><span className="mono dim">{l.tool}</span>
                    {l.text && <span className="fc-evt">{l.text}</span>}
                    {l.vals && <span className="mono fc-vals">{l.vals}</span>}
                  </div>
                );
              })}
            </div>
          )}
          {f.assumptions?.length > 0 && <List k="ASSUMPTIONS" items={f.assumptions} />}
          {f.not_tested?.length > 0 && <List k="NOT TESTED" items={f.not_tested} />}
          {asking ? (
            <div className="fc-dismiss">
              <label className="idx" htmlFor={`dr-${f.id}`}>WHY DISMISS IT?</label>
              <input id={`dr-${f.id}`} type="text" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Known and accepted, false positive, ..." autoFocus
                onKeyDown={(e) => { if (e.key === "Enter" && reason.trim()) { onDismiss(reason.trim()); setAsking(false); } }} />
              <div className="fc-act">
                <button className="btn sm primary" disabled={!reason.trim()} onClick={() => { onDismiss(reason.trim()); setAsking(false); }}>Dismiss</button>
                <button className="btn sm ghost" onClick={() => setAsking(false)}>Cancel</button>
              </div>
            </div>
          ) : (
            <div className="fc-act">
              <button className="btn sm" onClick={onAsk}><Icon name="chat" size={11} /> Ask about this</button>
              <button className="btn sm ghost" onClick={() => setAsking(true)}>Dismiss</button>
            </div>
          )}
        </div>
      )}
    </article>
  );
}
const Sec = ({ k, t }: { k: string; t: string }) => (<div className="fc-sec"><span className="idx">{k}</span><p>{t}</p></div>);
const List = ({ k, items }: { k: string; items: string[] }) => (<div className="fc-sec"><span className="idx">{k}</span><ul>{items.map((i) => (<li key={i}>{i}</li>))}</ul></div>);

/** What the finding draws on the board: counts by kind, the zones a layout finding names, and what a dashed sketch means. */
function MarksSummary({ marks }: { marks: Mark[] }) {
  if (!marks.length) return null;
  const by: Record<string, number> = {};
  let items = 0;
  const zones = new Map<string, number>();
  for (const m of marks) {
    by[m.type] = (by[m.type] ?? 0) + 1;
    items += m.targets.length;
    if (m.type === "region" && m.role) zones.set(m.role, (zones.get(m.role) ?? 0) + 1);
  }
  const KIND: Record<string, string> = { pin: "pin", object: "highlighted object", path: "route", region: "zone", link: "distance link", sketch: "sketch" };
  return (
    <div className="fc-sec fc-marks">
      <span className="idx">ON THE BOARD</span>
      <p>{Object.entries(by).map(([k, n]) => `${n} ${KIND[k] ?? k}${n > 1 ? "s" : ""}`).join(", ")} covering {items} item{items === 1 ? "" : "s"}. {items > 12 ? "Pins are numbered; the rest dim until you select this finding." : ""}</p>
      {zones.size > 0 && <p className="fc-zones">{[...zones.entries()].map(([role, n]) => (<span key={role} className="stamp">{role.replace(/_/g, " ")}{n > 1 ? ` x${n}` : ""}</span>))}</p>}
      {by.sketch > 0 && <p className="dim fc-sketchnote"><svg width="26" height="6" aria-hidden><path d="M1 3h24" stroke="currentColor" strokeWidth="1.6" strokeDasharray="2 4" strokeLinecap="round" /></svg> Dashed sketch: a way it could go, display only. It is never a real route.</p>}
    </div>
  );
}
