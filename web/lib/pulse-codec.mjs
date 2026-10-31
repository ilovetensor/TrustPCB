// Pulse frames as stored in the bundled sample runs.
//
// The solver writes pulse.bin as float32 |E|/scale frames (pulse.json: shape [frames, rows, cols]). The bundled samples
// carry the same frames packed by scripts/pack-pulse.py to keep the first page load small:
//   dtype "uint8"   one byte per value, q = round(255 * sqrt(|E|/scale)), the same square-root curve the shaders draw with
//   encoding "gzip" the whole byte array is gzip compressed (static hosting does not compress it)
// Frames, rows, columns and times are untouched. A pulse.json without "dtype" is the solver's own float32 file.

const SQUARE = Float32Array.from({ length: 256 }, (_, i) => (i / 255) ** 2);

async function inflate(bytes) {
  if (typeof DecompressionStream === "undefined") return null;
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/**
 * Frames (Float32Array of rows * cols each, |E|/scale) from the bytes of pulse.bin and its pulse.json.
 * Null when the bytes do not fit the declared shape or this browser cannot inflate gzip.
 */
export async function decodePulseFrames(buffer, meta) {
  const [frames, rows, cols] = meta.shape;
  const n = rows * cols;
  if (meta.dtype !== "uint8") {
    if (buffer.byteLength !== frames * n * 4) return null;
    const all = new Float32Array(buffer);
    return Array.from({ length: frames }, (_, i) => all.subarray(i * n, (i + 1) * n));
  }
  let bytes = new Uint8Array(buffer);
  if (meta.encoding === "gzip") bytes = await inflate(bytes);
  if (!bytes || bytes.length !== frames * n) return null;
  return Array.from({ length: frames }, (_, i) => {
    const out = new Float32Array(n);
    const at = i * n;
    for (let j = 0; j < n; j++) out[j] = SQUARE[bytes[at + j]];
    return out;
  });
}
