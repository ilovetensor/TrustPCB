"use client";
import "./extent.css";
import Link from "next/link";
import { costLine, costText, everythingExtent, extentCost, layoutOnly, sameExtent, type ExtentId, type ExtentOption } from "../../lib/extent.mjs";
import { Icon } from "../ui";

/** What the review checks: a tile per check with one line, its rough time, and why it is off when it cannot run. */
export function ExtentPicker({ options, value, onChange, disabled = false }: { options: ExtentOption[]; value: ExtentId[]; onChange: (v: ExtentId[]) => void; disabled?: boolean }) {
  const all = everythingExtent(options);
  const toggle = (o: ExtentOption) => {
    if (o.always || !o.available || disabled) return;
    onChange(value.includes(o.id) ? value.filter((x) => x !== o.id) : [...value, o.id]);
  };
  const cost = extentCost(value);
  const names = options.filter((o) => value.includes(o.id)).map((o) => o.label.toLowerCase());
  return (
    <div className="ex-pick" data-testid="extent-picker" role="group" aria-label="What the review checks">
      <div className="ex-pick-head">
        <span className="idx">What to check</span>
        <div className="seg" role="group" aria-label="Presets">
          <button type="button" aria-pressed={sameExtent(value, all)} disabled={disabled} onClick={() => onChange(all)} data-testid="extent-everything"><Icon name="layers" size={13} /> Everything</button>
          <button type="button" aria-pressed={sameExtent(value, layoutOnly())} disabled={disabled} onClick={() => onChange(layoutOnly())} data-testid="extent-layout-only"><Icon name="board" size={13} /> Layout only</button>
        </div>
      </div>
      <div className="ex-tiles">
        {options.map((o) => {
          const on = value.includes(o.id);
          const off = !o.available && !o.always;
          const body = (
            <>
              <span className="ex-tile-top">
                <span className="ex-ic"><Icon name={o.icon} size={15} /></span>
                <b>{o.label}</b>
                <span className="grow" />
                {o.always ? <span className="ex-state">Always on</span> : on ? <span className="ex-state on"><Icon name="check" size={12} /> On</span> : null}
              </span>
              <span className="ex-line">
                {off
                  ? <>{o.reason === "Add keys in Settings" ? <><Link href="/settings" className="ex-link">Add keys in Settings</Link></> : o.reason}</>
                  : o.line}
              </span>
              <span className="ex-cost num">{off ? "" : costLine(o.cost ? o.cost : null)}</span>
            </>
          );
          return off || o.always ? (
            <div key={o.id} className={`ex-tile ${on ? "on" : ""} ${off ? "off" : "fixed"}`} data-testid={`extent-${o.id}`} data-state={off ? "off" : "on"} aria-disabled={off || undefined}>{body}</div>
          ) : (
            <button key={o.id} type="button" className={`ex-tile ${on ? "on" : ""}`} aria-pressed={on} disabled={disabled} onClick={() => toggle(o)} data-testid={`extent-${o.id}`}>{body}</button>
          );
        })}
      </div>
      <p className="ex-sum" data-testid="extent-summary">
        {names.length ? `${names.slice(0, -1).join(", ")}${names.length > 1 ? " and " : ""}${names[names.length - 1]}.` : ""}{" "}
        <span className="dim">{cost ? `Adds ${costText(cost)} to the review.` : "Adds no time to the review."}</span>
      </p>
    </div>
  );
}
