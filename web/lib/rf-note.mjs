// Plain sentences for a review that has no simulation result.
// The server's reason is written to be joined into other text, so it starts with a lowercase letter.
export function sentence(text) {
  const t = String(text ?? "").trim();
  if (!t) return "";
  const s = t.charAt(0).toUpperCase() + t.slice(1);
  return /[.!?]$/.test(s) ? s : `${s}.`;
}

// The line under the "Not tested" label in the Simulations view (the label is already there, so it is not repeated).
export function simulationsNote(rf) {
  if (!rf || rf.status !== "not_tested") return null;
  return sentence(rf.reason) || "No simulation was run for this review.";
}

// The explanation on the RF lab page when the review has no run. A reason from the server says it all:
// the depth hint (Quick runs nothing, Standard simulates when the lab is installed) is only for a review with no stated reason,
// because it reads as a promise the board can never keep when the reason is, say, that it has no radio.
export function rfLabExplanation(rf) {
  const why = sentence(rf?.reason);
  return {
    lead: "Nothing in this review was simulated, so there is no resonance, S11 curve, field or pattern to show. Nothing here is estimated.",
    why,
    hint: why ? "" : "Quick reviews run no simulation. Standard and Deep reviews simulate the antenna when the RF lab is installed.",
  };
}
