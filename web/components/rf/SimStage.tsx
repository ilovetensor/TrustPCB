"use client";
import dynamic from "next/dynamic";
import { Line } from "@react-three/drei";
import { useMemo } from "react";
import type { Board } from "../../lib/types";
import type { CurrentData, PatternData, PulseData } from "../../lib/rf-data";
import type { Fields, SimMeta } from "../../lib/sims";
import type { SceneInfo } from "../../lib/api";
import { useScene } from "../board2d/useScene";
import { Board2D, type Insets } from "../board2d/Board2D";
import { DEFAULT_LAYERS } from "../board2d/layers";
import { CurrentCanvas2D, FieldCanvas2D, PulseCanvas2D } from "./FieldCanvas2D";
import { CurrentPlane3D, PulsePlane3D, WaveField3D } from "./WaveField3D";
import { RadiationBalloon } from "./RadiationBalloon";
import { boardYScale } from "../scene3d/boardModel";

const Scene3D = dynamic(() => import("../scene3d/Scene3D").then((m) => m.Scene3D), { ssr: false, loading: () => <div className="rf-loading idx">Loading the 3D board</div> });

export type SimView = "wave" | "pulse" | "pattern" | "current";
/** Where the field plane is drawn: on the board's top face, or at the height the solver sampled it. */
export type FieldPlane = "top" | "solver";
const TOP_FACE = 1.6;

/**
 * The solver samples its slice at a height inside the stack-up. On a thick or many-layer board that is inside the opaque
 * board, where nobody can see it. `inside` says so; `atMm` is the height the solver used, on the same scale as the drawn board.
 */
export function planeInfo(board: Board, view: SimView, fields?: Fields | null, current?: CurrentData | null, pulse?: PulseData | null) {
  const ky = boardYScale(board);
  const raw = view === "wave" ? fields?.xy?.at_mm : view === "pulse" ? pulse?.at_mm : view === "current" ? current?.at_mm : undefined;
  const drawn = raw == null ? null : raw * ky;
  return { ky, atMm: raw ?? null, inside: drawn != null && drawn < TOP_FACE - 0.2 };
}
const noop = () => {};
const none = [] as never[];
const noMeta = {};

/** What the solver modelled, in board millimetres: the region, the antenna copper and the port. */
function useOutline(meta: SimMeta | null | undefined) {
  return useMemo(() => {
    const r = meta?.region, a = meta?.setup?.antenna_bbox, p = meta?.setup?.port_xy;
    return { region: r && r.length === 4 ? (r as [number, number, number, number]) : null, antenna: a && a.length === 4 ? (a as [number, number, number, number]) : null, port: p ?? null };
  }, [meta]);
}

function Outline3D({ meta, y = 1.7 }: { meta: SimMeta | null | undefined; y?: number }) {
  const { region, port } = useOutline(meta);
  return (
    <>
      {region && <Line points={[[region[0], y, region[1]], [region[2], y, region[1]], [region[2], y, region[3]], [region[0], y, region[3]], [region[0], y, region[1]]]} color="#c7c7cc" lineWidth={1} dashed dashSize={1.2} gapSize={1} transparent opacity={0.5} />}
      {port && (
        <group position={[port[0], y + 0.1, port[1]]}>
          <mesh rotation={[-Math.PI / 2, 0, 0]}><ringGeometry args={[0.9, 1.3, 32]} /><meshBasicMaterial color="#ff4a1f" transparent opacity={0.95} depthWrite={false} /></mesh>
          <Line points={[[0, 0, 0], [0, 7, 0]]} color="#ff4a1f" lineWidth={1} transparent opacity={0.8} />
        </group>
      )}
    </>
  );
}

function Outline2D({ meta }: { meta: SimMeta | null | undefined }) {
  const { region, antenna, port } = useOutline(meta);
  return (
    <svg style={{ position: "absolute", left: 0, top: 0, width: 1, height: 1, overflow: "visible", pointerEvents: "none" }}>
      {region && <rect x={region[0]} y={region[1]} width={region[2] - region[0]} height={region[3] - region[1]} fill="none" stroke="#c7c7cc" strokeOpacity="0.6" strokeDasharray="4 3" vectorEffect="non-scaling-stroke" />}
      {antenna && <rect x={antenna[0]} y={antenna[1]} width={antenna[2] - antenna[0]} height={antenna[3] - antenna[1]} fill="none" stroke="#ff4a1f" strokeOpacity="0.55" strokeDasharray="2 3" vectorEffect="non-scaling-stroke" />}
      {port && <circle cx={port[0]} cy={port[1]} r={1.1} fill="none" stroke="#ff4a1f" strokeWidth="1.6" vectorEffect="non-scaling-stroke" />}
    </svg>
  );
}

interface SimStageProps {
  board: Board; glbUrl: string | null; sceneInfo?: SceneInfo | null;
  view: SimView; mode: "2d" | "3d"; meta: SimMeta | null | undefined;
  fields?: Fields | null; current?: CurrentData | null; pulse?: PulseData | null; pattern?: PatternData | null;
  frame: number; paused: boolean; calm: boolean; insets: Insets; plane?: FieldPlane;
}

/** The solver's field drawn on the review's own board: the GLB from its version in 3D, or the vector board in 2D. */
export function SimStage({ board, glbUrl, sceneInfo = null, view, mode, meta, fields, current, pulse, pattern, frame, paused, calm, insets, plane = "top" }: SimStageProps) {
  const sceneMarkup = useScene(sceneInfo);
  const { antenna } = useOutline(meta);
  const balloonAt = useMemo<[number, number, number]>(() => (antenna ? [(antenna[0] + antenna[2]) / 2, 3, (antenna[1] + antenna[3]) / 2] : [(board.bbox[0] + board.bbox[2]) / 2, 3, (board.bbox[1] + board.bbox[3]) / 2]), [antenna, board]);
  const use3d = mode === "3d" || view === "pattern";
  const { ky, inside } = planeInfo(board, view, fields, current, pulse);
  // inside the board: on top it sits just above the surface; in the solver's plane it is drawn through the board
  const seeThrough = inside && plane === "solver";
  const at = (mm: number) => (inside && plane === "top" ? TOP_FACE + 0.15 : mm * ky);

  if (!use3d || !glbUrl) {
    const layers = { ...DEFAULT_LAYERS, issues: false, regions: false, silk: false, zones: true };
    return (
      <Board2D
        board={board} marks={none} meta={noMeta} selectedId={null} onSelect={noop} layers={layers} insets={insets} scene={sceneMarkup} chrome
        overlay={() => (
          <>
            {view === "wave" && fields?.xy && <FieldCanvas2D slice={fields.xy} paused={paused || calm} />}
            {view === "pulse" && pulse && <PulseCanvas2D pulse={pulse} frame={frame} />}
            {view === "current" && current && <CurrentCanvas2D data={current} />}
            <Outline2D meta={meta} />
          </>
        )}
      />
    );
  }
  return (
    <Scene3D
      board={board} glbUrl={glbUrl} marks={none} meta={noMeta} selectedId={null} onSelect={noop} explode={0} insets={insets}
      showMarks={false} boardDims={false} autoRotate={view === "pattern" && !calm}
      view3d={{ az: 0.5, el: 0.7 }}
    >
      {view === "wave" && fields?.xy && <WaveField3D slice={fields.xy} y={at(fields.xy.at_mm)} paused={paused || calm} seeThrough={seeThrough} />}
      {view === "wave" && fields?.xz && <WaveField3D slice={fields.xz} paused={paused || calm} opacity={0.8} yScale={ky} />}
      {view === "pulse" && pulse && <PulsePlane3D pulse={pulse} y={at(pulse.at_mm)} frame={frame} seeThrough={seeThrough} />}
      {view === "current" && current && <CurrentPlane3D data={current} y={at(current.at_mm) + (inside && plane === "top" ? 0 : 0.05)} seeThrough={seeThrough} />}
      {view === "pattern" && pattern && <RadiationBalloon pattern={pattern} center={balloonAt} scale={22} rangeDb={20} colorDb={10} />}
      <Outline3D meta={meta} />
    </Scene3D>
  );
}
