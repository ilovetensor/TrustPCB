"use client";
import "./power.css";
import type { BestCandidate, Headline } from "../../lib/story.mjs";
import { acceptedPct } from "../../lib/sims";
import { PowerPair } from "./PowerPair";

export const PARTIAL_BADGE = "Improved, target not met";

/** Accepted power at the worst point of the band: the stored figure, else the exact value of the stored worst S11 (the worst S11 is the worst accepted power). */
const worstOf = (c: BestCandidate) => c.accepted_power_worst_pct ?? acceptedPct(c.band_worst_s11_db);
const db = (v: number | null | undefined) => (typeof v === "number" ? `${v.toFixed(1).replace("-", "−")} dB` : "-");

/**
 * The partial win: the best confirm-quality parts improved the band but did not reach the target. Every number is from the stored comparison.
 * `compact` drops the explanation, for a narrow column.
 */
export function PartialWin({ h, compact = false, wide = false, onFinding }: { h: Headline; compact?: boolean; wide?: boolean; onFinding?: (code: string) => void }) {
  const b = worstOf(h.base), a = worstOf(h.best);
  return (
    <section className={`pwin ${compact ? "compact" : ""} ${wide ? "wide" : ""}`} aria-label={PARTIAL_BADGE}>
      <header className="pwin-head">
        <span className="stamp solid pwin-badge">{PARTIAL_BADGE}</span>
        <span className="idx">CONFIRM QUALITY / SIMULATED</span>
        {h.finding && (onFinding ? <button className="idx pwin-find" onClick={() => onFinding(h.finding!)}>{h.finding}</button> : <span className="idx">{h.finding}</span>)}
      </header>
      <div className="pwin-body">
      <PowerPair
        size={compact ? "md" : wide ? "md" : "lg"} note={!compact && !wide}
        before={{ value: b, label: "Confirmed base", sub: "worst point of the band" }}
        after={{ value: a, label: "Best parts", sub: h.best.ref ? `${h.best.ref}, worst point of the band` : "worst point of the band" }}
      />
      <dl className="pwin-facts">
        <div><dt className="idx">PARTS TO TRY, IN YOUR OWN TOOL</dt><dd className="mono">{h.partsText || "no change to the board"}</dd></div>
        <div>
          <dt className="idx">WORST S11 IN THE BAND</dt>
          <dd className="mono">{db(h.base.band_worst_s11_db)} <span className="pwin-arrow">to</span> {db(h.best.band_worst_s11_db)}{h.gainDb != null && <span className="pwin-gain"> a gain of {h.gainDb.toFixed(1)} dB</span>}</dd>
        </div>
        <div><dt className="idx">TARGET</dt><dd className="mono">{h.targetDb != null ? `${db(h.targetDb)} across the band` : "the tuning target"} <b>not met</b></dd></div>
        {h.base.accepted_power_mean_pct != null && h.best.accepted_power_mean_pct != null && (
          <div><dt className="idx">MEAN ACROSS THE BAND</dt><dd className="mono">{h.base.accepted_power_mean_pct} % to {h.best.accepted_power_mean_pct} %</dd></div>
        )}
      </dl>
      </div>
    </section>
  );
}
