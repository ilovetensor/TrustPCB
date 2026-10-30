"use client";
import "./pages.css";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { Icon, Panel, TopBar, FixtureBadge } from "../ui";

type Depth = "Quick" | "Standard" | "Deep";
const DEPTH_NOTE: Record<Depth, string> = {
  Quick: "4 min. Code checks and an open review. No simulation.",
  Standard: "30 min. One investigation with simulation.",
  Deep: "75 min. Up to three investigations.",
};

interface Picked { names: string[]; zip: boolean; pcb: string | null; pro: boolean; sch: boolean; sample: boolean }

function inspect(files: { name: string }[]): Picked {
  const names = files.map((f) => f.name);
  const zip = names.length === 1 && /\.zip$/i.test(names[0]);
  return {
    names, zip, sample: false,
    pcb: names.find((n) => /\.kicad_pcb$/i.test(n)) ?? null,
    pro: names.some((n) => /\.kicad_pro$/i.test(n)),
    sch: names.some((n) => /\.kicad_sch$/i.test(n)),
  };
}

export function Upload() {
  const router = useRouter();
  const [over, setOver] = useState(false);
  const [picked, setPicked] = useState<Picked | null>(null);
  const [depth, setDepth] = useState<Depth>("Standard");
  const [goal, setGoal] = useState("");
  const [focus, setFocus] = useState("");
  const [target, setTarget] = useState("S11 better than -10 dB across 2.4 to 2.4835 GHz");
  const zipRef = useRef<HTMLInputElement>(null);
  const dirRef = useRef<HTMLInputElement>(null);

  const onFiles = (list: FileList | File[]) => setPicked(inspect([...list].map((f) => ({ name: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name }))));
  const sample = () => {
    setPicked({ names: ["nrf52840_rf_iot_node.kicad_pcb", "nrf52840_rf_iot_node.kicad_pro", "nrf52840_rf_iot_node.kicad_sch"], zip: false, pcb: "nrf52840_rf_iot_node.kicad_pcb", pro: true, sch: true, sample: true });
    setGoal("Battery Bluetooth sensor node on 2.4 GHz");
    setFocus("Is the antenna OK?");
  };
  const ready = !!picked?.pcb || !!picked?.zip;

  return (
    <div className="pg">
      <TopBar current="upload" />
      <main className="pg-main">
        <div className="pg-head">
          <div>
            <span className="idx">INTAKE / STEP {picked ? "02" : "01"} OF 02</span>
            <h1 className="serif">Bring a board to the bench.</h1>
            <p className="pg-sub">Drop a KiCad project as a zip or a folder. The server unpacks it on a copy and never edits your files.</p>
          </div>
        </div>
        <div className="drop-grid">
          <div>
            <div
              className={`drop hair ${over ? "over" : ""}`}
              onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
              onDrop={(e) => { e.preventDefault(); setOver(false); if (e.dataTransfer.files.length) onFiles(e.dataTransfer.files); }}
            >
              <div>
                <svg width="74" height="74" viewBox="0 0 74 74" fill="none" aria-hidden stroke="currentColor" strokeWidth="1.2">
                  <path d="M10 22V10h12M52 10h12v12M64 52v12H52M22 64H10V52" />
                  <rect x="22" y="24" width="30" height="26" strokeDasharray="3 3" />
                  <path d="M37 44V30M31 36l6-6 6 6" stroke="var(--signal)" />
                </svg>
                <h2 className="serif">Drop a project here</h2>
                <p>{picked ? "Drop again to replace it." : "A .zip of the project, or the project folder. The .kicad_pcb is required. The .kicad_pro is strongly recommended."}</p>
                <div style={{ display: "flex", gap: 8, justifyContent: "center", marginTop: 18, flexWrap: "wrap" }}>
                  <button className="btn" onClick={() => zipRef.current?.click()}><Icon name="file" size={13} /> Choose zip</button>
                  <button className="btn" onClick={() => dirRef.current?.click()}><Icon name="layers" size={13} /> Choose folder</button>
                  <button className="btn ghost" onClick={sample}>Use the sample board</button>
                </div>
                <input ref={zipRef} type="file" accept=".zip" hidden onChange={(e) => e.target.files && onFiles(e.target.files)} />
                <input ref={dirRef} type="file" hidden multiple {...({ webkitdirectory: "" } as object)} onChange={(e) => e.target.files && onFiles(e.target.files)} />
              </div>
            </div>
            {picked && (
              <Panel className="card rise" idx="DETECTION" title="What we found" delay={0} style={{ marginTop: 18 }}>
                <dl className="kv">
                  <dt>Files</dt><dd className="mono" style={{ wordBreak: "break-all" }}>{picked.names.length > 4 ? `${picked.names.slice(0, 4).join(", ")} and ${picked.names.length - 4} more` : picked.names.join(", ")}</dd>
                  <dt>Board file</dt><dd>{picked.zip ? "Inside the zip. Detected after upload." : picked.pcb ? <span className="mono">{picked.pcb}</span> : <span className="signal">No .kicad_pcb found</span>}</dd>
                  <dt>Design rules</dt><dd>{picked.zip ? "Checked after upload." : picked.pro ? ".kicad_pro found. Real rules are used." : <span className="signal">Missing</span>}</dd>
                  <dt>Schematic</dt><dd>{picked.zip ? "Checked after upload." : picked.sch ? "Found" : "Not found. ERC and part intent are skipped."}</dd>
                  {picked.sample && (<><dt>Board</dt><dd className="mono">66.0 x 53.0 mm, 2 copper layers, 50 parts</dd><dt>Stackup</dt><dd>1.6 mm, from the board file</dd><dt>Wireless</dt><dd>Printed meander antenna on 2.4 GHz</dd></>)}
                </dl>
                {!picked.pro && !picked.zip && <p className="warn"><span className="signal mono">!</span><span><b>Default rules.</b> Without a .kicad_pro the review runs with KiCad defaults and rule results are downgraded.</span></p>}
                {picked.sample && <p className="idx" style={{ marginTop: 12 }}>SAMPLE DETECTION IS A DEV FIXTURE</p>}
              </Panel>
            )}
          </div>

          <Panel className="card" idx="GOAL" title="What must this board do?" delay={100}>
            <div style={{ padding: 16 }}>
              <div className="field"><label className="idx" htmlFor="g1">WHAT THE BOARD DOES</label><input id="g1" type="text" value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="Battery Bluetooth sensor" /></div>
              <div className="field"><label className="idx" htmlFor="g2">TUNING TARGET</label><input id="g2" type="text" value={target} onChange={(e) => setTarget(e.target.value)} /></div>
              <div className="field"><label className="idx" htmlFor="g3">FOCUS</label><input id="g3" type="text" value={focus} onChange={(e) => setFocus(e.target.value)} placeholder="Is the antenna OK?" /></div>
              <div className="field">
                <span className="idx">REVIEW DEPTH</span>
                <div className="seg" role="group" aria-label="Review depth" style={{ width: "100%" }}>
                  {(["Quick", "Standard", "Deep"] as Depth[]).map((d) => (<button key={d} style={{ flex: 1 }} aria-pressed={depth === d} onClick={() => setDepth(d)}>{d}</button>))}
                </div>
                <span className="dim" style={{ fontSize: 12 }}>{DEPTH_NOTE[depth]}</span>
              </div>
              <p className="dim" style={{ fontSize: 12, margin: "0 0 14px" }}>Fields fill in automatically where possible and lock when the review starts. The agent does not stop to ask questions mid-review.</p>
              <button className="btn primary big" style={{ width: "100%" }} disabled={!ready} onClick={() => router.push("/reviews/r-0042")}>Start review <Icon name="arrow" size={14} /></button>
              <p className="idx" style={{ marginTop: 10, display: "flex", gap: 8, alignItems: "center" }}><FixtureBadge label="Fixture" /> No upload is sent. This opens the sample review replay.</p>
            </div>
          </Panel>
        </div>
      </main>
    </div>
  );
}
