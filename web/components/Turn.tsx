"use client";

// One request and the agent's answer: where it worked, each step in plain
// words, the verdict, and a results card once the run ends.

import type { FrameEvent } from "@/lib/events";
import { type HumanCheck, type Run, hostOf, isTerminal, money, plainly, stepText, waitingCheck } from "@/lib/run";

import type { CursorRegistry } from "./cursor";
import { LiveBrowser } from "./LiveBrowser";
import { Logo } from "./Logo";

type Props = {
  run: Run;
  // Only the newest turn shows the live browser; older turns keep their words only.
  frame: FrameEvent | null;
  showBrowser: boolean;
  cursor: CursorRegistry;
  onResumeCaptcha: () => void;
};

export function Turn({ run, frame, showBrowser, cursor, onResumeCaptcha }: Props) {
  const running = !isTerminal(run);
  const url = run.observation?.url ?? run.plan?.start_url ?? "";
  // The person's turn: a site's human check is up in the Chrome window.
  const check = run.status === "paused" && run.captcha ? waitingCheck(run) : null;
  return (
    <>
      <div className="turn user">
        <div className="bubble">{run.request}</div>
      </div>
      <div className="turn agent">
        <div className={`avatar ${running ? "active" : ""}`}>
          <Logo size={26} />
        </div>
        <div className="answer">
          {url && (
            <div className="site">
              Working on <code>{hostOf(url)}</code>
            </div>
          )}
          {/* The browser sits ABOVE the thinking trace. New steps grow below
              it and never push it around — layout stays stable, and the
              sticky offset keeps it visible while the step list scrolls. */}
          {showBrowser && (
            <div className={`turn-browser ${running ? "running" : ""} ${check ? "handoff" : ""}`}>
              <LiveBrowser frame={frame} url={url} live={running} cursor={cursor} />
              {/* Over the live view, which is sticky and so always on screen,
                  and which looks like a browser but takes no clicks. */}
              {check && <CheckNotice check={check} onContinue={onResumeCaptcha} overlay />}
            </div>
          )}
          {running && (
            <p className="now">
              {run.status === "starting"
                ? "Planning and opening the browser"
                : run.status === "paused"
                  ? "Waiting for you to finish the check in Chrome"
                  : plainly(run.decision, run.observation)}
            </p>
          )}
          {check && !showBrowser && <CheckNotice check={check} onContinue={onResumeCaptcha} />}
          {run.steps.length > 0 && (
            // Open while the agent works; folded to one line once it is done.
            <details className="steps" open={running ? true : undefined}>
              <summary>
                {running ? "Show steps" : "Steps"} <span>{run.steps.length}</span>
              </summary>
              <ol className="acts">
                {run.steps.map((step) => (
                  <li key={step.action.seq} className={step.outcome?.page_changed === false ? "failed" : ""}>
                    {stepText(step)}
                  </li>
                ))}
              </ol>
            </details>
          )}
          {run.checks.map((c) =>
            c.outcome === "waiting" && c.waitS > 0 ? null : <CheckLine key={c.startedAt} check={c} />,
          )}
          {!running && (
            <p className={`verdict ${run.status === "done" ? "ok" : "bad"}`}>
              {run.status === "done"
                ? "Done. The browser window shows the result."
                : run.status === "error"
                  ? `Something went wrong: ${run.reason || "the run failed."}`
                  : "I could not finish this one. The browser window shows where I stopped."}
            </p>
          )}
          {!running && <Results run={run} />}
        </div>
      </div>
    </>
  );
}

// "4:05": minutes and seconds left.
const clock = (ms: number) => {
  const total = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
};

// "14 s", "2 min 5 s".
const lasted = (ms: number) => {
  const total = Math.max(1, Math.round(ms / 1000));
  return total < 60 ? `${total} s` : `${Math.floor(total / 60)} min${total % 60 ? ` ${total % 60} s` : ""}`;
};

// The person's turn. The check lives in the Chrome window, not in this page:
// the live view only shows it. The run notices by itself when the check is
// done; Continue is for when it does not.
function CheckNotice({ check, onContinue, overlay }: { check: HumanCheck; onContinue: () => void; overlay?: boolean }) {
  const host = hostOf(check.url) || "The site";
  const left = check.startedAt + check.waitS * 1000 - Date.now();
  return (
    <div className={`notice check-notice ${overlay ? "overlay" : ""}`} role="alert">
      <strong>Your turn: {host} wants to check that you are human.</strong>
      <p>Complete the check in the Chrome window. The run continues automatically once it is done.</p>
      <div className="check-actions">
        <button
          type="button"
          className="ghost"
          onClick={onContinue}
          title="Use this if you finished the check and the run did not carry on by itself."
        >
          Continue now
        </button>
        <span className="check-meta">
          {overlay ? "This view only shows the tab; it does not take clicks. " : ""}
          {left > 0 ? `Stops in ${clock(left)} if the check is not done.` : "Stopping…"}
        </span>
      </div>
    </div>
  );
}

// A check that is over, in one line, kept in the turn's record.
function CheckLine({ check }: { check: HumanCheck }) {
  const host = hostOf(check.url) || "the site";
  const took = lasted((check.endedAt ?? Date.now()) - check.startedAt);
  let text: string;
  let tone = "";
  if (check.outcome === "completed") {
    text = `You finished ${host}'s human check. Continued after ${took}.`;
    tone = "ok";
  } else if (check.outcome === "continued") {
    text = `You pressed Continue at ${host}'s human check after ${took}.`;
  } else if (check.waitS === 0) {
    text = `${host} asked for a human check, and this browser has no window to do it in.`;
    tone = "bad";
  } else if (check.note === "stopped by user") {
    text = `Stopped by you during ${host}'s human check.`;
  } else {
    text = check.note || `The run ended during ${host}'s human check.`;
    tone = "bad";
  }
  return <p className={`check-line ${tone}`}>{text}</p>;
}

function Results({ run }: { run: Run }) {
  const budget = run.budget;
  if (!budget && run.steps.length === 0) return null;
  const elapsed = (run.endedAt ?? Date.now()) - run.startedAt;
  return (
    <div className="cost">
      <div className="result-head">
        <div className="result-figure">{budget ? money(budget.usd) : "–"}</div>
        <div className="result-caption">total for this task</div>
      </div>
      <div className="tiles">
        <Tile label="Steps" value={String(run.steps.length)} />
        <Tile label="Time" value={`${(elapsed / 1000).toFixed(1)}s`} />
        <Tile label="Model" value={`${((budget?.model_ms ?? run.modelMs) / 1000).toFixed(1)}s`} note="thinking" />
        <Tile label="Pages" value={`${((budget?.load_ms ?? 0) / 1000).toFixed(1)}s`} note="loading" />
      </div>
    </div>
  );
}

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="tile">
      <span className="tile-label">{label}</span>
      <strong>{value}</strong>
      {note && <small>{note}</small>}
    </div>
  );
}
