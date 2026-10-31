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
export function acceptedPct(s11_db: number | null | undefined): number | null;
export interface AcceptedSide { worst: number | null; mean: number | null }
export function acceptedFromCurve(curve: { f_ghz: number[]; s11_db: number[] } | null | undefined, band: [number, number] | null | undefined): { worst: number; mean: number } | null;
export function acceptedOf(measurements: Record<string, Measure> | null | undefined): { asBuilt: AcceptedSide | null; antenna: AcceptedSide | null; headline: AcceptedSide | null };
export function fixedDipsMhz(jobs: { measurements?: Record<string, Measure> }[] | null | undefined): number[];
export function isFixedDip(f_ghz: number, fixedMhz: number[] | null | undefined, tolMhz?: number): boolean;
export function sameGeometry(hashA: string | null | undefined, hashB: string | null | undefined): boolean | null;
export function fmtPct(v: number | null | undefined): string;
