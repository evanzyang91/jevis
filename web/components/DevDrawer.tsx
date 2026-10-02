"use client";

// Developer view: what the agent saw, how sure it was, and what each action did.

import type { DecisionEvent, FrameEvent } from "@/lib/events";
import { type Run, fromMemory, isTerminal, percent, plainly, seconds, stepText, targetIndex } from "@/lib/run";

import type { CursorRegistry } from "./cursor";
import { LiveBrowser } from "./LiveBrowser";

type Props = {
  run: Run | null;
  frame: FrameEvent | null;
  cursor: CursorRegistry;
  models: string[];
  model: string;
  onModel: (model: string) => void;
  busy: boolean;
};

export function DevDrawer({ run, frame, cursor, models, model, onModel, busy }: Props) {
  const decision = run ? run.decision ?? (isTerminal(run) ? run.lastDecision : null) : null;
  return (
    <div className="dev-drawer">
      <div className="dev-bar">
        {run?.plan?.refined_goal && run.plan.refined_goal !== run.plan.original_goal && (
          <span className="category" title={run.plan.refined_goal}>
            refined goal
          </span>
        )}
        <label className="control" htmlFor="text-model">
          Agent model
        </label>
        <select id="text-model" value={model} onChange={(e) => onModel(e.target.value)} disabled={busy}>
          <option value="">server default</option>
          {models.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </div>

      <div className="workspace">
        <LiveBrowser
          frame={frame}
          url={run?.observation?.url ?? ""}
          title={run?.observation?.title}
          live={!!run && !isTerminal(run)}
          cursor={cursor}
          footer
        />
        <Inspector run={run} decision={decision} />
      </div>

      <Trail run={run} />

      <details>
        <summary>What the model sees</summary>
        <pre className="model-state">
          {run
            ? JSON.stringify(
                {
                  goal: run.plan?.refined_goal ?? run.request,
                  page: run.observation && {
                    url: run.observation.url,
                    title: run.observation.title,
                    text: run.observation.text_preview,
                    scroll_area: run.observation.scroll_area,
                  },
                  elements: run.observation?.elements,
                  decision,
                },
                null,
                2,
              )
            : "Start a task to inspect its structured state."}
        </pre>
      </details>
    </div>
  );
}

function Inspector({ run, decision }: { run: Run | null; decision: DecisionEvent | null }) {
  const elements = run?.observation?.elements ?? [];
  const byIndex = new Map<number, number>();
  if (decision?.target) {
    for (const [id, p] of Object.entries(decision.probabilities)) {
      const index = targetIndex(id);
      if (index !== null) byIndex.set(index, Math.max(byIndex.get(index) ?? 0, p));
    }
  }
  const chosen = targetIndex(decision?.target ?? null);
  const ranked = [...elements].sort((a, b) => (byIndex.get(b[0]) ?? -1) - (byIndex.get(a[0]) ?? -1));
  const plan = run?.plan;
  const active = plan?.active_index ?? 0;
  return (
    <aside>
      <div className="aside-header">
        <div>
          <p className="eyebrow">NEXT ACTION</p>
          <h2>{run ? plainly(decision, run.observation) : "Waiting for a page"}</h2>
        </div>
        <span>{elements.length} elements</span>
      </div>
      {plan && (
        <div className="plan">
          {plan.plan.map((goal, i) => {
            // Per-step status from the ledger when present; older events only carry the index.
            const step = plan.steps?.[i];
            const status = step?.status ?? (i < active ? "done" : i === active ? "active" : "pending");
            return (
              <div key={i} className={`plan-step ${status === "active" ? "current" : ""}`}
                   title={step?.satisfied_by || undefined}>
                <span>{status === "done" ? "✓" : status === "skipped" ? "–" : i + 1}</span>
                {goal}
              </div>
            );
          })}
        </div>
      )}
      <div className="metrics">
        <div>
          <span>Decision</span>
          <strong>{decision ? `${decision.latency_ms} ms` : "–"}</strong>
        </div>
        <div>
          <span>Confidence</span>
          <strong>{decision ? percent(decision.confidence) : "–"}</strong>
        </div>
        <div>
          <span>Operation</span>
          <strong>{decision ? decision.operation : "–"}</strong>
        </div>
      </div>
      {decision && (
        <div className="operation-choices">
          <span className="operation-choice best">
            {decision.operation} <b>{percent(decision.confidence)}</b>
          </span>
          {decision.signal !== undefined && decision.signal < 1 && (
            <span className="operation-choice">
              signal <b>{percent(decision.signal)}</b>
            </span>
          )}
          {decision.check && (
            <span className={`operation-choice ${decision.check === "cleared" ? "best" : "warn"}`}>
              check {decision.check} <b>{percent(decision.check_p ?? 0)}</b>
            </span>
          )}
          {decision.switched && <span className="operation-choice">switched</span>}
          {decision.dialog && (
            <span className="operation-choice">
              dialog: {decision.dialog} <b>{percent(decision.dialog_p ?? 0)}</b>
            </span>
          )}
          {decision.escalate && <span className="operation-choice warn">escalated</span>}
        </div>
      )}
      {decision?.guidance && <p className="guidance">Hint: {decision.guidance}</p>}
      <p className="distribution-label">
        Indexed elements <span>{decision?.target ? "Ranked" : "Unranked"}</span>
      </p>
      <div className="choices">
        {ranked.length === 0 && <p className="muted">Available actions will appear here.</p>}
        {ranked.map(([index, role, label]) => {
          const p = byIndex.get(index);
          return (
            <div key={index} className={`choice ${index === chosen ? "best" : ""}`}>
              <span className="choice-id">[{index}]</span>
              <div className="choice-label">
                {label}
                <small>{role}</small>
                {p !== undefined && <div className="bar" style={{ ["--probability" as string]: `${p * 100}%` }} />}
              </div>
              <span className="probability">{p !== undefined ? percent(p) : "–"}</span>
            </div>
          );
        })}
      </div>
    </aside>
  );
}

function Trail({ run }: { run: Run | null }) {
  const steps = run?.steps ?? [];
  const elapsed = run ? (run.endedAt ?? Date.now()) - run.startedAt : 0;
  const load = steps.reduce((sum, s) => sum + (s.outcome?.load_ms ?? 0), 0);
  const exportTrace = () => {
    if (!run) return;
    const blob = new Blob([JSON.stringify({ run_id: run.id, request: run.request, events: run.events }, null, 2)], {
      type: "application/json",
    });
    const href = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = href;
    a.download = `agent-trace-${run.id.slice(0, 8)}.json`;
    a.click();
    URL.revokeObjectURL(href);
  };
  return (
    <div className="trace">
      <div className="trace-heading">
        <h2>
          Decision trail{" "}
          <span>
            {steps.length} actions · {seconds(elapsed)} total · {seconds(run?.budget?.model_ms ?? run?.modelMs ?? 0)}{" "}
            model · {seconds(run?.budget?.load_ms ?? load)} load
          </span>
        </h2>
        <button type="button" onClick={exportTrace} disabled={!run?.events.length}>
          Export trace ↓
        </button>
      </div>
      {steps.length === 0 ? (
        <p className="muted">Each executed action leaves an observed result.</p>
      ) : (
        <div className="trace-table" role="table">
          <div className="trace-row head" role="row">
            <span>#</span>
            <span>Action</span>
            <span className="num">Model</span>
            <span className="num">Load</span>
            <span className="num">Conf.</span>
            <span className="num">Effect</span>
          </div>
          {steps.map((step, i) => (
            <div key={step.action.seq} className="trace-row" role="row">
              <span className="number">{String(i + 1).padStart(2, "0")}</span>
              <div className="trace-action">
                {stepText(step)}
                {step.action.target_label && step.action.action_kind !== "click" && <small>{step.action.target_label}</small>}
              </div>
              <span className="num">
                {fromMemory(step) ? "memory" : step.decision ? `${step.decision.latency_ms} ms` : "–"}
              </span>
              <span className="num">{step.outcome ? `${step.outcome.load_ms} ms` : "–"}</span>
              <span className="num">{step.decision ? percent(step.decision.confidence) : "–"}</span>
              <span className={`num effect ${step.outcome && !step.outcome.page_changed ? "none" : ""}`}>
                {step.outcome ? (step.outcome.page_changed ? "Page changed" : "No change") : "…"}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
