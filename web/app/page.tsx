"use client";

// End-user route (Section 6 BrowserFrame). Two phases: composer, active.
// Active view keeps the last frame on-screen so the person can inspect the
// end state; the primary action swaps from Stop run to Start another task
// when the status is terminal.

import { useCallback, useEffect, useRef, useState } from "react";

import { CaptchaBanner } from "@/components/CaptchaBanner";
import { Composer } from "@/components/Composer";
import { CursorLayer } from "@/components/CursorLayer";
import { PlanView } from "@/components/PlanView";
import { ScreencastFrame } from "@/components/ScreencastFrame";
import type {
  CaptchaEvent,
  CursorClickEvent,
  CursorMoveEvent,
  CursorScrollEvent,
  FocusPulseEvent,
  FrameEvent,
  KeystrokeEvent,
  PlanEvent,
  StreamEvent,
} from "@/lib/events";

type CursorEvent =
  | CursorMoveEvent
  | CursorClickEvent
  | CursorScrollEvent
  | FocusPulseEvent
  | KeystrokeEvent;
import { type SocketState, subscribeEvents, subscribeFrames } from "@/lib/ws";

type Phase = "composer" | "active";
type Lifecycle = "starting" | "running" | "paused" | "done" | "blocked" | "error";

type RunResponse = { run_id: string };

const TERMINAL: readonly Lifecycle[] = ["done", "blocked", "error"];

const STATUS_TEXT: Record<Lifecycle, string> = {
  starting: "Queued",
  running: "Running",
  paused: "Needs you",
  done: "Done",
  blocked: "Stopped",
  error: "Failed",
};

const STATUS_TONE: Record<Lifecycle, string> = {
  starting: "muted",
  running: "running",
  paused: "needs-you",
  done: "done",
  blocked: "muted",
  error: "failed",
};

const LIVE_TONE = (lifecycle: Lifecycle): "live" | "paused" | "off" => {
  if (lifecycle === "running") return "live";
  if (lifecycle === "paused") return "paused";
  return "off";
};

const formatElapsed = (ms: number | null): string => {
  if (ms === null || ms < 0) return "—";
  const total = Math.floor(ms / 100) / 10; // 0.1s precision
  if (total < 60) return `${total.toFixed(1)}s`;
  const mins = Math.floor(total / 60);
  const secs = Math.floor(total - mins * 60);
  return `${mins}:${secs.toString().padStart(2, "0")}`;
};

export default function Home() {
  const [phase, setPhase] = useState<Phase>("composer");
  const [runId, setRunId] = useState<string | null>(null);
  const [plan, setPlan] = useState<PlanEvent | null>(null);
  const [frame, setFrame] = useState<FrameEvent | null>(null);
  const [captcha, setCaptcha] = useState<CaptchaEvent | null>(null);
  const [lifecycle, setLifecycle] = useState<Lifecycle>("starting");
  const [statusReason, setStatusReason] = useState<string>("");
  const [cost, setCost] = useState<number>(0);
  const [currentUrl, setCurrentUrl] = useState<string>("");
  const [eventsState, setEventsState] = useState<SocketState>("closed");
  const [framesState, setFramesState] = useState<SocketState>("closed");
  const [eventCount, setEventCount] = useState(0);
  const [frameCount, setFrameCount] = useState(0);
  const [lastKind, setLastKind] = useState<string>("");
  // Run-wall-clock stopwatch. `startedAt` is null until the first event
  // arrives (or lifecycle becomes running). `frozenMs` is set when the run
  // reaches a terminal state so the counter stops incrementing.
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const startedAtRef = useRef<number | null>(null);
  const [frozenMs, setFrozenMs] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());

  const cursorSubscribers = useRef<Set<(event: CursorEvent) => void>>(new Set());

  const registerCursorHandler = useCallback(
    (handler: (event: CursorEvent) => void) => {
      cursorSubscribers.current.add(handler);
      return () => {
        cursorSubscribers.current.delete(handler);
      };
    },
    [],
  );

  const resetRunState = () => {
    setPlan(null);
    setFrame(null);
    setCaptcha(null);
    setLifecycle("starting");
    setStatusReason("");
    setCost(0);
    setCurrentUrl("");
    setEventCount(0);
    setFrameCount(0);
    setLastKind("");
    setEventsState("closed");
    setFramesState("closed");
    setStartedAt(null);
    startedAtRef.current = null;
    setFrozenMs(null);
  };

  useEffect(() => {
    if (!runId) return;
    const stopEvents = subscribeEvents(
      runId,
      (event) => {
        setEventCount((prior) => prior + 1);
        setLastKind(event.kind);
        if (startedAtRef.current == null) {
          startedAtRef.current = Date.now();
          setStartedAt(startedAtRef.current);
        }
        if (event.kind === "plan") setPlan(event);
        if (event.kind === "captcha") setCaptcha(event);
        if (event.kind === "budget") setCost(event.usd);
        if (event.kind === "observation") setCurrentUrl(event.url);
        if (event.kind === "outcome") setLifecycle("running");
        if (event.kind === "error") {
          setLifecycle("error");
          setStatusReason(`${event.layer}: ${event.message}`);
        }
        if (event.kind === "status") {
          setLifecycle(event.status);
          setStatusReason(event.reason);
          if (event.status === "done" || event.status === "blocked" || event.status === "error") {
            setFrozenMs((prior) => prior ?? (startedAtRef.current ? Date.now() - startedAtRef.current : 0));
          }
        }
        if (
          event.kind === "cursor_move" ||
          event.kind === "cursor_click" ||
          event.kind === "cursor_scroll" ||
          event.kind === "focus_pulse" ||
          event.kind === "keystroke"
        ) {
          const cursorEvent = event as CursorEvent;
          cursorSubscribers.current.forEach((handler) => handler(cursorEvent));
        }
      },
      setEventsState,
    );
    const stopFrames = subscribeFrames(
      runId,
      (nextFrame) => {
        setFrame(nextFrame);
        setFrameCount((prior) => prior + 1);
      },
      setFramesState,
    );
    return () => {
      stopEvents();
      stopFrames();
    };
  }, [runId]);

  // Stopwatch tick. Only run the interval while there's an active,
  // non-frozen run — otherwise we'd re-render the whole tree every 100ms
  // for nothing.
  useEffect(() => {
    if (startedAt === null || frozenMs !== null) return;
    const id = window.setInterval(() => setNowMs(Date.now()), 100);
    return () => window.clearInterval(id);
  }, [startedAt, frozenMs]);

  const onStart = async (goal: string, url: string) => {
    resetRunState();
    setRunId(null);
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ goal, url }),
    });
    const data: RunResponse = await response.json();
    setRunId(data.run_id);
    // Share with /dev so opening it in a new tab auto-loads this run.
    try {
      localStorage.setItem("agent.lastRunId", data.run_id);
    } catch {
      // localStorage can throw in private mode; not fatal.
    }
    setPhase("active");
  };

  const backToComposer = () => {
    resetRunState();
    setRunId(null);
    setPhase("composer");
  };

  const stopRun = async () => {
    if (runId) await fetch(`/api/runs/${runId}/stop`, { method: "POST" });
    setLifecycle("blocked");
    setStatusReason("Stopped by you.");
  };

  const terminal = TERMINAL.includes(lifecycle);
  const captureW = frame?.capture_w ?? 1280;
  const captureH = frame?.capture_h ?? 800;

  return (
    <main
      style={{
        width: "min(var(--size-content), calc(100vw - var(--space-8)))",
        margin: "var(--space-8) auto",
        padding: "0 var(--space-4)",
        display: "grid",
        gap: "var(--space-6)",
      }}
    >
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <h1 className="fj-title" style={{ margin: 0 }}>Agent</h1>
        <a href="/dev" className="fj-small" style={{ color: "var(--ink-muted)" }}>Dev</a>
      </header>

      {phase === "composer" && (
        <div style={{ maxWidth: 640 }}>
          <Composer onStart={onStart} />
        </div>
      )}

      {phase === "active" && (
        <section style={{ display: "grid", gap: "var(--space-4)" }}>
          <div className="fj-browser">
            <div className="fj-browser-toolbar">
              <LiveSignal tone={LIVE_TONE(lifecycle)} />
              <div className="fj-browser-url" title={currentUrl}>
                {currentUrl || "—"}
              </div>
              {!terminal && (
                <button type="button" className="fj-btn fj-btn-sm fj-btn-danger" onClick={stopRun}>
                  Stop run
                </button>
              )}
            </div>
            <div
              className="fj-browser-viewport"
              style={{ aspectRatio: `${captureW} / ${captureH}` }}
            >
              <ScreencastFrame frame={frame} />
              <CursorLayer
                captureW={captureW}
                captureH={captureH}
                onEvent={registerCursorHandler}
              />
            </div>
          </div>

          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "var(--space-4)" }}>
            <div className="fj-status" data-tone={STATUS_TONE[lifecycle]}>
              <span>{STATUS_TEXT[lifecycle]}</span>
              {statusReason && (
                <span className="fj-small" style={{ color: "var(--ink-muted)" }}>
                  · {statusReason}
                </span>
              )}
            </div>
            <div className="fj-small fj-num" style={{ color: "var(--ink-muted)", display: "flex", gap: "var(--space-3)" }}>
              <span title="Elapsed run time">{formatElapsed(frozenMs ?? (startedAt ? nowMs - startedAt : null))}</span>
              <span>·</span>
              <span>${cost.toFixed(4)}</span>
            </div>
          </div>

          {plan && <PlanView plan={plan.plan} activeIndex={plan.active_index} />}

          {captcha && !terminal && (
            <CaptchaBanner
              runId={runId ?? ""}
              reason={captcha.reason}
              resumeToken={captcha.resume_token}
              onResumed={() => setCaptcha(null)}
            />
          )}

          {terminal && (
            <div>
              <button type="button" className="fj-btn fj-btn-primary" onClick={backToComposer}>
                Start another task
              </button>
            </div>
          )}

          <details className="fj-panel fj-panel-pad" style={{ padding: "var(--space-3) var(--space-4)" }}>
            <summary className="fj-small" style={{ cursor: "pointer", color: "var(--ink-muted)" }}>
              Details
            </summary>
            <div style={{ marginTop: "var(--space-3)", display: "grid", gap: "var(--space-2)" }} className="fj-small">
              <Row label="Run" value={runId ?? "—"} />
              <Row label="Events" value={`${eventsState} · ${eventCount}`} />
              <Row label="Frames" value={`${framesState} · ${frameCount}`} />
              <Row label="Last event" value={lastKind || "—"} />
              {plan?.original_goal && <Row label="Original goal" value={plan.original_goal} />}
              {plan?.refined_goal && <Row label="Refined goal" value={plan.refined_goal} />}
              {plan?.start_url && <Row label="Start URL" value={plan.start_url} />}
              {plan?.plan?.length ? (
                <div>
                  <div className="fj-label" style={{ marginTop: "var(--space-2)" }}>Subgoals</div>
                  <ol style={{ margin: "var(--space-2) 0 0", paddingLeft: "var(--space-5)" }}>
                    {plan.plan.map((s, i) => (
                      <li key={i} style={{ margin: "var(--space-1) 0" }}>{s}</li>
                    ))}
                  </ol>
                </div>
              ) : null}
            </div>
          </details>
        </section>
      )}
    </main>
  );
}

function LiveSignal({ tone }: { tone: "live" | "paused" | "off" }) {
  const label = tone === "live" ? "Live" : tone === "paused" ? "Paused" : "Disconnected";
  return (
    <span className="fj-small" style={{ display: "inline-flex", alignItems: "center", gap: "var(--space-2)", color: "var(--ink-muted)" }}>
      <span className="fj-dot" data-tone={tone} />
      {label}
    </span>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "120px 1fr", gap: "var(--space-3)" }}>
      <span className="fj-label">{label}</span>
      <span style={{ wordBreak: "break-all" }}>{value}</span>
    </div>
  );
}
