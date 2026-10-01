// Cursor overlay math. Kept out of the React tree so the animation logic can
// be tested (later) with plain values and so the component stays a thin driver.

export type Point = { x: number; y: number };

type Trail = { x: number; y: number; t: number };

// Trail visibility window. Longer than the default cursor animation would take
// so a swipe stays visible for a beat after the arrow lands — reads as motion
// blur rather than a snap-and-done.
const HISTORY_MS = 900;

export type CursorState = {
  x: number;
  y: number;
  trail: Trail[];
  moveFrom: Point | null;
  moveTo: Point | null;
  moveStart: number;
  moveDuration: number;
  clicks: { x: number; y: number; t: number }[];
  focusRect: { x: number; y: number; w: number; h: number; t: number } | null;
};

export function initial(x: number, y: number): CursorState {
  return {
    x,
    y,
    trail: [],
    moveFrom: null,
    moveTo: null,
    moveStart: 0,
    moveDuration: 0,
    clicks: [],
    focusRect: null,
  };
}

function easeOutCubic(t: number): number {
  const u = 1 - t;
  return 1 - u * u * u;
}

export function step(state: CursorState, now: number): CursorState {
  let x = state.x;
  let y = state.y;
  let moving = false;
  if (state.moveTo && state.moveFrom) {
    const t = Math.min(1, (now - state.moveStart) / Math.max(1, state.moveDuration));
    const eased = easeOutCubic(t);
    x = state.moveFrom.x + (state.moveTo.x - state.moveFrom.x) * eased;
    y = state.moveFrom.y + (state.moveTo.y - state.moveFrom.y) * eased;
    moving = t < 1;
    if (t >= 1) {
      state = { ...state, moveTo: null, moveFrom: null };
    }
  }
  // Only extend the trail while the cursor is actually moving. An idle
  // cursor was piling frame-by-frame points at the same coord, showing as
  // a stuck green dot at the last position. Also age out points normally
  // so an existing trail fades out after motion ends.
  const previous = state.trail[state.trail.length - 1];
  const moved = !previous || Math.hypot(previous.x - x, previous.y - y) > 0.5;
  const trail = (moving && moved ? [...state.trail, { x, y, t: now }] : state.trail)
    .filter((point) => now - point.t < HISTORY_MS);
  const clicks = state.clicks.filter((click) => now - click.t < 320);
  const focus = state.focusRect && now - state.focusRect.t < 400 ? state.focusRect : null;
  return { ...state, x, y, trail, clicks, focusRect: focus };
}

export function beginMove(state: CursorState, to: Point, duration: number, now: number): CursorState {
  if (duration <= 0) {
    // Teleport: arrow snaps to target immediately, but seed the trail with
    // interpolated points along the A→B path so a fading streak still shows
    // where it came from. One sample per ~10px keeps the streak dense enough
    // that adjacent segments overlap and no gaps appear on fast swipes.
    // Timestamps are staggered a few ms so the fade renders in age order
    // (oldest = start of path).
    const from = { x: state.x, y: state.y };
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const distance = Math.hypot(dx, dy);
    const samples = Math.min(96, Math.max(1, Math.round(distance / 10)));
    const seeded: Trail[] = [];
    for (let i = 1; i <= samples; i++) {
      const t = i / samples;
      seeded.push({
        x: from.x + dx * t,
        y: from.y + dy * t,
        t: now - (samples - i) * 4,
      });
    }
    const trail = [...state.trail, ...seeded].filter((point) => now - point.t < HISTORY_MS);
    return {
      ...state,
      x: to.x,
      y: to.y,
      trail,
      moveFrom: null,
      moveTo: null,
      moveStart: 0,
      moveDuration: 0,
    };
  }
  return {
    ...state,
    moveFrom: { x: state.x, y: state.y },
    moveTo: to,
    moveStart: now,
    moveDuration: duration,
  };
}

export function registerClick(state: CursorState, x: number, y: number, now: number): CursorState {
  return { ...state, clicks: [...state.clicks, { x, y, t: now }] };
}

export function registerFocus(
  state: CursorState,
  rect: { x: number; y: number; w: number; h: number },
  now: number,
): CursorState {
  return { ...state, focusRect: { ...rect, t: now } };
}
