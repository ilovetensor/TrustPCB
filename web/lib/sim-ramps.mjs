// Colour scales for the board heat maps. Data colours, not interface colours.
// Sequential: a plum floor (so low values stay visible on black) up through magenta and vermilion to pale amber.
// Diverging: cool violet for below the target, a pale neutral at zero, warm vermilion for above.
import { buildLut } from "./ramp.mjs";

export const HEAT_STOPS = [
  [0.0, [62, 30, 132]],
  [0.2, [112, 38, 156]],
  [0.42, [190, 44, 134]],
  [0.62, [240, 84, 58]],
  [0.82, [255, 166, 66]],
  [1.0, [255, 238, 190]],
];

export const DIVERGING_STOPS = [
  [0.0, [112, 98, 255]],
  [0.25, [158, 140, 236]],
  [0.5, [226, 224, 230]],
  [0.75, [255, 150, 96]],
  [1.0, [236, 58, 40]],
];

export const stopsFor = (ov) => (ov?.scale === "diverging" ? DIVERGING_STOPS : HEAT_STOPS);
const luts = new Map();
/** The 256-entry RGBA table for an overlay's scale, built once. */
export function lutFor(ov) {
  const stops = stopsFor(ov);
  if (!luts.has(stops)) luts.set(stops, buildLut(stops));
  return luts.get(stops);
}
