"use client";
import "../rf/rf.css";
import "../rf/power.css";
import "./simlab.css";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { BoardBundle } from "../../lib/api";
import {
  BOARD_KINDS, defaultCount, defaultsClaim, describeBoardOps, isDoneRun, isLiveRun, KIND_LABEL, KIND_NOUN, pairOf, panelModel, pickRun, trustOf,
  type BoardSimKind, type BoardSimRun, type BoardSimSource,
} from "../../lib/board-sims";
import { diffOverlay, fmtNum, fmtVal, mapsOf, sampleAt, sharedRange, type OverlayMeta } from "../../lib/overlay-math.mjs";
import { useAsync } from "../../lib/use-async";
import { tabProps, tablistKeys, useNarrow, useReducedMotion } from "../../lib/hooks";
import { Board2D, type Insets } from "../board2d/Board2D";
import { DEFAULT_LAYERS } from "../board2d/layers";
import { useScene } from "../board2d/useScene";
import { Icon, Panel } from "../ui";
import { Legend, HoverProbe, OverlayCanvas, OverlayPlane3D, ProbeRing } from "./OverlayLayer";
import { AssumptionList, HeatPanel, PowerPanel, SignalPanel, Stats, tilesOf, TrustBlock } from "./Panels";

const Scene3D = dynamic(() => import("../scene3d/Scene3D").then((m) => m.Scene3D), { ssr: false, loading: () => <div className="rf-loading idx">Loading the 3D board</div> });

type Phase = "before" | "after" | "split" | "change";
const KIND_ICON: Record<BoardSimKind, string> = { power: "bolt", thermal: "flame", signal: "pulse" };
const PHASE_LABEL: Record<Phase, string> = { before: "Before", after: "After", split: "Split", change: "Change" };
const none = [] as never[];
const noMeta = {};
const noop = () => {};

function useRaster(source: BoardSimSource, run: BoardSimRun | null, ov: OverlayMeta | null) {
  return useAsync(run && ov ? () => source.raster(run, ov) : null, `ras:${source.id}:${run?.run_id}:${ov?.id}`).data ?? null;
}

const headlineOf = (r: BoardSimRun) => (r.result ? panelModel(r.result).headline[0] ?? null : null);

function RunCard({ run, active, onPick }: { run: BoardSimRun; active: boolean; onPick: () => void }) {
  const done = isDoneRun(run), live = isLiveRun(run);
  const ops = describeBoardOps(run.whatif);
  const head = headlineOf(run);
  const t = run.result ? trustOf(run.result) : null;
  return (
    <div className={`rf-job ${active ? "on" : ""} ${done ? "" : "wait"}`}>
      <button className="rf-job-main" onClick={onPick} disabled={!done} aria-pressed={active} title={done ? "Show this run" : `This run is ${run.waiting ? "waiting for the solver" : run.status}`}>
        <span className="rf-job-top">
          {run.ref && <b className="num">{run.ref}</b>}
          <span className="idx">{run.whatif.length ? "What-if" : "Base"}</span>
          <span style={{ flex: 1 }} />
          {t && <span className={`stamp ${t.ok ? "" : "sig"}`}>{t.ok ? "Checks passed" : "Failed checks"}</span>}
        </span>
        <span className="rf-job-name">{run.label ?? "Simulation"}</span>
        {ops && <span className="rf-job-ops num">{ops}</span>}
        {done && head && <span className="rf-job-num num">{head.label} <b>{fmtVal(head.value, head.unit)}</b></span>}
        {live && (
          <span className="sl-live">
            <span className="rf-job-num num">{run.waiting ? <><i className="sl-pulse" aria-hidden />Waiting for the solver{run.waiting.ahead ? `, ${run.waiting.ahead} ahead` : run.waiting.position ? `, place ${run.waiting.position}` : ""}</> : <>{run.step ?? "Solving"}{run.progress != null ? ` ${Math.round(run.progress * 100)}%` : ""}</>}</span>
            {!run.waiting && <span className="meter sig"><b style={{ width: `${(run.progress ?? 0) * 100}%` }} /></span>}
          </span>
        )}
        {(run.status === "failed" || run.status === "canceled" || run.status === "timeout" || run.status === "interrupted") && <span className="rf-job-num">{run.error ?? `This run ${run.status === "failed" ? "failed" : `was ${run.status}`}.`}</span>}
      </button>
    </div>
  );
}

export function SimLabView({ reviewId, source, bundle, title, initialKind = null, initialRun = null, initialRef = null, notice, backHref, sampleNote = false }: {
  reviewId: string; source: BoardSimSource; bundle: BoardBundle | null; title: string; initialKind?: BoardSimKind | null; initialRun?: string | null; initialRef?: string | null;
  notice?: string; backHref: string; sampleNote?: boolean;
}) {
  const runs = source.runs;
  // with no kind asked for, the first kind that has a run, in the order power, heat, signal
  const firstKind = useMemo(() => initialKind ?? BOARD_KINDS.find((k) => runs.some((r) => r.kind === k)) ?? null, [runs, initialKind]);
  const first = useMemo(() => pickRun(runs, { run: initialRun, ref: initialRef, kind: initialRun || initialRef ? null : firstKind }), [runs, initialRun, initialRef, firstKind]);
  const [kind, setKind] = useState<BoardSimKind>(initialKind ?? first?.kind ?? "power");
  const kindRuns = useMemo(() => runs.filter((r) => r.kind === kind), [runs, kind]);
  const [activeId, setActiveId] = useState<string | null>(initialRun ?? first?.run_id ?? null);
  const active = useMemo(() => kindRuns.find((r) => r.run_id === activeId) ?? pickRun(kindRuns, { kind }), [kindRuns, activeId, kind]);

  const pair = useMemo(() => pairOf(kindRuns, active), [kindRuns, active]);
  const [phase, setPhase] = useState<Phase>("before");
  useEffect(() => { setPhase(active?.whatif.length ? "after" : "before"); }, [active?.run_id]);
  const eff: Phase = pair ? phase : "before";
  const primary: BoardSimRun | null = !pair ? active : eff === "before" ? pair.before : pair.after;
  const compare = pair && primary?.run_id === pair.after.run_id ? pair.before : null;
  const result = primary?.result ?? null;

  const maps = useMemo(() => mapsOf(result?.overlays), [result]);
  const [mapKey, setMapKey] = useState<string | null>(null);
  const [layer, setLayer] = useState<string | null>(null);
  const map = maps.find((m) => m.key === mapKey) ?? maps[0] ?? null;
  // with no layer chosen, the one that carries the most copper: the busiest layer is the one worth seeing first
  const busiest = useAsync(map && map.layers.length > 1 && primary ? () => Promise.all(map.layers.map(async (l) => { const d = await source.raster(primary, l); let n = 0; if (d) for (let i = 0; i < d.length; i++) if (d[i] === d[i]) n++; return { layer: l.layer, n }; })).then((r) => r.sort((a, b) => b.n - a.n)[0]?.layer ?? null) : null, `bl:${source.id}:${primary?.run_id}:${map?.key}`);
  const waitBusiest = !layer && !!map && map.layers.length > 1 && busiest.data === undefined;
  const meta0 = map?.layers.find((l) => l.layer === (layer ?? busiest.data)) ?? map?.layers[0] ?? null;
  const [mode, setMode] = useState<"2d" | "3d">("2d");
  const calm = useReducedMotion();
  const narrow = useNarrow();

  // the same map in the other run, drawn on one shared scale so the colours compare
  const find = (run: BoardSimRun | null) => run?.result?.overlays.find((o) => o.id === meta0?.id) ?? null;
  const rawB = pair ? find(pair.before) : null, rawA = pair ? find(pair.after) : null;
  const range = useMemo(() => sharedRange(rawB, rawA), [rawB, rawA]);
  const withRange = (o: OverlayMeta | null) => (o && range ? { ...o, ...range } : o);
  const ovB = useMemo(() => withRange(rawB), [rawB, range]); // eslint-disable-line react-hooks/exhaustive-deps
  const ovA = useMemo(() => withRange(rawA), [rawA, range]); // eslint-disable-line react-hooks/exhaustive-deps
  const dB = useRaster(source, pair?.before ?? null, rawB);
  const dA = useRaster(source, pair?.after ?? null, rawA);
  const dS = useRaster(source, pair ? null : active, pair ? null : meta0);
  const diff = useMemo(() => (pair && ovB && ovA && dB && dA ? diffOverlay(ovB, dB, ovA, dA) : null), [pair, ovB, ovA, dB, dA]);

  const bbox = bundle?.board.bbox ?? [0, 0, 1, 1];
  const [splitAt, setSplitAt] = useState(0.5);
  const splitMm = bbox[0] + (bbox[2] - bbox[0]) * splitAt;
  useEffect(() => { if (pair && eff === "change" && !diff && dB && dA) setPhase("after"); }, [pair, eff, diff, dB, dA]);

  // what is drawn: one raster, two cut at the slider, or the change
  type Layer = { key: string; ov: OverlayMeta; data: Float32Array; keep: "all" | "left" | "right"; name: string };
  const drawn: Layer[] = useMemo(() => {
    if (waitBusiest) return [];
    if (!pair) return meta0 && dS ? [{ key: "s", ov: meta0, data: dS, keep: "all", name: "" }] : [];
    if (eff === "change") return diff ? [{ key: "c", ov: diff.ov, data: diff.data, keep: "all", name: "Change" }] : [];
    if (eff === "split") return [ovB && dB ? { key: "b", ov: ovB, data: dB, keep: "left" as const, name: "Before" } : null, ovA && dA ? { key: "a", ov: ovA, data: dA, keep: "right" as const, name: "After" } : null].filter(Boolean) as Layer[];
    return eff === "before" ? (ovB && dB ? [{ key: "b", ov: ovB, data: dB, keep: "all", name: "Before" }] : []) : ovA && dA ? [{ key: "a", ov: ovA, data: dA, keep: "all", name: "After" }] : [];
  }, [pair, eff, meta0, dS, ovB, ovA, dB, dA, diff, waitBusiest]);
  const legendOv = eff === "change" && diff ? diff.ov : (pair ? ovA ?? ovB : meta0) ?? meta0;
  const loadingRaster = waitBusiest || (!!meta0 && drawn.length === 0 && !!result && (pair ? !(dB && dA) : !dS));

  const [probe, setProbe] = useState<{ x: number; y: number; sx: number; sy: number } | null>(null);
  const reads = useMemo(() => {
    if (!probe) return [];
    const at = (l: { ov: OverlayMeta; data: Float32Array }) => sampleAt(l.ov, l.data, probe.x, probe.y);
    if (!pair) return drawn.map((l) => ({ name: "", v: at(l), unit: l.ov.unit }));
    if (eff === "change") {
      const out = [{ name: "Change", v: diff ? at(diff) : null, unit: ovA?.unit ?? "" }];
      if (ovB && dB) out.push({ name: "Before", v: at({ ov: ovB, data: dB }), unit: ovB.unit });
      if (ovA && dA) out.push({ name: "After", v: at({ ov: ovA, data: dA }), unit: ovA.unit });
      return out;
    }
    if (eff === "split") return drawn.map((l) => ({ name: l.name, v: at(l), unit: l.ov.unit }));
    return drawn.map((l) => ({ name: "", v: at(l), unit: l.ov.unit }));
  }, [probe, pair, drawn, eff, diff, ovA, ovB, dA, dB]);
  const probeCopper = reads.some((r) => r.v != null);

  const layers = useMemo(() => ({ ...DEFAULT_LAYERS, issues: false, regions: false, silk: false, zones: true, top: meta0?.layer !== "B.Cu", bottom: meta0?.layer !== "F.Cu" }), [meta0?.layer]);
  const sceneMarkup = useScene(bundle?.scene ?? null);
  const insets: Insets = narrow ? { l: 0, r: 0, t: 0, b: 0 } : { l: 358, r: 450, t: 66, b: 74 };
  const can3d = !!bundle?.glbUrl && (meta0?.layer === "board" || meta0?.layer === "F.Cu");
  const use3d = mode === "3d" && can3d;
  const outline = bundle?.board.outline ?? null;
  const kindsPresent = BOARD_KINDS.filter((k) => runs.some((r) => r.kind === k));

  const board = bundle?.board;
  const stage = (
    <div className="rf-stage sl-stage">
      {board && drawn.length > 0 ? (
        use3d ? (
          <Scene3D board={board} glbUrl={bundle!.glbUrl!} marks={none} meta={noMeta} selectedId={null} onSelect={noop} explode={0} insets={insets} showMarks={false} boardDims={false} view3d={{ az: 0.5, el: 0.7 }}>
            {drawn.map((l) => (<OverlayPlane3D key={l.key} ov={l.ov} data={l.data} />))}
          </Scene3D>
        ) : (
          <Board2D
            board={board} marks={none} meta={noMeta} selectedId={null} onSelect={noop} layers={layers} insets={insets} scene={sceneMarkup} chrome
            overlay={(view) => (
              <>
                {drawn.map((l) => (<OverlayCanvas key={`${l.key}:${l.ov.id}:${l.ov.layer}`} ov={l.ov} data={l.data} outline={outline} keep={l.keep} split={eff === "split" ? splitMm : null} fadeKey={`${l.key}:${l.ov.id}:${l.ov.layer}`} />))}
                {pair && eff === "split" && (<svg style={{ position: "absolute", left: 0, top: 0, width: 1, height: 1, overflow: "visible", pointerEvents: "none" }} aria-hidden><line x1={splitMm} x2={splitMm} y1={bbox[1] - 2} y2={bbox[3] + 2} stroke="var(--paper)" strokeWidth={1.4 / view.scale} /></svg>)}
                {probe && probeCopper && <ProbeRing x={probe.x} y={probe.y} scale={view.scale} />}
                <HoverProbe view={view} onProbe={setProbe} />
              </>
            )}
          />
        )
      ) : <div className="rf-loading idx">{!bundle ? "Loading the board" : loadingRaster ? "Loading the map" : result ? "This run kept no map" : active && isLiveRun(active) ? "Not finished yet" : "No finished run yet"}</div>}
      {!use3d && probe && drawn.length > 0 && (
        <div className="sl-probe" style={{ left: Math.min(probe.sx + 16, 99999), top: probe.sy + 16 }} role="status" aria-live="off">
          {probeCopper ? reads.map((r, i) => (
            <div key={i} className={r.v == null ? "none" : ""}>{r.name && <span className="idx">{r.name}</span>}<b className="num">{r.v == null ? "no copper" : fmtVal(r.v, r.unit)}</b></div>
          )) : <div className="none"><b>{meta0?.layer === "board" ? "Off the board" : "No copper here"}</b></div>}
          <span className="idx num">{legendOv?.label} · {meta0?.layer} · {fmtNum(probe.x)}, {fmtNum(probe.y)} mm</span>
        </div>
      )}
    </div>
  );

  const runList = (
    <Panel className="rf-runs" title="Simulations" bodyClass="rf-body tight" actions={<span className="idx">{kindRuns.filter(isDoneRun).length} of {kindRuns.length} done</span>}>
      <div className="seg sl-kinds" role="tablist" aria-label="Kind of simulation" onKeyDown={tablistKeys()}>
        {BOARD_KINDS.map((k) => {
          const n = runs.filter((r) => r.kind === k).length;
          return (<button key={k} {...tabProps(kind === k)} disabled={!n} title={n ? undefined : `No ${KIND_LABEL[k].toLowerCase()} simulation in this review`} onClick={() => { setKind(k); setActiveId(pickRun(runs, { kind: k })?.run_id ?? null); setMapKey(null); setLayer(null); }}><Icon name={KIND_ICON[k]} size={13} /> {KIND_LABEL[k]}{n ? <small className="num">{n}</small> : null}</button>);
        })}
      </div>
      <div className="rf-joblist">
        {kindRuns.length === 0 && <p className="dim" style={{ padding: "8px 12px" }}>No {KIND_LABEL[kind].toLowerCase()} simulation in this review.</p>}
        {kindRuns.map((r) => (<RunCard key={r.run_id} run={r} active={r.run_id === active?.run_id} onPick={() => { setActiveId(r.run_id); }} />))}
      </div>
    </Panel>
  );

  const controls = (
    <Panel className="rf-ctl" title="On the board" bodyClass="rf-body">
      {maps.length > 1 && (
        <div className="seg sl-maps" role="tablist" aria-label="Map" onKeyDown={tablistKeys()}>
          {maps.map((m) => (<button key={m.key} {...tabProps(map?.key === m.key)} onClick={() => { setMapKey(m.key); setLayer(null); }}>{m.label}</button>))}
        </div>
      )}
      {map && map.layers.length > 1 && (
        <div className="sl-row2"><span className="idx">Layer</span>
          <div className="seg" role="group" aria-label="Copper layer">{map.layers.map((l) => (<button key={l.layer} aria-pressed={meta0?.layer === l.layer} onClick={() => setLayer(l.layer)}>{l.layer}</button>))}</div>
        </div>
      )}
      {pair && (
        <div className="sl-row2"><span className="idx">What-if</span>
          <div className="seg" role="group" aria-label="Before and after">
            {(["before", "after", "split", "change"] as Phase[]).map((p) => (<button key={p} aria-pressed={eff === p} disabled={p === "change" && !(ovB && ovA && dB && dA && diff)} onClick={() => setPhase(p)}>{PHASE_LABEL[p]}</button>))}
          </div>
        </div>
      )}
      {legendOv && (
        <Legend ov={legendOv} label={eff === "change" ? `${legendOv.label}, after minus before` : legendOv.label}
          note={pair && eff !== "change" ? "Before and after share this scale." : eff === "change" ? "Cool is lower after the change, warm is higher." : meta0?.layer === "board" ? "Whole board surface." : `On the copper of ${meta0?.layer ?? "the board"}.`} />
      )}
      <div className="rf-row">
        <div className="seg" role="group" aria-label="Board view">
          <button aria-pressed={use3d} disabled={!can3d} title={can3d ? undefined : "The 3D view shows the top of the board only"} onClick={() => setMode("3d")}><Icon name="cube" size={11} /> 3D</button>
          <button aria-pressed={!use3d} onClick={() => setMode("2d")}><Icon name="board" size={11} /> 2D</button>
        </div>
      </div>
    </Panel>
  );

  const cmp = compare?.result ?? null;
  const dc = result ? defaultCount(result.assumptions) : 0;
  const panel = (
    <Panel className="rf-charts sl-result" title={KIND_NOUN[kind]} bodyClass="rf-body"
      actions={<span className="rf-acts">{primary && result && <span className={`stamp ${trustOf(result).ok ? "" : "sig"}`}>{trustOf(result).ok ? "Checks passed" : "Failed checks"}</span>}{pair && <span className="stamp solid">{eff === "before" ? "Before" : eff === "after" ? "After" : PHASE_LABEL[eff]}</span>}</span>}>
      {!primary && <p className="dim">Nothing to show yet.</p>}
      {primary && !result && (
        <p className="sl-wait">{primary.waiting ? "Waiting for the solver." : isLiveRun(primary) ? `${primary.step ?? "Solving"}${primary.progress != null ? `, ${Math.round(primary.progress * 100)}%` : ""}.` : primary.error ?? `This run ${primary.status}.`}</p>
      )}
      {primary && result && result.status === "not_possible" && (
        <div className="sl-np"><span className="stamp">Not possible</span><p>{result.reason ?? "This simulation cannot run on this board."}</p><p className="dim">Nothing is invented: no result is shown. {result.assumptions.length ? "The inputs it looked at are listed below." : ""}</p></div>
      )}
      {primary && result && result.status === "ok" && (
        <>
          <Stats tiles={tilesOf(result, cmp)} defaults={dc} paired={!!cmp} claim={defaultsClaim(result.assumptions)} />
          {kind === "power" && <PowerPanel run={primary} before={cmp} />}
          {kind === "thermal" && <HeatPanel run={primary} before={cmp} />}
          {kind === "signal" && <SignalPanel run={primary} before={compare} />}
        </>
      )}
      {result && <AssumptionList assumptions={result.assumptions} />}
      {result && <TrustBlock result={result} />}
      {result && result.notes.length > 0 && <div className="sl-notes">{result.notes.map((n, i) => (<p key={i} className="dim">{n}</p>))}</div>}
      <p className="idx rf-note">Simulated, not measured. The numbers follow from the inputs listed above and the board files. They are not a lab measurement.</p>
    </Panel>
  );

  const dock = (
    <div className="rf-dock sl-dock">
      {pair && eff === "split" ? (
        <>
          <span className="idx">Before</span>
          <input type="range" min={0} max={1} step={0.002} value={splitAt} onChange={(e) => setSplitAt(+e.target.value)} aria-label="Move the divider between before and after" style={{ width: 240, accentColor: "var(--paper)" }} />
          <span className="idx">After</span>
        </>
      ) : use3d ? <span className="idx">Drag to orbit. The map lies on the top of the board.</span>
        : <span className="idx">{narrow ? "Touch the board to read a value." : "Move over the board to read a value. Scroll to zoom, drag to pan."}</span>}
    </div>
  );

  return (
    <main id="main" tabIndex={-1} className="rf sl">
      {stage}
      <header className="rf-top">
        <Link className="btn sm" href={backHref}><Icon name="back" size={13} /> {sampleNote ? "Back to home" : "Back to review"}</Link>
        <div className="rf-top-id"><b className="rf-title">Simulation lab</b><span className="rf-sub dim">{title}</span></div>
        <span style={{ flex: 1 }} />
        {!sampleNote && <Link className="btn sm ghost sl-rflink" href={`/reviews/${reviewId}/rf`}><Icon name="wave" size={13} /> RF lab</Link>}
        <span className="stamp sim" title="Computed from the board files and the inputs listed. Not a measurement."><i />Simulated, not measured</span>
      </header>
      {notice && <div className="rf-banner num"><span className="stamp fixture">Dev fixture</span> {notice}</div>}
      <div className="rf-left">{runList}{controls}</div>
      {panel}
      {dock}
    </main>
  );
}

