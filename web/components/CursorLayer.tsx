"use client";

// Overlay canvas above the screencast. Renders the synthetic cursor, its
// fading trail, click ripples, focus pulses, and typing tags.
//
// The typing tag uses the page's Jevis purple (--logo); the canvas strokes
// keep their own fixed colours, drawn over the site's pixels.

import { useEffect, useRef, useState } from "react";
import type {
  CursorClickEvent,
  CursorMoveEvent,
  CursorScrollEvent,
  FocusPulseEvent,
  KeystrokeEvent,
} from "@/lib/events";
import {
  type CursorState,
  beginMove,
  initial,
  registerClick,
  registerFocus,
  step,
} from "@/lib/cursor";

type Props = {
  captureW: number;
  captureH: number;
  onEvent: (
    handler: (
      event:
        | CursorMoveEvent
        | CursorClickEvent
        | CursorScrollEvent
        | FocusPulseEvent
        | KeystrokeEvent,
    ) => void,
  ) => () => void;
};

function drawArrow(context: CanvasRenderingContext2D, x: number, y: number) {
  context.save();
  context.translate(x, y);
  context.beginPath();
  context.moveTo(0, 0);
  context.lineTo(0, 20);
  context.lineTo(5, 15);
  context.lineTo(9, 22);
  context.lineTo(12, 20);
  context.lineTo(8, 13);
  context.lineTo(14, 12);
  context.closePath();
  context.fillStyle = "#111";
  context.strokeStyle = "#fff";
  context.lineWidth = 1;
  context.fill();
  context.stroke();
  context.restore();
}

function drawTrail(context: CanvasRenderingContext2D, state: CursorState, _now: number) {
  const trail = state.trail;
  if (trail.length < 2) return;
  const head = trail[trail.length - 1];
  const tail = trail[0];
  // One stroke, one width, one alpha gradient along the length. Stacking a
  // narrower brighter stroke on top of a wider dim stroke created a visible
  // brighter band down the middle where the two widths overlapped — the
  // "different coloured part" that read as a hard edge. A single stroke has
  // no such boundary; alpha is smooth per-pixel from tail → head courtesy
  // of the gradient interpolation, and uniform across width.
  const gradient = context.createLinearGradient(tail.x, tail.y, head.x, head.y);
  gradient.addColorStop(0, "hsla(160, 34%, 27%, 0)");
  gradient.addColorStop(1, "hsla(160, 34%, 27%, 0.6)");
  context.lineCap = "round";
  context.lineJoin = "round";
  context.beginPath();
  context.moveTo(tail.x, tail.y);
  for (let i = 1; i < trail.length; i++) {
    context.lineTo(trail[i].x, trail[i].y);
  }
  context.strokeStyle = gradient;
  context.lineWidth = 4;
  context.stroke();
}

function drawClicks(context: CanvasRenderingContext2D, state: CursorState, now: number) {
  for (const click of state.clicks) {
    const age = (now - click.t) / 320;
    if (age >= 1) continue;
    const radius = 12 + age * 28;
    context.beginPath();
    context.arc(click.x, click.y, radius, 0, Math.PI * 2);
    context.strokeStyle = `hsla(160, 34%, 27%, ${0.4 * (1 - age)})`;
    context.lineWidth = 1.5;
    context.stroke();
    context.beginPath();
    context.arc(click.x, click.y, 12 + age * 10, 0, Math.PI * 2);
    context.fillStyle = `hsla(160, 34%, 27%, ${0.5 * (1 - age)})`;
    context.fill();
  }
}

function drawFocus(context: CanvasRenderingContext2D, state: CursorState, now: number) {
  if (!state.focusRect) return;
  const age = (now - state.focusRect.t) / 600;
  if (age >= 1) return;
  const alpha = age < 0.2 ? age * 5 : 1 - (age - 0.2) / 0.8;
  context.strokeStyle = `hsla(160, 34%, 27%, ${alpha})`;
  context.lineWidth = 2;
  context.strokeRect(state.focusRect.x, state.focusRect.y, state.focusRect.w, state.focusRect.h);
}

export function CursorLayer({ captureW, captureH, onEvent }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const stateRef = useRef<CursorState>(initial(captureW / 2, captureH / 2));
  const [tag, setTag] = useState<string | null>(null);
  const tagTimer = useRef<number | null>(null);

  useEffect(() => {
    const unsubscribe = onEvent((event) => {
      const now = performance.now();
      if (event.kind === "cursor_move") {
        stateRef.current = beginMove(stateRef.current, event.to, event.duration_ms, now);
      } else if (event.kind === "cursor_click") {
        stateRef.current = registerClick(stateRef.current, event.x, event.y, now);
      } else if (event.kind === "focus_pulse") {
        stateRef.current = registerFocus(stateRef.current, event.rect, now);
      } else if (event.kind === "keystroke") {
        const text = event.secret ? "•".repeat(Math.min(8, event.text.length || 4)) : event.text;
        setTag(text);
        if (tagTimer.current) window.clearTimeout(tagTimer.current);
        const holdMs = Math.min(1500, 40 * event.text.length);
        tagTimer.current = window.setTimeout(() => setTag(null), holdMs);
      }
    });
    return unsubscribe;
  }, [onEvent]);

  useEffect(() => {
    let raf = 0;
    const draw = () => {
      const canvas = canvasRef.current;
      if (canvas) {
        canvas.width = captureW;
        canvas.height = captureH;
        const context = canvas.getContext("2d");
        if (context) {
          context.clearRect(0, 0, canvas.width, canvas.height);
          const now = performance.now();
          stateRef.current = step(stateRef.current, now);
          drawFocus(context, stateRef.current, now);
          drawTrail(context, stateRef.current, now);
          drawClicks(context, stateRef.current, now);
          drawArrow(context, stateRef.current.x, stateRef.current.y);
        }
      }
      raf = window.requestAnimationFrame(draw);
    };
    raf = window.requestAnimationFrame(draw);
    return () => window.cancelAnimationFrame(raf);
  }, [captureW, captureH]);

  return (
    <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
      <canvas ref={canvasRef} style={{ width: "100%", height: "100%" }} />
      {tag !== null && (
        <div
          style={{
            position: "absolute",
            left: `${(100 * stateRef.current.x) / captureW}%`,
            top: `${(100 * stateRef.current.y) / captureH}%`,
            transform: "translate(-100%, -140%)",
            padding: "4px 8px",
            borderRadius: 6,
            background: "var(--logo)",
            color: "#fff",
            fontSize: 12,
            fontWeight: 500,
            fontFamily: "inherit",
            maxWidth: "60%",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {tag}
        </div>
      )}
    </div>
  );
}
