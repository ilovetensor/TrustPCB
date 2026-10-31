// Same-origin HTTP helpers and the event-stream client for the live backend.
// Every request goes to /api/... on this origin. The Next route handler adds the launch token,
// so no secret ever reaches the browser.

import { nextAfter, readFrames } from "./events.mjs";
import type { EventEnvelope } from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function failure(r: Response): Promise<ApiError> {
  let detail = `${r.status} ${r.statusText}`;
  try {
    const j = await r.json();
    if (typeof j?.detail === "string") detail = j.detail;
    else if (j?.detail) detail = JSON.stringify(j.detail);
  } catch { /* not JSON */ }
  return new ApiError(r.status, detail);
}

export async function apiGet<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api/${path}`, { ...init, headers: { accept: "application/json", ...(init?.headers ?? {}) } });
  if (!r.ok) throw await failure(r);
  return r.json() as Promise<T>;
}

export async function apiSend<T>(method: "POST" | "PATCH", path: string, body?: unknown): Promise<T> {
  const r = await fetch(`/api/${path}`, {
    method,
    headers: { "content-type": "application/json", accept: "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw await failure(r);
  return r.json() as Promise<T>;
}

/** Upload files with progress. A single zip goes as one part; a folder sends each file with its relative path. */
export function uploadFiles(
  path: string, files: File[], fields: Record<string, string> = {}, onProgress?: (fraction: number) => void,
): Promise<any> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const [k, v] of Object.entries(fields)) form.append(k, v);
    for (const f of files) {
      const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
      form.append("file", f, rel);
    }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/${path}`);
    xhr.responseType = "json";
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress?.(e.loaded / e.total); };
    xhr.onerror = () => reject(new ApiError(0, "The upload could not reach the local service."));
    xhr.onload = () => {
      const body = xhr.response;
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new ApiError(xhr.status, typeof body?.detail === "string" ? body.detail : `Upload failed (${xhr.status})`));
    };
    xhr.send(form);
  });
}

export type StreamState = "connecting" | "live" | "retrying" | "ended" | "error";

/**
 * Read an SSE endpoint with fetch streaming. Replays from `after`, follows new events and reconnects
 * from the last sequence it saw. Resolves when `stopWhen` returns true for an event or `signal` aborts.
 */
export async function followEvents(opts: {
  url: string; after?: number; signal: AbortSignal;
  onBatch: (events: EventEnvelope[]) => void; onState?: (s: StreamState) => void;
  stopWhen?: (e: EventEnvelope) => boolean;
}) {
  let after = opts.after ?? 0;
  let failures = 0;
  const decoder = new TextDecoder();
  while (!opts.signal.aborted) {
    opts.onState?.(failures ? "retrying" : "connecting");
    try {
      const r = await fetch(`${opts.url}${opts.url.includes("?") ? "&" : "?"}after=${after}`, { signal: opts.signal, headers: { accept: "text/event-stream" } });
      if (!r.ok || !r.body) throw new ApiError(r.status, `stream ${r.status}`);
      opts.onState?.("live");
      failures = 0;
      const reader = r.body.getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const { events, rest } = readFrames(buffer);
        buffer = rest;
        if (events.length) {
          after = nextAfter(events);
          opts.onBatch(events);
          if (opts.stopWhen && events.some(opts.stopWhen)) {
            opts.onState?.("ended");
            await reader.cancel().catch(() => {});
            return;
          }
        }
      }
    } catch (e) {
      if (opts.signal.aborted) return;
      if (e instanceof ApiError && (e.status === 404 || e.status === 401 || e.status === 403)) {
        opts.onState?.("error");
        return;
      }
    }
    if (opts.signal.aborted) return;
    failures += 1;
    opts.onState?.("retrying");
    await new Promise((res) => setTimeout(res, Math.min(8000, 600 * 2 ** Math.min(failures, 4))));
  }
}
