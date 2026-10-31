// The one definition of who an agent is, for every place the interface shows its work.
// Keyed by the runner that actually wrote the text (a review's stored profile.runner), never by the app's default.
// A runner that is not listed here, or missing, gets the neutral agent icon with no brand: that is the honest default.
//
// To add another runner, add one entry below. Nothing else changes: <AgentMark> is the only thing that draws a logo.
//
// Logo files live in web/public/brand and their sources are in NOTICE. If a logo file cannot load, the mark falls back
// to the neutral icon, and its accessible name still says who wrote the text.

/** @typedef {{ id: string, name: string, brand: string | null, logo: string | null, logoAlt: string }} AgentInfo */

/** @type {Readonly<AgentInfo>} */
export const NEUTRAL = Object.freeze({ id: "agent", name: "the agent", brand: null, logo: null, logoAlt: "Agent" });

/** @type {Readonly<Record<string, AgentInfo>>} */
const REGISTRY = Object.freeze({
  muse_api: Object.freeze({ id: "muse_api", name: "Meta Muse", brand: "meta", logo: "/brand/meta-logo.svg", logoAlt: "Meta" }),
});

/** The registry entry for a runner. Unknown, empty or missing runners are neutral. */
export function agentFor(runner) {
  return typeof runner === "string" && Object.hasOwn(REGISTRY, runner) ? REGISTRY[runner] : NEUTRAL;
}

/** The runner of a stored review profile: `{ runner, name, model, ... }`. Older rows may lack it. */
export function runnerOfProfile(profile) {
  if (!profile || typeof profile !== "object") return null;
  const r = profile.runner ?? profile.name;
  return typeof r === "string" && r ? r : null;
}

const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);

/** Sentence-case accessible name: "Answered by Meta Muse", "Written by the agent", "Meta Muse is working". */
export function agentLabel(runner, kind = "answered") {
  const name = agentFor(runner).name;
  if (kind === "working") return `${cap(name)} is working`;
  if (kind === "wrote") return `Written by ${name}`;
  return `Answered by ${name}`;
}
