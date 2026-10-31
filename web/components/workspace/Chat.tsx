"use client";
import { useEffect, useRef, useState } from "react";
import type { ReviewState } from "../../lib/review-reducer.mjs";
import { Icon } from "../ui";

export interface SelectedChip { id: string; label: string; type: "finding" | "object" }

export function ChatDock({ state, selected, onClearSel, onSend, onDecide, busy, seed, error, suggest }: {
  state: ReviewState; selected: SelectedChip[]; onClearSel: (id: string) => void; onSend: (t: string) => void;
  onDecide: (id: string, ok: boolean) => void; busy: boolean; seed?: string; error?: string | null; suggest: string[];
}) {
  const [text, setText] = useState("");
  const end = useRef<HTMLDivElement>(null);
  const f = state.followup;
  const locked = state.status === "running" || state.status === "idle";
  useEffect(() => { if (seed) setText(seed); }, [seed]);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [f.messages.length, f.proposal?.status, f.messages[f.messages.length - 1]?.text]);
  const submit = () => { const t = text.trim(); if (!t || locked || f.running || busy) return; onSend(t); setText(""); };
  const others = Object.values(state.requests).filter((r) => r.kind !== "test" && r.status === "pending");
  const waiting = f.running && (f.proposal?.status === "pending" || others.length > 0);
  return (
    <div className="chat">
      <div className="chat-log">
        {f.messages.length === 0 && (
          <div className="empty">
            {locked ? "Chat opens when the review ends. While it runs only Stop is available." : "Ask about a finding, or click a part, pin or track on the board and choose Ask about this."}
            {!locked && suggest.length > 0 && <div className="chat-sug">{suggest.map((s) => (<button key={s} className="btn sm ghost" onClick={() => setText(s)}>{s}</button>))}</div>}
          </div>
        )}
        {f.messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}${m.narration ? " narration" : ""}`}>
            <span className="idx">{m.role === "user" ? "YOU" : m.narration ? "WORKING" : `AGENT${f.level ? " / " + f.level.toUpperCase() : ""}`}</span>
            <p style={m.error ? { color: "var(--sev-high)" } : { whiteSpace: "pre-wrap" }}>{m.text}</p>
            {m.selected && m.selected.length > 0 && <div className="chat-chips">{m.selected.map((s) => (<span className="stamp" key={s}>{s}</span>))}</div>}
          </div>
        ))}
        {f.proposal && (
          <div className="proposal hair">
            <span className="idx">PROPOSED TEST / REQUIRES APPROVAL</span>
            <p><b>{f.proposal.what}</b></p>
            <p className="dim">{f.proposal.why}{f.proposal.eta_s ? <>. About <span className="num">{Math.max(1, Math.round(f.proposal.eta_s / 60))}</span> min of solver time.</> : null}</p>
            {f.proposal.status === "pending" && f.proposal.request_id ? (
              <div className="proposal-act"><button className="btn sm primary" onClick={() => onDecide(f.proposal!.request_id!, true)}>Run</button><button className="btn sm" onClick={() => onDecide(f.proposal!.request_id!, false)}>Not now</button></div>
            ) : (<span className="stamp">{f.proposal.status === "pending" ? "waiting" : f.proposal.status}</span>)}
          </div>
        )}
        {others.map((r) => (
          <div key={r.request_id} className="proposal hair">
            <span className="idx">{r.kind === "permission" ? "PERMISSION REQUIRED" : "THE AGENT ASKS"}</span>
            <p><b>{String(r.payload.title ?? r.payload.tool_name ?? r.payload.question ?? r.payload.message ?? r.kind)}</b></p>
            {r.payload.description || r.payload.why ? <p className="dim">{String(r.payload.description ?? r.payload.why)}</p> : null}
            {r.kind === "permission" ? (
              <div className="proposal-act"><button className="btn sm primary" onClick={() => onDecide(r.request_id, true)}>Allow</button><button className="btn sm" onClick={() => onDecide(r.request_id, false)}>Deny</button></div>
            ) : <span className="stamp">Answer from a connected client</span>}
          </div>
        ))}
        {f.whatif && (
          <div className="whatif hair">
            <span className="stamp">{f.whatif.label ?? "What-if, not your board"}</span>
            <p><b>{f.whatif.ref}</b> is drawn as a ghost on the board{Array.isArray(f.whatif.new_position) ? <> at <span className="mono">{f.whatif.new_position.map((v: number) => v.toFixed(1)).join(", ")}</span> mm</> : null}. Your design is not changed.</p>
            {Array.isArray(f.whatif.distances) && f.whatif.distances.length > 0 && (
              <ul className="whatif-d mono">{(f.whatif.distances as { to: string; distance_mm: number; was_mm?: number }[]).slice(0, 4).map((d) => (<li key={d.to}><span>{d.to.replace(/^ref:/, "")}</span><b>{d.distance_mm} mm</b>{d.was_mm != null && <span className="dim">was {d.was_mm}</span>}</li>))}</ul>
            )}
            {Array.isArray(f.whatif.conflicts) && f.whatif.conflicts.length > 0 && <p className="whatif-c">{f.whatif.conflicts.length} conflict{f.whatif.conflicts.length > 1 ? "s" : ""}: {f.whatif.conflicts.slice(0, 3).map((c: any) => (typeof c === "string" ? c : c.text ?? c.kind ?? JSON.stringify(c))).join("; ")}</p>}
            <p className="dim">RF effect of a moved part: Not tested.</p>
          </div>
        )}
        {error && <p className="chat-err">{error}</p>}
        <div ref={end} />
      </div>
      <div className="chat-in">
        {selected.length > 0 && (
          <div className="chat-chips">{selected.map((s) => (<button key={s.id} className="stamp sel" onClick={() => onClearSel(s.id)} title="Remove">{s.label} <Icon name="close" size={8} /></button>))}</div>
        )}
        <div className="chat-box">
          <textarea
            value={text} rows={2} placeholder={locked ? "Chat is off while the review runs" : "Ask, challenge, or add a fact"} disabled={locked}
            onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
            aria-label="Follow-up message"
          />
          <button className="btn primary sm" onClick={submit} disabled={locked || f.running || busy || !text.trim()} aria-label="Send"><Icon name="send" size={12} /></button>
        </div>
        <div className="idx chat-foot">{waiting ? <span className="blink">WAITING FOR YOUR APPROVAL</span> : busy || f.running ? <span className="blink">RUNNING</span> : "ANSWER / CHECK / TEST. A TEST NEEDS YOUR APPROVAL."}</div>
      </div>
    </div>
  );
}
