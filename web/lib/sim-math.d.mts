import type { SimJob, Summary, Measure } from "./sims";
export interface Dip { f_ghz: number; db: number }
export function findDips(c: { f_ghz: number[]; s11_db: number[] }, floorDb?: number): Dip[];
export function zFromGamma(re: number[], im: number[], z0?: number): { z_re: number[]; z_im: number[] };
export function describeOps(ops: SimJob["whatif_ops"] | null | undefined, material?: unknown): string;
export function describeFreq(f_hz: number | undefined | null, band: [number, number] | null | undefined): string;
export function measurementsOfSummary(s: Summary): Record<string, Measure>;
export function pickStageJob(jobs: SimJob[]): SimJob | null;
export function bestRun(jobs: SimJob[]): SimJob | null;
export function boardYRange(v_mm: [number, number]): [number, number];
