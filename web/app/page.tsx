"use client";

// End-user route. A conversation, not a dashboard: one question and a box
// before the first request; afterwards each request is a turn that narrates
// what the agent did, with the live browser beside it. The developer view
// opens underneath with the decision inspector and the decision trail.

import { useCallback, useEffect, useRef, useState } from "react";

import { DevDrawer } from "@/components/home/DevDrawer";
import { Turn } from "@/components/home/Turn";
import { type CursorEvent, isCursorEvent } from "@/components/home/cursor";
import type { FrameEvent } from "@/lib/events";
import { type Run, isTerminal, newRun, reduce, seconds } from "@/lib/run";
import { useVoice } from "@/lib/voice";
import { subscribeEvents, subscribeFrames } from "@/lib/ws";

import "./home.css";

const EXAMPLES = [
  "Add the ingredients for a chocolate cake to my cart",
  "Find a highly rated wireless mouse under $50",
  "Open the Wikipedia article on Gödel's incompleteness theorems",
];

const STATUS_LINE: Record<Run["status"], string> = {
  starting: "Planning the task and opening the browser…",
  running: "Running…",
  paused: "Paused · needs you",
  done: "Finished. Check the result.",
  blocked: "Stopped. It could not find a way forward.",
  error: "Paused · needs attention",
};

const THEME_KEY = "agent-theme";
const DEV_KEY = "agent-dev-view";

const remember = (key: string, value: string) => {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Private mode or blocked storage: the setting lasts for this page only.
  }
};
const recall = (key: string) => {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
};

export default function Home() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [frame, setFrame] = useState<FrameEvent | null>(null);
  const [goal, setGoal] = useState("");
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [dev, setDev] = useState(false);
  const [theme, setTheme] = useState<"" | "light" | "dark">("");
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [, tick] = useState(0);
  const goalBox = useRef<HTMLTextAreaElement | null>(null);
  const thread = useRef<HTMLDivElement | null>(null);
  const cursorHandlers = useRef<Set<(event: CursorEvent) => void>>(new Set());

  const current = runs.length ? runs[runs.length - 1] : null;
  const running = !!current && !isTerminal(current);

  // Settings remembered across visits, read after mount so the server render matches.
  useEffect(() => {
    setDev(recall(DEV_KEY) === "1");
    const saved = recall(THEME_KEY);
    setTheme(saved === "light" || saved === "dark" ? saved : "");
    fetch("/api/models")
      .then((r) => r.json())
      .then((data: { models: { id: string; modalities: string[] }[] }) => {
        // Writers only: Jev is the policy, not a text model. An empty choice
        // sends no model, so the server's TEXT_MODEL applies.
        setModels(data.models.filter((m) => m.modalities.includes("text") && !m.id.startsWith("jev")).map((m) => m.id));
      })
      .catch(() => setError("Cannot reach the agent server. Start it with `uv run agent`."));
  }, []);

  const cursor = useCallback((handler: (event: CursorEvent) => void) => {
    cursorHandlers.current.add(handler);
    return () => {
      cursorHandlers.current.delete(handler);
    };
  }, []);

  // The newest run streams; older turns are already final.
  const liveId = current?.id ?? null;
  useEffect(() => {
    if (!liveId) return;
    const stopEvents = subscribeEvents(liveId, (event) => {
      if (isCursorEvent(event)) cursorHandlers.current.forEach((handler) => handler(event));
      setRuns((prior) => prior.map((run) => (run.id === liveId ? reduce(run, event) : run)));
    });
    const stopFrames = subscribeFrames(liveId, setFrame);
    return () => {
      stopEvents();
      stopFrames();
    };
  }, [liveId]);

  // The header clock moves while a run is going.
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => tick((n) => n + 1), 100);
    return () => window.clearInterval(id);
  }, [running]);

  useEffect(() => {
    const box = thread.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [runs]);

  const grow = () => {
    const box = goalBox.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight, 200)}px`;
  };
  useEffect(grow, [goal]);

  const voice = useVoice(() => goal, setGoal);

  const start = async () => {
    const request = goal.trim();
    if (!request || busy || running) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/runs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ goal: request, url: url.trim(), ...(model ? { text_model: model } : {}) }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || data.error || "The agent server refused the task.");
      setFrame(null);
      setRuns((prior) => [...prior, newRun(data.run_id, request)]);
      setGoal("");
      remember("agent.lastRunId", data.run_id); // the full inspector at /dev opens this run
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    if (!current) return;
    await fetch(`/api/runs/${current.id}/stop`, { method: "POST" });
  };

  const resumeCaptcha = async () => {
    if (!current?.captcha) return;
    await fetch(`/api/runs/${current.id}/resume-captcha`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ resume_token: current.captcha.resume_token }),
    });
  };

  const toggleDev = (on: boolean) => {
    setDev(on);
    remember(DEV_KEY, on ? "1" : "");
  };

  // Follow the system unless the reader says otherwise, and remember that choice.
  const toggleTheme = () => {
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const now = theme || (dark ? "dark" : "light");
    const next = now === "dark" ? "light" : "dark";
    setTheme(next);
    remember(THEME_KEY, next);
  };

  const elapsed = current ? (current.endedAt ?? Date.now()) - current.startedAt : 0;
  const modelMs = current ? current.budget?.model_ms ?? current.modelMs : 0;

  return (
    <div
      className={`wa ${dev ? "dev" : ""} ${runs.length ? "" : "idle"} ${running ? "running" : ""}`}
      data-theme={theme || undefined}
    >
      <header>
        <div className="brand-space" />
        <div className="header-right">
          {current && <span className="elapsed">{seconds(elapsed)}</span>}
          {current && dev && <span className="elapsed model">model {seconds(modelMs)}</span>}
          <button type="button" className="icon small" aria-label="Switch theme" title="Switch theme" onClick={toggleTheme}>
            <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
              <circle cx="12" cy="12" r="4.2" />
              <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4" />
            </svg>
          </button>
          <label className="mode">
            <input type="checkbox" checked={dev} onChange={(e) => toggleDev(e.target.checked)} /> Developer
          </label>
        </div>
      </header>

      <main>
        <div className="thread" ref={thread}>
          {runs.length === 0 ? (
            <div className="intro">
              <h1>What should I do on the web?</h1>
              <p className="sub">Describe a task in your own words. I&apos;ll open a browser and do it while you watch.</p>
              <div className="examples">
                {EXAMPLES.map((example) => (
                  <button
                    key={example}
                    type="button"
                    className="example"
                    onClick={() => {
                      setGoal(example);
                      goalBox.current?.focus();
                    }}
                  >
                    {example}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            runs.map((run) => (
              <Turn
                key={run.id}
                run={run}
                frame={run.id === liveId ? frame : null}
                showBrowser={run.id === liveId && !dev}
                cursor={cursor}
                onResumeCaptcha={resumeCaptcha}
              />
            ))
          )}
        </div>

        <div className="composer-wrap">
          {error && (
            <div className="error-box" role="alert">
              {error}
            </div>
          )}
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              start();
            }}
          >
            <textarea
              ref={goalBox}
              className="goal"
              rows={1}
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              onKeyDown={(event) => {
                // Enter sends, Shift+Enter makes a new line — the convention everywhere else.
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  start();
                }
              }}
              placeholder="Ask anything, or press the microphone to speak…"
              aria-label="Task"
            />
            <div className="composer-tools">
              <input
                className="url-input"
                type="text"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="Start on a site (optional)"
                aria-label="Starting site, optional"
                spellCheck={false}
              />
              <span className="tool-spacer" />
              <button
                type="button"
                className={`mic ${voice.supported ? "" : "off"} ${voice.listening ? "listening" : ""}`}
                aria-label="Speak your request"
                title={voice.supported ? "Speak your request" : "This browser has no speech recognition"}
                onClick={voice.toggle}
              >
                <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true">
                  <path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Z" />
                  <path d="M19 11a7 7 0 0 1-14 0M12 18v3" />
                </svg>
                <span>{voice.listening ? "Listening" : "Speak"}</span>
              </button>
              <button type="submit" className="send" aria-label="Start" disabled={busy || running || !goal.trim()}>
                <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
                  <path d="M12 19V5M5 12l7-7 7 7" />
                </svg>
              </button>
            </div>
          </form>
          {voice.message && <p className="hint">{voice.message}</p>}
          <div className="run-controls">
            {running && (
              <button type="button" className="ghost" onClick={stop}>
                Stop
              </button>
            )}
            <span className="status-line" role="status">
              {busy ? "Sending the task…" : current ? STATUS_LINE[current.status] : ""}
            </span>
          </div>
        </div>

        {dev && (
          <DevDrawer
            run={current}
            frame={frame}
            cursor={cursor}
            models={models}
            model={model}
            onModel={setModel}
            busy={running}
          />
        )}
      </main>
    </div>
  );
}
