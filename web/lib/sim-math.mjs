// Pure helpers for solver results. No browser, no React: unit tested against the bundled real nRF run.

/** Local minima of an S11 curve deeper than `floorDb` that stand out from their surroundings. Used only when the solver summary did not list the dips. */
export function findDips(c, floorDb = -3) {
  const out = [];
  const n = c.s11_db.length;
  for (let i = 1; i < n - 1; i++) {
    const y = c.s11_db[i];
    if (y <= floorDb && y <= c.s11_db[i - 1] && y < c.s11_db[i + 1]) {
      let hi = -Infinity;
      for (let k = Math.max(0, i - 12); k <= Math.min(n - 1, i + 12); k++) hi = Math.max(hi, c.s11_db[k]);
      if (hi - y >= 1.5) out.push({ f_ghz: c.f_ghz[i], db: y });
    }
  }
  return out;
}

/** Impedance (ohm) from a complex reflection coefficient against z0. */
export function zFromGamma(re, im, z0 = 50) {
  const z_re = [], z_im = [];
  for (let i = 0; i < re.length; i++) {
    const nr = 1 + re[i], ni = im[i], dr = 1 - re[i], di = -im[i];
    const den = dr * dr + di * di || 1e-12;
    z_re.push(z0 * ((nr * dr + ni * di) / den));
    z_im.push(z0 * ((ni * dr - nr * di) / den));
  }
  return { z_re, z_im };
}

/** What-if operations in words. */
export function describeOps(ops, material) {
  const parts = (ops ?? []).map((o) => {
    if (o.op === "adjust_antenna_length") { const d = Number(o.delta_mm); return `antenna length ${d >= 0 ? "+" : "−"}${Math.abs(d).toFixed(1)} mm`; }
    if (o.op === "remove_nearby_part") return `remove ${String(o.ref)}`;
    if (o.op === "clear_copper_region") return `clear copper${o.layer ? ` on ${String(o.layer)}` : ""}`;
    if (o.op === "set_material") return "set material";
    return String(o.op).replace(/_/g, " ");
  });
  if (material && !(ops ?? []).some((o) => o.op === "set_material")) parts.push("material changed");
  return parts.join(", ");
}

/** The frequency a view is drawn at, said plainly against the target band (GHz). */
export function describeFreq(f_hz, band) {
  if (!f_hz) return "";
  const g = (f_hz / 1e9).toFixed(3);
  if (!band) return `${g} GHz`;
  const inside = f_hz / 1e9 >= band[0] - 1e-9 && f_hz / 1e9 <= band[1] + 1e-9;
  return `${g} GHz (${inside ? "inside" : "outside"} the ${band[0].toFixed(2)} to ${band[1].toFixed(2)} GHz band)`;
}

/** Measurements in the same names and units the API sends (resonance in MHz), taken from a summary.json. */
export function measurementsOfSummary(s) {
  const out = {};
  const r = s.sim;
  if (r?.f_res != null) out.resonance = { value: +(r.f_res / 1e6).toFixed(3), unit: "MHz" };
  if (r?.s11_min_db != null) out.s11_min = { value: +r.s11_min_db.toFixed(2), unit: "dB" };
  if (r?.bandwidth?.width_hz != null) out.bandwidth = { value: +(r.bandwidth.width_hz / 1e6).toFixed(2), unit: "MHz" };
  if (r?.dmax_dbi != null) out.peak_directivity = { value: +r.dmax_dbi.toFixed(2), unit: "dBi" };
  if (r?.efficiency_est != null) out.radiation_efficiency = { value: +(r.efficiency_est * 100).toFixed(1), unit: "%" };
  if (s.s11?.antenna_only) out.antenna_only_band_worst_s11 = { value: +s.s11.antenna_only.band_worst_db.toFixed(2), unit: "dB" };
  if (s.s11?.as_built) out.as_built_band_worst_s11 = { value: +s.s11.as_built.band_worst_db.toFixed(2), unit: "dB" };
  if (r?.first_series_resonance_hz != null) out.first_series_resonance = { value: +(r.first_series_resonance_hz / 1e6).toFixed(1), unit: "MHz" };
  (r?.s11_dips ?? []).forEach((d, i) => { out[`s11_dip_${i + 1}`] = { value: +(d.f_hz / 1e6).toFixed(1), unit: "MHz" }; out[`s11_dip_${i + 1}_depth`] = { value: +d.s11_db.toFixed(2), unit: "dB" }; });
  return out;
}

const withFile = (j, ...names) => j.status === "done" && names.every((n) => j.files.includes(n));
/** The run whose field the board stage shows: the latest confirm run with a field, else the latest run with one. */
export function pickStageJob(jobs) {
  const withField = jobs.filter((j) => withFile(j, "field.json", "field_xy.bin"));
  return [...withField].reverse().find((j) => j.kind === "confirm") ?? withField[withField.length - 1] ?? null;
}
/** The run a version is compared by: its confirm run, else its latest finished run with a curve. */
export function bestRun(jobs) {
  const done = jobs.filter((j) => withFile(j, "s11.json"));
  return [...done].reverse().find((j) => j.kind === "confirm") ?? done[done.length - 1] ?? null;
}

/**
 * Board y and scenario y are opposites (meta.json y_convention: scenario_y = -board_y). Returns the board-y extent of a sidecar's v range.
 * Image rows increase with scenario y, so row 0 sits at the LARGEST board y: a picture on a y-down board is drawn with its rows flipped.
 */
export function boardYRange(v_mm) {
  return [-v_mm[1], -v_mm[0]];
}
