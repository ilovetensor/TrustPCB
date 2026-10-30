export const FIELD_STOPS: [number, [number, number, number]][];
export const SIGNED_STOPS: [number, [number, number, number]][];
export function sample(stops: [number, [number, number, number]][], t: number): [number, number, number];
export function buildLut(stops: [number, [number, number, number]][], n?: number): Uint8Array;
export function cssGradient(stops: [number, [number, number, number]][], dir?: string): string;
export function rgb(stops: [number, [number, number, number]][], t: number): string;
