"use client";
import "./stage.css";
import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Board2D, type ActiveMarkItem, type FindingMeta, type FocusRequest, type Ghost, type Insets } from "../board2d/Board2D";
import { DEFAULT_LAYERS, LAYER_TOGGLES, type LayerState } from "../board2d/layers";
import { WaveField3D } from "../rf/WaveField3D";
import { CurrentCanvas2D, FieldCanvas2D } from "../rf/FieldCanvas2D";
import { Icon } from "../ui/icons";
import type { Board } from "../../lib/types";
import type { Zone as FunctionZone } from "../../lib/api";
import { useScene } from "../board2d/useScene";
import type { SceneInfo } from "../../lib/api";
import type { RfBundle } from "../../lib/rf-data";

const Scene3D = dynamic(() => import("../scene3d/Scene3D").then((m) => m.Scene3D), { ssr: false, loading: () => <div className="stage-loading mono idx">LOADING 3D BOARD</div> });

export type StageMode = "2d" | "3d";

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
  board, glbUrl, marks, meta, selectedId, onSelect, insets, focus, spotlight, rf, ghost, pickedId, onPick, initialMode = "2d", toolbarOffset = 0, rfAvailable = true, initialExplode = 0,
  scene = null, zones = [], onAsk, toolbarTop,
}: {
  scene?: SceneInfo | null; zones?: FunctionZone[]; onAsk?: (id: string) => void; toolbarTop?: number;
  board: Board; glbUrl: string | null; marks: ActiveMarkItem[]; meta: Record<string, FindingMeta>; selectedId: string | null; onSelect: (id: string | null) => void;
  insets: Insets; focus?: FocusRequest | null; spotlight?: { targets: string[]; nonce: number } | null; rf?: RfBundle | null; ghost?: Ghost | null;
  pickedId?: string | null; onPick?: (id: string | null) => void; initialMode?: StageMode; toolbarOffset?: number; rfAvailable?: boolean; initialExplode?: number;
}) {
  const [mode, setMode] = useState<StageMode>(initialMode);
  const sceneMarkup = useScene(scene);
  const top = toolbarTop ?? insets.t + 34 + toolbarOffset;
  const pick = pickedId ? describePick(board, pickedId) : null;
  const [layers, setLayers] = useState<LayerState>(DEFAULT_LAYERS);
  const [explode, setExplode] = useState(initialExplode);
  const toggle = useCallback((k: keyof LayerState) => setLayers((l) => ({ ...l, [k]: !l[k] })), []);
  useEffect(() => { setMode(initialMode); }, [initialMode]);

  const exploded = explode > 0.05;
  const view3d = useMemo(() => (exploded ? { az: 0.62, el: 0.4, scale: 1.25 } : null), [exploded]);
  const overlay2d = useMemo(() => {
    if (!rf || (!layers.rf && !layers.heat)) return undefined;
    return () => (
      <>
        {layers.heat && rf.current && <CurrentCanvas2D data={rf.current} />}
        {layers.rf && rf.fieldXY && <FieldCanvas2D slice={rf.fieldXY} />}
      </>
    );
  }, [rf, layers.rf, layers.heat]);

  const toggles = mode === "2d" ? LAYER_TOGGLES : LAYER_TOGGLES.filter((t) => ["issues", "regions", "rf"].includes(t.key));

  return (
    <div className="stage">
      <div className="stage-bg" aria-hidden />
      {mode === "2d" ? (
        <Board2D
          board={board} marks={marks} meta={meta} selectedId={selectedId} onSelect={onSelect} layers={layers} insets={insets}
          focus={focus} spotlight={spotlight} overlay={overlay2d} ghost={ghost} pickedId={pickedId} onPick={onPick}
          scene={sceneMarkup} zones={zones} chromeTop={top}
        />
      ) : glbUrl ? (
        <Scene3D
          board={board} glbUrl={glbUrl} marks={layers.issues ? marks.filter((m) => m.mark.type !== "region" || layers.regions) : []} meta={meta} selectedId={selectedId} onSelect={onSelect}
          explode={explode} view3d={view3d} focus={focus} insets={insets} pickedId={pickedId} onPick={onPick}
        >
          {layers.rf && rf?.fieldXY && <WaveField3D slice={rf.fieldXY} y={2.4} lift={explode * 20} />}
          {layers.rf && rf?.fieldXZ && <WaveField3D slice={rf.fieldXZ} />}
        </Scene3D>
      ) : (
        <div className="stage-loading mono idx">NO 3D MODEL FOR THIS VERSION</div>
      )}
      {pick && pickedId && (
        <div className="pick-chip hair panel" style={{ left: insets.l + 12, bottom: insets.b + 12 }} role="status">
          <div className="pick-t"><span className="idx">SELECTED</span><b>{pick.title}</b><span className="dim">{pick.sub}</span></div>
          {onAsk && <button className="btn sm primary" onClick={() => onAsk(pickedId)}><Icon name="chat" size={11} /> Ask about this</button>}
          <button className="icon-btn" aria-label="Clear selection" onClick={() => onPick?.(null)}><Icon name="close" size={11} /></button>
        </div>
      )}
      <div className="stage-tools hair panel" style={{ left: insets.l + 12, top }}>
        <div className="seg" role="group" aria-label="View mode">
          <button aria-pressed={mode === "2d"} onClick={() => setMode("2d")}><Icon name="board" size={12} /> 2D</button>
          <button aria-pressed={mode === "3d"} onClick={() => setMode("3d")}><Icon name="cube" size={12} /> 3D</button>
        </div>
        <span className="vr" />
        {toggles.filter((t) => (t.key !== "rf" && t.key !== "heat") || rfAvailable).map((t) => (
          <button key={t.key} className="tog" aria-pressed={layers[t.key]} onClick={() => toggle(t.key)}><i />{t.label}</button>
        ))}
        {mode === "3d" && (
          <>
            <span className="vr" />
            <label className="explode">
              <Icon name="layers" size={13} /> <span className="idx">EXPLODE</span>
              <input type="range" min="0" max="1" step="0.01" value={explode} onChange={(e) => setExplode(+e.target.value)} aria-label="Explode layers" />
            </label>
          </>
        )}
      </div>
    </div>
  );
}

export function StageSlot({ children }: { children: ReactNode }) { return <>{children}</>; }
