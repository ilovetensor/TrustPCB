"use client";
import "./power.css";
import { useEffect, useState } from "react";
import { fmtPct } from "../../lib/sims";

/** The one sentence that goes with every accepted-power figure. */
export const ACCEPTED_LABEL = "Accepted power (mismatch only, not radiated power or efficiency)";
export const ACCEPTED_HOW = "100 x (1 - |S11|^2) at each frequency, from the solver's S11. It says how much of the source power gets past the mismatch. Loss in the board and the parts is not in it.";

/** True once the first frame is painted, so a bar can grow from 0 to its real value. With reduced motion it is true at once and the CSS has no transition. */
function useArmed() {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => requestAnimationFrame(() => setArmed(true)));
    return () => cancelAnimationFrame(id);
  }, []);
  return armed;
}

export interface PowerValue { value: number | null | undefined; label: string; sub?: string }

/** One horizontal bar: a real percentage (0 to 100) drawn as the filled share of the track. The number is the real value; only the fill moves. */
export function PowerBar({ value, label, sub, tone = "now", size = "md", delay = 0 }: PowerValue & { tone?: "before" | "now"; size?: "sm" | "md" | "lg"; delay?: number }) {
  const armed = useArmed();
  const ok = typeof value === "number" && Number.isFinite(value);
  const pct = ok ? Math.max(0, Math.min(100, value as number)) : 0;
  return (
    <div className={`pw-row ${tone} ${size}`}>
      <div className="pw-head"><span className="idx">{label}</span>{sub && <span className="idx pw-sub">{sub}</span>}</div>
      <div className="pw-line">
        <div className="pw-track" role="img" aria-label={ok ? `${label}: ${fmtPct(value)} percent accepted` : `${label}: not available`}>
          <b style={{ width: armed ? `${pct}%` : "0%", transitionDelay: `${delay}ms` }} />
        </div>
        <span className="pw-num mono">{ok ? <>{fmtPct(value)}<small>%</small></> : <small>n/a</small>}</span>
      </div>
    </div>
  );
}

/** Before and after: two bars on the same 0 to 100 % scale, with the change in percentage points between them. */
export function PowerPair({ before, after, caption, size = "lg", note = true }: { before: PowerValue; after: PowerValue; caption?: string; size?: "sm" | "md" | "lg"; note?: boolean }) {
  const both = typeof before.value === "number" && typeof after.value === "number";
  const delta = both ? (after.value as number) - (before.value as number) : null;
  return (
    <figure className={`pw ${size}`}>
      <PowerBar {...before} tone="before" size={size} />
      <div className="pw-delta mono" aria-hidden={delta == null}>
        {delta != null && <><svg width="12" height="14" viewBox="0 0 12 14" aria-hidden><path d="M6 1v11M2 8l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" /></svg><span>{delta >= 0 ? "+" : "−"}{fmtPct(Math.abs(delta))} points</span></>}
      </div>
      <PowerBar {...after} tone="now" size={size} delay={350} />
      <figcaption className="pw-cap">
        <span className="idx">{ACCEPTED_LABEL}</span>
        {caption && <span className="pw-cap2">{caption}</span>}
        {note && <span className="pw-cap2">{ACCEPTED_HOW}</span>}
      </figcaption>
    </figure>
  );
}
