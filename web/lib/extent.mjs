// The extent of a review: which checks run. Chosen on the start form, sent with the review, saved in the report's method section.
// Pure helpers, unit tested. The time figures are rough: the minutes a check adds to the layout review, as a low and a high. A power or heat run itself
// takes seconds (measured on the sample boards: power 2 to 4 s, heat 1 to 2 s, signal 1 to 20 s); the minutes are the agent reading and choosing between runs.
// A quick review stops before any simulation.

export const EXTENT_IDS = ["layout", "antenna", "power", "thermal", "signal", "datasheets"];

export const EXTENT_INFO = {
  layout: { label: "Layout", icon: "board", line: "KiCad checks, layout rules and an open review of the board.", cost: null, always: true },
  antenna: { label: "Antenna", icon: "wave", line: "Field solver on a printed antenna: tuning, match and pattern.", cost: [20, 30] },
  power: { label: "Power delivery", icon: "bolt", line: "Voltage drop and current density through the copper. Each run takes seconds.", cost: [1, 2] },
  thermal: { label: "Heat", icon: "flame", line: "Board and part temperatures from what each part dissipates. Each run takes seconds.", cost: [1, 2] },
  signal: { label: "Signals", icon: "pulse", line: "Impedance along a net, reflections and return paths. A run takes seconds to half a minute.", cost: [1, 3] },
  datasheets: { label: "Datasheets", icon: "file", line: "Looks up parts and reads their datasheets to improve the inputs.", cost: [2, 5] },
};

/** The keys that let datasheet lookup work: a DigiKey id and secret together, or a Mouser key. `status` is the answer of GET /api/settings/keys. */
export function datasheetKeysSet(status) {
  const set = (n) => !!status?.[n]?.set;
  return (set("DIGIKEY_CLIENT_ID") && set("DIGIKEY_CLIENT_SECRET")) || set("MOUSER_API_KEY");
}

/**
 * The tiles of the picker with what is available and why not.
 *  detection  the version's detection (wireless, antenna) or null before the upload
 *  keys       the key status, or null when it could not be read
 *  server     what the simulators say about this board: {power: {available, reason}, ...}, when the server sends it
 *  depth      the review depth: a quick review stops before any simulation
 */
export function extentOptions({ detection = null, keys = null, server = null, depth = "standard" } = {}) {
  const known = !!detection;
  return EXTENT_IDS.map((id) => {
    const info = EXTENT_INFO[id];
    let available = true, reason = null;
    if (id === "layout") { /* always */ }
    else if (!known) { available = false; reason = "Checked after the upload"; }
    else if (id === "antenna") {
      const a = detection.antenna;
      if (depth === "quick") { available = false; reason = "A quick review stops before simulation. Pick Standard or Deep."; }
      else if (!detection.wireless) { available = false; reason = "No printed antenna on this board"; }
      else if (a && a.type === "chip") { available = false; reason = "Chip antenna: a keep-clear review only"; }
      else if (!a) { available = false; reason = "No printed antenna found"; }
    } else if (depth === "quick") {
      available = false; reason = id === "datasheets" ? "A quick review runs no simulation, so datasheets add nothing. Pick Standard or Deep." : "A quick review stops before simulation. Pick Standard or Deep.";
    } else if (id === "datasheets") {
      if (!datasheetKeysSet(keys)) { available = false; reason = "Add keys in Settings"; }
    } else {
      const s = server?.[id];
      if (s && s.available === false) { available = false; reason = s.reason || "Not possible on this board"; }
    }
    return { id, label: info.label, icon: info.icon, line: info.line, cost: info.cost, always: !!info.always, available, reason };
  });
}

/** What a new form starts with: layout and every simulation this board can run. Datasheet lookup stays off until it is chosen. */
export const defaultExtent = (options) => options.filter((o) => (o.always || o.available) && o.id !== "datasheets").map((o) => o.id);

/** The preset the demo uses: every check this board can run. */
export const everythingExtent = (options) => options.filter((o) => o.always || o.available).map((o) => o.id);
export const layoutOnly = () => ["layout"];

/** A selection cleaned against the options: layout is always in, anything unavailable is out, the order is the picker's. */
export function cleanExtent(selected, options) {
  const ok = new Set(options.filter((o) => o.always || o.available).map((o) => o.id));
  const want = new Set([...selected, "layout"]);
  return EXTENT_IDS.filter((id) => want.has(id) && ok.has(id));
}

export const sameExtent = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

/** The minutes the selected checks add to the layout review: [low, high], or null when none adds time. */
export function extentCost(selected) {
  let lo = 0, hi = 0, any = false;
  for (const id of selected) { const c = EXTENT_INFO[id]?.cost; if (c) { lo += c[0]; hi += c[1]; any = true; } }
  return any ? [lo, hi] : null;
}
export const costText = (c) => (!c ? "" : c[0] === c[1] ? `about ${c[0]} min` : `${c[0]} to ${c[1]} min`);
export const costLine = (c) => (!c ? "No added time" : `Adds ${costText(c)}`);
