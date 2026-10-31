"use client";
import { useEffect, useState } from "react";
import { limitShare, panelModel, seriesOf, SOURCE_HINT, SOURCE_LABEL, trustOf, usesDefault, type BoardSimRun, type PanelRow, type SimAssumption, type SimResultJson } from "../../lib/board-sim-model.mjs";
import { fmtNum, fmtVal } from "../../lib/overlay-math.mjs";
import { Icon } from "../ui";
import { LineChart, type Line } from "./LineChart";

/* ------------------------------ sources and assumptions ------------------------------ */

export function SourceBadge({ source, cite }: { source: SimAssumption["source"]; cite?: string | null }) {
  return (
    <span className={`sl-src ${source}`} title={`${SOURCE_HINT[source] ?? ""}${cite ? ` ${cite}` : ""}`.trim()}>
      {source === "default" ? <i aria-hidden /> : source === "datasheet" ? <Icon name="file" size={10} /> : null}
      {SOURCE_LABEL[source] ?? source}
    </span>
  );
}

const valueText = (a: SimAssumption) => {
  if (typeof a.value !== "number") return `${a.value}${a.unit ? ` ${a.unit}` : ""}`;
  if ((a.unit === "A" || a.unit === "W") && Math.abs(a.value) < 1) return fmtVal(a.value * 1000, a.unit === "A" ? "mA" : "mW");
  return fmtVal(a.value, a.unit);
};
function AssumptionRow({ a }: { a: SimAssumption }) {
  return (
    <li className={`sl-as ${a.source}`}>
      <span className="sl-as-name">{a.name}</span>
      <b className="sl-as-val num">{valueText(a)}</b>
      <SourceBadge source={a.source} cite={a.cite} />
      {a.cite && <span className="sl-as-cite dim">{a.cite}</span>}
    </li>
  );
}

/** Every input of the run, always on screen: defaults first, marked, then the inputs that came from a datasheet, from you or from the board. */
export function AssumptionList({ assumptions }: { assumptions: SimAssumption[] }) {
  const defaults = assumptions.filter((a) => a.source === "default");
  const known = assumptions.filter((a) => a.source !== "default");
  if (!assumptions.length) return <p className="dim">This run lists no inputs.</p>;
  return (
    <section className="sl-assume" aria-label="Assumptions">
      {defaults.length > 0 && (
        <>
          <div className="sl-as-head"><h3>Defaults in this result</h3><span className="sl-def"><i aria-hidden />{defaults.length}</span></div>
          <p className="dim sl-as-lead">Typical values used because nothing better was known. Numbers that depend on them are marked. Replace them to make the result yours.</p>
          <ul className="sl-as-list">{defaults.map((a) => (<AssumptionRow key={a.name} a={a} />))}</ul>
        </>
      )}
      {known.length > 0 && (
        <>
          <div className="sl-as-head"><h3>{defaults.length ? "From the datasheet, you and the board" : "Inputs"}</h3><span className="idx num">{known.length}</span></div>
          <ul className="sl-as-list">{known.map((a) => (<AssumptionRow key={a.name} a={a} />))}</ul>
        </>
      )}
    </section>
  );
}

/* ------------------------------ trust ------------------------------ */

export function TrustBlock({ result }: { result: SimResultJson }) {
  const t = trustOf(result);
  return (
    <section className={`sl-trust ${t.ok ? "ok" : "bad"}`} aria-label="Result checks">
      <div className="sl-trust-head">
        <span className={`stamp ${t.ok ? "" : "sig"}`}>{t.ok ? "Checks passed" : "Failed its checks"}</span>
        <span className="idx num">Solved in {fmtNum(result.solve_s)} s</span>
      </div>
      {!t.ok && <p className="sl-trust-warn">This result failed a check, so it cannot support a finding. Read it as a hint only.</p>}
      {t.checks.length > 0 && (
        <ul className="sl-checks">
          {t.checks.map((c) => (
            <li key={c.check} className={c.ok ? "" : "bad"}>
              <span className="sl-check-ic"><Icon name={c.ok ? "check" : "close"} size={11} /></span>
              <span>{c.check}{c.detail && <small className="dim"> {c.detail}</small>}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/* ------------------------------ numbers ------------------------------ */

/** The row's bar grows from empty to its value once painted. With reduced motion the CSS has no transition. */
function useArmed(key: string) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    setArmed(false);
    const id = requestAnimationFrame(() => requestAnimationFrame(() => setArmed(true)));
    return () => cancelAnimationFrame(id);
  }, [key]);
  return armed;
}

/** A bar on a 0 to max scale with quarter ticks. `before` draws the earlier value as a hatched bar behind; `limit` is a notch. */
export function QBar({ value, max, before = null, limit = null, label, armKey = "" }: { value: number; max: number; before?: number | null; limit?: number | null; label: string; armKey?: string }) {
  const armed = useArmed(armKey);
  const pc = (v: number) => `${Math.max(0, Math.min(100, (v / (max || 1)) * 100))}%`;
  return (
    <div className="sl-bar" role="img" aria-label={label}>
      {before != null && <i className="sl-bar-before" style={{ width: armed ? pc(before) : "0%" }} />}
      <b style={{ width: armed ? pc(value) : "0%" }} />
      {limit != null && limit <= max && <u style={{ left: pc(limit) }} title="Limit" />}
    </div>
  );
}

interface Tile { key: string; label: string; value: number | null; unit: string; was?: number | null }
const delta = (a: number, b: number) => b - a;
export function Stats({ tiles, defaults, paired }: { tiles: Tile[]; defaults: number; paired: boolean }) {
  if (!tiles.length) return null;
  return (
    <section className="sl-stats" aria-label="Key numbers">
      {tiles.map((t, i) => (
        <div key={t.key} className={`sl-stat ${i === 0 ? "hero" : ""}`}>
          <span className="idx">{t.label}{defaults > 0 && <i className="sl-dot" title="Depends on default assumptions" aria-label="depends on defaults" />}</span>
          <b className="num">{t.value == null ? "n/a" : fmtNum(t.value)}<small>{t.value == null ? "" : ` ${t.unit}`}</small></b>
          {paired && t.was != null && t.value != null && (
            <span className="sl-was num">{fmtNum(delta(t.was, t.value)) === "0" ? `was ${fmtVal(t.was, t.unit)}, no change` : `was ${fmtVal(t.was, t.unit)}, ${delta(t.was, t.value) < 0 ? "\u2212" : "+"}${fmtNum(Math.abs(delta(t.was, t.value)))} ${t.unit}`}</span>
          )}
        </div>
      ))}
    </section>
  );
}

/** The figures of a run as tiles, with the earlier run's value beside each when a before and after exists. */
export function tilesOf(result: SimResultJson, before: SimResultJson | null, max = 5): Tile[] {
  return panelModel(result).headline.slice(0, max).map((m) => ({ key: m.key, label: m.label, value: m.value, unit: m.unit, was: before?.metrics?.[m.key]?.value ?? null }));
}

/* ------------------------------ per kind ------------------------------ */

const DefaultTag = () => <span className="sl-tag" title={SOURCE_HINT.default}>default</span>;
const byValueDesc = (a: PanelRow, b: PanelRow) => (b.value ?? -Infinity) - (a.value ?? -Infinity);

export function PowerPanel({ run, before }: { run: BoardSimRun; before: SimResultJson | null }) {
  const r = run.result!;
  const rows = panelModel(r).load.slice().sort(byValueDesc);
  const max = Math.max(...rows.map((x) => x.value ?? 0), 1e-9);
  if (!rows.length) return null;
  return (
    <section className="sl-sec" aria-label="Loads">
      <div className="sl-sec-head"><h3>Loads, worst drop first</h3><span className="idx">drop from the source</span></div>
      <ul className="sl-rows">
        {rows.map((x, i) => {
          const was = before?.metrics?.[x.key]?.value ?? null;
          const share = limitShare(x.value ?? NaN, x.limit);
          return (
            <li key={x.key} className={`sl-row ${i === 0 ? "worst" : ""}`}>
              <div className="sl-row-top"><span className="sl-ref">{x.ref}</span><span className="sl-name">{x.label}</span><b className="num sl-big">{fmtNum(x.value)}<small> {x.unit}</small></b></div>
              <QBar value={x.value ?? 0} max={max} before={was} label={`${x.label}: ${fmtVal(x.value, x.unit)} drop`} armKey={run.run_id} />
              <div className="sl-row-sub num">
                {x.current != null && <span>{fmtVal(x.current < 1 ? x.current * 1000 : x.current, x.current < 1 ? "mA" : "A")}{usesDefault(x.ref, r.assumptions) && <DefaultTag />}</span>}
                {share != null && <span>{fmtNum(share)}% of the {fmtVal(x.limit, x.unit)} allowed</span>}
                {was != null && x.value != null && <span>was {fmtVal(was, x.unit)}</span>}
                {i === 0 && <span className="sl-worst">Worst</span>}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function HeatPanel({ run, before }: { run: BoardSimRun; before: SimResultJson | null }) {
  const r = run.result!;
  const rows = panelModel(r).part.slice().sort(byValueDesc);
  if (!rows.length) return null;
  const air = Number(r.assumptions.find((a) => /air temperature/i.test(a.name))?.value);
  const base = Number.isFinite(air) ? air : 0;
  const top = Math.max(...rows.map((x) => Math.max(x.value ?? 0, x.limit ?? 0)));
  return (
    <section className="sl-sec" aria-label="Hottest parts">
      <div className="sl-sec-head"><h3>Hottest parts</h3><span className="idx">bars rise from {Number.isFinite(air) ? `${fmtNum(air)}°C air` : "zero"}</span></div>
      <ul className="sl-rows">
        {rows.map((x, i) => {
          const was = before?.metrics?.[x.key]?.value ?? null;
          const over = x.limit != null && x.value != null && x.value > x.limit;
          return (
            <li key={x.key} className={`sl-row ${i === 0 ? "worst" : ""}`}>
              <div className="sl-row-top"><span className="sl-ref">{x.ref}</span><span className="sl-name">{x.label}</span><b className={`num sl-big ${over ? "over" : ""}`}>{fmtNum(x.value)}<small>{x.unit}</small></b></div>
              <QBar value={(x.value ?? base) - base} max={top - base} before={was != null ? was - base : null} limit={x.limit != null ? x.limit - base : null} label={`${x.label}: ${fmtVal(x.value, x.unit)}`} armKey={run.run_id} />
              <div className="sl-row-sub num">
                {x.power != null && <span>{fmtVal(x.power * 1000, "mW")}{usesDefault(x.ref, r.assumptions) && <DefaultTag />}</span>}
                {x.limit != null && <span className={over ? "over" : ""}>{over ? `Over the ${fmtNum(x.limit)}${x.unit} limit` : `${fmtNum(x.limit - (x.value ?? 0))} K under the ${fmtNum(x.limit)}${x.unit} limit`}</span>}
                {was != null && x.value != null && <span>was {fmtVal(was, x.unit)}</span>}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function SignalPanel({ run, before }: { run: BoardSimRun; before: BoardSimRun | null }) {
  const r = run.result!;
  const mine = seriesOf(r), theirs = before?.result ? seriesOf(before.result) : [];
  const prof = mine.find((s) => s.id === "profile") ?? mine[0];
  const tdr = mine.find((s) => s.id === "tdr") ?? mine[1];
  const mk = (cur: typeof prof | undefined, was: typeof prof | undefined): Line[] => {
    if (!cur) return [];
    const out: Line[] = [];
    if (was) out.push({ id: "was", label: "before", x: was.x, y: was.y, color: "var(--paper-3)", dashed: true, width: 1.3 });
    out.push({ id: "now", label: before ? "after" : "", x: cur.x, y: cur.y, color: "var(--paper)" });
    return out;
  };
  const segs = panelModel(r).segment;
  return (
    <>
      {prof && (
        <section className="sl-sec" aria-label="Impedance along the net">
          <div className="sl-sec-head"><h3>Impedance along the net</h3><span className="idx">from the source pad</span></div>
          <LineChart lines={mk(prof, theirs.find((s) => s.id === prof.id))} xUnit={prof.x_unit} yUnit={prof.y_unit} xLabel="Distance" yLabel="Z" ariaLabel="Impedance along the net" target={prof.target != null ? { y: prof.target, tolPct: prof.tolerance_pct } : null} />
        </section>
      )}
      {tdr && (
        <section className="sl-sec" aria-label="Time domain reflection">
          <div className="sl-sec-head"><h3>Time domain reflection</h3><span className="idx">what a TDR scope would see</span></div>
          <LineChart lines={mk(tdr, theirs.find((s) => s.id === tdr.id))} xUnit={tdr.x_unit} yUnit={tdr.y_unit} xLabel="Time" yLabel="Z" ariaLabel="Time domain reflection" target={tdr.target != null ? { y: tdr.target, tolPct: tdr.tolerance_pct } : null} />
        </section>
      )}
      {segs.length > 0 && (
        <section className="sl-sec" aria-label="Sections of the net">
          <div className="sl-sec-head"><h3>Sections</h3><span className="idx">deviation from {prof?.target != null ? `${fmtNum(prof.target)} ${prof.y_unit}` : "the target"}</span></div>
          <ul className="sl-rows tight">
            {segs.map((s) => {
              const out = s.limit != null && s.deviation != null && Math.abs(s.deviation) > s.limit;
              const was = before?.result?.metrics?.[s.key];
              return (
                <li key={s.key} className="sl-row">
                  <div className="sl-row-top"><span className="sl-name">{s.label}</span><b className="num sl-big">{fmtNum(s.value)}<small> {s.unit}</small></b></div>
                  <div className="sl-row-sub num">
                    {s.deviation != null && <span className={out ? "over" : ""}>{s.deviation > 0 ? "+" : s.deviation < 0 ? "−" : ""}{fmtNum(Math.abs(s.deviation))}%{out ? `, outside ${fmtNum(s.limit)}%` : `, within ${fmtNum(s.limit)}%`}</span>}
                    {was?.value != null && s.value != null && <span>was {fmtVal(was.value, s.unit)}</span>}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </>
  );
}
