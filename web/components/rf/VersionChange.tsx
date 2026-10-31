"use client";
import "./power.css";
import type { VersionChange as VC } from "../../lib/story.mjs";
import { acceptedPct } from "../../lib/sims";
import { PowerPair } from "./PowerPair";

const db = (v: number | null | undefined) => (typeof v === "number" ? `${v.toFixed(2).replace(/\.?0+$/, "").replace("-", "−")} dB` : "-");
const TONE = { improved: "Improved", worse: "Worse", unchanged: "No change" } as const;

/**
 * The badge that says how this version's number relates to the earlier review's prediction. With the copper unchanged the number is the same solver run
 * finalised with this version's parts (not an independent result), so it is "consistent" or not; "held" is only for a separate confirm run.
 */
export function HeldBadge({ vc }: { vc: VC }) {
  if (!vc.prediction_from_previous) return null;
  if (vc.applied === false) return <span className="stamp" title="The parts the earlier review recommended are not the ones this version has.">Prediction not tested</span>;
  if (vc.independent_check) {
    return vc.held
      ? <span className="stamp solid" title={`A separate confirm run is within ${vc.hold_tolerance_db} dB of the earlier prediction`}>Prediction held</span>
      : <span className="stamp sig" title={`A separate confirm run is more than ${vc.hold_tolerance_db} dB from the earlier prediction`}>Prediction did not hold</span>;
  }
  return vc.consistent
    ? <span className="stamp solid" title={`Consistent with the prediction (within ${vc.hold_tolerance_db} dB): the same solver run, recalculated with this version's parts. Not an independent result. Simulated.`}>Same solver run, new parts</span>
    : <span className="stamp sig" title={`More than ${vc.hold_tolerance_db} dB from the prediction although it is the same solver run recalculated with this version's parts. Simulated.`}>Same solver run, not consistent</span>;
}

/**
 * What changed since the previous version, as built: the accepted power before and after on one scale, the worst S11, whether it is consistent with the earlier prediction.
 * Every figure comes from the server's `version_change`, which reads stored confirm-quality results.
 */
export function VersionChange({ vc, compact = false, wide = false, onFinding }: { vc: VC; compact?: boolean; wide?: boolean; onFinding?: (code: string) => void }) {
  const a = vc.from_version, b = vc.to_version;
  const worst = (s: VC["from"]) => s.accepted_power_worst_pct ?? acceptedPct(s.band_worst_s11_db);
  const p = vc.prediction_from_previous;
  const finding = vc.finding ?? null;     // the finding of THIS version that cites the confirmed base (the prediction's own finding is a code of the older review)
  return (
    <section className={`pwin vchange ${compact ? "compact" : ""} ${wide ? "wide" : ""}`} aria-label={`Compared with version ${a}`}>
      <header className="pwin-head">
        <span className="stamp solid pwin-badge">Compared with version {a}</span>
        <span className="stamp">{TONE[vc.direction]}</span>
        <HeldBadge vc={vc} />
        <span className={`stamp ${vc.target_met ? "solid" : ""}`}>{vc.target_met ? "Target met" : "Target not met"}</span>
        {finding && onFinding && <button className="idx pwin-find" onClick={() => onFinding(finding)}>{finding}</button>}
      </header>
      <div className="pwin-body">
        <PowerPair
          size={compact ? "md" : wide ? "md" : "lg"} note={!compact && !wide}
          before={{ value: worst(vc.from), label: `Version ${a}, as built`, sub: "worst point of the band" }}
          after={{ value: worst(vc.to), label: `Version ${b}, as built`, sub: "worst point of the band" }}
        />
        <dl className="pwin-facts">
          <div>
            <dt className="idx">WORST S11 ACROSS THE BAND</dt>
            <dd className="mono">{db(vc.from.band_worst_s11_db)} <span className="pwin-arrow">to</span> {db(vc.to.band_worst_s11_db)}
              <span className="pwin-gain"> {vc.direction === "unchanged" ? "no change beyond the solver's repeat" : `${vc.direction === "improved" ? "a gain" : "a loss"} of ${Math.abs(vc.gain_db).toFixed(2).replace(/\.?0+$/, "")} dB`}</span></dd>
          </div>
          {vc.from.accepted_power_mean_pct != null && vc.to.accepted_power_mean_pct != null && (
            <div><dt className="idx">ACCEPTED POWER, MEAN ACROSS THE BAND</dt><dd className="mono">{vc.from.accepted_power_mean_pct} % <span className="pwin-arrow">to</span> {vc.to.accepted_power_mean_pct} %</dd></div>
          )}
          {p && (
            <div>
              <dt className="idx">PREDICTED BY VERSION {a}</dt>
              <dd className="mono">{db(p.band_worst_s11_db)} for {p.parts_text || "no change to the board"}<br />
                {vc.applied === false ? "these parts are not what this version has" : <>{vc.independent_check ? "separate confirm run" : "same solver run, version " + b + "'s parts"}: {db(vc.to.band_worst_s11_db)}{vc.difference_db != null && <span className="pwin-gain"> ({vc.difference_db === 0 ? "equal to the prediction" : `${vc.difference_db > 0 ? "+" : "−"}${Math.abs(vc.difference_db).toFixed(2).replace(/\.?0+$/, "")} dB from the prediction`})</span>}</>}
              </dd>
            </div>
          )}
          <div><dt className="idx">TARGET</dt><dd className="mono">{db(vc.target_db)} across the band <b>{vc.target_met ? "met" : "not met"}</b></dd></div>
        </dl>
      </div>
    </section>
  );
}
