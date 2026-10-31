// The antenna story, read from a report snapshot: the partial win, and what screen predicted against what confirm measured.
// Pure functions over stored data. Nothing here estimates anything; a missing field stays missing.

export const OUTCOME_TEXT = {
  held: "The screen result held at confirm quality",
  held_smaller: "The improvement held, but the screen overstated it",
  held_after_retune: "Held once the matching parts were re-tuned at confirm quality",
  not_held: "The screen result did not hold at confirm quality",
  improved_short_of_target: "Improved, target not met",
  unconfirmed: "Not confirmed",
};

/** Text of one part change: "L1 2.2 nH to 6.8 nH (series)". */
export function partText(p) {
  const was = String(p.was ?? "").replace(/ \(fitted\)$/, "");
  return `${p.ref}${was ? ` ${was} to ${p.now}` : ` ${p.now}`}${p.position ? ` (${p.position})` : ""}`;
}

/** What a best candidate changes, as one line: antenna length and parts. */
export function changeText(c) {
  const bits = [];
  for (const op of c?.ops ?? []) {
    if (op.op === "adjust_antenna_length" && typeof op.delta_mm === "number") bits.push(`${op.delta_mm < 0 ? "shorten" : "lengthen"} the antenna by ${Math.abs(op.delta_mm)} mm`);
    else if (op.op) bits.push(String(op.op).replace(/_/g, " "));
  }
  for (const p of c?.parts ?? []) bits.push(partText(p));
  return bits.join("; ");
}

/**
 * The partial win of a report snapshot, or null. The snapshot's top-level `headline` is the source. A confirmation that says
 * `improved_short_of_target` and carries `best` and `base` counts too, for a snapshot that has the confirmation but no headline.
 */
export function headlineOf(snapshot) {
  if (!snapshot) return null;
  const raw = snapshot.headline;
  const h = raw && raw.kind === "version_change" ? raw.partial_win : raw;     // a recheck's headline is the version change; its own partial win, if any, rides inside it
  if (h && h.best && h.base) return normalise(h, h.investigation ?? null);
  for (const inv of snapshot.investigations ?? []) {
    const confs = [inv.confirmation, ...(inv.refine_rounds ?? []).map((r) => r?.confirmation)].filter(Boolean);
    for (const c of confs) {
      if (c.outcome === "improved_short_of_target" && c.best && c.base)
        return normalise({ ...c, gain_db: c.best_gain_db, investigation: inv.code, finding: inv.outcome?.finding ?? null }, inv.code ?? null);
    }
  }
  return null;
}

/**
 * What changed since the previous version, from a report snapshot: the server's `version_change` (also the `headline` of a recheck).
 * A snapshot stored before that field existed has none, and none is made up here.
 */
export function versionChangeOf(snapshot) {
  if (!snapshot) return null;
  const v = snapshot.version_change ?? (snapshot.headline?.kind === "version_change" ? snapshot.headline : null);
  return v && v.from && v.to ? v : null;
}

/** The change state of each finding code in a snapshot's version changes: fixed, improved, still_present, worse or new. */
export function changeByCode(snapshot) {
  const out = {};
  const vc = snapshot?.version_changes;
  if (!vc) return out;
  for (const bucket of ["still_present", "changed", "new"]) {
    for (const f of vc[bucket] ?? []) if (f.code) out[f.code] = { change: f.change ?? (bucket === "new" ? "new" : "still_present"), note: f.note ?? null, changes: f.changes ?? [] };
  }
  return out;
}

function normalise(h, investigation) {
  return {
    outcome: "improved_short_of_target",
    investigation,
    finding: h.finding ?? null,
    findingTitle: h.finding_title ?? null,
    gainDb: typeof h.gain_db === "number" ? h.gain_db : typeof h.best_gain_db === "number" ? h.best_gain_db : null,
    targetDb: typeof h.target_db === "number" ? h.target_db : null,
    partsText: h.parts_text ?? changeText(h.best),
    parts: h.best?.parts ?? [],
    best: h.best,
    base: h.base,
    statement: h.statement ?? null,
    text: h.text ?? null,
    evidence: [h.base_evidence, h.best?.evidence, h.comparison_evidence].filter(Boolean),
  };
}

/**
 * Screen against confirm, per investigation round: what the quick screen predicted and what the confirm run measured, for the base and for the change.
 * Each entry: {investigation, round, outcome, statement, rows: [{label, screenRef, screen, confirmRef, confirm}]} with worst in-band S11 in dB.
 */
export function screenVsConfirm(snapshot) {
  const out = [];
  const rowsOf = (conf) => {
    const rows = [];
    const r = conf?.rows;
    if (!r) return rows;
    const line = (label, side) => {
      if (!side || (!side.screen && !side.confirm)) return;
      rows.push({
        label,
        screenRef: side.screen?.ref ?? null, screen: side.screen?.band_worst_s11_db ?? null,
        confirmRef: side.confirm?.ref ?? null, confirm: side.confirm?.band_worst_s11_db ?? null,
      });
    };
    line("As built", r.base);
    line("With the change", r.winner);
    return rows;
  };
  for (const inv of snapshot?.investigations ?? []) {
    const rounds = [{ round: 0, conf: inv.confirmation }, ...(inv.refine_rounds ?? []).map((r) => ({ round: r.round ?? 1, conf: r.confirmation }))];
    for (const { round, conf } of rounds) {
      if (!conf) continue;
      const rows = rowsOf(conf);
      if (!rows.length) continue;
      out.push({ investigation: inv.code ?? null, round, outcome: conf.outcome ?? null, statement: conf.statement ?? null, held: conf.held ?? null, meetsTarget: conf.meets_target ?? null, rows });
    }
  }
  return out;
}

/**
 * Every test of a report snapshot that belongs to a suspect, in the shape the board draws (a run, or a matching calculation that used no solver time).
 * Rows come from stored evidence only.
 */
export function testsOf(snapshot) {
  const out = [];
  for (const e of snapshot?.evidence ?? []) {
    const v = e.values ?? {};
    if (!v.ref || !v.suspect) continue;
    out.push({
      id: v.ref, label: v.label ?? e.code, suspect: v.suspect, quality: v.quality ?? null,
      kind: e.source_tool === "tune_matching" ? "matching" : e.type === "confirm" ? "confirm" : "whatif",
      status: "done", measurements: v.measurements ?? {},
    });
  }
  return out;
}
