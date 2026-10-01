// One run's state, folded from its ordered event stream. The conversation and
// the developer view both read this; neither keeps its own copy of the events.

import type {
  ActionEvent,
  BudgetEvent,
  CaptchaEvent,
  DecisionEvent,
  ObservationEvent,
  OutcomeEvent,
  PlanEvent,
  StreamEvent,
} from "./events";

export type Lifecycle = "starting" | "running" | "paused" | "done" | "blocked" | "error";

export const TERMINAL: readonly Lifecycle[] = ["done", "blocked", "error"];

// One executed action with what led to it and what it did.
export type Step = {
  action: ActionEvent;
  decision: DecisionEvent | null;
  outcome: OutcomeEvent | null;
  typed: string | null; // a fill action's text arrives in the keystroke event after it
};

export type Run = {
  id: string;
  request: string;
  status: Lifecycle;
  reason: string;
  startedAt: number; // wall clock, ms
  endedAt: number | null;
  plan: PlanEvent | null;
  observation: ObservationEvent | null;
  decision: DecisionEvent | null; // the latest; null again once its action runs
  lastDecision: DecisionEvent | null; // the latest, kept after its action runs
  steps: Step[];
  modelMs: number; // decision latency so far; the final budget replaces it
  budget: BudgetEvent | null; // the final totals, when the run sent them
  captcha: CaptchaEvent | null;
  events: StreamEvent[]; // everything, for the trace export
};

export function newRun(id: string, request: string): Run {
  return {
    id,
    request,
    status: "starting",
    reason: "",
    startedAt: Date.now(),
    endedAt: null,
    plan: null,
    observation: null,
    decision: null,
    lastDecision: null,
    steps: [],
    modelMs: 0,
    budget: null,
    captcha: null,
    events: [],
  };
}

export function isTerminal(run: Run): boolean {
  return TERMINAL.includes(run.status);
}

export function reduce(run: Run, event: StreamEvent): Run {
  const next: Run = { ...run, events: [...run.events, event] };
  const end = () => next.endedAt ?? Date.now();
  switch (event.kind) {
    case "plan":
      next.plan = event;
      break;
    case "observation":
      next.observation = event;
      if (next.status === "starting") next.status = "running";
      break;
    case "decision":
      next.decision = event;
      next.lastDecision = event;
      next.modelMs = run.modelMs + event.latency_ms;
      break;
    case "action":
      next.steps = [...run.steps, { action: event, decision: run.decision, outcome: null, typed: event.text }];
      next.decision = null;
      break;
    case "keystroke": {
      const last = run.steps[run.steps.length - 1];
      if (last && last.action.action_kind === "fill" && last.typed === null) {
        const typed = event.secret ? "•".repeat(Math.min(8, event.text.length || 4)) : event.text;
        next.steps = [...run.steps.slice(0, -1), { ...last, typed }];
      }
      break;
    }
    case "outcome": {
      const last = run.steps[run.steps.length - 1];
      if (last && last.outcome === null) next.steps = [...run.steps.slice(0, -1), { ...last, outcome: event }];
      break;
    }
    case "budget":
      if (event.steps > 0 || event.usd > 0 || event.model_ms > 0) next.budget = event; // skip the start heartbeat
      break;
    case "captcha":
      next.captcha = event;
      break;
    case "status":
      next.status = event.status;
      next.reason = event.reason;
      if (event.status !== "paused") next.captcha = null;
      if (isTerminal(next)) next.endedAt = end();
      break;
    case "error":
      if (!event.recoverable) {
        next.status = "error";
        next.reason = event.message;
        next.endedAt = end();
      }
      break;
  }
  return next;
}

// ---- Words -----------------------------------------------------------------

// Accessibility names are written for screen readers ("Add to cart - Robin Hood
// Flour"): precise, but not for reading. Keep the part before " · ".
const short = (label: string) => (label || "").split(" · ")[0].trim();

export function stepText(step: Step): string {
  const label = short(step.action.target_label);
  switch (step.action.action_kind) {
    case "fill":
      return step.typed ? `Typed “${step.typed}”` : `Typed into ${label || "a field"}`;
    case "select":
      return `Chose ${label}`;
    case "scroll":
      return "Scrolled the page";
    case "wait":
      return "Waited for the page";
    case "back":
      return "Went back";
    case "enter":
      return "Submitted";
    default:
      return `Clicked ${label}`;
  }
}

// What the agent is about to do, said the way a person would.
export function plainly(decision: DecisionEvent | null, observation: ObservationEvent | null): string {
  if (!decision) return "Reading the page";
  const label = short(targetLabel(decision.target, observation) ?? "");
  switch (decision.operation) {
    case "TYPE_TEXT":
      return label ? `Typing into ${label}` : "Typing";
    case "SELECT":
      return label ? `Choosing ${label}` : "Choosing an option";
    case "SCROLL_DOWN":
    case "SCROLL_UP":
      return "Looking further along the page";
    case "WAIT":
      return "Waiting for the page";
    case "BACK":
      return "Going back";
    case "ENTER":
      return "Submitting";
    case "DONE":
      return "Checking the result";
    case "BLOCKED":
      return "Stopping";
    default:
      return label ? `Clicking ${label}` : "Clicking";
  }
}

// "c9" and "s9_2" act on element 9 of the observation.
export function targetIndex(target: string | null): number | null {
  const match = target?.match(/^[a-z]+(\d+)/);
  return match ? Number(match[1]) : null;
}

export function targetLabel(target: string | null, observation: ObservationEvent | null): string | null {
  const index = targetIndex(target);
  if (index === null || !observation?.elements) return null;
  return observation.elements.find(([i]) => i === index)?.[2] ?? null;
}

export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

// Fractions of a cent are the normal case: show enough digits to be truthful.
export function money(usd: number): string {
  if (!usd) return "$0";
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

export const percent = (value: number) =>
  value === 0 ? "0%" : value < 0.001 ? "<0.1%" : `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`;

export const seconds = (ms: number, digits = 2) => `${(ms / 1000).toFixed(digits)} s`;
