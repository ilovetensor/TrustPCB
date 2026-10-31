// Simulation jobs for the dashboard: list a review's solver runs and load their processed files.
// Real solver data only. A file that is absent or unreadable gives `null`, never a made-up value.
//
// Coordinates: field and current sidecars are in scenario millimetres, where scenario y = minus board y
// (meta.json: y_convention). `u` runs along x. `v` is scenario y, so board y = -v. Image rows increase with
// scenario y, which is DEcreasing board y, so a renderer that draws on a y-down board picture flips the rows.
// The canvases in rf/FieldCanvas2D and the planes in rf/WaveField3D do exactly that.

import { apiGet } from "./live";
import { decodePulseFrames } from "./pulse-codec.mjs";
import type { CurrentData, FieldSlice, PatternData, PulseData, S11Data, ZinData } from "./rf-data";
import { acceptedFromCurve, acceptedOf, findDips, measurementsOfSummary } from "./sim-math.mjs";
export { acceptedFromCurve, acceptedOf, acceptedPct, bestRun, boardYRange, describeFreq, describeOps, findDips, fixedDipsMhz, fmtPct, isFixedDip, measurementsOfSummary, pickStageJob, sameGeometry, zFromGamma } from "./sim-math.mjs";
export type { Dip } from "./sim-math.mjs";
import type { Dip } from "./sim-math.mjs";

export type SimStatus = "queued" | "running" | "done" | "failed" | "canceled" | "timeout" | "interrupted";
export interface Measure { value: number; unit: string }
export interface SimJob {
  job_id: string; status: SimStatus; scenario_hash: string | null; quality: "screen" | "confirm" | null;
  label: string | null; ref: string | null; kind: "base" | "whatif" | "confirm"; suspect: string | null; investigation_id: string | null;
  evidence: string | null; whatif_ops: { op: string; [k: string]: unknown }[]; material: unknown;
  trust: { status: "ok" | "fail" | null; reasons: string[] } | null;
  measurements: Record<string, Measure>; solve_s: number | null; progress: number | null; error: string | null; created_at: string | null; files: string[];
}

/** Where a job's files come from: the API through the proxy, or a bundled sample folder. */
export interface SimSource {
  id: string;
  jobs: SimJob[];
  fileUrl: (job: SimJob, name: string) => string;
  /** present for the bundled sample: said out loud in the page */
  sample?: { note: string };
}

const liveFileUrl = (job: SimJob, name: string) => `/api/sim/${encodeURIComponent(job.job_id)}/files/${name}`;
export const listSims = (reviewId: string) => apiGet<SimJob[]>(`reviews/${reviewId}/sims`);
export const liveSource = (reviewId: string, jobs: SimJob[]): SimSource => ({ id: `review:${reviewId}`, jobs, fileUrl: liveFileUrl });

/** Jobs a viewer can look at: finished with at least the tuning curve. */
const hasFile = (job: SimJob, name: string) => job.files.includes(name);
export const isDone = (job: SimJob) => job.status === "done";

/* ------------------------------ fetching, cached per url ------------------------------ */

const cache = new Map<string, Promise<unknown>>();
function cached<T>(url: string, load: () => Promise<T>): Promise<T> {
  let p = cache.get(url) as Promise<T> | undefined;
  if (!p) { p = load(); cache.set(url, p); p.catch(() => cache.delete(url)); }
  return p;
}
const getJson = <T,>(url: string) => cached<T>(url, async () => { const r = await fetch(url); if (!r.ok) throw new Error(`${r.status} ${url}`); return r.json() as Promise<T>; });
const getF32 = (url: string) => cached<Float32Array>(url, async () => { const r = await fetch(url); if (!r.ok) throw new Error(`${r.status} ${url}`); return new Float32Array(await r.arrayBuffer()); });
const getBytes = (url: string) => cached<ArrayBuffer>(url, async () => { const r = await fetch(url); if (!r.ok) throw new Error(`${r.status} ${url}`); return r.arrayBuffer(); });
const optional = async <T,>(job: SimJob, name: string, run: () => Promise<T>): Promise<T | null> => {
  if (!hasFile(job, name)) return null;
  try { return await run(); } catch { return null; }
};

/* ------------------------------ file shapes ------------------------------ */

export interface Summary {
  quality?: string; note?: string;
  setup?: { band_ghz?: [number, number]; antenna_bbox?: number[]; port_xy?: [number, number]; port_on?: string; assumptions?: string[]; warnings?: string[]; region?: number[] };
  trust?: { status: string; reasons?: string[]; checks?: { check: string; ok: boolean; detail: string }[] };
  s11?: {
    antenna_only?: BandStats;
    as_built?: BandStats & { parts?: { topology: string; kind: string; value: number; text: string }[] };
    network_note?: string | null;
  };
  sim?: {
    f_res?: number; s11_min_db?: number; dmax_dbi?: number; efficiency_est?: number; pattern_f_hz?: number; solve_s?: number; cells?: number;
    s11_dips?: { f_hz: number; s11_db: number }[]; first_series_resonance_hz?: number | null;
    bandwidth?: { f_low_hz: number; f_high_hz: number; width_hz: number } | null;
  };
  zin_at_band_centre?: { f_hz: number; re: number; im: number };
  plausibility?: string[];
  pattern?: { f_hz: number; dmax_dbi: number; dmax_theta_deg: number; dmax_phi_deg: number };
}
export interface BandStats { f_min_hz: number; s11_min_db: number; band_worst_db: number; band_best_db: number; band_mean_db: number; band_covered_10db: boolean }
export interface SimMeta {
  y_convention?: { scenario_y: string; note?: string };
  setup?: { antenna_bbox?: number[]; port_xy?: [number, number]; band_ghz?: [number, number]; f0_hz?: number; antenna_net?: string; port_on?: string };
  region?: number[]; scenario?: { band_ghz?: [number, number]; quality?: string; whatif_ops?: unknown[] };
}

export const loadSummary = (src: SimSource, job: SimJob) => optional(job, "summary.json", () => getJson<Summary>(src.fileUrl(job, "summary.json")));
export const loadMeta = (src: SimSource, job: SimJob) => optional(job, "meta.json", () => getJson<SimMeta>(src.fileUrl(job, "meta.json")));

interface RawS11 { freq_hz: number[]; s11_db: number[]; s11_re?: number[]; s11_im?: number[] }
export interface Curves {
  antenna: (S11Data & { re?: number[]; im?: number[] }) | null;
  asBuilt: (S11Data & { re?: number[]; im?: number[] }) | null;
  zin: ZinData | null;
}
export function loadCurves(src: SimSource, job: SimJob): Promise<Curves> {
  const conv = (r: RawS11 | null) => (r ? { f_ghz: r.freq_hz.map((f) => f / 1e9), s11_db: r.s11_db, re: r.s11_re, im: r.s11_im } : null);
  return Promise.all([
    optional(job, "s11.json", () => getJson<RawS11>(src.fileUrl(job, "s11.json"))),
    optional(job, "s11_as_built.json", () => getJson<RawS11>(src.fileUrl(job, "s11_as_built.json"))),
    optional(job, "zin.json", () => getJson<{ freq_hz: number[]; z_re: number[]; z_im: number[] }>(src.fileUrl(job, "zin.json"))),
  ]).then(([a, b, z]) => ({ antenna: conv(a), asBuilt: conv(b), zin: z ? { f_ghz: z.freq_hz.map((f) => f / 1e9), z_re: z.z_re, z_im: z.z_im } : null }));
}

export function loadPattern(src: SimSource, job: SimJob): Promise<PatternData | null> {
  return optional(job, "pattern.json", async () => {
    const p = await getJson<any>(src.fileUrl(job, "pattern.json"));
    return { f_hz: p.f_hz, theta_deg: p.theta_deg, phi_deg: p.phi_deg, dir_dbi: p.directivity_dbi, dmax_dbi: p.dmax_dbi, dmax_theta_deg: p.dmax_theta_deg, dmax_phi_deg: p.dmax_phi_deg, floor_dbi: p.floor_dbi ?? -40 };
  });
}

export interface Fields { xy: FieldSlice | null; xz: FieldSlice | null; f_hz: number; normalization?: string }
export function loadField(src: SimSource, job: SimJob): Promise<Fields | null> {
  return optional(job, "field.json", async () => {
    const field = await getJson<any>(src.fileUrl(job, "field.json"));
    const slice = async (k: "xy" | "xz", file: string): Promise<FieldSlice | null> => {
      const m = field[k];
      if (!m || !hasFile(job, file)) return null;
      const buf = await getF32(src.fileUrl(job, file));
      const [, rows, cols] = m.shape as number[];
      if (buf.length < 2 * rows * cols) return null;
      return { plane: k, u: m.u_mm, v: m.v_mm, at_mm: m.plane_mm, nx: cols, ny: rows, re: buf.subarray(0, rows * cols), im: buf.subarray(rows * cols, 2 * rows * cols), f_hz: field.f_hz };
    };
    const [xy, xz] = await Promise.all([slice("xy", "field_xy.bin"), slice("xz", "field_xz.bin")]);
    return { xy, xz, f_hz: field.f_hz, normalization: field.normalization };
  });
}

export function loadCurrent(src: SimSource, job: SimJob): Promise<CurrentData | null> {
  return optional(job, "current.json", async () => {
    const c = await getJson<any>(src.fileUrl(job, "current.json"));
    if (!hasFile(job, "current.bin")) return null;
    const [rows, cols] = c.shape as number[];
    return { nx: cols, ny: rows, u: c.u_mm, v: c.v_mm, at_mm: c.plane_mm, mag: await getF32(src.fileUrl(job, "current.bin")), f_hz: c.f_hz };
  });
}

export function loadPulse(src: SimSource, job: SimJob): Promise<PulseData | null> {
  return optional(job, "pulse.json", async () => {
    const p = await getJson<any>(src.fileUrl(job, "pulse.json"));
    if (!hasFile(job, "pulse.bin")) return null;
    const [, rows, cols] = p.shape as number[];
    const frames = await decodePulseFrames(await getBytes(src.fileUrl(job, "pulse.bin")), p);
    return frames && { nx: cols, ny: rows, u: p.u_mm, v: p.v_mm, at_mm: p.plane_mm, times_s: p.times_s, frames };
  });
}

/* ------------------------------ derived numbers ------------------------------ */

export const dipsOf = (summary: Summary | null | undefined, curve: { f_ghz: number[]; s11_db: number[] } | null): { dips: Dip[]; fromSummary: boolean } => {
  const d = summary?.sim?.s11_dips;
  if (d?.length) return { dips: d.map((x) => ({ f_ghz: x.f_hz / 1e9, db: x.s11_db })), fromSummary: true };
  return { dips: curve ? findDips(curve) : [], fromSummary: false };
};

/** Accepted power of one run, in percent: the numbers the solver side stored, else worked out here from the run's own S11 curve over the band. */
export interface AcceptedPower { worst: number | null; mean: number | null; fromCurve: boolean }
export function acceptedForJob(job: SimJob | null | undefined, curves: Curves | null | undefined, band: [number, number] | null | undefined): { asBuilt: AcceptedPower | null; antenna: AcceptedPower | null } {
  const m = acceptedOf(job?.measurements);
  const stored = (s: { worst: number | null; mean: number | null } | null): AcceptedPower | null => (s ? { ...s, fromCurve: false } : null);
  const calc = (c: { f_ghz: number[]; s11_db: number[] } | null | undefined): AcceptedPower | null => { const r = c && band ? acceptedFromCurve(c, band) : null; return r ? { ...r, fromCurve: true } : null; };
  // `acceptedOf` folds an unprefixed figure into as-built; the antenna-alone side stays its own
  return { asBuilt: stored(m.asBuilt) ?? calc(curves?.asBuilt), antenna: stored(m.antenna) ?? calc(curves?.antenna) };
}

/** The numbers the API sends are in friendly units (resonance in MHz). Missing ones stay missing. */
export function measure(job: SimJob, name: string): number | null {
  const m = job.measurements?.[name];
  return m && typeof m.value === "number" ? m.value : null;
}

/** "screen" runs are for comparing, not for reading absolute values. */
export const isScreen = (job: SimJob | null | undefined) => job?.quality === "screen";
export const QUALITY_NOTE = "Screen run: compare only, do not read these as absolute numbers.";

/* ------------------------------ the bundled sample ------------------------------ */

const NRF_SIM_BASE = "/fixtures/nrf-sim";
export const NRF_BOARD = { board: "/fixtures/nrf/board.json", glb: "/fixtures/nrf/board.glb.gz" };
const SAMPLE_DIRS: { id: string; dir: string; kind: SimJob["kind"]; quality: "screen" | "confirm"; label: string; ref: string }[] = [
  { id: "sample-screen", dir: "screen", kind: "base", quality: "screen", label: "Base, antenna as drawn", ref: "R1" },
  { id: "sample-whatif", dir: "whatif_shorten_6mm_screen", kind: "whatif", quality: "screen", label: "Shorten the antenna by 6 mm", ref: "R2" },
  { id: "sample-confirm", dir: "", kind: "confirm", quality: "confirm", label: "Confirm, antenna as drawn", ref: "R3" },
];
const ALL_FILES = ["s11.json", "s11_as_built.json", "zin.json", "pattern.json", "field_xy.bin", "field_xz.bin", "field.json", "pulse.bin", "pulse.json", "current.bin", "current.json", "result.json", "summary.json", "model.png", "meta.json"];
/** Screen and what-if folders keep only the curves, the pattern and the summary (see the sample's README.txt). */
const LIGHT_FILES = ["s11.json", "s11_as_built.json", "zin.json", "pattern.json", "result.json", "summary.json", "model.png", "meta.json"];

let sampleCache: Promise<SimSource> | null = null;
/** The real nRF52840 openEMS run in public/fixtures/nrf-sim, as three jobs. */
export function nrfSampleSource(): Promise<SimSource> {
  sampleCache ??= (async () => {
    const jobs: SimJob[] = [];
    for (const d of SAMPLE_DIRS) {
      const base = d.dir ? `${NRF_SIM_BASE}/${d.dir}` : NRF_SIM_BASE;
      const files = d.dir ? LIGHT_FILES : ALL_FILES;
      const summary: Summary | null = files.includes("summary.json") ? await fetch(`${base}/summary.json`).then((r) => r.json()).catch(() => null) : null;
      const meta: SimMeta | null = files.includes("meta.json") ? await fetch(`${base}/meta.json`).then((r) => r.json()).catch(() => null) : null;
      jobs.push({
        job_id: d.id, status: "done", scenario_hash: (meta as { scenario_hash?: string } | null)?.scenario_hash ?? null, quality: d.quality, label: d.label, ref: d.ref, kind: d.kind,
        suspect: null, investigation_id: null, evidence: null, whatif_ops: (meta?.scenario?.whatif_ops ?? []) as SimJob["whatif_ops"], material: null,
        trust: summary?.trust ? { status: summary.trust.status === "ok" ? "ok" : "fail", reasons: summary.trust.reasons ?? [] } : null,
        measurements: summary ? measurementsOfSummary(summary) : {}, solve_s: summary?.sim?.solve_s ?? null, progress: 1, error: null, created_at: null, files,
      });
    }
    return {
      id: "sample:nrf", jobs, sample: { note: "The solver output is a real openEMS run of the nRF52840 sample board, 31 Oct 2026. It is bundled with the app and is not a review of your board." },
      fileUrl: (job, name) => `${NRF_SIM_BASE}/${SAMPLE_DIRS.find((d) => d.id === job.job_id)?.dir ? SAMPLE_DIRS.find((d) => d.id === job.job_id)!.dir + "/" : ""}${name}`,
    } satisfies SimSource;
  })();
  return sampleCache;
}

/* ------------------------------ what the workspace stage draws ------------------------------ */

export interface StageRf { job: SimJob; fieldXY?: FieldSlice; fieldXZ?: FieldSlice; current?: CurrentData; f_hz?: number; sample?: boolean }
export async function loadStageRf(src: SimSource, job: SimJob): Promise<StageRf> {
  const [f, c] = await Promise.all([loadField(src, job).catch(() => null), loadCurrent(src, job).catch(() => null)]);
  return { job, fieldXY: f?.xy ?? undefined, fieldXZ: f?.xz ?? undefined, current: c ?? undefined, f_hz: f?.f_hz, sample: !!src.sample };
}
