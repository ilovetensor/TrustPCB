"use client";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { colorize, fmtNum, legendLabels, type OverlayMeta } from "../../lib/overlay-math.mjs";
import { cssGradient } from "../../lib/ramp.mjs";
import { lutFor, stopsFor } from "../../lib/sim-ramps.mjs";
import type { View } from "../board2d/Board2D";
import type { Ring } from "../../lib/types";

/** The outline of the board as one CSS path in the raster's own millimetres, so the heat is cut off at the board edge. */
const outlinePath = (rings: Ring[], ov: OverlayMeta) => `path(evenodd, "${rings.map((r) => `M${r.map((p) => `${(p[0] - ov.x0).toFixed(3)} ${(p[1] - ov.y0).toFixed(3)}`).join("L")}Z`).join("")}")`;

/**
 * A raster over the board, drawn in board millimetres inside Board2D's overlay slot. Cells with no value are transparent, so on a copper
 * map the heat sits exactly on the copper. The browser scales the small canvas up smoothly.
 * `split` cuts the raster at a board x: `keep` says which side stays visible, for the before and after slider.
 */
export function OverlayCanvas({ ov, data, outline, opacity, split = null, keep = "all", fadeKey }: {
  ov: OverlayMeta; data: Float32Array; outline?: Ring[] | null; opacity?: number; split?: number | null; keep?: "all" | "left" | "right"; fadeKey?: string;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const cv = ref.current;
    const ctx = cv?.getContext("2d");
    if (!cv || !ctx) return;
    ctx.putImageData(new ImageData(colorize(ov, data, lutFor(ov), 1), ov.nx, ov.ny), 0, 0);
  }, [ov, data]);
  const w = ov.nx * ov.dx, h = ov.ny * ov.dy;
  opacity ??= ov.layer === "board" ? 0.8 : 0.94;          // a map over the whole board lets the parts show through
  const clip = keep === "all" || split == null ? null : keep === "left" ? `inset(0 ${Math.max(0, ov.x0 + w - split)}px 0 0)` : `inset(0 0 0 ${Math.max(0, split - ov.x0)}px)`;
  return (
    <div key={fadeKey} className="sl-ras" style={{ position: "absolute", left: ov.x0, top: ov.y0, width: w, height: h, clipPath: clip ?? undefined, opacity }}>
      <canvas ref={ref} width={ov.nx} height={ov.ny} style={{ width: w, height: h, display: "block", clipPath: outline?.length ? outlinePath(outline, ov) : undefined }} />
    </div>
  );
}

/**
 * Reports the board point under the pointer. It listens on the board viewer that holds it, because the viewer owns the pointer events.
 * Touch has no hover, so a touch or a press also sets the point.
 */
export function HoverProbe({ view, onProbe }: { view: View; onProbe: (p: { x: number; y: number; sx: number; sy: number } | null) => void }) {
  const probe = useRef(onProbe);
  probe.current = onProbe;
  const spot = useRef<HTMLSpanElement>(null);
  const viewRef = useRef(view);
  viewRef.current = view;
  useEffect(() => {
    const host = spot.current?.closest(".b2") as HTMLElement | null;
    if (!host) return;
    const at = (e: PointerEvent) => {
      if ((e.target as Element).closest(".b2-controls, button, .sl-probe")) return;
      const r = host.getBoundingClientRect();
      const sx = e.clientX - r.left, sy = e.clientY - r.top, v = viewRef.current;
      probe.current({ x: (sx - v.tx) / v.scale, y: (sy - v.ty) / v.scale, sx, sy });
    };
    const leave = (e: PointerEvent) => { if (e.pointerType === "mouse") probe.current(null); };
    host.addEventListener("pointermove", at);
    host.addEventListener("pointerdown", at);
    host.addEventListener("pointerleave", leave);
    return () => { host.removeEventListener("pointermove", at); host.removeEventListener("pointerdown", at); host.removeEventListener("pointerleave", leave); };
  }, []);
  return <span ref={spot} style={{ position: "absolute", width: 0, height: 0 }} />;
}

/** A ring on the board at the probed point, drawn in the overlay slot so it follows pan and zoom. */
export function ProbeRing({ x, y, scale }: { x: number; y: number; scale: number }) {
  return (
    <svg style={{ position: "absolute", left: 0, top: 0, width: 1, height: 1, overflow: "visible", pointerEvents: "none" }} aria-hidden>
      <circle cx={x} cy={y} r={5 / scale} fill="none" stroke="var(--paper)" strokeWidth={1.6 / scale} />
      <circle cx={x} cy={y} r={1.3 / scale} fill="var(--paper)" />
    </svg>
  );
}

/** The raster as a plane on the top of the board in the 3D view. Rows are flipped: board y runs toward the viewer. */
export function OverlayPlane3D({ ov, data, y = 1.66 }: { ov: OverlayMeta; data: Float32Array; y?: number }) {
  const tex = useMemo(() => {
    const px = colorize(ov, data, lutFor(ov), 1);
    const flipped = new Uint8Array(px.length);
    const row = ov.nx * 4;
    for (let j = 0; j < ov.ny; j++) flipped.set(px.subarray((ov.ny - 1 - j) * row, (ov.ny - j) * row), j * row);
    const t = new THREE.DataTexture(flipped, ov.nx, ov.ny, THREE.RGBAFormat);
    t.minFilter = t.magFilter = THREE.LinearFilter;
    t.generateMipmaps = false;
    t.colorSpace = THREE.SRGBColorSpace;
    t.needsUpdate = true;
    return t;
  }, [ov, data]);
  useEffect(() => () => tex.dispose(), [tex]);
  const w = ov.nx * ov.dx, h = ov.ny * ov.dy;
  return (
    <mesh position={[ov.x0 + w / 2, y, ov.y0 + h / 2]} rotation={[-Math.PI / 2, 0, 0]} renderOrder={6}>
      <planeGeometry args={[w, h]} />
      <meshBasicMaterial map={tex} transparent opacity={0.96} depthWrite={false} toneMapped={false} />
    </mesh>
  );
}

/** The colour scale with round numbers and its unit. For a diverging scale the middle is zero. */
export function Legend({ ov, label, note }: { ov: Pick<OverlayMeta, "vmin" | "vmax" | "unit" | "scale">; label: string; note?: string }) {
  const ticks = legendLabels(ov, 4);
  return (
    <div className="sl-legend" role="img" aria-label={`Colour scale for ${label}, ${fmtNum(ov.vmin)} to ${fmtNum(ov.vmax)} ${ov.unit}`}>
      <div className="sl-legend-head"><span className="idx">{label}</span><span className="idx num">{ov.unit}</span></div>
      <div className="sl-ramp" style={{ background: cssGradient(stopsFor(ov)) }} />
      <div className="sl-ticks num" aria-hidden>
        {ticks.map((t) => (<span key={t.at} style={{ left: `${t.at * 100}%` }}>{t.v > 0 && ov.scale === "diverging" ? "+" : ""}{fmtNum(t.v)}</span>))}
      </div>
      {note && <p className="dim sl-legend-note">{note}</p>}
    </div>
  );
}
