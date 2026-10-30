"use client";
import { useEffect, useRef, useState } from "react";
import type { ReviewState } from "../../lib/review-reducer.mjs";
import { findingLabel } from "../../lib/review-view";
import { Icon } from "../ui";

const SUGGEST = [
  "Why is F-007 a problem?",
  "What about moving C12 next to U3?",
  "Simulate it with the pour cleared",
];

export function ChatDock({ state, selected, onClearSel, onSend, onDecide, busy, seed }: {
  state: ReviewState; selected: string[]; onClearSel: (id: string) => void; onSend: (t: string) => void;
  onDecide: (id: string, ok: boolean) => void; busy: boolean; seed?: string;
}) {
  const [text, setText] = useState("");
  const end = useRef<HTMLDivElement>(null);
  const f = state.followup;
  const locked = state.status === "running";
  useEffect(() => { if (seed) setText(seed); }, [seed]);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [f.messages.length, f.proposal?.status, f.messages[f.messages.length - 1]?.text]);
  const submit = () => { const t = text.trim(); if (!t || locked || f.running) return; onSend(t); setText(""); };
  return (
    <div className="chat">
      <div className="chat-log">
        {f.messages.length === 0 && (
          <div className="empty">
            {locked ? "Chat opens when the review ends. During the review only Stop is available." : "Ask about a finding, or click a part or track on the board and choose Ask about this."}
            {!locked && <div className="chat-sug">{SUGGEST.map((s) => (<button key={s} className="btn sm ghost" onClick={() => setText(s)}>{s}</button>))}</div>}
          </div>
        )}
        {f.messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            <span className="idx">{m.role === "user" ? "YOU" : `AGENT${f.level ? " / " + f.level.toUpperCase() : ""}`}</span>
            <p>{m.text}</p>
            {m.selected && m.selected.length > 0 && <div className="chat-chips">{m.selected.map((s) => (<span className="stamp" key={s}>{s}</span>))}</div>}
          </div>
        ))}
        {f.proposal && (
          <div className="proposal hair">
            <span className="idx">PROPOSED TEST / REQUIRES APPROVAL</span>
            <p><b>{f.proposal.what}</b></p>
            <p className="dim">{f.proposal.why}. About <span className="num">{Math.round(f.proposal.eta_s / 60)}</span> min of solver time.</p>
            {f.proposal.status === "pending" ? (
              <div className="proposal-act"><button className="btn sm primary" onClick={() => onDecide(f.proposal!.request_id, true)}>Run</button><button className="btn sm" onClick={() => onDecide(f.proposal!.request_id, false)}>Not now</button></div>
            ) : (<span className="stamp">{f.proposal.status}</span>)}
          </div>
        )}
        {f.whatif && <div className="stamp" style={{ alignSelf: "flex-start" }}>What-if, not your board: {f.whatif.ref} ghosted on the board</div>}
        <div ref={end} />
      </div>
      <div className="chat-in">
        {selected.length > 0 && (
          <div className="chat-chips">{selected.map((s) => (<button key={s} className="stamp sel" onClick={() => onClearSel(s)} title="Remove">{s.startsWith("F") || s.startsWith("f-") ? findingLabel(s) : s} <Icon name="close" size={8} /></button>))}</div>
        )}
        <div className="chat-box">
          <textarea
            value={text} rows={2} placeholder={locked ? "Chat is off while the review runs" : "Ask, challenge, or add a fact"} disabled={locked}
            onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
            aria-label="Follow-up message"
          />
          <button className="btn primary sm" onClick={submit} disabled={locked || f.running || !text.trim()} aria-label="Send"><Icon name="send" size={12} /></button>
        </div>
        <div className="idx chat-foot">{busy || f.running ? <span className="blink">RUNNING</span> : "ANSWER / CHECK / TEST. TEST NEEDS YOUR APPROVAL."}</div>
      </div>
    </div>
  );
}
