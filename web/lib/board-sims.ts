// Power, heat and signal simulations of a review, for the simulation lab and the workspace.
//
// What the API is expected to send (written to match the simulators' result.json):
//   GET /api/reviews/{id}/sims                          the review's runs. A run of these kinds has `kind` (or `sim_kind`) power | thermal | signal,
//                                                      `run_id`, `status`, `label`, `ref`, `whatif`, `base_run`, `progress`, `step`, and, once done, `result`.
//   GET /api/reviews/{id}/sims/{run}                    one run, with its `result`, when the list leaves it out.
//   GET /api/reviews/{id}/sims/{run}/overlay/{oid}      the raster of one overlay: float32 little endian [ny, nx], NaN where nothing exists.
// A result carries `metrics`, `trust`, `overlays`, `assumptions`, `notes`, `solve_s` and optionally `series` (see board-sim-model.d.mts).
// Real data only. A missing file gives `null`, never a made-up value.

import { apiGet } from "./live";
import { decodeRaster, type OverlayMeta } from "./overlay-math.mjs";
import { isBoardRow, normaliseRun, type BoardSimRun, type SimResultJson } from "./board-sim-model.mjs";
import { DEV_SIMS_ON, loadDevSims } from "./dev-sims-loader";

export * from "./board-sim-model.mjs";

export interface BoardSimSource {
  id: string;
  runs: BoardSimRun[];
  /** the raster of one overlay of one run, or null when it cannot be read */
  raster: (run: BoardSimRun, ov: OverlayMeta) => Promise<Float32Array | null>;
  /** present for the dev fixture: said out loud on the page */
  fixture?: { note: string };
}

export async function listBoardRuns(reviewId: string): Promise<BoardSimRun[]> {
  const rows = await apiGet<Record<string, any>[]>(`reviews/${encodeURIComponent(reviewId)}/sims`);
  return rows.filter(isBoardRow).map(normaliseRun);
}

const cache = new Map<string, Promise<Float32Array | null>>();
function cachedRaster(url: string, ov: OverlayMeta): Promise<Float32Array | null> {
  let p = cache.get(url);
  if (!p) {
    p = fetch(url).then((r) => (r.ok ? r.arrayBuffer() : null)).then((b) => decodeRaster(b, ov)).catch(() => null);
    cache.set(url, p);
    p.then((v) => { if (!v) cache.delete(url); });
  }
  return p;
}

/** A done run whose list row left the result out: ask for the run itself. */
async function withResult(reviewId: string, run: BoardSimRun): Promise<BoardSimRun> {
  if (run.status !== "done" || run.result) return run;
  try {
    const row = await apiGet<Record<string, any>>(`reviews/${encodeURIComponent(reviewId)}/sims/${encodeURIComponent(run.run_id)}`);
    const result = (row.result ?? (row.metrics ? row : null)) as SimResultJson | null;
    return result ? { ...run, result } : run;
  } catch { return run; }
}

export async function liveBoardSource(reviewId: string, runs: BoardSimRun[]): Promise<BoardSimSource> {
  const full = await Promise.all(runs.map((r) => withResult(reviewId, r)));
  return {
    id: `review:${reviewId}`, runs: full,
    raster: (run, ov) => cachedRaster(`/api/reviews/${encodeURIComponent(reviewId)}/sims/${encodeURIComponent(run.run_id)}/overlay/${encodeURIComponent(ov.id)}`, ov),
  };
}

/** The dev fixture: runs solved in the browser on the bundled nRF52840 board. Only under `next dev` (or the fixtures flag). */
export const devFixtureAvailable = DEV_SIMS_ON;
let fixtureCache: Promise<BoardSimSource> | null = null;
export function devFixtureSource(board: import("./types").Board): Promise<BoardSimSource> {
  fixtureCache ??= loadDevSims().then(async (m) => {
    await new Promise((r) => setTimeout(r, 30));          // let the page paint before the solvers take the thread
    const { runs, rasters } = m.buildFixtureRuns(board, { fine: true });
    return {
      id: "dev-fixture", runs,
      raster: async (run, ov) => rasters.get(`${run.run_id}/${ov.id}`) ?? null,
      fixture: { note: "Dev fixture: these runs were solved in your browser on the sample board to build the screens. They are not a review of your board." },
    } satisfies BoardSimSource;
  });
  return fixtureCache;
}
