"use client";
import { useEffect, useRef, useState } from "react";
import { useAppEnv } from "../../lib/demo";
import type { ReviewState } from "../../lib/review-reducer.mjs";
import { Icon } from "../ui";
import { AgentMark } from "../ui/AgentMark";

export interface SelectedChip { id: string; label: string; type: "finding" | "object" }

export function ChatDock({ state, runner, selected, onClearSel, onSend, onDecide, busy, seed, error, suggest }: {
  state: ReviewState; /** the runner that wrote this review and its answers */ runner?: string | null; selected: SelectedChip[]; onClearSel: (id: string) => void; onSend: (t: string) => void;
  onDecide: (id: string, ok: boolean) => void; busy: boolean; seed?: string; error?: string | null; suggest: string[];
}) {
  const { demo, repoUrl } = useAppEnv();          // the read-only demo refuses chat: it shows the recorded conversation and no composer
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
            {demo ? "No conversation was recorded for this review." : locked ? "Chat opens when the review ends. While it runs only Stop is available." : "Ask about a finding, or click a part, pin or track on the board and choose Ask about this."}
            {!demo && !locked && suggest.length > 0 && <div className="chat-sug">{suggest.map((s) => (<button key={s} className="btn sm ghost" onClick={() => setText(s)}>{s}</button>))}</div>}
          </div>
        )}
        {f.messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}${m.narration ? " narration" : ""}`}>
            <span className="idx am-row">
              {m.role !== "user" && !m.error && <AgentMark runner={runner} kind={m.narration ? "wrote" : "answered"} />}
              {m.role === "user" ? "You" : m.narration ? "Working" : `Agent${f.level ? `, ${f.level}` : ""}`}
            </span>
            <p style={m.error ? { color: "var(--sev-high)" } : { whiteSpace: "pre-wrap" }}>{m.text}</p>
            {m.selected && m.selected.length > 0 && <div className="chat-chips">{m.selected.map((s) => (<span className="stamp" key={s}>{s}</span>))}</div>}
          </div>
        ))}
        {f.proposal && (
          <div className="proposal">
            <span className="idx am-row"><AgentMark runner={runner} kind="wrote" />Proposed test, needs your approval</span>
            <p><b>{f.proposal.what}</b></p>
            <p className="dim">{f.proposal.why}{f.proposal.eta_s ? <>. About <span className="num">{Math.max(1, Math.round(f.proposal.eta_s / 60))}</span> min of solver time.</> : null}</p>
            {f.proposal.status === "pending" && f.proposal.request_id ? (
              <div className="proposal-act"><button className="btn sm primary" onClick={() => onDecide(f.proposal!.request_id!, true)}><Icon name="play" size={11} /> Run</button><button className="btn sm" onClick={() => onDecide(f.proposal!.request_id!, false)}><Icon name="close" size={11} /> Not now</button></div>
            ) : (<span className="stamp">{f.proposal.status === "pending" ? "Waiting" : f.proposal.status}</span>)}
          </div>
        )}
        {others.map((r) => (
          <div key={r.request_id} className="proposal">
            <span className="idx am-row">{r.kind !== "permission" && <AgentMark runner={runner} kind="wrote" />}{r.kind === "permission" ? "Permission required" : "The agent asks"}</span>
            <p><b>{String(r.payload.title ?? r.payload.tool_name ?? r.payload.question ?? r.payload.message ?? r.kind)}</b></p>
            {r.payload.description || r.payload.why ? <p className="dim">{String(r.payload.description ?? r.payload.why)}</p> : null}
            {r.kind === "permission" ? (
              <div className="proposal-act"><button className="btn sm primary" onClick={() => onDecide(r.request_id, true)}><Icon name="check" size={12} /> Allow</button><button className="btn sm" onClick={() => onDecide(r.request_id, false)}><Icon name="close" size={11} /> Deny</button></div>
            ) : <span className="stamp">Answer from a connected client</span>}
          </div>
        ))}
        {f.whatif && (
          <div className="whatif">
            <span className="stamp">{f.whatif.label ?? "What-if, not your board"}</span>
            <p><b>{f.whatif.ref}</b> is drawn as a ghost on the board{Array.isArray(f.whatif.new_position) ? <> at <span>{f.whatif.new_position.map((v: number) => v.toFixed(1)).join(", ")}</span> mm</> : null}. Your design is not changed.</p>
            {Array.isArray(f.whatif.distances) && f.whatif.distances.length > 0 && (
              <ul className="whatif-d">{(f.whatif.distances as { to: string; distance_mm: number; was_mm?: number }[]).slice(0, 4).map((d) => (<li key={d.to}><span>{d.to.replace(/^ref:/, "")}</span><b>{d.distance_mm} mm</b>{d.was_mm != null && <span className="dim">was {d.was_mm}</span>}</li>))}</ul>
            )}
            {Array.isArray(f.whatif.conflicts) && f.whatif.conflicts.length > 0 && <p className="whatif-c">{f.whatif.conflicts.length} conflict{f.whatif.conflicts.length > 1 ? "s" : ""}: {f.whatif.conflicts.slice(0, 3).map((c: any) => (typeof c === "string" ? c : c.text ?? c.kind ?? JSON.stringify(c))).join("; ")}</p>}
            <p className="dim">RF effect of a moved part: Not tested.</p>
          </div>
        )}
        {error && <p className="chat-err">{error}</p>}
        <div ref={end} />
      </div>
      {demo ? (
        <div className="chat-off" role="note">
          <Icon name="info" size={15} />
          <p><b>Chat is off in the read-only demo.</b> To ask about a finding or a part, run TrustPCB locally on your own board.{repoUrl && <> <a href={repoUrl} target="_blank" rel="noreferrer" className="signal">See the repository</a>.</>}</p>
        </div>
      ) : (
        <div className="chat-in">
          {selected.length > 0 && (
            <div className="chat-chips">{selected.map((s) => (<button key={s.id} className="stamp sel" onClick={() => onClearSel(s.id)} title="Remove">{s.label} <Icon name="close" size={9} /></button>))}</div>
          )}
          <div className="chat-box">
            <textarea
              value={text} rows={2} placeholder={locked ? "Chat is off while the review runs" : "Ask, challenge, or add a fact"} disabled={locked}
              onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
              aria-label="Follow-up message"
            />
            <button className="btn primary" onClick={submit} disabled={locked || f.running || busy || !text.trim()}><Icon name="send" size={14} /> Send</button>
          </div>
          <div className="idx chat-foot">{locked ? "Chat opens when the review finishes." : waiting ? <span className="am-row"><AgentMark runner={runner} kind="working" />Waiting for your approval</span> : busy || f.running ? <span className="am-row"><AgentMark runner={runner} kind="working" /><span className="blink">Running</span></span> : "Answer, check or test. A test needs your approval."}</div>
        </div>
      )}
    </div>
  );
}
