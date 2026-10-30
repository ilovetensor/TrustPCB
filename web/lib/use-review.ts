"use client";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { askFollowup, getReviewReplay, type ReplaySource } from "./api";
import { initialReviewState, reviewReducer, type ReviewState } from "./review-reducer.mjs";

type Action = { type: "event"; event: any } | { type: "reset" };
function reducer(state: ReviewState, a: Action): ReviewState {
  if (a.type === "reset") return initialReviewState();
  return reviewReducer(state, a.event);
}

/**
 * Drives the review reducer from the event source. Today the source is a fixture
 * replay; the live source feeds the same reducer from SSE (lib/events.mjs).
 */
export function useReview(reviewId: string, opts: { startAt?: number; autoplay?: boolean } = {}) {
  const [state, dispatch] = useReducer(reducer, undefined, initialReviewState);
  const [src, setSrc] = useState<ReplaySource | null>(null);
  const [clock, setClock] = useState(0);
  const [playing, setPlaying] = useState(opts.autoplay ?? true);
  const [speed, setSpeed] = useState(1);
  const cursor = useRef(0);
  const clockRef = useRef(0);
  const fx = useRef(100000);

  useEffect(() => {
    let live = true;
    getReviewReplay(reviewId).then((s) => { if (live) setSrc(s); });
    return () => { live = false; };
  }, [reviewId]);

  const seek = useCallback((t: number) => {
    if (!src) return;
    if (t < clockRef.current) { dispatch({ type: "reset" }); cursor.current = 0; }
    clockRef.current = t;
    setClock(t);
    while (cursor.current < src.events.length && src.events[cursor.current]._t <= t) dispatch({ type: "event", event: src.events[cursor.current++] });
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
        while (cursor.current < src.events.length && src.events[cursor.current]._t <= t) dispatch({ type: "event", event: src.events[cursor.current++] });
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [src, playing, speed]);

  const restart = useCallback(() => { dispatch({ type: "reset" }); cursor.current = 0; clockRef.current = 0; setClock(0); setPlaying(true); }, []);

  /* follow-up conversation (fixture runner; live version posts to /api/threads/{id}/messages) */
  const emit = useCallback((type: string, payload: Record<string, unknown>) => {
    fx.current += 1;
    dispatch({ type: "event", event: { sequence: fx.current, type, payload, turn_id: "fx-turn", created_at: new Date().toISOString() } });
  }, []);
  const send = useCallback((text: string, selected: string[]) => {
    emit("user_message", { text, selected });
    let delay = 0;
    for (const c of askFollowup(text, selected)) {
      delay += c.delay;
      setTimeout(() => emit(c.type, c.payload), delay);
    }
  }, [emit]);
  const decide = useCallback((requestId: string, approved: boolean) => {
    emit("test_decided", { request_id: requestId, approved });
    setTimeout(() => {
      emit("text_delta", { text: approved ? "Approved. The live solver queue would run this now. The fixture runner does not simulate, so no result is added." : "Understood. Nothing will be simulated." });
      emit("followup_done", {});
    }, 300);
  }, [emit]);

  return { state, src, clock, duration: src?.duration ?? 0, playing, setPlaying, speed, setSpeed, restart, seek, send, decide };
}
