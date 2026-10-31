"use client";
import "./scene3d.css";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Line, OrbitControls, useGLTF } from "@react-three/drei";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as THREE from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { bboxCenter, indexBoard, markAnchor, nearestPair, resolveMark } from "../../lib/geometry.mjs";
import type { BBox, Board, Geometry, Severity } from "../../lib/types";
import type { ActiveMarkItem, FindingMeta, FocusRequest, Insets } from "../board2d/Board2D";
import { CalloutLayer, type CalloutAnchor, type DimLine, type Rect } from "../callouts/CalloutLayer";
import { buildBoardModel, type BoardModel, type LayerGroupKey } from "./boardModel";
import { FloorGrid } from "./FloorGrid";

export const EXPLODE_SPREAD = 9; // mm between layers when fully exploded
const GROUP_LIFT: Record<LayerGroupKey, number> = { botCu: -1, substrate: 0, topCu: 1, components: 2 };
const SEVS: Severity[] = ["critical", "high", "medium", "low", "info"];

interface Anchor3 {
  id: string; n: number; sev: Severity; title: string; value?: string; pos: THREE.Vector3; top: boolean;
}
interface Dim3 { id: string; a: THREE.Vector3; b: THREE.Vector3; text: string; sev: Severity; top: boolean }

/** Heights of each exploded layer at a given explode amount. */
export function layerY(group: LayerGroupKey, explode: number) {
  const base = { botCu: -0.04, substrate: 0, topCu: 1.55, components: 1.6 }[group];
  return base + GROUP_LIFT[group] * EXPLODE_SPREAD * explode;
}

export function Scene3D({
  board, glbUrl, marks, meta, selectedId, onSelect, explode, focus, insets, hero, autoRotate, pickedId, onPick,
  showMarks = true, children, view3d, shift, className, onReady, boardDims = true, bare = false, center: centerProp,
}: {
  board: Board; glbUrl: string; marks: ActiveMarkItem[]; meta: Record<string, FindingMeta>; selectedId: string | null;
  onSelect: (id: string | null) => void; explode: number; focus?: FocusRequest | null; insets: Insets; hero?: boolean;
  autoRotate?: boolean; pickedId?: string | null; onPick?: (id: string | null) => void; showMarks?: boolean;
  children?: ReactNode | ((api: { explode: number; layerY: (g: LayerGroupKey) => number }) => ReactNode);
  view3d?: { az: number; el: number; dist?: number; /** multiple of the fit distance */ scale?: number } | null; shift?: [number, number]; className?: string; onReady?: () => void; boardDims?: boolean;
  /** skip the GLB (a caller draws the board itself) */ bare?: boolean; center?: [number, number, number];
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [bx0, by0, bx1, by1] = board.bbox;
  const center = useMemo<[number, number, number]>(() => centerProp ?? [(bx0 + bx1) / 2, 0.8, (by0 + by1) / 2], [centerProp, bx0, by0, bx1, by1]);
  /** camera distance at which the whole board sits inside the free area between the panels */
  const fitDist = useMemo(() => {
    if (!size.w || !size.h) return 165;
    const fov = (32 * Math.PI) / 180;
    const freeW = Math.max(120, size.w - insets.l - insets.r - 40), freeH = Math.max(120, size.h - insets.t - insets.b - 40);
    const wx = bx1 - bx0, wz = by1 - by0;
    const R = 0.5 * Math.hypot(wx, wz) * 0.81;
    const aspect = size.w / size.h;
    const visW = Math.tan(fov / 2) * 2 * aspect, visH = Math.tan(fov / 2) * 2;
    return Math.min(2400, Math.max(60, R * 2 / Math.min((freeH / size.h) * visH, (freeW / size.w) * visW)));
  }, [size.w, size.h, insets.l, insets.r, insets.t, insets.b, bx0, by0, bx1, by1]);
  const [proj, setProj] = useState<{ anchors: CalloutAnchor[]; dims: DimLine[]; tags: { k: LayerGroupKey; x: number; y: number; side: 1 | -1 }[] }>({ anchors: [], dims: [], tags: [] });
  const index = useMemo(() => indexBoard(board), [board]);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  /* 3D anchors per finding, in board mm; y is resolved per frame from explode state */
  const anchors3: Anchor3[] = useMemo(() => {
    const out: Anchor3[] = [];
    const seen = new Map<string, { a: [number, number]; pin: boolean; top: boolean; group: "topCu" | "components" | "botCu" }>();
    for (const m of marks) {
      if (!m.findingId) continue;
      const geoms = resolveMark(index, m.mark) as Geometry[];
      if (!geoms.length) continue;
      const a = markAnchor(m.mark, geoms);
      if (!a) continue;
      const g0 = geoms[0];
      const isBottom = g0.layer === "B.Cu" || g0.side === "bottom";
      const group = m.mark.targets[0]?.startsWith("ref:") ? "components" : isBottom ? "botCu" : "topCu";
      const prev = seen.get(m.findingId);
      if (!prev || (m.mark.type === "pin" && !prev.pin)) seen.set(m.findingId, { a: a as [number, number], pin: m.mark.type === "pin", top: !isBottom, group });
    }
    for (const [fid, s] of seen) {
      const mt = meta[fid];
      if (!mt) continue;
      out.push({ id: fid, n: mt.n, sev: mt.severity, title: mt.title, value: mt.value, pos: new THREE.Vector3(s.a[0], s.group === "components" ? 2.4 : 1.6, s.a[1]), top: s.top });
      (out[out.length - 1] as Anchor3 & { group?: string }).group = s.group;
    }
    return out;
  }, [marks, meta, index]);

  const dims3: Dim3[] = useMemo(() => {
    const out: Dim3[] = [];
    for (const m of marks) {
      if (m.mark.type !== "link") continue;
      const geoms = resolveMark(index, m.mark) as Geometry[];
      if (geoms.length < 2) continue;
      const np = nearestPair(geoms[0], geoms[1]);
      if (!np) continue;
      out.push({
        id: m.key, a: new THREE.Vector3(np.a[0], 1.7, np.a[1]), b: new THREE.Vector3(np.b[0], 1.7, np.b[1]),
        text: m.mark.value ? `${m.mark.value.value} ${m.mark.value.unit}` : `${np.d.toFixed(2)} mm`, sev: m.severity, top: true,
      });
    }
    return out;
  }, [marks, index]);

  const obstacles: Rect[] = useMemo(() => {
    const out: Rect[] = [];
    if (insets.l) out.push({ x: 0, y: 0, w: insets.l, h: size.h });
    if (insets.r) out.push({ x: size.w - insets.r, y: 0, w: insets.r, h: size.h });
    if (insets.t) out.push({ x: 0, y: 0, w: size.w, h: insets.t });
    if (insets.b) out.push({ x: 0, y: size.h - insets.b, w: size.w, h: insets.b });
    return out;
  }, [insets, size]);

  const onCluster = useCallback(() => {}, []);

  return (
    <div ref={wrap} className={`s3 ${className ?? ""}`}>
      {size.w > 0 && (
        <Canvas
          dpr={[1, 2]}
          camera={{ fov: 32, near: 5, far: 4000, position: [95, 100, 140] }}
          gl={{ antialias: true, alpha: true, powerPreference: "high-performance", preserveDrawingBuffer: true }}
          onCreated={({ gl }) => { gl.setClearColor(0x000000, 0); gl.toneMapping = THREE.ACESFilmicToneMapping; gl.toneMappingExposure = 1.05; }}
        >
          <Suspense fallback={null}>
            <Env />
            <Lights />
            <FloorGrid center={[center[0], center[2]]} />
            {!bare && <BoardScene
              board={board} glbUrl={glbUrl} explode={explode} marks={marks} index={index} selectedId={selectedId} showMarks={showMarks}
              pickedId={pickedId ?? null} onPick={(id, hitFinding) => { if (hitFinding) onSelect(hitFinding); else onSelect(null); onPick?.(id); }}
              onReady={onReady}
            />}
            {typeof children === "function" ? children({ explode, layerY: (g) => layerY(g, explode) }) : children}
            <CameraRig centre3={center} fitDist={fitDist} focus={focus ?? null} insets={insets} hero={!!hero} autoRotate={!!autoRotate} view3d={view3d ?? null} shift={shift ?? null} size={size} />
            <Projector anchors={anchors3} dims={dims3} explode={explode} size={size} onUpdate={setProj} boardDims={boardDims && !hero} selectedId={selectedId} insets={insets} bbox={board.bbox} />
          </Suspense>
        </Canvas>
      )}
      {proj.tags.map((t) => (
        <div key={t.k} className="layer-tag mono" style={{ left: t.x, top: t.y, flexDirection: t.side === 1 ? "row" : "row-reverse", transform: `translate(${t.side === 1 ? 0 : "-100%"}, -50%)` }}>
          <i /><span>{LAYER_NAMES[t.k][0]}</span> {LAYER_NAMES[t.k][1]}
        </div>
      ))}
      {showMarks && (
        <CalloutLayer
          anchors={proj.anchors.filter((a) => a.opacity !== 0)} dims={proj.dims} width={size.w} height={size.h} obstacles={obstacles}
          selectedId={selectedId} onSelect={onSelect} onCluster={onCluster}
        />
      )}
    </div>
  );
}

function Env() {
  const { gl, scene } = useThree();
  useEffect(() => {
    const pm = new THREE.PMREMGenerator(gl);
    const env = pm.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environment = env;
    scene.environmentIntensity = 0.42;
    return () => { scene.environment = null; env.dispose(); pm.dispose(); };
  }, [gl, scene]);
  return null;
}

function Lights() {
  return (
    <>
      <ambientLight intensity={0.25} color="#8f8cff" />
      <directionalLight position={[-60, 120, 70]} intensity={2.3} color="#fff3e0" />
      <directionalLight position={[90, 40, -90]} intensity={1.7} color="#7a6bff" />
      <directionalLight position={[40, -60, 40]} intensity={0.5} color="#b54a9a" />
    </>
  );
}

function BoardScene({
  board, glbUrl, explode, marks, index, selectedId, showMarks, pickedId, onPick, onReady,
}: {
  board: Board; glbUrl: string; explode: number; marks: ActiveMarkItem[]; index: ReturnType<typeof indexBoard>;
  selectedId: string | null; showMarks: boolean; pickedId: string | null; onPick: (id: string | null, finding: string | null) => void; onReady?: () => void;
}) {
  const gltf = useGLTF(glbUrl);
  const refs = useMemo(() => new Set(board.footprints.map((f) => f.ref)), [board]);
  const model: BoardModel = useMemo(() => buildBoardModel(gltf.scene.clone(true), refs), [gltf, refs]);
  const groupRefs = useRef<Record<LayerGroupKey, THREE.Group | null>>({ components: null, topCu: null, substrate: null, botCu: null });
  const eased = useRef(explode);
  const readyFired = useRef(false);

  useFrame((_, dt) => {
    eased.current += (explode - eased.current) * Math.min(1, dt * 5);
    (Object.keys(groupRefs.current) as LayerGroupKey[]).forEach((k) => {
      const g = groupRefs.current[k];
      if (g) g.position.y = GROUP_LIFT[k] * EXPLODE_SPREAD * eased.current;
    });
    if (!readyFired.current) { readyFired.current = true; onReady?.(); }
  });

  const handle = (e: { object: THREE.Object3D; delta: number; stopPropagation: () => void }) => {
    if (e.delta > 4) return;
    e.stopPropagation();
    const ref = e.object.userData.ref as string | undefined;
    if (!ref) { onPick(null, null); return; }
    const id = `ref:${ref}`;
    const f = marks.find((m) => m.findingId && m.mark.targets.includes(id));
    onPick(id, f?.findingId ?? null);
  };

  return (
    <group onClick={handle as never}>
      {(Object.keys(model.groups) as LayerGroupKey[]).map((k) => (
        <group key={k} ref={(el) => { groupRefs.current[k] = el; }}>
          <primitive object={model.groups[k]} />
        </group>
      ))}
      {showMarks && <Marks3D marks={marks} index={index} selectedId={selectedId} explode={explode} pickedId={pickedId} />}
    </group>
  );
}

const LAYER_NAMES: Record<LayerGroupKey, [string, string]> = {
  components: ["01", "PARTS, TOP"], topCu: ["02", "F.Cu + PADS"], substrate: ["03", "SUBSTRATE + VIAS"], botCu: ["04", "B.Cu"],
};
const LAYER_Y: Record<LayerGroupKey, number> = { botCu: 0, substrate: 0.8, topCu: 1.55, components: 2.6 };

/* Marks drawn in the 3D scene: tracks as fat lines, parts as box outlines, regions as outlined fills. */
function Marks3D({ marks, index, selectedId, explode, pickedId }: { marks: ActiveMarkItem[]; index: ReturnType<typeof indexBoard>; selectedId: string | null; explode: number; pickedId: string | null }) {
  const g = useRef<THREE.Group>(null);
  const ex = useRef(explode);
  useFrame((_, dt) => {
    ex.current += (explode - ex.current) * Math.min(1, dt * 5);
    if (g.current) g.current.position.y = GROUP_LIFT.topCu * EXPLODE_SPREAD * ex.current;
  });
  const colors = useMemo(() => Object.fromEntries(SEVS.map((s) => [s, getComputedColor(`--sev-${s}`)])) as Record<Severity, string>, []);
  const resolved = useMemo(() => marks.map((m) => ({ m, geoms: resolveMark(index, m.mark) as Geometry[] })), [marks, index]);
  const flat = (x: Geometry): Geometry[] => (x.parts ? x.parts.flatMap(flat) : [x]);
  const picked = pickedId ? (resolveMark(index, { type: "object", targets: [pickedId] }) as Geometry[])[0] : null;
  return (
    <group ref={g}>
      {resolved.map(({ m, geoms }) => {
        const dim = selectedId && m.findingId !== selectedId;
        const color = colors[m.severity];
        const opacity = dim ? 0.25 : 1;
        return geoms.flatMap(flat).map((geo, i) => {
          const key = `${m.key}:${i}`;
          if (m.mark.type === "path" || (m.mark.type === "object" && geo.kind === "polyline")) {
            if (geo.kind !== "polyline") return null;
            const y = geo.layer === "B.Cu" ? -0.05 : 1.7;
            return <Line key={key} points={geo.points!.map((p) => [p[0], y + 0.05, p[1]] as [number, number, number])} color={color} lineWidth={3.2} transparent opacity={opacity} />;
          }
          if (m.mark.type === "sketch" && geo.kind === "polyline") {
            return <Line key={key} points={geo.points!.map((p) => [p[0], 2, p[1]] as [number, number, number])} color={color} lineWidth={2} dashed dashSize={0.8} gapSize={0.8} transparent opacity={opacity} />;
          }
          if (geo.kind === "polygon" && (m.mark.type === "object" || m.mark.type === "region")) {
            const ring = geo.rings![0];
            const yy = m.mark.type === "region" ? 1.75 : m.mark.targets[0].startsWith("ref:") ? 2.6 : 1.8;
            const pts = [...ring, ring[0]].map((p) => [p[0], yy, p[1]] as [number, number, number]);
            return (
              <group key={key}>
                <Line points={pts} color={color} lineWidth={m.mark.type === "region" ? 1.6 : 2.6} dashed={m.mark.type === "region"} dashSize={1} gapSize={0.7} transparent opacity={opacity} />
                {m.mark.type === "region" && <RegionFill ring={ring} y={yy - 0.05} color={color} opacity={0.1 * opacity} />}
                {m.mark.type === "object" && geo.bbox && <BoxOutline bbox={geo.bbox} color={color} opacity={opacity} />}
              </group>
            );
          }
          if (geo.kind === "point") return <mesh key={key} position={[geo.points![0][0], 1.8, geo.points![0][1]]}><sphereGeometry args={[0.7, 12, 12]} /><meshBasicMaterial color={color} transparent opacity={opacity} /></mesh>;
          return null;
        });
      })}
      {picked && <BoxOutline bbox={picked.bbox} color="#ff4a1f" opacity={1} dashed />}
    </group>
  );
}

function getComputedColor(v: string): string {
  if (typeof document === "undefined") return "#fff";
  return getComputedStyle(document.documentElement).getPropertyValue(v).trim() || "#fff";
}

function RegionFill({ ring, y, color, opacity }: { ring: [number, number][]; y: number; color: string; opacity: number }) {
  const geom = useMemo(() => {
    const shape = new THREE.Shape(ring.map((p) => new THREE.Vector2(p[0], p[1])));
    const g = new THREE.ShapeGeometry(shape);
    g.rotateX(Math.PI / 2); // lie in xz; shape (x, y) -> (x, 0, y)
    g.translate(0, y, 0);
    return g;
  }, [ring, y]);
  return <mesh geometry={geom}><meshBasicMaterial color={color} transparent opacity={opacity} side={THREE.DoubleSide} depthWrite={false} /></mesh>;
}

function BoxOutline({ bbox, color, opacity, dashed }: { bbox: BBox; color: string; opacity: number; dashed?: boolean }) {
  const [x0, y0, x1, y1] = bbox;
  const h0 = 1.6, h1 = 2.7;
  const tp = (y: number) => [[x0, y, y0], [x1, y, y0], [x1, y, y1], [x0, y, y1], [x0, y, y0]] as [number, number, number][];
  return (
    <group>
      <Line points={tp(h0)} color={color} lineWidth={1.4} transparent opacity={opacity} dashed={dashed} dashSize={0.6} gapSize={0.4} />
      <Line points={tp(h1)} color={color} lineWidth={2.2} transparent opacity={opacity} dashed={dashed} dashSize={0.6} gapSize={0.4} />
      {([[x0, y0], [x1, y0], [x1, y1], [x0, y1]] as const).map((c, i) => (
        <Line key={i} points={[[c[0], h0, c[1]], [c[0], h1, c[1]]]} color={color} lineWidth={1.2} transparent opacity={opacity * 0.8} />
      ))}
    </group>
  );
}

/* Camera: orbit, plus gliding to evidence and the hero's slow turn */
function CameraRig({ centre3, fitDist, focus, insets, hero, autoRotate, view3d, shift, size }: {
  fitDist: number;
  focus: FocusRequest | null; insets: Insets; hero: boolean; autoRotate: boolean;
  view3d: { az: number; el: number; dist?: number; scale?: number } | null; shift: [number, number] | null; size: { w: number; h: number }; centre3: [number, number, number];
}) {
  const { camera, invalidate } = useThree();
  const controls = useRef<any>(null);
  const goal = useRef<{ pos: THREE.Vector3; tgt: THREE.Vector3; t0: number } | null>(null);
  const lastFocus = useRef(0);
  const centre = useMemo(() => new THREE.Vector3(...centre3), [centre3]);

  // frame the board once, shifted into the free region between panels
  useEffect(() => {
    const cam = camera as THREE.PerspectiveCamera;
    cam.aspect = size.w / size.h;
    const sx = shift ? shift[0] : (insets.l - insets.r) / 2 / size.w;
    const sy = shift ? shift[1] : (insets.t - insets.b) / 2 / size.h;
    // negative offset moves the image right/down
    cam.setViewOffset(size.w, size.h, -sx * size.w, -sy * size.h, size.w, size.h);
    cam.updateProjectionMatrix();
  }, [camera, size.w, size.h, insets, shift]);

  useEffect(() => {
    const el = view3d?.el ?? (hero ? 0.62 : 0.9);
    const az = view3d?.az ?? (hero ? 0.62 : 0.5);
    const dist = view3d?.dist ?? fitDist * (view3d?.scale ?? (hero ? 1.2 : 1));
    camera.position.set(centre.x + Math.sin(az) * Math.cos(el) * dist, centre.y + Math.sin(el) * dist, centre.z + Math.cos(az) * Math.cos(el) * dist);
    controls.current?.target.copy(centre);
    controls.current?.update();
  }, [camera, centre, hero, view3d, fitDist]);

  useEffect(() => {
    if (!focus || focus.nonce === lastFocus.current) return;
    lastFocus.current = focus.nonce;
    const c = bboxCenter(focus.bbox);
    const span = Math.max(focus.bbox[2] - focus.bbox[0], focus.bbox[3] - focus.bbox[1], 8);
    const tgt = new THREE.Vector3(c[0], 1.6, c[1]);
    const dir = camera.position.clone().sub(controls.current?.target ?? centre).normalize();
    const dist = Math.min(200, Math.max(100, span * 5.6));
    dir.y = Math.max(dir.y, 0.55);
    dir.normalize();
    goal.current = { pos: tgt.clone().add(dir.multiplyScalar(dist)), tgt, t0: performance.now() };
  }, [focus, camera, centre]);

  useFrame(() => {
    const g = goal.current;
    if (g && controls.current) {
      const k = Math.min(1, (performance.now() - g.t0) / 900);
      const e = 1 - Math.pow(1 - k, 3);
      const reduce = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const f = reduce ? 1 : 0.07 + 0.1 * e;
      camera.position.lerp(g.pos, f);
      controls.current.target.lerp(g.tgt, f);
      controls.current.update();
      if (k >= 1 && camera.position.distanceTo(g.pos) < 0.2) goal.current = null;
      invalidate();
    }
  });

  return (
    <OrbitControls
      ref={controls} makeDefault enableDamping dampingFactor={0.08} minDistance={22} maxDistance={420}
      autoRotate={autoRotate} autoRotateSpeed={hero ? 0.55 : 0.4} maxPolarAngle={Math.PI * 0.62}
      onStart={() => { goal.current = null; }} target={centre3}
    />
  );
}

/* Projects mark anchors into screen space every frame (throttled by change). */
function Projector({ anchors, dims, explode, size, onUpdate, boardDims, selectedId, insets, bbox }: {
  bbox: BBox;
  anchors: Anchor3[]; dims: Dim3[]; explode: number; size: { w: number; h: number };
  onUpdate: (v: { anchors: CalloutAnchor[]; dims: DimLine[]; tags: { k: LayerGroupKey; x: number; y: number; side: 1 | -1 }[] }) => void; boardDims: boolean; selectedId: string | null; insets: Insets;
}) {
  const { camera } = useThree();
  const last = useRef<string>("");
  const ex = useRef(explode);
  const v = useMemo(() => new THREE.Vector3(), []);
  useFrame((_, dt) => {
    ex.current += (explode - ex.current) * Math.min(1, dt * 5);
    const lift = (group: string) => (group === "components" ? 2 : group === "botCu" ? -1 : 1) * EXPLODE_SPREAD * ex.current;
    const proj = (p: THREE.Vector3, dy: number): [number, number, number] => {
      v.set(p.x, p.y + dy, p.z).project(camera);
      return [(v.x * 0.5 + 0.5) * size.w, (-v.y * 0.5 + 0.5) * size.h, v.z];
    };
    const out: CalloutAnchor[] = [];
    for (const a of anchors) {
      const group = (a as Anchor3 & { group?: string }).group ?? "topCu";
      const [sx, sy, z] = proj(a.pos, lift(group));
      // fade when the camera is on the far side of the board from the target
      const camY = camera.position.y;
      const targetY = a.pos.y + lift(group);
      const side = a.top ? camY - targetY : targetY - camY;
      const behind = Math.min(1, Math.max(0, (side + 6) / 26));
      const opacity = z > 1 || z < -1 ? 0 : behind;
      out.push({ id: a.id, n: a.n, sev: a.sev, ax: sx, ay: sy, title: a.title, value: a.value, opacity, priority: 5 - SEVS.indexOf(a.sev) });
    }
    const dimOut: DimLine[] = [];
    for (const d of dims) {
      const [ax, ay] = proj(d.a, lift("topCu"));
      const [bx, by] = proj(d.b, lift("topCu"));
      if (selectedId === null || out.find((o) => o.id === selectedId)) {
        const sel = anchors.find((a) => a.id === selectedId);
        if (selectedId && !sel) continue;
        dimOut.push({ id: d.id, a: [ax, ay], b: [bx, by], text: d.text, sev: d.sev, opacity: selectedId ? 1 : 0.8 });
      }
    }
    if (boardDims) {
      const c = (x: number, z: number) => proj(new THREE.Vector3(x, 0, z), 0);
      const [x0, z0, x1, z1] = bbox;
      const p1 = c(x0, z0 - 5), p2 = c(x1, z0 - 5), p3 = c(x1 + 5, z0), p4 = c(x1 + 5, z1);
      dimOut.push({ id: "dim-w", a: [p1[0], p1[1]], b: [p2[0], p2[1]], text: `${(x1 - x0).toFixed(1)} mm`, sev: "info", opacity: 0.55 });
      dimOut.push({ id: "dim-h", a: [p3[0], p3[1]], b: [p4[0], p4[1]], text: `${(z1 - z0).toFixed(1)} mm`, sev: "info", opacity: 0.55 });
    }
    const tags: { k: LayerGroupKey; x: number; y: number; side: 1 | -1 }[] = [];
    if (ex.current > 0.25) {
      (Object.keys(LAYER_Y) as LayerGroupKey[]).forEach((k) => {
        const lift2 = GROUP_LIFT[k] * EXPLODE_SPREAD * ex.current;
        const r = proj(new THREE.Vector3(bbox[2] + 6, LAYER_Y[k] + lift2, (bbox[1] + bbox[3]) / 2), 0);
        const l = proj(new THREE.Vector3(bbox[0] - 6, LAYER_Y[k] + lift2, (bbox[1] + bbox[3]) / 2), 0);
        const roomR = size.w - insets.r - r[0], roomL = l[0] - insets.l;
        tags.push(roomR >= roomL ? { k, x: r[0], y: r[1], side: 1 } : { k, x: l[0], y: l[1], side: -1 });
      });
    }
    const sig = tags.map((t) => `${t.k}${t.x.toFixed(0)},${t.y.toFixed(0)}`).join("") + out.map((o) => `${o.id}:${o.ax.toFixed(0)},${o.ay.toFixed(0)},${(o.opacity ?? 1).toFixed(1)}`).join("|") + "#" + dimOut.map((d) => `${d.id}:${d.a[0].toFixed(0)},${d.a[1].toFixed(0)},${d.b[0].toFixed(0)},${d.b[1].toFixed(0)}`).join("|");
    if (sig !== last.current) {
      last.current = sig;
      onUpdate({ anchors: out, dims: dimOut, tags });
    }
  });
  return null;
}

