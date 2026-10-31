export interface OverlayMeta {
  id: string; label: string; unit: string; layer: string;
  x0: number; y0: number; dx: number; dy: number; nx: number; ny: number;
  vmin: number; vmax: number; file?: string; scale?: "sequential" | "diverging";
}
export interface OverlayMap { key: string; label: string; unit: string; scale: "sequential" | "diverging"; layers: OverlayMeta[] }
export interface RasterStats { min: number | null; max: number | null; mean: number | null; count: number; maxAt: [number, number] | null }
export function overlayBox(ov: OverlayMeta): [number, number, number, number];
export function cellAt(ov: OverlayMeta, x: number, y: number): number;
export function sampleAt(ov: OverlayMeta, data: Float32Array, x: number, y: number): number | null;
export function statsOf(ov: OverlayMeta, data: Float32Array): RasterStats;
export function gridsMatch(a: OverlayMeta, b: OverlayMeta): boolean;
export function diffOverlay(before: OverlayMeta, dBefore: Float32Array, after: OverlayMeta, dAfter: Float32Array): { ov: OverlayMeta; data: Float32Array } | null;
export function legendTicks(vmin: number, vmax: number, n?: number): number[];
export function fmtNum(v: number | null | undefined, digits?: number): string;
export function fmtVal(v: number | null | undefined, unit?: string): string;
export function scalePos(ov: Pick<OverlayMeta, "vmin" | "vmax" | "scale">, v: number): number;
export function colorize(ov: OverlayMeta, data: Float32Array, lut: Uint8Array, alpha?: number): Uint8ClampedArray;
export function mapsOf(overlays: OverlayMeta[] | null | undefined): OverlayMap[];
export function sharedRange(a: OverlayMeta | null | undefined, b: OverlayMeta | null | undefined): { vmin: number; vmax: number } | null;
export function decodeRaster(buffer: ArrayBuffer | null | undefined, ov: Pick<OverlayMeta, "nx" | "ny">): Float32Array | null;
export function legendLabels(ov: Pick<OverlayMeta, "vmin" | "vmax" | "scale">, n?: number): { v: number; at: number }[];
