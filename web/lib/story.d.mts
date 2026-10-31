export const OUTCOME_TEXT: Record<string, string>;
export interface PartChange { ref: string; position?: string | null; kind?: string | null; value?: number | null; was?: string | null; now?: string | null }
export interface BestCandidate {
  mode?: string; ref?: string; evidence?: string; ops?: { op: string; [k: string]: unknown }[]; parts?: PartChange[];
  band_worst_s11_db: number; band_mean_s11_db?: number | null; accepted_power_worst_pct?: number | null; accepted_power_mean_pct?: number | null; meets_target?: boolean;
}
export interface Headline {
  outcome: "improved_short_of_target"; investigation: string | null; finding: string | null; findingTitle: string | null; gainDb: number | null; targetDb: number | null;
  partsText: string; parts: PartChange[]; best: BestCandidate; base: BestCandidate; statement: string | null; text: string | null; evidence: string[];
}
export function partText(p: PartChange): string;
export function changeText(c: { ops?: unknown[]; parts?: PartChange[] } | null | undefined): string;
export function headlineOf(snapshot: any): Headline | null;
export interface StoryRow { label: string; screenRef: string | null; screen: number | null; confirmRef: string | null; confirm: number | null }
export interface StoryRound { investigation: string | null; round: number; outcome: string | null; statement: string | null; held: boolean | null; meetsTarget: boolean | null; rows: StoryRow[] }
export function screenVsConfirm(snapshot: any): StoryRound[];
export interface StoryTest { id: string; label: string; suspect: string; quality: string | null; kind: string; status: "done"; measurements: Record<string, { value: number; unit: string }> }
export function testsOf(snapshot: any): StoryTest[];
