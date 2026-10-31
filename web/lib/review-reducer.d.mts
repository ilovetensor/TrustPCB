import type { EventEnvelope, Finding, Mark, Severity } from "./types";

export interface TrailEntry { id: string; kind: string; text: string; seq: number; at?: string; phase?: number; tool?: string; state?: string; detail?: string; findingId?: string; severity?: Severity; status?: string; pct?: number; simId?: string; summary?: any; investigationId?: string; result?: string; input?: any; substep?: string; round?: number }
export interface Suspect { id: string; code?: string; title: string; status: string; conclusion?: string; /** refine round that added this suspect */ round?: number }
export interface Investigation { id: string; code?: string; question: string; status: string; outcome?: any; goodEnough?: boolean; suspects: Suspect[] }
export interface Measurement { value: number; unit: string }
export interface Experiment { id: string; label: string; kind: string; quality?: string; suspect?: string; hash?: string; solveS?: number; measurements: Record<string, Measurement>; delta: Record<string, number> | null; curve: any; trust: string; ok: boolean; meetsTarget?: boolean; evidence?: string }
export interface Sim { id: string; label: string; kind: string; quality?: string; suspect?: string; hash?: string; change: string | null; status: string; pct: number; step: string; measurements?: Record<string, Measurement>; curve?: any }
export interface Quota { used: number; limit: number; left?: number }
export interface Budget { phase?: number; depth?: string; wall_s?: Quota; solver_s?: Quota; turns?: Quota; images?: Quota; investigations?: Quota; confirm_runs?: Quota; input_tokens?: Quota; output_tokens?: Quota; [k: string]: any }
export interface Brief { purpose: string; part_roles?: Record<string, string>; risks?: { text: string; targets?: string[] }[]; priorities?: string[]; unknowns?: string[]; wireless?: any; roles?: Record<string, string[]>; zones: Record<string, string[]> }
export interface Proposal { request_id: string | null; what: string; why: string; eta_s: number; plan?: any; limits?: any; status: "pending" | "approved" | "declined" }
export interface PendingRequest { request_id: string; kind: "test" | "permission" | "question"; payload: Record<string, any>; status: "pending" | "approved" | "declined" }
export interface FollowupMessage { id: string; role: "user" | "agent"; text: string; selected?: string[]; turn?: string; error?: boolean; /** the message is finished: later text starts a new one */ closed?: boolean; /** written before a working tool, not the reply */ narration?: boolean }
export interface FollowupState { level: string | null; selected: string[]; messages: FollowupMessage[]; proposal: Proposal | null; whatif: any; running: boolean; /** turn whose reply end_followup has closed */ endedTurn: string | null }
export interface ReviewState {
  mode: "review" | "thread"; status: "idle" | "running" | "complete" | "failed" | "canceled"; endState: string | null; failure: string | null; phase: number; seen: number;
  budget: Budget | null; /** ms timestamp of the last budget event */ budgetAt: number | null; trail: TrailEntry[]; findings: Record<string, Finding>; findingOrder: string[]; marks: Mark[]; brief: Brief | null;
  refine: { round: number; rounds: number | null; investigation: string | null } | null;
  cleared: { id: string; rule: string | null; count: number; reason: string }[];
  candidates: { total: number; reviewed: number; byRule: Record<string, number> };
  investigations: Record<string, Investigation>; investigationOrder: string[]; sims: Record<string, Sim>; experiments: Experiment[];
  spotlight: { targets: string[]; findingId: string | null; reason: string; seq: number } | null; reportReady: { revision: number } | null;
  assumptions: { id: string; text: string; source?: string }[]; requests: Record<string, PendingRequest>; followup: FollowupState; notices: { id: string; text: string }[];
}
export const PHASES: { id: number; key: string; label: string; short: string; actor: string }[];
export function initialReviewState(mode?: "review" | "thread"): ReviewState;
export function roundOf(title: string | undefined | null): number | undefined;
export function toolLabel(tool: string | undefined): string;
export function resultText(content: unknown): string;
export function reviewReducer(state: ReviewState, event: Pick<EventEnvelope, "sequence" | "type" | "payload"> & { created_at?: string; turn_id?: string }): ReviewState;
export function replayAll(events: any[], state?: ReviewState): ReviewState;
export const SEVERITY_ORDER: Severity[];
export function severityCounts(findings: Finding[]): Record<Severity, number>;
export function sortFindings(findings: Finding[]): Finding[];
export function activeMarks(state: ReviewState): { key: string; findingId: string | null; severity: Severity; mark: Mark }[];
export function numberFindings(state: ReviewState): Map<string, number>;
