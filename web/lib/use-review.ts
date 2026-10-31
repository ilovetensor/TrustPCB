"use client";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  ApiError, cancelReview, cancelThread, FIXTURE_MODE, getReview, patchFinding, respondRequest, sendThreadMessage, startThread, type ReviewMeta,
} from "./api";
import { followEvents, type StreamState } from "./live";
import { initialReviewState, reviewReducer, type ReviewState } from "./review-reducer.mjs";

type Action =
  | { type: "events"; events: any[] }
  | { type: "reset" }
  | { type: "patch_finding"; id: string; patch: Record<string, unknown> };

function makeReducer(mode: "review" | "thread") {
  return (state: ReviewState, a: Action): ReviewState => {
    if (a.type === "reset") return initialReviewState(mode);
    if (a.type === "patch_finding") {
      const f = state.findings[a.id];
      return f ? { ...state, findings: { ...state.findings, [a.id]: { ...f, ...a.patch } } } : state;
    }
    let s = state;
    for (const e of a.events) s = reviewReducer(s, e);
    return s;
  };
}
const reviewReducerFn = makeReducer("review");
const threadReducerFn = makeReducer("thread");

/** A selected board object or finding handed to the follow-up chat. */
export interface Selected { id: string; type: "finding" | "object" }

export interface ReplayControls {
  clock: number; duration: number; playing: boolean; setPlaying: (p: boolean) => void; speed: number; setSpeed: (s: number) => void;
  restart: () => void; seek: (t: number) => void;
}

export interface ReviewHandle {
  state: ReviewState;
  /** the follow-up conversation alone */
  thread: ReviewState;
  meta: ReviewMeta | null;
  stream: StreamState;
  error: string | null;
  replay: ReplayControls | null;
  sending: boolean;
  send: (text: string, selected: Selected[]) => Promise<void>;
  decide: (requestId: string, allow: boolean) => Promise<void>;
  stop: () => Promise<void>;
  setFindingStatus: (findingId: string, status: "dismissed" | "open", reason?: string) => Promise<void>;
  clearError: () => void;
}

const newRequestId = () => (typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `req-${Date.now()}-${Math.random().toString(16).slice(2)}`);

function merge(review: ReviewState, thread: ReviewState): ReviewState {
  if (thread.seen < 0 && !thread.findingOrder.length) return review;
  const order = [...review.findingOrder];
  for (const id of thread.findingOrder) if (!order.includes(id)) order.push(id);
  return {
    ...review,
    findings: { ...review.findings, ...thread.findings },
    findingOrder: order,
    followup: thread.followup,
    requests: { ...review.requests, ...thread.requests },
    notices: [...review.notices, ...thread.notices],
    reportReady: thread.reportReady ?? review.reportReady,
    assumptions: [...review.assumptions, ...thread.assumptions],
    marks: review.marks,
  };
}

/* ------------------------------------------------ live ------------------------------------------------ */

function useLiveReview(reviewId: string): ReviewHandle {
  const [review, dispatchReview] = useReducer(reviewReducerFn, "review", initialReviewState as (m: "review") => ReviewState);
  const [thread, dispatchThread] = useReducer(threadReducerFn, "thread", initialReviewState as (m: "thread") => ReviewState);
  const [meta, setMeta] = useState<ReviewMeta | null>(null);
  const [stream, setStream] = useState<StreamState>("connecting");
  const [threadId, setThreadId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const metaRef = useRef<ReviewMeta | null>(null);
  metaRef.current = meta;

  const refreshMeta = useCallback(async () => {
    try {
      const m = await getReview(reviewId);
      setMeta(m);
      setThreadId((cur) => cur ?? m.threads[m.threads.length - 1]?.id ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the review.");
    }
  }, [reviewId]);

  useEffect(() => { dispatchReview({ type: "reset" }); dispatchThread({ type: "reset" }); setThreadId(null); void refreshMeta(); }, [reviewId, refreshMeta]);

  // the review's own event log: replay from the start, then follow
  useEffect(() => {
    const ctl = new AbortController();
    void followEvents({
      url: `/api/reviews/${reviewId}/stream`, signal: ctl.signal,
      onBatch: (events) => dispatchReview({ type: "events", events }),
      onState: setStream,
      stopWhen: (e) => e.type === "completed" || e.type === "failed" || e.type === "canceled",
    }).then(() => { if (!ctl.signal.aborted) void refreshMeta(); });
    return () => ctl.abort();
  }, [reviewId, refreshMeta]);

  // the follow-up conversation, once there is one
  useEffect(() => {
    if (!threadId) return;
    const ctl = new AbortController();
    void followEvents({ url: `/api/threads/${threadId}/stream`, signal: ctl.signal, onBatch: (events) => dispatchThread({ type: "events", events }) });
    return () => ctl.abort();
  }, [threadId]);

  // a newly finished follow-up turn may have revised the report
  const revisedAt = review.reportReady?.revision ?? 0;
  const threadRevision = thread.reportReady?.revision ?? 0;
  useEffect(() => { if (threadRevision) void refreshMeta(); }, [threadRevision, refreshMeta]);
  void revisedAt;

  const send = useCallback(async (text: string, selected: Selected[]) => {
    setError(null);
    setSending(true);
    try {
      let id = threadId;
      if (!id) {
        const t = await startThread(reviewId);
        id = t.id;
        setThreadId(id);
      }
      await sendThreadMessage(id, text, selected, newRequestId());
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : "The message was not sent.");
    } finally {
      setSending(false);
    }
  }, [reviewId, threadId]);

  const decide = useCallback(async (requestId: string, allow: boolean) => {
    setError(null);
    try { await respondRequest(requestId, { allow }); } catch (e) { setError(e instanceof Error ? e.message : "The answer was not sent."); }
  }, []);

  const stop = useCallback(async () => {
    setError(null);
    try {
      if (review.status === "running") await cancelReview(reviewId);
      else if (threadId && thread.followup.running) await cancelThread(threadId);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not stop."); }
  }, [reviewId, threadId, review.status, thread.followup.running]);

  const setFindingStatus = useCallback(async (findingId: string, status: "dismissed" | "open", reason?: string) => {
    setError(null);
    try {
      const row = await patchFinding(findingId, status, reason);
      const patch = { status: row.status, dismissed_reason: row.dismissed_reason ?? null };
      dispatchReview({ type: "patch_finding", id: findingId, patch });
      dispatchThread({ type: "patch_finding", id: findingId, patch });
    } catch (e) { setError(e instanceof Error ? e.message : "Could not change the finding."); }
  }, []);

  const state = useMemo(() => merge(review, thread), [review, thread]);
  return { state, thread, meta, stream, error, replay: null, sending, send, decide, stop, setFindingStatus, clearError: () => setError(null) };
}

/* ------------------------------------------------ fixture replay (design aid only) ------------------------------------------------ */

function useFixtureReview(reviewId: string, opts: { startAt?: number; autoplay?: boolean }): ReviewHandle {
  const [review, dispatchReview] = useReducer(reviewReducerFn, "review", initialReviewState as (m: "review") => ReviewState);
  const [thread, dispatchThread] = useReducer(threadReducerFn, "thread", initialReviewState as (m: "thread") => ReviewState);
  const [src, setSrc] = useState<{ events: any[]; duration: number } | null>(null);
  const [meta, setMeta] = useState<ReviewMeta | null>(null);
  const [clock, setClock] = useState(0);
  const [playing, setPlaying] = useState(opts.autoplay ?? true);
  const [speed, setSpeed] = useState(1);
  const cursor = useRef(0);
  const clockRef = useRef(0);
  const fx = useRef(100000);

  useEffect(() => {
    let live = true;
    void import("./fixture-api").then(async (m) => {
      const [s, mt] = await Promise.all([m.getReviewReplay(reviewId), getReview(reviewId)]);
      if (live) { setSrc(s); setMeta(mt); }
    });
    return () => { live = false; };
  }, [reviewId]);

  const seek = useCallback((t: number) => {
    if (!src) return;
    if (t < clockRef.current) { dispatchReview({ type: "reset" }); cursor.current = 0; }
    clockRef.current = t;
    setClock(t);
    const batch: any[] = [];
    while (cursor.current < src.events.length && src.events[cursor.current]._t <= t) batch.push(src.events[cursor.current++]);
    if (batch.length) dispatchReview({ type: "events", events: batch });
  }, [src]);

  const started = useRef(false);
  useEffect(() => {
    if (!src || started.current) return;
    started.current = true;
    if (opts.startAt) seek(opts.startAt);
  }, [src, opts.startAt, seek]);

  useEffect(() => {
    if (!src || !playing) return;
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = (now - last) / 1000;
      last = now;
      if (clockRef.current < src.duration + 0.5) {
        const t = clockRef.current + dt * speed;
        clockRef.current = t;
        setClock(t);
        const batch: any[] = [];
        while (cursor.current < src.events.length && src.events[cursor.current]._t <= t) batch.push(src.events[cursor.current++]);
        if (batch.length) dispatchReview({ type: "events", events: batch });
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [src, playing, speed]);

  const restart = useCallback(() => { dispatchReview({ type: "reset" }); cursor.current = 0; clockRef.current = 0; setClock(0); setPlaying(true); }, []);

  const emit = useCallback((type: string, payload: Record<string, unknown>) => {
    fx.current += 1;
    dispatchThread({ type: "events", events: [{ sequence: fx.current, type, payload, turn_id: "fx-turn", created_at: new Date().toISOString() }] });
  }, []);
  const send = useCallback(async (text: string, selected: Selected[]) => {
    const { askFollowup } = await import("./fixture-api");
    emit("user_message", { text, selected: selected.map((s) => s.id) });
    let delay = 0;
    for (const c of askFollowup(text, selected.map((s) => s.id))) {
      delay += c.delay;
      setTimeout(() => emit(c.type, c.payload), delay);
    }
  }, [emit]);
  const decide = useCallback(async (requestId: string, approved: boolean) => {
    emit("request_resolved", { request_id: requestId, kind: "test", allow: approved });
    setTimeout(() => {
      emit("text_delta", { text: approved ? "Approved. The live solver queue would run this now. The fixture runner does not simulate, so no result is added." : "Understood. Nothing will be simulated." });
      emit("completed", {});
    }, 300);
  }, [emit]);

  const state = useMemo(() => merge(review, thread), [review, thread]);
  const replay: ReplayControls = { clock, duration: src?.duration ?? 0, playing, setPlaying, speed, setSpeed, restart, seek };
  return {
    state, thread, meta, stream: "live", error: null, replay, sending: false, send, decide,
    stop: async () => setPlaying(false), setFindingStatus: async (id, status) => dispatchReview({ type: "patch_finding", id, patch: { status } }), clearError: () => {},
  };
}

/** Review state from the event source. Live by default; the fixture replay only when explicitly enabled. */
export const useReview: (reviewId: string, opts?: { startAt?: number; autoplay?: boolean }) => ReviewHandle = FIXTURE_MODE
  ? (id, opts = {}) => useFixtureReview(id, opts)
  : (id) => useLiveReview(id);
