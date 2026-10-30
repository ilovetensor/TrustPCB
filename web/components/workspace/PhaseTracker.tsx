"use client";
import { PHASES } from "../../lib/review-reducer.mjs";

/** Phase tracker drawn as a dimension line: ticks at each phase, the live one ringed in signal. */
export function PhaseTracker({ phase, status, compact }: { phase: number; status: string; compact?: boolean }) {
  const n = PHASES.length;
  const done = status === "complete";
  return (
    <div className="pt" role="group" aria-label={`Phase ${Math.max(phase, 0)} of ${n - 1}`}>
      <div className="pt-top">
        <span className="idx">PHASE {String(Math.max(phase, 0)).padStart(2, "0")}/{String(n - 1).padStart(2, "0")}</span>
        <span className="pt-now">{phase >= 0 ? PHASES[phase].label : "Waiting"}</span>
        <span className="idx pt-actor">{phase >= 0 ? (PHASES[phase].actor === "agent" ? "MUSE AGENT" : "CODE") : ""}</span>
      </div>
      <div className="pt-line" style={{ ["--n" as string]: n }}>
        <svg className="pt-rule" width="100%" height="14" preserveAspectRatio="none" aria-hidden>
          <line x1="0" x2="100%" y1="7" y2="7" stroke="var(--hair-2)" />
        </svg>
        {PHASES.map((p) => {
          const st = done || p.id < phase ? "done" : p.id === phase ? "live" : "todo";
          return (
            <div key={p.id} className={`pt-node ${st}`} aria-current={st === "live" ? "step" : undefined}>
              <i />
              {!compact && <span className="idx">{p.short}</span>}
            </div>
          );
        })}
        <div className="pt-fill" style={{ width: `${done ? 100 : phase < 0 ? 0 : ((phase + 0.5) / n) * 100}%` }} />
      </div>
    </div>
  );
}
