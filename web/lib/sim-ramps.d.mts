export const HEAT_STOPS: [number, [number, number, number]][];
export const DIVERGING_STOPS: [number, [number, number, number]][];
export function stopsFor(ov: { scale?: "sequential" | "diverging" } | null | undefined): [number, [number, number, number]][];
export function lutFor(ov: { scale?: "sequential" | "diverging" } | null | undefined): Uint8Array;
