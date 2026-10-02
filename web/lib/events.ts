// Wire types for the event stream. Kept in step with agent/transport/events.py
// by hand for now; a codegen step from the pydantic schema lands in step 7 when
// the UI needs the types checked end-to-end.

export type Point = { x: number; y: number };
export type Rect = { x: number; y: number; w: number; h: number };

export type EventBase = {
  run_id: string;
  seq: number;
  ts: string; // ISO 8601, UTC
};

export type ObservationEvent = EventBase & {
  kind: "observation";
  source: "dom" | "vision";
  url: string;
  title: string;
  fingerprint: string;
  element_count: number;
  text_preview: string;
  marker?: string;
  scroll_y?: number;
  scroll_area?: string;
  loading?: boolean;
  viewport_w?: number;
  viewport_h?: number;
  // [index, role, label] per element; the index matches a target id's digits ("c9" -> 9).
  elements?: [number, string, string][];
};

export type DecisionEvent = EventBase & {
  kind: "decision";
  operation: string;
  choice: string;
  target: string | null;
  probabilities: Record<string, number>;
  confidence: number;
  latency_ms: number;
  model: string;
  remembered: boolean;
  banned?: string[];
  offered?: Record<string, string[]>;
  dialog?: string;
  dialog_p?: number;
  signal?: number;
  check?: string;
  check_p?: number;
  check_switch?: string;
  switched?: boolean;
  escalate?: boolean;
  guidance?: string;
};

export type ActionEvent = EventBase & {
  kind: "action";
  action_kind:
    | "click"
    | "fill"
    | "select"
    | "scroll"
    | "wait"
    | "back"
    | "enter"
    | "navigate";
  target_label: string;
  node_id: number | null;
  text: string | null;
  secret: boolean;
};

export type OutcomeEvent = EventBase & {
  kind: "outcome";
  page_changed: boolean;
  url_changed: boolean;
  guard_held: boolean;
  load_ms: number;
};

export type CursorMoveEvent = EventBase & {
  kind: "cursor_move";
  from: Point;
  to: Point;
  duration_ms: number;
  intent: "hover" | "click" | "scroll";
};

export type CursorClickEvent = EventBase & {
  kind: "cursor_click";
  x: number;
  y: number;
};

export type CursorScrollEvent = EventBase & {
  kind: "cursor_scroll";
  x: number;
  y: number;
  delta_y: number;
};

export type KeystrokeEvent = EventBase & {
  kind: "keystroke";
  text: string;
  secret: boolean;
};

export type FocusPulseEvent = EventBase & {
  kind: "focus_pulse";
  rect: Rect;
};

export type FrameEvent = EventBase & {
  kind: "frame";
  data_b64: string;
  capture_w: number;
  capture_h: number;
  device_ratio: number;
};

export type PlanStepStatus = "pending" | "active" | "done" | "skipped";

// One tracked step, as the supervisor's ledger sees it (agent/planner/progress.py).
export type PlanStep = {
  text: string;
  status: PlanStepStatus;
  // What finished it ("added Great Value Flour"), or why it was skipped.
  satisfied_by: string;
  search_term: string | null;
  done_when: string;
};

// Emitted once when the plan is made, then again whenever a step finishes,
// is skipped, is rewritten, or is reopened. The latest one is current.
export type PlanEvent = EventBase & {
  kind: "plan";
  plan: string[];
  active_index: number;
  original_goal?: string;
  refined_goal?: string;
  start_url?: string;
  steps?: PlanStep[];
  constraints?: string[];
  stop_when?: string;
  note?: string;
};

export type BudgetEvent = EventBase & {
  kind: "budget";
  steps: number;
  model_ms: number;
  load_ms: number;
  usd: number;
};

export type CaptchaEvent = EventBase & {
  kind: "captcha";
  reason: string;
  resume_token: string;
  url?: string; // the check page
  wait_s?: number; // how long the run waits for the person before it stops
};

export type ErrorEvent = EventBase & {
  kind: "error";
  layer: string;
  error_kind: string;
  message: string;
  recoverable: boolean;
};

export type StatusEvent = EventBase & {
  kind: "status";
  status: "running" | "paused" | "done" | "blocked" | "error";
  reason: string;
};

// One channel per shape. Frames go on `/ws/frames`; everything else on `/ws/events`.
export type StreamEvent =
  | ObservationEvent
  | DecisionEvent
  | ActionEvent
  | OutcomeEvent
  | CursorMoveEvent
  | CursorClickEvent
  | CursorScrollEvent
  | KeystrokeEvent
  | FocusPulseEvent
  | PlanEvent
  | BudgetEvent
  | CaptchaEvent
  | ErrorEvent
  | StatusEvent;

export type EventKind = StreamEvent["kind"] | FrameEvent["kind"];
