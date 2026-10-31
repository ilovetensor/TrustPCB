// Typed loaders for the RF solver output (worker/run_scenario.py). Real solver data only:
// when a result is missing, callers show "Not tested". Nothing is synthesised.
// Coordinates are scenario millimetres: x right, y UP, z up from the bottom copper face.
// Scene mapping used by every RF component: scene = (x, z, -y).

import { decodePulseFrames } from "./pulse-codec.mjs";
import type { Board, Pt, Ring } from "./types";

export interface FieldSlice {
  plane: "xy" | "xz";
  /** Extents of the plane in scenario mm: u along x, v along y (xy) or z (xz). */
  u: [number, number];
  v: [number, number];
  /** Position of the plane on its normal axis, mm. */
  at_mm: number;
  nx: number; ny: number;
  re: Float32Array; im: Float32Array;
  f_hz: number;
}
export interface PulseData { nx: number; ny: number; u: [number, number]; v: [number, number]; at_mm: number; frames: Float32Array[]; times_s: number[] }
export interface CurrentData { nx: number; ny: number; u: [number, number]; v: [number, number]; at_mm: number; mag: Float32Array; f_hz: number }
export interface PatternData {
  f_hz: number; theta_deg: number[]; phi_deg: number[]; dir_dbi: number[][];
  dmax_dbi: number; dmax_theta_deg: number; dmax_phi_deg: number; floor_dbi: number;
}
export interface S11Data { f_ghz: number[]; s11_db: number[] }
export interface ZinData { f_ghz: number[]; z_re: number[]; z_im: number[] }

export interface ScenarioGeometry {
  substrate: { box: [number, number, number, number]; zmin: number; zmax: number; epsilon_r: number };
  metals: { name: string; z_mm: number; rects: [number, number, number, number][] }[];
  port: { start: [number, number, number]; end: [number, number, number] } | null;
}
export interface RfBundle {
  synthetic: false;
  label: string;
  source: string;
  f0_hz: number;
  f_res_hz: number;
  /** scenario substrate and copper, for drawing the board the field belongs to */
  scenario: ScenarioGeometry;
  band: { lo_ghz: number; hi_ghz: number; target_db: number };
  s11: S11Data;
  zin: ZinData;
  /** extra tuning curves to overlay (before/after), when known */
  overlays?: { id: string; label: string; s11: S11Data }[];
  fieldXY?: FieldSlice;
  fieldXZ?: FieldSlice;
  pattern?: PatternData;
  pulse?: PulseData;
  current?: CurrentData;
  result?: Record<string, unknown>;
}

async function f32(url: string): Promise<Float32Array> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return new Float32Array(await r.arrayBuffer());
}
async function json<T = any>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

/** Load the worker's output directory (served from `base`, e.g. /fixtures/patch-sim). */
async function loadWorkerOutput(base: string, label: string, bandHalfWidthGhz = 0.05): Promise<RfBundle> {
  const [s11, zin, pattern, field, current, pulse, result, scn] = await Promise.all([
    json(`${base}/s11.json`), json(`${base}/zin.json`), json(`${base}/pattern.json`).catch(() => null),
    json(`${base}/field.json`).catch(() => null), json(`${base}/current.json`).catch(() => null),
    json(`${base}/pulse.json`).catch(() => null), json(`${base}/result.json`).catch(() => null),
    json(`${base}/scenario.json`),
  ]);
  const hz = (a: number[]) => a.map((f) => f / 1e9);
  const f0 = (result?.f0_hz as number) ?? scn.f0_hz;
  const sub = scn.substrates?.[0];
  const bounds = (tris: number[][][]): [number, number, number, number] => {
    const xs = tris.flat().map((p) => p[0]), ys = tris.flat().map((p) => p[1]);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  };
  const out: RfBundle = {
    synthetic: false, label, source: base, f0_hz: f0, f_res_hz: (result?.f_res as number) ?? f0,
    scenario: {
      substrate: { box: sub.box, zmin: sub.zmin, zmax: sub.zmax, epsilon_r: sub.epsilon_r },
      metals: scn.metals.map((m: any) => ({ name: m.name, z_mm: m.z_mm, rects: [bounds(m.triangles)] })),
      port: scn.port ? { start: scn.port.start, end: scn.port.end } : null,
    },
    // The target band is not part of the worker output; show the scenario centre frequency +/- a display half width.
    band: { lo_ghz: f0 / 1e9 - bandHalfWidthGhz, hi_ghz: f0 / 1e9 + bandHalfWidthGhz, target_db: -10 },
    s11: { f_ghz: hz(s11.freq_hz), s11_db: s11.s11_db },
    zin: { f_ghz: hz(zin.freq_hz), z_re: zin.z_re, z_im: zin.z_im },
    result: result ?? undefined,
  };
  if (pattern) out.pattern = { f_hz: pattern.f_hz, theta_deg: pattern.theta_deg, phi_deg: pattern.phi_deg, dir_dbi: pattern.directivity_dbi, dmax_dbi: pattern.dmax_dbi, dmax_theta_deg: pattern.dmax_theta_deg, dmax_phi_deg: pattern.dmax_phi_deg, floor_dbi: pattern.floor_dbi ?? -40 };
  const slice = async (k: "xy" | "xz"): Promise<FieldSlice | undefined> => {
    const m = field?.[k];
    if (!m) return undefined;
    const buf = await f32(`${base}/${m.file}`);
    const [, rows, cols] = m.shape as number[];
    return { plane: k, u: m.u_mm, v: m.v_mm, at_mm: m.plane_mm, nx: cols, ny: rows, re: buf.subarray(0, rows * cols), im: buf.subarray(rows * cols, 2 * rows * cols), f_hz: field.f_hz };
  };
  [out.fieldXY, out.fieldXZ] = await Promise.all([slice("xy"), slice("xz")]);
  if (current) {
    const [rows, cols] = current.shape as number[];
    out.current = { nx: cols, ny: rows, u: current.u_mm, v: current.v_mm, at_mm: current.plane_mm, mag: await f32(`${base}/${current.file}`), f_hz: current.f_hz };
  }
  if (pulse) {
    const [, rows, cols] = pulse.shape as number[];
    const r = await fetch(`${base}/${pulse.file}`);
    if (!r.ok) throw new Error(`${base}/${pulse.file}: ${r.status}`);
    const frames = await decodePulseFrames(await r.arrayBuffer(), pulse);
    if (frames) out.pulse = { nx: cols, ny: rows, u: pulse.u_mm, v: pulse.v_mm, at_mm: pulse.plane_mm, times_s: pulse.times_s, frames };
  }
  return out;
}

const PATCH_SIM_BASE = "/fixtures/patch-sim";
let patchCache: Promise<RfBundle | null> | null = null;
/** The real patch-antenna run shipped with the repo. Null when the files are absent. */
export function loadPatchSim(): Promise<RfBundle | null> {
  patchCache ??= loadWorkerOutput(PATCH_SIM_BASE, "Solver output: 50 x 60 mm patch, screen quality").catch(() => null);
  return patchCache;
}

/** A Board model for the scenario, for the 2D viewer. Scenario y is up, board y is down. */
export function scenarioBoard(b: RfBundle): Board {
  const [x0, y0, x1, y1] = b.scenario.substrate.box;
  const rect = (a: number, c: number, d: number, e: number): Ring => [[a, -e], [d, -e], [d, -c], [a, -c]] as Pt[];
  const outline = rect(x0, y0, x1, y1);
  return {
    version_id: "patch-sim", title: "2.5 GHz patch antenna", outline: [outline], bbox: [x0, -y1, x1, -y0], layers: [], stackup: { source: "board", assumed: false, layers: [] },
    nets: [], footprints: [], pads: [], tracks: [], vias: [], keepouts: [],
    zones: b.scenario.metals.map((m) => ({
      id: `zone:${m.name}`, net: null, name: m.name, layers: [m.z_mm > 0.5 ? "F.Cu" : "B.Cu"], is_keepout: false as const,
      filled: { [m.z_mm > 0.5 ? "F.Cu" : "B.Cu"]: m.rects.map((r) => rect(r[0], r[1], r[2], r[3])) },
    })),
  };
}
