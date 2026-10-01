"use client";

// One request and the agent's answer: where it worked, each step in plain
// words, the verdict, and a results card once the run ends.

import type { FrameEvent } from "@/lib/events";
import { type Run, hostOf, isTerminal, money, plainly, stepText } from "@/lib/run";

import type { CursorRegistry } from "./cursor";
import { LiveBrowser } from "./LiveBrowser";

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
  return (
    <>
      <div className="turn user">
        <div className="bubble">{run.request}</div>
      </div>
      <div className="turn agent">
        {url && (
          <div className="site">
            Working on <code>{hostOf(url)}</code>
          </div>
        )}
        <ol className="acts">
          {run.steps.map((step) => (
            <li key={step.action.seq} className={step.outcome?.page_changed === false ? "failed" : ""}>
              {stepText(step)}
            </li>
          ))}
          {running && (
            <li className="current">
              {run.status === "starting"
                ? "Planning and opening the browser"
                : run.status === "paused"
                  ? "Waiting for you"
                  : plainly(run.decision, run.observation)}
            </li>
          )}
        </ol>
        {run.captcha && run.status === "paused" && (
          <div className="notice" role="alert">
            <strong>The site asked to check that you are human.</strong> {run.captcha.reason} Solve it in the browser
            window, then continue.
            <div>
              <button type="button" className="ghost" onClick={onResumeCaptcha}>
                Continue
              </button>
            </div>
          </div>
        )}
        {showBrowser && (
          <div className="turn-browser">
            <LiveBrowser frame={frame} url={url} live={running} cursor={cursor} />
          </div>
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
    </>
  );
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
