import type { BBox, Geometry, Mark, Pt } from "./types";

export interface BoardIndex { board: any; byId: Map<string, any>; netTracks: Map<string, any[]>; netPads: Map<string, any[]>; netVias: Map<string, any[]> }
export function bboxUnion(boxes: (BBox | null | undefined)[]): BBox | null;
export function bboxCenter(b: BBox): [number, number];
export function arcPoints(a: Pt, m: Pt, b: Pt, steps?: number): Pt[];
export function indexBoard(board: any): BoardIndex;
export function resolveTarget(index: BoardIndex, id: string): Geometry | null;
export function resolveMark(index: BoardIndex, mark: Partial<Mark> & { targets: string[] }): Geometry[];
export function markAnchor(mark: Partial<Mark>, geoms: Geometry[]): Pt | null;
export interface ViewT { scale: number; tx: number; ty: number }
export function fitView(bbox: BBox, width: number, height: number, pad?: { l: number; r: number; t: number; b: number }): ViewT;
export function toScreen(view: ViewT, x: number, y: number): [number, number];
export function toBoard(view: ViewT, sx: number, sy: number): [number, number];
export function zoomAt(view: ViewT, sx: number, sy: number, factor: number, min?: number, max?: number): ViewT;
export function clusterAnchors<T extends { id: string; ax: number; ay: number; priority?: number }>(items: T[], radius: number, keep?: Set<string>): { ids: string[]; members: T[]; ax: number; ay: number; count: number; priority: number }[];
export interface Placed { bx: number; by: number; ex: number; ey: number; sx: number; sy: number }
export function layoutCallouts(items: { id: string; ax: number; ay: number; priority?: number }[], opts: { width: number; height: number; obstacles?: { x: number; y: number; w: number; h: number }[]; radius?: number; prev?: Map<string, { bx: number; by: number }>; margin?: number }): Map<string, Placed>;
export function nearestPair(ga: Geometry, gb: Geometry): { a: Pt; b: Pt; d: number } | null;
export function dimensionGeometry(a: Pt, b: Pt, offset?: number): { ext: [Pt, Pt][]; line: [Pt, Pt]; arrows: Pt[][]; mid: Pt; len: number; inside: boolean };
export function netId(name: string | null | undefined): string | null;
