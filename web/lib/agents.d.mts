export interface AgentInfo { id: string; name: string; brand: string | null; logo: string | null; logoAlt: string }
export type AgentKind = "answered" | "wrote" | "working";
export const NEUTRAL: Readonly<AgentInfo>;
export function agentFor(runner: string | null | undefined): Readonly<AgentInfo>;
export function runnerOfProfile(profile: unknown): string | null;
export function agentLabel(runner: string | null | undefined, kind?: AgentKind): string;
