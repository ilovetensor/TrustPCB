import type { OverlayMeta } from "./overlay-math.mjs";
export type BoardSimKind = "power" | "thermal" | "signal";
export interface SimAssumption { name: string; value: number | string; unit: string; source: "datasheet" | "user" | "default" | "board"; cite?: string | null }
export interface SimMetric { value: number | null; unit: string; label: string; group?: string; ref?: string; net?: string; current?: number; current_unit?: string; limit?: number; power?: number; power_unit?: string; board?: number; deviation?: number; series?: unknown; [k: string]: unknown }
export interface SimSeries { id: string; label: string; x_label?: string; x_unit: string; y_unit: string; x: number[]; y: number[]; target?: number; tolerance_pct?: number }
export interface SimTrust { status: "ok" | "failed" | null; checks?: { check: string; ok: boolean; detail?: string }[] }
export interface SimResultJson {
  kind: BoardSimKind; status: "ok" | "not_possible"; reason: string | null; metrics: Record<string, SimMetric>; trust: SimTrust;
  overlays: OverlayMeta[]; assumptions: SimAssumption[]; notes: string[]; solve_s: number; series?: SimSeries[];
}
export interface WhatIfOp { op: string; [k: string]: unknown }
export interface BoardSimRun {
  run_id: string; kind: BoardSimKind; status: string; label: string | null; ref: string | null; whatif: WhatIfOp[]; base_run: string | null;
  progress: number | null; step: string | null; waiting: { position: number | null; ahead: number | null } | null;
  result: SimResultJson | null; evidence: string | null; created_at: string | null; error: string | null; fixture: boolean;
}
export const BOARD_KINDS: BoardSimKind[];
export const KIND_LABEL: Record<BoardSimKind, string>;
export const KIND_NOUN: Record<BoardSimKind, string>;
export const SOURCE_LABEL: Record<SimAssumption["source"], string>;
export const SOURCE_HINT: Record<SimAssumption["source"], string>;
export function isBoardKind(k: unknown): k is BoardSimKind;
export function isBoardRow(row: unknown): boolean;
export function normaliseRun(row: Record<string, any>): BoardSimRun;
export function describeBoardOps(ops: WhatIfOp[] | null | undefined): string;
export function isDoneRun(r: BoardSimRun): boolean;
export function isLiveRun(r: BoardSimRun): boolean;
export function pairOf(runs: BoardSimRun[], active: BoardSimRun | null | undefined): { before: BoardSimRun; after: BoardSimRun } | null;
export function pickRun(runs: BoardSimRun[], opts?: { kind?: BoardSimKind | null; run?: string | null; ref?: string | null }): BoardSimRun | null;
export interface PanelRow extends SimMetric { key: string }
export function panelModel(result: SimResultJson | null | undefined): { headline: PanelRow[]; load: PanelRow[]; part: PanelRow[]; segment: PanelRow[]; net: PanelRow[]; other: PanelRow[] };
export function seriesOf(result: SimResultJson | null | undefined): SimSeries[];
export function defaultsClaim(assumptions: SimAssumption[] | null | undefined): string;
export function defaultCount(assumptions: SimAssumption[] | null | undefined): number;
export function sourcesFor(ref: string | null | undefined, assumptions: SimAssumption[] | null | undefined): string[];
export function usesDefault(ref: string | null | undefined, assumptions: SimAssumption[] | null | undefined): boolean;
export function trustOf(result: SimResultJson | null | undefined): { ok: boolean; status: string | null; checks: NonNullable<SimTrust["checks"]>; failed: NonNullable<SimTrust["checks"]> };
export function limitShare(value: number, limit: number | null | undefined): number | null;
export function metricChange(before: SimResultJson | null | undefined, after: SimResultJson | null | undefined, key: string): { before: number; after: number; delta: number; unit: string } | null;
export function simKindName(kind: string): string;
export function simStatusText(s: { waiting?: { position: number | null; ahead: number | null } | null; step?: string; status?: string; pct?: number }): string;
export function evidenceLink(reviewId: string, evidence: { source_tool?: string; values?: Record<string, any> } | null | undefined): { href: string; label: string } | null;
