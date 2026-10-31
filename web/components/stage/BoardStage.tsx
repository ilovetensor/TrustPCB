"use client";
import "./stage.css";
import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Board2D, type ActiveMarkItem, type FindingMeta, type FocusRequest, type Ghost, type Insets } from "../board2d/Board2D";
import { DEFAULT_LAYERS, LAYER_TOGGLES, type LayerState } from "../board2d/layers";
import { CurrentPlane3D, WaveField3D } from "../rf/WaveField3D";
import { CurrentCanvas2D, FieldCanvas2D } from "../rf/FieldCanvas2D";
import { Icon } from "../ui/icons";
import type { Board } from "../../lib/types";
import type { Zone as FunctionZone } from "../../lib/api";
import { useScene } from "../board2d/useScene";
import type { SceneInfo } from "../../lib/api";
import type { StageRf } from "../../lib/sims";
import { QUALITY_NOTE } from "../../lib/sims";

const Scene3D = dynamic(() => import("../scene3d/Scene3D").then((m) => m.Scene3D), { ssr: false, loading: () => <div className="stage-loading idx">Loading the 3D board</div> });

type StageMode = "2d" | "3d";

/** A short description of a picked board object for the "Ask about this" chip. */
export function describePick(board: Board, id: string): { title: string; sub: string } {
  if (id.startsWith("ref:")) {
    const f = board.footprints.find((x) => x.id === id);
    return { title: f ? f.ref : id.slice(4), sub: f ? `${f.value} / ${f.lib_id.split(":").pop()}` : "part" };
  }
  if (id.startsWith("pad:")) {
    const p = board.pads.find((x) => x.id === id);
    return { title: p ? `${p.ref} pin ${p.number}` : id, sub: p?.net ? `pin on ${p.net.replace(/^net:/, "")}` : "pin" };
  }
  if (id.startsWith("track:")) {
    const t = board.tracks.find((x) => x.id === id);
    return { title: id, sub: t ? `${t.layer} track on ${(t.net ?? "no net").replace(/^net:/, "")}, ${t.length.toFixed(1)} mm, ${t.width} mm wide` : "track" };
  }
  if (id.startsWith("via:")) {
    const v = board.vias.find((x) => x.id === id);
    return { title: id, sub: v ? `via on ${(v.net ?? "no net").replace(/^net:/, "")}` : "via" };
  }
  return { title: id, sub: "board object" };
}

export function BoardStage({
  board, glbUrl, marks, meta, selectedId, onSelect, insets, focus, spotlight, rf, ghost, pickedId, onPick, initialMode = "2d", rfAvailable = true, initialExplode = 0,
  scene = null, zones = [], onAsk,
}: {
  scene?: SceneInfo | null; zones?: FunctionZone[]; /** left out in the read-only demo, where the backend refuses chat */ onAsk?: (id: string) => void;
  board: Board; glbUrl: string | null; marks: ActiveMarkItem[]; meta: Record<string, FindingMeta>; selectedId: string | null; onSelect: (id: string | null) => void;
  insets: Insets; focus?: FocusRequest | null; spotlight?: { targets: string[]; nonce: number } | null; rf?: StageRf | null; ghost?: Ghost | null;
  pickedId?: string | null; onPick?: (id: string | null) => void; initialMode?: StageMode; rfAvailable?: boolean; initialExplode?: number;
}) {
  const [mode, setMode] = useState<StageMode>(initialMode);
  const sceneMarkup = useScene(scene);
  const pick = pickedId ? describePick(board, pickedId) : null;
  const [layers, setLayers] = useState<LayerState>(DEFAULT_LAYERS);
  const [explode, setExplode] = useState(initialExplode);
  const [layersOpen, setLayersOpen] = useState(false);
  const toolsRef = useRef<HTMLDivElement>(null);
  const toggle = useCallback((k: keyof LayerState) => setLayers((l) => ({ ...l, [k]: !l[k] })), []);
  useEffect(() => { setMode(initialMode); }, [initialMode]);
  useEffect(() => {
    if (!layersOpen) return;
    const away = (e: PointerEvent) => { if (!toolsRef.current?.contains(e.target as Node)) setLayersOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setLayersOpen(false); };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("pointerdown", away); document.removeEventListener("keydown", esc); };
  }, [layersOpen]);

  const exploded = explode > 0.05;
  // the default view pulls back a little so the turned board stays inside a stage that is taller than it is wide
  const view3d = useMemo(() => (exploded ? { az: 0.62, el: 0.4, scale: 1.4 } : { az: 0.5, el: 0.9, scale: 1.28 }), [exploded]);
  const overlay2d = useMemo(() => {
    if (!rf || (!layers.rf && !layers.heat)) return undefined;
    return () => (
      <>
        {layers.heat && rf.current && <CurrentCanvas2D data={rf.current} opacity={0.8} />}
        {layers.rf && rf.fieldXY && <FieldCanvas2D slice={rf.fieldXY} />}
      </>
    );
  }, [rf, layers.rf, layers.heat]);

  const toggles = mode === "2d" ? LAYER_TOGGLES : LAYER_TOGGLES.filter((t) => ["issues", "regions", "rf", "heat"].includes(t.key));

  return (
    <div className="stage">
      <div className="stage-bg" aria-hidden />
      {mode === "2d" ? (
        <Board2D
          board={board} marks={marks} meta={meta} selectedId={selectedId} onSelect={onSelect} layers={layers} insets={insets}
          focus={focus} spotlight={spotlight} overlay={overlay2d} ghost={ghost} pickedId={pickedId} onPick={onPick}
          scene={sceneMarkup} zones={zones} chromeTop={14}
        />
      ) : glbUrl ? (
        <Scene3D
          board={board} glbUrl={glbUrl} marks={layers.issues ? marks : []} meta={meta} selectedId={selectedId} onSelect={onSelect}
          explode={explode} view3d={view3d} focus={focus} insets={insets} pickedId={pickedId} onPick={onPick}
        >
          {layers.heat && rf?.current && <CurrentPlane3D data={rf.current} y={1.62 + explode * 9} />}
          {layers.rf && rf?.fieldXY && <WaveField3D slice={rf.fieldXY} y={rf.fieldXY.at_mm} lift={explode * 20} />}
          {layers.rf && rf?.fieldXZ && <WaveField3D slice={rf.fieldXZ} />}
        </Scene3D>
      ) : (
        <div className="stage-loading idx">There is no 3D model for this version.</div>
      )}
      <div className="stage-foot">
        {rf && (layers.rf || layers.heat) && (
          <div className="stage-chip" role="status">
            <span className="dim">{layers.rf && layers.heat ? "Field and current" : layers.heat ? "Surface current" : "Field"}</span>
            <span>{rf.job.ref ?? ""} {rf.job.label ?? "solver run"}{rf.f_hz ? `, at ${(rf.f_hz / 1e9).toFixed(3)} GHz` : ""}</span>
            {rf.job.quality === "screen" && <span className="stamp" title={QUALITY_NOTE}>Screen, compare only</span>}
            {rf.sample && <span className="stamp fixture">Bundled sample run</span>}
          </div>
        )}
        {pick && pickedId && (
          <div className="stage-chip" role="status">
            <span className="dim">Selected</span>
            <b>{pick.title}</b>
            <span className="dim">{pick.sub}</span>
            {onAsk && <button className="btn sm primary" onClick={() => onAsk(pickedId)}><Icon name="chat" size={13} /> Ask about this</button>}
            <button className="icon-btn" aria-label="Clear selection" onClick={() => onPick?.(null)}><Icon name="close" size={12} /></button>
          </div>
        )}
      </div>
      <div className="stage-tools" ref={toolsRef}>
        <div className="seg" role="group" aria-label="View mode">
          <button aria-pressed={mode === "2d"} onClick={() => setMode("2d")}><Icon name="board" size={14} /> 2D</button>
          <button aria-pressed={mode === "3d"} onClick={() => setMode("3d")}><Icon name="cube" size={14} /> 3D</button>
        </div>
        <button className="btn sm ghost" aria-expanded={layersOpen} aria-haspopup="true" onClick={() => setLayersOpen((o) => !o)}>
          <Icon name="layers" size={14} /> Layers <Icon name="chevd" size={11} />
        </button>
        {mode === "3d" && (
          <label className="explode">
            <Icon name="explode" size={14} /> Explode
            <input type="range" min="0" max="1" step="0.01" value={explode} onChange={(e) => setExplode(+e.target.value)} aria-label="Explode layers" />
          </label>
        )}
        {layersOpen && (
          <div className="stage-pop" role="group" aria-label="Board layers">
            {toggles.filter((t) => (t.key !== "rf" && t.key !== "heat") || rfAvailable).map((t) => (
              <button key={t.key} className="tog" title={t.key === "rf" ? "Solver field on the board" : t.key === "heat" ? "Surface current on the copper" : undefined} aria-pressed={layers[t.key]} onClick={() => toggle(t.key)}><span>{t.label}</span><i /></button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

