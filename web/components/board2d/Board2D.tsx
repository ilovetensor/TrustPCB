"use client";
import "./board2d.css";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  bboxCenter, bboxUnion, fitView, indexBoard, markAnchor, nearestPair, resolveMark, toBoard, toScreen, zoomAt,
} from "../../lib/geometry.mjs";
import type { BBox, Board, Geometry, Mark, Severity } from "../../lib/types";
import { CalloutLayer, type CalloutAnchor, type DimLine, type Rect } from "../callouts/CalloutLayer";
import { Icon } from "../ui/icons";
import type { Zone as FunctionZone } from "../../lib/api";
import { BoardLayers, ZoneLabels } from "./BoardLayers";
import type { SceneMarkup } from "./useScene";
import type { LayerState } from "./layers";

export interface View { scale: number; tx: number; ty: number }
export interface ActiveMarkItem { key: string; findingId: string | null; severity: Severity; mark: Mark }
export interface FindingMeta { n: number; title: string; severity: Severity; value?: string }
export interface Insets { l: number; r: number; t: number; b: number }
export interface FocusRequest { bbox: BBox; nonce: number; maxScale?: number }
export interface Ghost { ref: string; from: [number, number]; to: [number, number]; size: [number, number]; label: string; /** the part outline at its new place, as the server sent it */ ring?: [number, number][]; pads?: { id: string; x: number; y: number }[] }

const SEVS: Severity[] = ["critical", "high", "medium", "low", "info"];
const clampView = (v: View): View => v;

export function Board2D({
  board, marks, meta, selectedId, onSelect, layers, insets, focus, spotlight, pickedId, onPick, overlay, ghost, className, interactive = true, fitTo, externalView, onViewChange, chrome = true, scene = null, zones = [], chromeTop = 12,
}: {
  board: Board; marks: ActiveMarkItem[]; meta: Record<string, FindingMeta>; selectedId: string | null;
  onSelect: (id: string | null) => void; layers: LayerState; insets: Insets; focus?: FocusRequest | null;
  spotlight?: { targets: string[]; nonce: number } | null; pickedId?: string | null; onPick?: (id: string | null) => void;
  overlay?: (view: View) => ReactNode; ghost?: Ghost | null; className?: string;
  interactive?: boolean; fitTo?: BBox | null; externalView?: View | null; onViewChange?: (v: View) => void; chrome?: boolean;
  scene?: SceneMarkup | null; zones?: FunctionZone[]; /** top of the zoom pill, in stage pixels */ chromeTop?: number;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [view, setViewState] = useState<View | null>(null);
  const viewRef = useRef<View | null>(null);
  const userMoved = useRef(false);
  const anim = useRef(0);
  const index = useMemo(() => indexBoard(board), [board]);

  const onViewChangeRef = useRef(onViewChange);
  onViewChangeRef.current = onViewChange;
  const setView = useCallback((v: View) => {
    viewRef.current = v;
    setViewState(v);
    onViewChangeRef.current?.(v);
  }, []);
  useEffect(() => {
    const c = viewRef.current;
    if (!externalView || (c && c.scale === externalView.scale && c.tx === externalView.tx && c.ty === externalView.ty)) return;
    viewRef.current = externalView;
    setViewState(externalView);
    userMoved.current = true;
  }, [externalView]);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  // a plain margin around the board, plus whatever the caller keeps free
  const pad = useMemo(() => ({ l: insets.l + 28, r: insets.r + 28, t: insets.t + 28, b: insets.b + 28 }), [insets]);
  const fitBox = fitTo ?? board.bbox;
  const fitAll = useCallback(() => fitView(fitBox, size.w, size.h, pad), [fitBox, size.w, size.h, pad]);

  const glide = useCallback((to: View, ms = 750) => {
    cancelAnimationFrame(anim.current);
    const from = viewRef.current ?? to;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) { setView(to); return; }
    const t0 = performance.now();
    const step = (now: number) => {
      const k = Math.min(1, (now - t0) / ms);
      const e = 1 - Math.pow(1 - k, 3);
      // interpolate in log-scale so zoom feels even
      const s = Math.exp(Math.log(from.scale) + (Math.log(to.scale) - Math.log(from.scale)) * e);
      const cx = (size.w / 2 - from.tx) / from.scale, cy = (size.h / 2 - from.ty) / from.scale;
      const tcx = (size.w / 2 - to.tx) / to.scale, tcy = (size.h / 2 - to.ty) / to.scale;
      const mx = cx + (tcx - cx) * e, my = cy + (tcy - cy) * e;
      setView({ scale: s, tx: size.w / 2 - mx * s, ty: size.h / 2 - my * s });
      if (k < 1) anim.current = requestAnimationFrame(step);
    };
    anim.current = requestAnimationFrame(step);
  }, [setView, size.w, size.h]);

  // initial fit and refit on resize until the user moves the camera
  useEffect(() => {
    if (!size.w || !size.h) return;
    if (externalView && viewRef.current) return;
    if (!viewRef.current || !userMoved.current) setView(fitAll());
  }, [size.w, size.h, fitAll, setView]);

  // camera glide to evidence
  const lastFocus = useRef(0);
  useEffect(() => {
    if (!focus || !size.w || focus.nonce === lastFocus.current) return;
    lastFocus.current = focus.nonce;
    const b: BBox = focus.bbox;
    const f = fitView([b[0] - 2, b[1] - 2, b[2] + 2, b[3] + 2], size.w, size.h, { ...pad, l: pad.l + 30, r: pad.r + 30, t: pad.t + 30, b: pad.b + 30 });
    const maxS = focus.maxScale ?? 34;
    let to: View = f;
    if (f.scale > maxS) {
      const c = bboxCenter(b);
      const availCx = (pad.l + (size.w - pad.r)) / 2, availCy = (pad.t + (size.h - pad.b)) / 2;
      to = { scale: maxS, tx: availCx - c[0] * maxS, ty: availCy - c[1] * maxS };
    }
    userMoved.current = true;
    glide(to);
  }, [focus, size.w, size.h, pad, glide]);

  /* ----------------------- pan / zoom ----------------------- */
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<number | null>(null);

  const local = (e: { clientX: number; clientY: number }): [number, number] => {
    const r = wrap.current!.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if ((e.target as Element).closest(".cl-balloon, .b2-controls, button")) return;
    cancelAnimationFrame(anim.current);
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    const [x, y] = local(e);
    pointers.current.set(e.pointerId, { x, y });
    drag.current = { x, y, moved: false };
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = Math.hypot(a.x - b.x, a.y - b.y);
    }
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const [x, y] = local(e);
    const v = viewRef.current;
    if (!v) return;
    if (!pointers.current.has(e.pointerId)) return;
    const prev = pointers.current.get(e.pointerId)!;
    pointers.current.set(e.pointerId, { x, y });
    if (pointers.current.size === 2 && pinch.current) {
      const [a, b] = [...pointers.current.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      userMoved.current = true;
      setView(zoomAt(v, (a.x + b.x) / 2, (a.y + b.y) / 2, d / pinch.current));
      pinch.current = d;
      return;
    }
    if (drag.current) {
      if (Math.hypot(x - drag.current.x, y - drag.current.y) > 4) drag.current.moved = true;
      if (drag.current.moved) {
        userMoved.current = true;
        setView(clampView({ ...v, tx: v.tx + (x - prev.x), ty: v.ty + (y - prev.y) }));
      }
    }
  };
  const onPointerUp = (e: React.PointerEvent) => {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
    const d = drag.current;
    drag.current = null;
    if (d && !d.moved && viewRef.current) {
      const [x, y] = local(e);
      const [bx, by] = toBoard(viewRef.current, x, y);
      handleClick(bx, by);
    }
  };

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const v = viewRef.current;
      if (!v) return;
      cancelAnimationFrame(anim.current);
      const r = el.getBoundingClientRect();
      const k = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0016));
      userMoved.current = true;
      setView(zoomAt(v, e.clientX - r.left, e.clientY - r.top, k, 1.5, 300));
    };
    if (!interactive) return;
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [setView, interactive]);

  /* ----------------------- resolve marks ----------------------- */
  const resolved = useMemo(
    () => marks.map((m) => ({ ...m, geoms: resolveMark(index, m.mark) as Geometry[] })),
    [marks, index],
  );

  // one balloon per finding: anchor of its pin mark, else its first mark
  const findingAnchors = useMemo(() => {
    const out = new Map<string, [number, number]>();
    for (const r of resolved) {
      if (!r.findingId || !r.geoms.length) continue;
      const have = out.has(r.findingId);
      if (!have || r.mark.type === "pin") {
        if (have && r.mark.type !== "pin") continue;
        const a = markAnchor(r.mark, r.geoms);
        if (a) out.set(r.findingId, a as [number, number]);
      }
    }
    return out;
  }, [resolved]);

  /* ----------------------- click-to-select ----------------------- */
  const handleClick = (bx: number, by: number) => {
    // hit-test: pads, footprints, tracks, vias
    let hit: string | null = null;
    const tol = 6 / (viewRef.current?.scale ?? 8);
    for (const p of board.pads) {
      if (Math.abs(p.x - bx) <= Math.max(p.w, p.h) / 2 + tol * 0.2 && Math.abs(p.y - by) <= Math.max(p.w, p.h) / 2 + tol * 0.2) { hit = p.id; break; }
    }
    if (!hit)
      for (const t of board.tracks) {
        if (t.layer === "B.Cu" && !layers.bottom) continue;
        if (t.layer === "F.Cu" && !layers.top) continue;
        const [x1, y1] = t.start, [x2, y2] = t.end;
        const dx = x2 - x1, dy = y2 - y1, l2 = dx * dx + dy * dy || 1e-9;
        const k = Math.max(0, Math.min(1, ((bx - x1) * dx + (by - y1) * dy) / l2));
        if (Math.hypot(bx - (x1 + dx * k), by - (y1 + dy * k)) < t.width / 2 + tol * 0.5) { hit = t.id; break; }
      }
    if (!hit)
      for (const v of board.vias) if (Math.hypot(v.x - bx, v.y - by) < v.diameter / 2 + tol * 0.3) { hit = v.id; break; }
    if (!hit) {
      let best: { id: string; area: number } | null = null;
      for (const f of board.footprints) {
        const [a, b, c, d] = f.bbox;
        if (bx >= a && bx <= c && by >= b && by <= d) {
          const area = (c - a) * (d - b);
          if (!best || area < best.area) best = { id: f.id, area };
        }
      }
      if (best) hit = best.id;
    }
    if (!hit) {
      onPick?.(null);
      onSelect(null);
      return;
    }
    // a pad resolves up to its part for picking, but findings may target the pad, net, or part
    const pad = board.pads.find((p) => p.id === hit);
    const ids = new Set([hit, pad ? `ref:${pad.ref}` : "", pad?.net ?? "", board.tracks.find((t) => t.id === hit)?.net ?? ""].filter(Boolean));
    const f = marks.find((m) => m.findingId && m.mark.targets.some((t) => ids.has(t)));
    if (f?.findingId) onSelect(f.findingId);
    else onSelect(null);
    onPick?.(hit);
  };

  /* ----------------------- screen-space overlay items ----------------------- */
  const v = view;
  const showIssues = layers.issues;
  const anchors: CalloutAnchor[] = useMemo(() => {
    if (!v || !showIssues) return [];
    const out: CalloutAnchor[] = [];
    for (const [fid, a] of findingAnchors) {
      const m = meta[fid];
      if (!m) continue;
      const [sx, sy] = toScreen(v, a[0], a[1]);
      out.push({ id: fid, n: m.n, sev: m.severity, ax: sx, ay: sy, title: m.title, value: m.value, priority: 5 - SEVS.indexOf(m.severity) });
    }
    return out;
  }, [v, findingAnchors, meta, showIssues]);

  const dims: DimLine[] = useMemo(() => {
    if (!v || !showIssues) return [];
    const out: DimLine[] = [];
    for (const r of resolved) {
      if (r.mark.type !== "link" || r.geoms.length < 2) continue;
      if (selectedId && r.findingId !== selectedId) continue;
      const np = nearestPair(r.geoms[0], r.geoms[1]);
      if (!np) continue;
      const a = toScreen(v, np.a[0], np.a[1]), b = toScreen(v, np.b[0], np.b[1]);
      const text = r.mark.value ? `${r.mark.value.value} ${r.mark.value.unit}` : `${np.d.toFixed(2)} mm`;
      out.push({ id: r.key, a: a as [number, number], b: b as [number, number], text, sev: r.severity, opacity: selectedId ? 1 : 0.85 });
    }
    return out;
  }, [v, resolved, showIssues, selectedId]);

  const obstacles: Rect[] = useMemo(() => {
    const out: Rect[] = [];
    if (insets.l) out.push({ x: 0, y: 0, w: insets.l, h: size.h });
    if (insets.r) out.push({ x: size.w - insets.r, y: 0, w: insets.r, h: size.h });
    if (insets.t) out.push({ x: 0, y: 0, w: size.w, h: insets.t });
    if (insets.b) out.push({ x: 0, y: size.h - insets.b, w: size.w, h: insets.b });
    return out;
  }, [insets, size.w, size.h]);

  const onClusterClick = useCallback((ids: string[]) => {
    const boxes: BBox[] = [];
    for (const id of ids) for (const r of resolved) if (r.findingId === id) for (const g of r.geoms) boxes.push(g.bbox);
    const u = bboxUnion(boxes);
    if (!u || !size.w) return;
    userMoved.current = true;
    glide(fitView([u[0] - 3, u[1] - 3, u[2] + 3, u[3] + 3], size.w, size.h, pad));
  }, [resolved, size.w, size.h, pad, glide]);

  const spot = useMemo(() => {
    if (!spotlight || !v) return null;
    const geoms = spotlight.targets.map((t) => resolveMark(index, { type: "object", targets: [t] })).flat() as Geometry[];
    const u = bboxUnion(geoms.map((g) => g.bbox));
    return u ? { bbox: u as BBox, nonce: spotlight.nonce } : null;
  }, [spotlight, index, v]);

  const ready = !!v && size.w > 0;
  const px = (n: number) => (v ? n / v.scale : n);
  // spotlight: everything outside the selected finding's geometry is dimmed
  const veil = useMemo(() => {
    if (!selectedId || !showIssues) return null;
    const holes: BBox[] = [];
    const add = (g: Geometry) => { if (g.parts) g.parts.forEach(add); else holes.push([g.bbox[0] - 1.6, g.bbox[1] - 1.6, g.bbox[2] + 1.6, g.bbox[3] + 1.6]); };
    for (const r of resolved) if (r.findingId === selectedId) r.geoms.forEach(add);
    return holes.length ? { holes: holes.slice(0, 120), opacity: 0.56 } : null;
  }, [selectedId, showIssues, resolved]);

  return (
    <div
      ref={wrap}
      className={`b2 ${className ?? ""}`}
      onPointerDown={interactive ? onPointerDown : undefined}
      onPointerMove={interactive ? onPointerMove : undefined}
      onPointerUp={interactive ? onPointerUp : undefined}
      onPointerCancel={interactive ? onPointerUp : undefined}
      style={interactive ? undefined : { cursor: "default", pointerEvents: "none" }}
      tabIndex={0}
      role="application"
      aria-label="Board viewer. Drag to pan, scroll to zoom."
      onKeyDown={(e) => {
        const cur = viewRef.current;
        if (!cur) return;
        const step = 60;
        userMoved.current = true;
        if (e.key === "ArrowLeft") setView({ ...cur, tx: cur.tx + step });
        else if (e.key === "ArrowRight") setView({ ...cur, tx: cur.tx - step });
        else if (e.key === "ArrowUp") setView({ ...cur, ty: cur.ty + step });
        else if (e.key === "ArrowDown") setView({ ...cur, ty: cur.ty - step });
        else if (e.key === "+" || e.key === "=") setView(zoomAt(cur, size.w / 2, size.h / 2, 1.25));
        else if (e.key === "-") setView(zoomAt(cur, size.w / 2, size.h / 2, 0.8));
        else if (e.key === "0") { userMoved.current = false; glide(fitAll()); }
      }}
    >
      {ready && v && (
        <>
          <svg className="b2-svg" width={size.w} height={size.h}>
            <defs>
              <filter id="b2-soft" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation={px(5)} /></filter>
              {SEVS.map((s) => (
                <pattern key={s} id={`mk-hatch-${s}`} patternUnits="userSpaceOnUse" width={px(7)} height={px(7)} patternTransform="rotate(45)">
                  <path d={`M0 0H${px(7)}`} stroke={`var(--sev-${s})`} strokeWidth={px(1)} opacity="0.55" />
                </pattern>
              ))}
            </defs>
            <g transform={`translate(${v.tx} ${v.ty}) scale(${v.scale})`}>
              <g className="b2-base"><BoardLayers board={board} layers={layers} scene={scene} functionZones={zones} /></g>
              {veil && (
                <g className="b2-veil" style={{ opacity: veil.opacity }}>
                  <mask id="b2-veil-mask" maskUnits="userSpaceOnUse" x={board.bbox[0] - 6} y={board.bbox[1] - 6} width={board.bbox[2] - board.bbox[0] + 12} height={board.bbox[3] - board.bbox[1] + 12}>
                    <rect x={board.bbox[0] - 6} y={board.bbox[1] - 6} width={board.bbox[2] - board.bbox[0] + 12} height={board.bbox[3] - board.bbox[1] + 12} fill="white" />
                    <g filter="url(#b2-soft)" fill="black">{veil.holes.map((h, i) => (<rect key={i} x={h[0]} y={h[1]} width={h[2] - h[0]} height={h[3] - h[1]} rx={px(6)} />))}</g>
                  </mask>
                  <rect x={board.bbox[0] - 6} y={board.bbox[1] - 6} width={board.bbox[2] - board.bbox[0] + 12} height={board.bbox[3] - board.bbox[1] + 12} fill="#000" mask="url(#b2-veil-mask)" />
                </g>
              )}
              <g className="b2-marks">
                {resolved.map((r) => {
                  const isSel = !r.findingId || !selectedId || r.findingId === selectedId;
                  // an area mark would cover the board at rest, so it appears only for its selected finding
                  const hidden = !showIssues || (r.mark.type === "region" && !!r.findingId && r.findingId !== selectedId);
                  if (hidden) return null;
                  return <MarkShape key={r.key} item={r} px={px} dim={!isSel} sel={!!selectedId && r.findingId === selectedId} quiet={!selectedId && resolved.length > 6} />;
                })}
                <ZoneLabels zones={zones} px={px} show={layers.groups && v.scale > 6} />
                {pickedId && <PickBox index={index} id={pickedId} px={px} />}
                {ghost && <GhostPart ghost={ghost} px={px} />}
              </g>
            </g>
            {spot && <SpotRing key={spot.nonce} view={v} bbox={spot.bbox} />}
          </svg>
          {overlay && (
            <div className="b2-overlay" style={{ transform: `translate(${v.tx}px, ${v.ty}px) scale(${v.scale})` }}>{overlay(v)}</div>
          )}
          <CalloutLayer
            anchors={anchors} dims={dims} width={size.w} height={size.h} obstacles={obstacles}
            selectedId={selectedId} onSelect={onSelect} onCluster={onClusterClick}
          />
          {ghost && <GhostLabel view={v} ghost={ghost} />}
          {chrome && (
            <div className="b2-pill" style={{ right: insets.r + 12, top: chromeTop }}>
              <div className="b2-controls">
                <button className="icon-btn" aria-label="Zoom in" onClick={() => { userMoved.current = true; setView(zoomAt(v, size.w / 2, size.h / 2, 1.4)); }}><Icon name="plus" /></button>
                <button className="icon-btn" aria-label="Zoom out" onClick={() => { userMoved.current = true; setView(zoomAt(v, size.w / 2, size.h / 2, 1 / 1.4)); }}><Icon name="minus" /></button>
                <button className="icon-btn" aria-label="Fit board" onClick={() => { userMoved.current = false; glide(fitAll()); }}><Icon name="fit" /></button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function PolyD({ g }: { g: Geometry }): string {
  if (g.kind === "polyline" || g.kind === "point") return "M" + (g.points ?? []).map((p) => p.join(" ")).join("L");
  return (g.rings ?? []).map((r) => "M" + r.map((p) => p.join(" ")).join("L") + "Z").join("");
}
function flat(g: Geometry): Geometry[] { return g.parts ? g.parts.flatMap(flat) : [g]; }

function MarkShape({ item, px, dim, sel, quiet }: { item: ActiveMarkItem & { geoms: Geometry[] }; px: (n: number) => number; dim: boolean; sel: boolean; quiet: boolean }) {
  const { mark, severity } = item;
  const col = `var(--sev-${severity})`;
  // with many findings, unselected marks stay as thin outlines until one is chosen
  const opacity = dim ? 0.1 : quiet ? 0.6 : 1;
  const parts = item.geoms.flatMap(flat);
  switch (mark.type) {
    case "path":
      return (
        <g opacity={opacity} fill="none" strokeLinecap="round" strokeLinejoin="round" className="mk mk-path">
          {parts.map((g, i) => g.kind === "polyline" ? (
            <g key={i}>
              <path d={PolyD({ g })} stroke={col} strokeWidth={px(sel ? 11 : 8)} opacity="0.16" />
              <path d={PolyD({ g })} stroke={col} strokeWidth={px(sel ? 3.2 : 2.4)} pathLength={1} className="mk-draw" />
            </g>
          ) : (
            <path key={i} d={PolyD({ g })} fill={col} fillOpacity="0.35" stroke={col} strokeWidth={px(1.4)} />
          ))}
        </g>
      );
    case "object":
      return (
        <g opacity={opacity} className="mk mk-object" strokeLinejoin="round">
          {parts.map((g, i) => g.kind === "point" ? (
            <circle key={i} cx={g.points![0][0]} cy={g.points![0][1]} r={(g.radius ?? 0.4) + px(3)} fill="none" stroke={col} strokeWidth={px(1.6)} />
          ) : g.kind === "polyline" ? (
            <g key={i} fill="none"><path d={PolyD({ g })} stroke={col} strokeWidth={px(8)} opacity="0.2" /><path d={PolyD({ g })} stroke={col} strokeWidth={px(2.4)} /></g>
          ) : (
            <g key={i}>
              <path d={PolyD({ g })} fill={col} fillOpacity="0.1" stroke={col} strokeWidth={px(7)} opacity="0.18" />
              <path d={PolyD({ g })} fill={col} fillOpacity="0.1" stroke={col} strokeWidth={px(1.6)} pathLength={1} className="mk-draw" />
            </g>
          ))}
        </g>
      );
    case "region":
      return (
        <g opacity={opacity} className="mk mk-region">
          {parts.map((g, i) => (
            <g key={i}>
              <path d={PolyD({ g })} fill={`url(#mk-hatch-${severity})`} />
              <path d={PolyD({ g })} fill={col} fillOpacity="0.05" stroke={col} strokeWidth={px(1.3)} strokeDasharray={`${px(7)} ${px(4)}`} />
            </g>
          ))}
        </g>
      );
    case "sketch":
      return (
        <g opacity={opacity} fill="none" stroke={col} strokeWidth={px(1.6)} strokeDasharray={`${px(2)} ${px(5)}`} strokeLinecap="round" className="mk mk-sketch">
          {parts.map((g, i) => (<path key={i} d={PolyD({ g })} />))}
        </g>
      );
    case "link":
      return (
        <g opacity={opacity * 0.9} className="mk mk-link" fill="none" stroke={col} strokeWidth={px(1.4)}>
          {parts.map((g, i) => (<path key={i} d={PolyD({ g })} fill={col} fillOpacity="0.12" />))}
        </g>
      );
    default:
      return null;
  }
}

function PickBox({ index, id, px }: { index: ReturnType<typeof indexBoard>; id: string; px: (n: number) => number }) {
  const g = (resolveMark(index, { type: "object", targets: [id] }) as Geometry[])[0];
  if (!g) return null;
  const [x0, y0, x1, y1] = g.bbox;
  const m = px(5);
  return (
    <g className="b2-pick">
      <rect x={x0 - m} y={y0 - m} width={x1 - x0 + 2 * m} height={y1 - y0 + 2 * m} fill="none" stroke="var(--signal)" strokeWidth={px(1.2)} strokeDasharray={`${px(5)} ${px(3)}`} />
      {[[x0 - m, y0 - m], [x1 + m, y0 - m], [x0 - m, y1 + m], [x1 + m, y1 + m]].map((p, i) => (
        <circle key={i} cx={p[0]} cy={p[1]} r={px(2.2)} fill="var(--signal)" />
      ))}
    </g>
  );
}

function GhostPart({ ghost, px }: { ghost: Ghost; px: (n: number) => number }) {
  const [w, h] = ghost.size;
  return (
    <g className="b2-ghost" fill="none" stroke="var(--paper)" strokeWidth={px(2)} strokeDasharray={`${px(5)} ${px(3)}`}>
      {ghost.ring && ghost.ring.length > 2
        ? <path d={"M" + ghost.ring.map((p) => p.join(" ")).join("L") + "Z"} fill="var(--paper)" fillOpacity="0.28" />
        : <rect x={ghost.to[0] - w / 2} y={ghost.to[1] - h / 2} width={w} height={h} fill="var(--paper)" fillOpacity="0.06" />}
      {(ghost.pads ?? []).map((p) => (<circle key={p.id} cx={p.x} cy={p.y} r={px(2.4)} strokeDasharray="none" fill="var(--paper)" fillOpacity="0.5" />))}
      <path d={`M${ghost.from[0]} ${ghost.from[1]}L${ghost.to[0]} ${ghost.to[1]}`} />
    </g>
  );
}
function GhostLabel({ view, ghost }: { view: View; ghost: Ghost }) {
  const [sx, sy] = toScreen(view, ghost.to[0], ghost.to[1]);
  return <div className="b2-ghost-label stamp" style={{ left: sx - 14, top: sy - 30, transform: "translateX(-100%)" }}>{ghost.label}</div>;
}

function SpotRing({ view, bbox }: { view: View; bbox: BBox }) {
  const [cx, cy] = bboxCenter(bbox);
  const [sx, sy] = toScreen(view, cx, cy);
  const r = Math.min(64, Math.max(14, (Math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]) / 2) * view.scale + 8));
  return (
    <g className="b2-spot" transform={`translate(${sx} ${sy})`} fill="none" stroke="var(--signal)">
      <circle r={r} className="b2-spot-a" strokeWidth="1" />
      <circle r={r} className="b2-spot-b" strokeWidth="1" />
      <path d={`M${-r - 8} 0h${12}M${r - 4} 0h${12}M0 ${-r - 8}v12M0 ${r - 4}v12`} strokeWidth="1" />
    </g>
  );
}
