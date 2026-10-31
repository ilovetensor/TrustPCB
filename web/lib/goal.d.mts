export function parseBand(text: string): [number, number] | undefined;
export function buildGoal(input?: { purpose?: string; band?: string; target?: string; focus?: string; wireless?: boolean }): { purpose?: string; band_hz?: [number, number]; target_s11_db?: number; focus?: string };
export function depthNote(note: string, depth: string, wireless?: boolean, sims?: boolean): string;
