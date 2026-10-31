// The goal sent with a review start. A board with no radio has no band and no S11 target,
// so those two fields are left out of the goal rather than sent as a default.
export function parseBand(text) {
  const m = String(text ?? "").match(/(\d+(?:\.\d+)?)\s*(?:to|-|–)\s*(\d+(?:\.\d+)?)\s*(GHz|MHz)?/i);
  if (!m) return undefined;
  const k = /mhz/i.test(m[3] ?? "") ? 1e6 : 1e9;
  const lo = parseFloat(m[1]) * k, hi = parseFloat(m[2]) * k;
  return hi > lo ? [lo, hi] : undefined;
}

// `wireless` is the detection result: true, false, or undefined when nothing was detected yet.
export function buildGoal({ purpose = "", band = "", target = "", focus = "", wireless } = {}) {
  const goal = {};
  if (purpose.trim()) goal.purpose = purpose.trim();
  if (wireless !== false) {
    const b = parseBand(band);
    if (b) goal.band_hz = b;
    const t = parseFloat(target);
    if (Number.isFinite(t)) goal.target_s11_db = t;
  }
  if (focus.trim()) goal.focus = focus.trim();
  return goal;
}

// Depth note on the upload page. With no radio there is no antenna simulation, so only the time limit is kept.
// `sims` says that power, heat or signal checks are chosen: those still run on a board with no radio.
export function depthNote(note, depth, wireless, sims = false) {
  if (wireless !== false || depth === "quick") return note;
  const limit = note.split(". ")[0].replace(/\.$/, "");
  return sims ? `${limit}. No radio found: the power, heat and signal checks you chose are simulated.` : `${limit}. No radio found, so nothing is simulated.`;
}
