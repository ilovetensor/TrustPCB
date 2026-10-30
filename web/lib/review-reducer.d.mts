import type { EventEnvelope, Finding, Mark, Severity } from "./types";

export interface TrailEntry { id: string; kind: string; text: string; seq: number; at?: string; phase?: number; tool?: string; state?: string; detail?: string; findingId?: string; severity?: Severity; status?: string; pct?: number; simId?: string; summary?: any; investigationId?: string; result?: string }
export interface Suspect { id: string; title: string; status: string; conclusion?: string }
export interface Investigation { id: string; question: string; status: string; suspects: Suspect[] }
export interface Experiment { id: string; label: string; kind: string; change: string | null; summary: any; curve: any; trust: string }
export interface Sim { id: string; label: string; kind: string; change: string | null; status: string; pct: number; step: string; summary?: any; curve?: any }
export interface Budget { wall_total_s: number; wall_left_s: number; solver_total_s: number; solver_left_s: number; sims_total: number; sims_left: number; turns_total: number; turns_used: number; images_total: number; images_left: number }
export interface Brief { purpose: string; zones: { id: string; label: string; kind: string; target: string }[]; priorities: string[] }
export interface FollowupState { level: string | null; selected: string[]; messages: { id: string; role: "user" | "agent"; text: string; selected?: string[]; turn?: string }[]; proposal: { request_id: string; what: string; why: string; eta_s: number; status: "pending" | "approved" | "declined" } | null; whatif: any; running: boolean }
export interface ReviewState {
  status: "idle" | "running" | "complete" | "failed" | "canceled"; endState: string | null; phase: number; seen: number;
  budget: Budget | null; trail: TrailEntry[]; findings: Record<string, Finding>; findingOrder: string[]; marks: Mark[]; brief: Brief | null;
  candidates: { total: number; reviewed: number; byRule: Record<string, number> };
  investigations: Record<string, Investigation>; investigationOrder: string[]; sims: Record<string, Sim>; experiments: Experiment[];
  spotlight: { targets: string[]; findingId: string | null; reason: string; seq: number } | null; reportReady: { revision: number } | null;
  assumptions: any[]; followup: FollowupState; notices: { id: string; text: string }[];
}
export const PHASES: { id: number; key: string; label: string; short: string; actor: string }[];
export function initialReviewState(): ReviewState;
export function toolLabel(tool: string): string;
export function reviewReducer(state: ReviewState, event: Pick<EventEnvelope, "sequence" | "type" | "payload"> & { created_at?: string; turn_id?: string }): ReviewState;
export function replayAll(events: any[], state?: ReviewState): ReviewState;
export const SEVERITY_ORDER: Severity[];
export function severityCounts(findings: Finding[]): Record<Severity, number>;
export function sortFindings(findings: Finding[]): Finding[];
export function activeMarks(state: ReviewState): { key: string; findingId: string | null; severity: Severity; mark: Mark }[];
export function numberFindings(state: ReviewState): Map<string, number>;
