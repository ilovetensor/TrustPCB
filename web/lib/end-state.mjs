// How a finished review is named on screen. One place, so the sidebar, the lists and the report say the same thing.
// "Complete" is only for a review that ran to the end. An agent that stopped before finishing, or one stopped by the user, is not complete.
export const END_STATE_TEXT = {
  complete: "Complete",
  complete_with_limits: "Complete, limits",
  agent_stopped: "Stopped early",
  canceled: "Stopped",
  interrupted: "Interrupted",
  pipeline_failed: "Failed",
  failed: "Failed",
};

/** The words for a review's end state; `fallback` (for example the stream status) is used when the end state is unknown. */
export function endStateText(endState, fallback = "") {
  return END_STATE_TEXT[endState] ?? fallback;
}
