"use client";
import { useEffect, useRef, useState } from "react";
import { getEvidence } from "../../lib/api";
import { SEVERITY_ORDER, sortFindings, type ReviewState } from "../../lib/review-reducer.mjs";
import { findingLabel, findingNumber } from "../../lib/review-view";
import type { Finding, Severity } from "../../lib/types";
import { ClassStamp, Icon, SevGlyph } from "../ui";

export function FindingsPanel({ state, selectedId, onSelect, onAsk, dismissed, onDismiss }: {
  state: ReviewState; selectedId: string | null; onSelect: (id: string | null) => void; onAsk: (id: string) => void;
  dismissed: Set<string>; onDismiss: (id: string) => void;
}) {
  const [sev, setSev] = useState<Severity | "all">("all");
  const all = state.findingOrder.map((id) => state.findings[id]).filter(Boolean);
  const list = sortFindings(all).filter((f) => (sev === "all" || f.severity === sev) && !dismissed.has(f.id));
  const counts = Object.fromEntries(SEVERITY_ORDER.map((s) => [s, all.filter((f) => f.severity === s && !dismissed.has(f.id)).length])) as Record<Severity, number>;
  const refs = useRef<Record<string, HTMLElement | null>>({});
  useEffect(() => { if (selectedId) refs.current[selectedId]?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [selectedId]);
  const c = state.candidates;
  return (
    <div className="fp">
      <div className="fp-tabs" role="tablist" aria-label="Severity filter">
        <button role="tab" aria-selected={sev === "all"} onClick={() => setSev("all")}><span className="num">{all.length - dismissed.size > 0 ? all.filter((f) => !dismissed.has(f.id)).length : 0}</span><span className="idx">ALL</span></button>
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
        {list.length === 0 && (
          <p className="empty">{all.length === 0 ? "No findings yet. Each one appears here, with its numbered balloon on the board, the moment the agent records it." : "No findings at this level."}</p>
        )}
        {list.map((f) => (
          <FindingCard key={f.id} f={f} sel={selectedId === f.id} onSelect={() => onSelect(selectedId === f.id ? null : f.id)} onAsk={() => onAsk(f.id)} onDismiss={() => onDismiss(f.id)} setRef={(el) => { refs.current[f.id] = el; }} />
        ))}
      </div>
    </div>
  );
}

function FindingCard({ f, sel, onSelect, onAsk, onDismiss, setRef }: { f: Finding; sel: boolean; onSelect: () => void; onAsk: () => void; onDismiss: () => void; setRef: (el: HTMLElement | null) => void }) {
  const ev = getEvidence(f.evidence_ids);
  return (
    <article ref={setRef} className={`fc ${sel ? "sel" : ""}`} data-sev={f.severity}>
      <button className="fc-head" onClick={onSelect} aria-expanded={sel}>
        <span className={`fc-bal ${f.severity}`}>{findingNumber(f.id)}</span>
        <span className="fc-main">
          <span className="fc-meta"><span className="idx">{findingLabel(f.id)}</span><SevGlyph sev={f.severity} size={10} /><span className="idx">{f.severity}</span>{f.status === "needs_recheck" && <span className="stamp sig">Needs recheck</span>}</span>
          <span className="fc-title">{f.title}</span>
        </span>
        <Icon name="chevron" size={12} style={{ transform: sel ? "rotate(90deg)" : undefined, transition: "transform .2s" }} />
      </button>
      {sel && (
        <div className="fc-body rise">
          <div className="fc-stamps"><ClassStamp cls={f.class} />{f.origin === "followup" && <span className="stamp">Found in follow-up</span>}</div>
          <Sec k="WHAT" t={f.what} />
          <Sec k="WHY IT MATTERS" t={f.why_it_matters} />
          <Sec k="RECOMMENDATION" t={f.recommendation} />
          {ev.length > 0 && (
            <div className="fc-ev">
              <span className="idx">EVIDENCE</span>
              {ev.map((e) => (
                <div key={e.id} className="fc-evrow">
                  <span className="idx">{e.id}</span><span className="mono dim">{e.source_tool}</span>
                  <span className="mono fc-vals">{Object.entries(e.values).map(([k, v]) => `${k.replace(/_/g, " ")} ${v.value} ${v.unit}`).join(" / ")}</span>
                </div>
              ))}
            </div>
          )}
          {f.assumptions.length > 0 && <List k="ASSUMPTIONS" items={f.assumptions} />}
          {f.not_tested.length > 0 && <List k="NOT TESTED" items={f.not_tested} />}
          <div className="fc-act">
            <button className="btn sm" onClick={onAsk}><Icon name="chat" size={11} /> Ask about this</button>
            <button className="btn sm ghost" onClick={onDismiss}>Dismiss</button>
          </div>
        </div>
      )}
    </article>
  );
}
const Sec = ({ k, t }: { k: string; t: string }) => (<div className="fc-sec"><span className="idx">{k}</span><p>{t}</p></div>);
const List = ({ k, items }: { k: string; items: string[] }) => (<div className="fc-sec"><span className="idx">{k}</span><ul>{items.map((i) => (<li key={i}>{i}</li>))}</ul></div>);
