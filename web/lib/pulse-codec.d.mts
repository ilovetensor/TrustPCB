export function decodePulseFrames(buffer: ArrayBuffer, meta: { shape: number[]; dtype?: string; encoding?: string }): Promise<Float32Array[] | null>;
