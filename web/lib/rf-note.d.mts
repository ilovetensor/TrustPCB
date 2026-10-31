export function sentence(text: unknown): string;
export function simulationsNote(rf: { status: string; reason?: string | null } | null | undefined): string | null;
export function rfLabExplanation(rf: { reason?: string | null } | null | undefined): { lead: string; why: string; hint: string };
