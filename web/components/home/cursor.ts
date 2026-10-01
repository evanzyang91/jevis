// Cursor events fan out to every CursorLayer on screen (the conversation's live
// view and the developer view's), each animating the same pointer.

import type {
  CursorClickEvent,
  CursorMoveEvent,
  CursorScrollEvent,
  FocusPulseEvent,
  KeystrokeEvent,
  StreamEvent,
} from "@/lib/events";

export type CursorEvent = CursorMoveEvent | CursorClickEvent | CursorScrollEvent | FocusPulseEvent | KeystrokeEvent;

export type CursorRegistry = (handler: (event: CursorEvent) => void) => () => void;

const CURSOR_KINDS = new Set(["cursor_move", "cursor_click", "cursor_scroll", "focus_pulse", "keystroke"]);

export function isCursorEvent(event: StreamEvent): event is CursorEvent {
  return CURSOR_KINDS.has(event.kind);
}
