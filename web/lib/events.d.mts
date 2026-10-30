export function mergeEvents<T extends { sequence: number }>(previous: T[], incoming: T[]): T[];
export function readFrames(buffer: string): { events: any[]; rest: string };
export function isReviewRunning(events: { type: string; payload?: any }[], fallbackStatus?: string): boolean;
export function nextAfter(events: { sequence: number }[]): number;
export function streamUrl(base: string, after: number): string;
