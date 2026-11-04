"use client";
import "./pages.css";
import "./settings.css";
import { useEffect, useState } from "react";
import { getKeySettings, removeKey, saveKey, type KeyRow, type KeySettings } from "../../lib/api";
import { useAppEnv } from "../../lib/demo";
import { Icon, TopBar } from "../ui";

const WHY: Record<string, string> = {
  Meta: "Runs the review agent on Meta Muse Spark through Meta's own API. Set this or a Baseten key; if both are set, Meta is used.",
  Baseten: "Runs the review agent on Muse Spark Open (meta-models/muse-spark-open-1026) hosted by Baseten.",
  DigiKey: "Looks up manufacturer, description, parameters and the datasheet link for a part number. Needs both the client ID and the client secret of a DigiKey Product Information app.",
  Mouser: "A second source for the same lookups, and a fallback when DigiKey has no key.",
};
const SOURCE: Record<string, string> = { app: "Stored on this machine", env: "From your environment" };

function KeyField({ row, onChange }: { row: KeyRow; onChange: (next: KeySettings) => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fromEnv = row.source === "env";
  const run = async (job: () => Promise<KeySettings>, done: boolean) => {
    setBusy(true); setError(null); setSaved(false);
    try { const next = await job(); setValue(""); setSaved(done); onChange(next); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not save."); }
    finally { setBusy(false); }
  };
  return (
    <form className="se-row" onSubmit={(e) => { e.preventDefault(); if (value.trim()) void run(() => saveKey(row.name, value.trim()), true); }} autoComplete="off">
      <label className="se-label" htmlFor={`key-${row.name}`}>
        {row.label}
        <span className={`se-state ${row.set ? "" : "off"}`}>{row.set ? `Set. ${SOURCE[row.source ?? "app"]}` : "Not set"}</span>
      </label>
      <div className="se-controls">
        <input
          id={`key-${row.name}`} name={`k-${row.name.toLowerCase()}`} type="password" value={value} onChange={(e) => { setValue(e.target.value); setSaved(false); }}
          autoComplete="new-password" spellCheck={false} autoCapitalize="off" maxLength={512} disabled={busy || fromEnv}
          placeholder={fromEnv ? "Set in your environment" : row.set ? "Paste a new value to replace it" : "Paste the value"}
          data-1p-ignore data-lpignore="true"
        />
        <button type="submit" className="btn primary" disabled={busy || fromEnv || !value.trim()}><Icon name="check" size={14} /> {row.set ? "Replace" : "Save"}</button>
        {row.source === "app" && <button type="button" className="btn" disabled={busy} onClick={() => void run(() => removeKey(row.name), false)}><Icon name="trash" size={14} /> Remove</button>}
        {saved && <span className="se-saved" role="status"><Icon name="check" size={14} /> Saved</span>}
      </div>
      {fromEnv && <p className="se-hint">This value comes from your environment (.env or the shell) and takes priority. Change or remove it there.</p>}
      {error && <p className="se-hint" role="alert" style={{ color: "var(--sev-high)" }}>{error}</p>}
    </form>
  );
}

export function Settings() {
  const { demo } = useAppEnv();
  const [state, setState] = useState<KeySettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { getKeySettings().then(setState).catch((e) => setError(e instanceof Error ? e.message : "Could not load settings.")); }, []);
  const readOnly = demo || state?.read_only;
  const services = state ? [...new Set(state.keys.map((k) => k.service))] : [];
  return (
    <div className="pg">
      <TopBar current="settings" />
      <main id="main" tabIndex={-1} className="pg-main se-main">
        <div className="pg-head">
          <div>
            <h1 className="heading">Settings</h1>
            <p className="pg-sub">A Meta or Baseten key runs the Muse review agent. DigiKey and Mouser keys are optional and add part lookup and datasheet reading.</p>
          </div>
        </div>
        <p className="se-note"><Icon name="lock" size={16} /><span>Keys stay on this machine. They are kept in a private file in the data folder, and each one is sent only to its own service: the model key to Meta or Baseten, a part key to DigiKey or Mouser. None goes to the solver, KiCad or the browser. A saved value is never shown again.</span></p>
        {readOnly && <p className="se-note" role="note" data-testid="settings-readonly"><Icon name="info" size={16} /><span>Settings are switched off in the read-only demo.</span></p>}
        {error && <p className="empty" role="alert" style={{ color: "var(--sev-high)" }}>{error}</p>}
        {!state && !error && <p className="empty dim">Loading settings.</p>}
        {!readOnly && state && services.map((service) => {
          const rows = state.keys.filter((k) => k.service === service);
          return (
            <section key={service} className="se-card" aria-label={service}>
              <div className="se-card-head">
                {(service === "Meta" || service === "Baseten") && <img src="/brand/meta-logo.svg" alt="" width={20} height={20} />}
                <h2>{service}</h2>
                <span className="grow" />
                <a className="se-get" href={rows[0].get_url} target="_blank" rel="noopener noreferrer">Get a {service} key <Icon name="external" size={13} /></a>
              </div>
              <p className="se-why">{WHY[service]}</p>
              {rows.map((row) => <KeyField key={row.name} row={row} onChange={setState} />)}
            </section>
          );
        })}
        {!readOnly && state && state.model_available === false && <p className="se-hint">No model key yet: reviews use the scripted checks only.</p>}
        {!readOnly && state && <p className="se-hint">{state.parts_available ? "Part lookup is on." : "Part lookup is off until a key is set. Tools that need it report that it is not configured."}</p>}
      </main>
    </div>
  );
}
