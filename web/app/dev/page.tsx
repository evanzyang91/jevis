"use client";

// Developer route. Everything the main route has (task composer, screencast
// with cursor overlay, status, stop / start-another), plus the dev panels:
// filterable event log, decision inspector, timeline, run-id attach box.
//
// Auto-loads the last run id started here or on the main page (localStorage).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { CaptchaBanner } from "@/components/CaptchaBanner";
import { Composer } from "@/components/Composer";
import { CursorLayer } from "@/components/CursorLayer";
import { DecisionInspector } from "@/components/DecisionInspector";
import { EventLog } from "@/components/EventLog";
import { ScreencastFrame } from "@/components/ScreencastFrame";
import { Timeline } from "@/components/Timeline";
import type {
  CaptchaEvent,
  CursorClickEvent,
  CursorMoveEvent,
  CursorScrollEvent,
  DecisionEvent,
  FocusPulseEvent,
  FrameEvent,
  KeystrokeEvent,
  ObservationEvent,
  OutcomeEvent,
  PlanEvent,
  StreamEvent,
} from "@/lib/events";
import { type SocketState, subscribeEvents, subscribeFrames } from "@/lib/ws";

type CursorEvent =
  | CursorMoveEvent
  | CursorClickEvent
  | CursorScrollEvent
  | FocusPulseEvent
  | KeystrokeEvent;

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

const formatElapsed = (ms: number | null): string => {
  if (ms === null || ms < 0) return "—";
  const total = Math.floor(ms / 100) / 10;
  if (total < 60) return `${total.toFixed(1)}s`;
  const mins = Math.floor(total / 60);
  const secs = Math.floor(total - mins * 60);
  return `${mins}:${secs.toString().padStart(2, "0")}`;
};

export default function Dev() {
  const [runId, setRunId] = useState<string>("");
  const [runInput, setRunInput] = useState<string>("");
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [selected, setSelected] = useState<StreamEvent | null>(null);
  const [frame, setFrame] = useState<FrameEvent | null>(null);
  const [eventsState, setEventsState] = useState<SocketState>("closed");
  const [framesState, setFramesState] = useState<SocketState>("closed");
  const [lifecycle, setLifecycle] = useState<Lifecycle>("starting");
  const [statusReason, setStatusReason] = useState<string>("");
  const [cost, setCost] = useState<number>(0);
  const [captcha, setCaptcha] = useState<CaptchaEvent | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const startedAtRef = useRef<number | null>(null);
  const [frozenMs, setFrozenMs] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState<number>(() => Date.now());

  const cursorSubscribers = useRef<Set<(event: CursorEvent) => void>>(new Set());
  const registerCursorHandler = useCallback((handler: (event: CursorEvent) => void) => {
    cursorSubscribers.current.add(handler);
    return () => {
      cursorSubscribers.current.delete(handler);
    };
  }, []);

  useEffect(() => {
    try {
      const stored = localStorage.getItem("agent.lastRunId");
      if (stored) {
        setRunId(stored);
        setRunInput(stored);
      }
    } catch {
      // ignore
    }
  }, []);

  const resetRunState = () => {
    setEvents([]);
    setSelected(null);
    setFrame(null);
    setLifecycle("starting");
    setStatusReason("");
    setCost(0);
    setCaptcha(null);
    setStartedAt(null);
    startedAtRef.current = null;
    setFrozenMs(null);
  };

  useEffect(() => {
    if (!runId) return;
    resetRunState();
    const stopEvents = subscribeEvents(
      runId,
      (event) => {
        setEvents((prior) => [...prior, event]);
        if (startedAtRef.current == null) {
          startedAtRef.current = Date.now();
          setStartedAt(startedAtRef.current);
        }
        if (event.kind === "captcha") setCaptcha(event);
        if (event.kind === "budget") setCost(event.usd);
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
    const stopFrames = subscribeFrames(runId, (nextFrame) => setFrame(nextFrame), setFramesState);
    return () => {
      stopEvents();
      stopFrames();
    };
  }, [runId]);

  useEffect(() => {
    if (startedAt === null || frozenMs !== null) return;
    const id = window.setInterval(() => setNowMs(Date.now()), 100);
    return () => window.clearInterval(id);
  }, [startedAt, frozenMs]);

  const observations = useMemo(
    () => events.filter((event): event is ObservationEvent => event.kind === "observation"),
    [events],
  );
  const decisions = useMemo(
    () => events.filter((event): event is DecisionEvent => event.kind === "decision"),
    [events],
  );
  const outcomes = useMemo(
    () => events.filter((event): event is OutcomeEvent => event.kind === "outcome"),
    [events],
  );
  const plan = useMemo(
    () => events.slice().reverse().find((event): event is PlanEvent => event.kind === "plan") ?? null,
    [events],
  );

  const observation = selected?.kind === "observation" ? selected : observations.at(-1) ?? null;
  const decision = selected?.kind === "decision" ? selected : decisions.at(-1) ?? null;
  const captureW = frame?.capture_w ?? 1280;
  const captureH = frame?.capture_h ?? 800;
  const terminal = TERMINAL.includes(lifecycle);

  const onStart = async (goal: string, url: string) => {
    resetRunState();
    setRunId("");
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ goal, url }),
    });
    const data: RunResponse = await response.json();
    setRunId(data.run_id);
    setRunInput(data.run_id);
    try {
      localStorage.setItem("agent.lastRunId", data.run_id);
    } catch {
      // ignore
    }
  };

  const stopRun = async () => {
    if (runId) await fetch(`/api/runs/${runId}/stop`, { method: "POST" });
    setLifecycle("blocked");
    setStatusReason("Stopped by you.");
  };

  return (
    <main
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(360px, 1.1fr) minmax(0, 2fr) minmax(300px, 1fr)",
        gridTemplateRows: "auto auto 1fr auto",
        gap: "var(--space-3)",
        height: "100vh",
        padding: "var(--space-3)",
        boxSizing: "border-box",
      }}
    >
      <header
        style={{
          gridColumn: "1 / span 3",
          display: "flex",
          gap: "var(--space-3)",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <div style={{ display: "flex", alignItems: "baseline", gap: "var(--space-4)" }}>
          <h1 className="fj-title" style={{ margin: 0 }}>Dev</h1>
          <span className="fj-small" style={{ color: "var(--ink-muted)" }}>
            events {eventsState} · frames {framesState} · ${cost.toFixed(4)}
          </span>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const id = runInput.trim();
            if (id) setRunId(id);
          }}
          style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", flex: 1, maxWidth: 520 }}
        >
          <input
            className="fj-input fj-num"
            value={runInput}
            onChange={(event) => setRunInput(event.target.value)}
            placeholder="attach to run id"
            style={{ flex: 1 }}
          />
          <button type="submit" className="fj-btn fj-btn-secondary fj-btn-sm">
            Attach
          </button>
        </form>
        <a href="/" className="fj-small" style={{ color: "var(--ink-muted)" }}>← Home</a>
      </header>

      <section style={{ gridColumn: "1 / span 3" }}>
        <Composer onStart={onStart} disabled={!terminal && !!runId && lifecycle === "running"} />
      </section>

      <section
        className="fj-panel"
        style={{ padding: "var(--space-3)", overflow: "hidden", minHeight: 0 }}
      >
        <EventLog events={events} selectedSeq={selected?.seq ?? null} onSelect={setSelected} />
      </section>

      <section style={{ minHeight: 0, display: "grid", gap: "var(--space-3)", gridTemplateRows: "1fr auto" }}>
        <div className="fj-browser">
          <div className="fj-browser-toolbar">
            <span
              className="fj-small"
              style={{ display: "inline-flex", alignItems: "center", gap: "var(--space-2)", color: "var(--ink-muted)" }}
            >
              <span
                className="fj-dot"
                data-tone={lifecycle === "running" ? "live" : lifecycle === "paused" ? "paused" : "off"}
              />
              {STATUS_TEXT[lifecycle]}
            </span>
            <div className="fj-browser-url" title={observation?.url}>
              {observation?.url || "—"}
            </div>
            {!terminal && runId && (
              <button type="button" className="fj-btn fj-btn-sm fj-btn-danger" onClick={stopRun}>
                Stop run
              </button>
            )}
          </div>
          <div className="fj-browser-viewport" style={{ position: "relative", display: "flex" }}>
            <div
              style={{
                margin: "auto",
                width: "100%",
                aspectRatio: `${captureW} / ${captureH}`,
                position: "relative",
              }}
            >
              <ScreencastFrame frame={frame} />
              <CursorLayer captureW={captureW} captureH={captureH} onEvent={registerCursorHandler} />
            </div>
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "var(--space-3)" }}>
          <div className="fj-status" data-tone={STATUS_TONE[lifecycle]}>
            <span>{STATUS_TEXT[lifecycle]}</span>
            {statusReason && (
              <span className="fj-small" style={{ color: "var(--ink-muted)" }}>· {statusReason}</span>
            )}
          </div>
          <span className="fj-small fj-num" style={{ color: "var(--ink-muted)", display: "flex", gap: "var(--space-3)" }}>
            <span title="Elapsed run time">{formatElapsed(frozenMs ?? (startedAt ? nowMs - startedAt : null))}</span>
            <span>·</span>
            <span>{runId ? `run ${runId.slice(0, 8)}…` : "no run attached"}</span>
          </span>
        </div>

        {captcha && !terminal && (
          <CaptchaBanner
            runId={runId}
            reason={captcha.reason}
            resumeToken={captcha.resume_token}
            onResumed={() => setCaptcha(null)}
          />
        )}
      </section>

      <section
        className="fj-panel"
        style={{ padding: "var(--space-4)", overflowY: "auto", minHeight: 0 }}
      >
        <DecisionInspector observation={observation ?? null} decision={decision ?? null} />
        {plan && (
          <div style={{ marginTop: "var(--space-5)" }}>
            <h3 className="fj-heading" style={{ margin: "0 0 var(--space-3)" }}>Plan</h3>
            {plan.refined_goal && (
              <div className="fj-small" style={{ color: "var(--ink-muted)", marginBottom: "var(--space-3)" }}>
                {plan.refined_goal}
              </div>
            )}
            <ol style={{ paddingLeft: "var(--space-5)", margin: 0 }}>
              {plan.plan.map((item, i) => (
                <li key={i} className="fj-small" style={{ marginBottom: "var(--space-1)" }}>{item}</li>
              ))}
            </ol>
          </div>
        )}
      </section>

      <footer className="fj-panel" style={{ gridColumn: "1 / span 3", padding: "var(--space-3)" }}>
        <Timeline
          steps={outcomes.map((outcome) => ({ seq: outcome.seq, outcome }))}
          activeSeq={selected?.seq ?? null}
          onSelect={(seq) => {
            const event = events.find((e) => e.seq === seq);
            if (event) setSelected(event);
          }}
        />
      </footer>
    </main>
  );
}
