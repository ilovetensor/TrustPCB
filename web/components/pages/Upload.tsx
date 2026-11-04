"use client";
import "./pages.css";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { ApiError, createProject, FIXTURE_MODE, getKeyStatus, getModelAvailable, getProject, getVersion, startReview, uploadVersion, type Depth, type Goal, type VersionInfo } from "../../lib/api";
import { cleanExtent, defaultExtent, extentOptions, type ExtentId } from "../../lib/extent.mjs";
import { ExtentPicker } from "./ExtentPicker";
import { DemoNotice, Icon, Panel, TopBar } from "../ui";
import { useAppEnv } from "../../lib/demo";
import { buildGoal, depthNote } from "../../lib/goal.mjs";

const DEPTHS: { id: Depth; label: string; note: string }[] = [
  { id: "quick", label: "Quick", note: "About 6 min. KiCad checks and an open review. No simulation." },
  { id: "standard", label: "Standard", note: "Up to 60 min. One investigation with simulation when the RF lab is installed." },
  { id: "deep", label: "Deep", note: "Up to 120 min. Up to three investigations." },
];

interface Picked { files: File[]; names: string[]; zip: boolean; pcb: string | null; pro: boolean; sch: boolean; title: string }

function inspect(files: File[]): Picked {
  const names = files.map((f) => (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name);
  const zip = names.length === 1 && /\.zip$/i.test(names[0]);
  const pcb = names.find((n) => /\.kicad_pcb$/i.test(n)) ?? null;
  const title = zip ? names[0].replace(/\.zip$/i, "") : pcb ? pcb.split("/").pop()!.replace(/\.kicad_pcb$/i, "") : names[0]?.split("/")[0] ?? "Board";
  return { files, names, zip, pcb, pro: names.some((n) => /\.kicad_pro$/i.test(n)), sch: names.some((n) => /\.kicad_sch$/i.test(n)), title };
}

const bandText = (d: VersionInfo["detection"]) => (d?.band ? `${d.band.low_ghz} to ${d.band.high_ghz} GHz` : "");
function UploadInner() {
  const { demo } = useAppEnv();
  const router = useRouter();
  const sp = useSearchParams();
  const existingVersion = sp.get("version");
  const existing = sp.get("project");
  const [over, setOver] = useState(false);
  const [picked, setPicked] = useState<Picked | null>(null);
  const [projectName, setProjectName] = useState("");
  const [existingName, setExistingName] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [version, setVersion] = useState<VersionInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyProject, setBusyProject] = useState<string | null>(null);      // set when the server answers 409: another review of this board is running
  const [depth, setDepth] = useState<Depth>("quick");
  const [extentPick, setExtentPick] = useState<ExtentId[] | null>(null);          // null: the board's own default until the user chooses
  const [keys, setKeys] = useState<Record<string, { set: boolean }> | null>(null);
  const [purpose, setPurpose] = useState("");
  const [band, setBand] = useState("");
  const [target, setTarget] = useState("-10");
  const [focus, setFocus] = useState("");
  const [starting, setStarting] = useState(false);
  const zipRef = useRef<HTMLInputElement>(null);
  const dirRef = useRef<HTMLInputElement>(null);

  // arriving from a project page with an uploaded version that has no review yet
  useEffect(() => {
    if (!existingVersion) return;
    void getVersion(existingVersion).then((v) => { setVersion(v); if (v.detection?.band) setBand(bandText(v.detection)); }).catch((e) => setError(e instanceof Error ? e.message : "Could not load the version."));
  }, [existingVersion]);
  useEffect(() => {
    if (!existing) return;
    void getProject(existing).then((p) => { if (p) setExistingName(p.project.name); });
  }, [existing]);

  useEffect(() => { void getKeyStatus().then(setKeys); }, []);
  const [modelOk, setModelOk] = useState<boolean | null>(null);
  useEffect(() => { void getModelAvailable().then(setModelOk); }, []);

  const onFiles = (list: FileList | File[]) => {
    const files = [...list].filter((f) => f.size >= 0);
    if (!files.length) return;
    const p = inspect(files);
    setPicked(p);
    setError(null);
    setVersion(null);
    if (!existing && !projectName) setProjectName(p.title);
  };

  const upload = async () => {
    if (!picked) return;
    setError(null);
    setProgress(0);
    try {
      let pid = existing;
      if (!pid) pid = (await createProject(projectName.trim() || picked.title)).id;
      const v = await uploadVersion(pid, picked.files, undefined, setProgress);
      setVersion(v);
      if (v.detection?.band) setBand(bandText(v.detection));
      if (!purpose && v.detection?.wireless) setPurpose("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "The upload failed.");
    } finally {
      setProgress(null);
    }
  };

  const start = async () => {
    if (!version) return;
    setStarting(true);
    setError(null);
    const goal: Goal = buildGoal({ purpose, band, target, focus, wireless: version.detection?.wireless });
    setBusyProject(null);
    try {
      const r = await startReview(version.id, goal, depth, extent);
      router.push(`/reviews/${r.id}`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setBusyProject(version.project_id);
      setError(e instanceof Error ? e.message : "The review did not start.");
      setStarting(false);
    }
  };

  const d = version?.detection;
  const noRadio = !!d && !d.wireless;
  // what the board can be checked for; `sims` is the server's own answer per simulation when the version carries one
  const serverSims = (version as (VersionInfo & { sims_available?: Record<string, { available: boolean; reason?: string }> }) | null)?.sims_available ?? null;
  const extentChoices = extentOptions({ detection: d ?? null, keys, server: serverSims, depth });
  const extent = cleanExtent(extentPick ?? defaultExtent(extentChoices), extentChoices);
  const simsOn = extent.some((x) => x === "power" || x === "thermal" || x === "signal");
  const st = version?.stackup;
  const uploading = progress !== null;
  const ready = !!picked && (!!picked.pcb || picked.zip);

  if (demo) {
    return (
      <div className="pg">
        <TopBar current="upload" />
        <main id="main" tabIndex={-1} className="pg-main">
          <div className="pg-head"><div><h1 className="heading">Uploads are off in this demo.</h1><p className="pg-sub">Everything here is a finished review you can read, not a service you can feed.</p></div></div>
          <DemoNotice block />
          <p><Link href="/projects" className="btn primary"><Icon name="folder" size={14} /> Browse the finished reviews</Link></p>
        </main>
      </div>
    );
  }

  return (
    <div className="pg">
      <TopBar current="upload" />
      <main id="main" tabIndex={-1} className="pg-main">
        <div className="pg-head">
          <div>
            <span className="idx">Step {version ? "2" : "1"} of 2</span>
            <h1 className="heading">{existing ? `New version of ${existingName ?? "this board"}.` : "Bring a board to review."}</h1>
            <p className="pg-sub">Drop a KiCad project as a zip or a folder. The server unpacks it on a copy and never edits your files.</p>
          </div>
        </div>
        {FIXTURE_MODE && (
          <p className="warn"><span className="signal"><Icon name="info" size={15} /></span><span><b>Fixture mode.</b> Uploads are off. <Link href="/reviews/r-0042" className="signal">Open the sample review replay</Link>.</span></p>
        )}
        {modelOk === false && (
          <p className="warn" role="note" data-testid="no-model-key"><span className="signal"><Icon name="info" size={15} /></span><span><b>No model key found.</b> Add a Meta or Baseten API key in <Link href="/settings" className="signal">Settings</Link> to run the Muse review. Without one, only the scripted checks run.</span></p>
        )}
        <div className="drop-grid">
          <div>
            <div
              className={`drop ${over ? "over" : ""}`}
              onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
              onDrop={(e) => { e.preventDefault(); setOver(false); if (e.dataTransfer.files.length) onFiles(e.dataTransfer.files); }}
            >
              <div>
                <span className="drop-ic"><Icon name="upload" size={26} /></span>
                <h2 className="heading">Drop a project here</h2>
                <p>{picked ? "Drop again to replace it." : "A .zip of the project, or the project folder. The .kicad_pcb is required. The .kicad_pro is strongly recommended."}</p>
                <div style={{ display: "flex", gap: 8, justifyContent: "center", marginTop: 18, flexWrap: "wrap" }}>
                  <button className="btn" onClick={() => zipRef.current?.click()} disabled={uploading}><Icon name="file" size={14} /> Choose a zip</button>
                  <button className="btn" onClick={() => dirRef.current?.click()} disabled={uploading}><Icon name="folder" size={14} /> Choose a folder</button>
                </div>
                <input ref={zipRef} data-testid="zip-input" type="file" accept=".zip,application/zip" hidden onChange={(e) => e.target.files && onFiles(e.target.files)} />
                <input ref={dirRef} data-testid="dir-input" type="file" hidden multiple {...({ webkitdirectory: "" } as object)} onChange={(e) => e.target.files && onFiles(e.target.files)} />
              </div>
            </div>

            {picked && !version && (
              <Panel className="card rise" title="What you chose" style={{ marginTop: 14 }}>
                <dl className="kv">
                  <dt>Files</dt><dd className="num" style={{ wordBreak: "break-all", fontSize: 12.5 }}>{picked.names.length > 4 ? `${picked.names.slice(0, 4).join(", ")} and ${picked.names.length - 4} more` : picked.names.join(", ")}</dd>
                  <dt>Board file</dt><dd>{picked.zip ? "Inside the zip. Detected after upload." : picked.pcb ? <span className="num">{picked.pcb}</span> : <span className="signal">No .kicad_pcb found</span>}</dd>
                  <dt>Design rules</dt><dd>{picked.zip ? "Checked after upload." : picked.pro ? ".kicad_pro found. Real rules are used." : <span className="signal">Missing</span>}</dd>
                  <dt>Schematic</dt><dd>{picked.zip ? "Checked after upload." : picked.sch ? "Found" : "Not found. ERC and part intent are skipped."}</dd>
                </dl>
                {!picked.pro && !picked.zip && <p className="warn"><span className="signal"><Icon name="info" size={15} /></span><span><b>Default rules.</b> Without a .kicad_pro the review runs with KiCad defaults and rule results are downgraded.</span></p>}
                <div style={{ padding: "14px 0 0", display: "grid", gap: 12 }}>
                  {!existing && (
                    <div className="field"><label className="idx" htmlFor="pn">Project name</label><input id="pn" type="text" value={projectName} onChange={(e) => setProjectName(e.target.value)} /></div>
                  )}
                  <button className="btn primary" disabled={!ready || uploading || FIXTURE_MODE} onClick={upload} data-testid="upload-go">
                    {uploading ? `Uploading ${Math.round((progress ?? 0) * 100)}%` : <><Icon name="upload" size={14} /> Upload and detect</>}
                  </button>
                  {uploading && <div className="meter sig"><b style={{ width: `${(progress ?? 0) * 100}%` }} /></div>}
                </div>
              </Panel>
            )}

            {version && (
              <Panel className="card rise" title="What the server found" style={{ marginTop: 14 }}>
                <dl className="kv" data-testid="detection">
                  <dt>Version</dt><dd>v{version.number}{version.duplicate_of ? <span className="signal"> (same files as an earlier upload)</span> : null}</dd>
                  <dt>Status</dt><dd>{version.status}</dd>
                  <dt>Wireless</dt>
                  <dd>{d?.wireless ? `Yes (${d.confidence ?? "unknown"} confidence)${d.band ? `. ${d.band.label}` : ""}` : d ? "No radio part or antenna found" : "Not detected"}</dd>
                  {d?.antenna && (<><dt>Antenna</dt><dd>{[...new Set([d.antenna.type, d.antenna.shape].filter(Boolean))].join(" ")}{d.antenna.length_mm ? `, ${d.antenna.length_mm} mm` : ""}{d.antenna.net ? ` on ${d.antenna.net}` : ""}{d.antenna.in_keepout ? ", inside a keep-out" : ""}</dd></>)}
                  {d?.parts && d.parts.length > 0 && (<><dt>Radio parts</dt><dd>{d.parts.map((p) => `${p.ref} ${p.value}`).join(", ")}</dd></>)}
                  {st && (<><dt>Stackup</dt><dd>{st.total_thickness_mm} mm, {st.copper_layers ?? "?"} copper layers, {st.source === "default" ? "default FR-4 assumed" : `from the ${st.source}`}</dd></>)}
                </dl>
                {st?.layers && (
                  <table className="rp-tbl" style={{ margin: "14px 0 0", width: "100%", fontSize: 12.5 }}>
                    <thead><tr><th>Layer</th><th>Kind</th><th>Thickness</th><th>Er</th></tr></thead>
                    <tbody>{st.layers.map((l) => (<tr key={l.name}><td className="num">{l.name}</td><td>{l.kind}</td><td className="num">{l.thickness_mm} mm</td><td className="num">{l.epsilon_r ?? "-"}</td></tr>))}</tbody>
                  </table>
                )}
                {st?.assumptions && st.assumptions.length > 0 && (
                  <p className="warn"><span className="signal"><Icon name="info" size={15} /></span><span><b>Assumed:</b> {st.assumptions.map((a) => a.text).join(" ")} They are listed in the report.</span></p>
                )}
                {st?.warnings && st.warnings.length > 0 && <p className="warn"><span className="signal"><Icon name="info" size={15} /></span><span>{st.warnings.join(" ")}</span></p>}
              </Panel>
            )}
          </div>

          <Panel className="card" title="What must this board do?">
            <div>
              <div className="field"><label className="idx" htmlFor="g1">What the board does</label><input id="g1" type="text" value={purpose} onChange={(e) => setPurpose(e.target.value)} placeholder={noRadio ? "USB powered 3.3 V sensor" : "Battery Bluetooth sensor"} disabled={!version} /></div>
              {noRadio
                ? <p className="dim" style={{ fontSize: 12, margin: "0 0 14px" }} data-testid="no-radio-note">No radio part or antenna was found, so there is no band or S11 target to set. The review covers the rules and the layout, and any power, heat or signal checks you pick below.</p>
                : <>
                  <div className="field"><label className="idx" htmlFor="g2">Band to tune for</label><input id="g2" type="text" value={band} onChange={(e) => setBand(e.target.value)} placeholder="2.4 to 2.4835 GHz" disabled={!version} /></div>
                  <div className="field"><label className="idx" htmlFor="g4">S11 target (dB)</label><input id="g4" type="text" inputMode="decimal" value={target} onChange={(e) => setTarget(e.target.value)} disabled={!version} /></div>
                </>}
              <div className="field"><label className="idx" htmlFor="g3">Focus</label><input id="g3" type="text" value={focus} onChange={(e) => setFocus(e.target.value)} placeholder={noRadio ? "Is the power path OK?" : "Is the antenna OK?"} disabled={!version} /></div>
              <div className="field">
                <span className="idx">Review depth</span>
                <div className="seg" role="group" aria-label="Review depth" style={{ width: "100%" }}>
                  {DEPTHS.map((x) => (<button key={x.id} style={{ flex: 1 }} aria-pressed={depth === x.id} onClick={() => setDepth(x.id)}>{x.label}</button>))}
                </div>
                <span className="dim" style={{ fontSize: 12 }}>{depthNote(DEPTHS.find((x) => x.id === depth)!.note, depth, d?.wireless, simsOn)}</span>
              </div>
              <ExtentPicker options={extentChoices} value={extent} onChange={setExtentPick} disabled={!version || starting} />
              <p className="dim" style={{ fontSize: 12, margin: "0 0 14px" }}>The agent does not stop to ask questions mid-review. Fields lock when it starts.</p>
              {error && <p className="chat-err" role="alert" style={{ margin: "0 0 12px" }} data-testid="start-error">{error}{busyProject && <> <Link href={`/projects/${busyProject}`} className="signal">Open the board</Link> to see or stop the running review.</>}</p>}
              <button className="btn primary big" style={{ width: "100%" }} disabled={!version || starting || version.status === "failed"} onClick={start} data-testid="start-review">
                <Icon name="play" size={14} /> {starting ? "Starting" : "Start the review"}
              </button>
              {!version && <p className="idx" style={{ marginTop: 10, whiteSpace: "normal" }}>Upload a board first. The review starts from what the server detects.</p>}
            </div>
          </Panel>
        </div>
      </main>
    </div>
  );
}

export function Upload() {
  return (<Suspense fallback={null}><UploadInner /></Suspense>);
}
