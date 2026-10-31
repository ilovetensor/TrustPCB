import type { BoardSimRun } from "../board-sim-model.mjs";
import type { Board } from "../types";
export function buildFixtureRuns(board: Board, opts?: { fine?: boolean }): { runs: BoardSimRun[]; rasters: Map<string, Float32Array> };
