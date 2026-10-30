// Event helpers shared by the live SSE client and the fixture replay.
export function mergeEvents(previous, incoming) {
  const bySequence = new Map(previous.map((event) => [event.sequence, event]));
  for (const event of incoming) bySequence.set(event.sequence, event);
  return [...bySequence.values()].sort((a, b) => a.sequence - b.sequence);
}

export function readFrames(buffer) {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const frames = normalized.split("\n\n");
  const rest = frames.pop() ?? "";
  const events = [];
  for (const frame of frames) {
    const data = frame
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (data) events.push(JSON.parse(data));
  }
  return { events, rest };
}

const terminal = ["completed", "failed", "canceled"];
export function isReviewRunning(events, fallbackStatus = "") {
  const busy = (s) => s === "running" || String(s).startsWith("waiting");
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (terminal.includes(e.type)) return false;
    if (e.type === "status" && typeof e.payload?.status === "string")
      return busy(e.payload.status);
    if (e.type === "phase_changed") return true;
  }
  return busy(fallbackStatus);
}

export function nextAfter(events) {
  return events.length ? events[events.length - 1].sequence : 0;
}

export function streamUrl(base, after) {
  return `${base}/stream?after=${after}`;
}
