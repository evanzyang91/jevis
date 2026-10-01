"use client";

// Keep a scrolling box pinned to its bottom while content grows, smoothly.
//
// Setting scrollTop on every event, with CSS smooth scrolling on, made each
// new step jitter: dozens of events a second restarted the scroll animation
// toward a moving target. Here one animation loop eases toward the bottom
// instead, so growth of any size and rate reads as one continuous glide.
// Scrolling up to read stops the following; reaching the bottom resumes it.

import { type RefObject, useCallback, useEffect, useRef } from "react";

const EASE = 0.18; // share of the remaining distance covered per frame
const NEAR_BOTTOM_PX = 48;

export function useFollowBottom(ref: RefObject<HTMLElement | null>): () => void {
  const stuck = useRef(true);

  useEffect(() => {
    const box = ref.current;
    if (!box) return;
    let frame = 0;
    const gap = () => box.scrollHeight - box.clientHeight - box.scrollTop;
    // Only the reader's own gestures unpin; the loop's scrolling never does.
    const unpinOnUp = (event: WheelEvent) => {
      if (event.deltaY < 0) stuck.current = false;
    };
    const unpin = () => {
      stuck.current = false;
    };
    const repin = () => {
      if (gap() < NEAR_BOTTOM_PX) stuck.current = true;
    };
    const step = () => {
      if (stuck.current) {
        const remaining = gap();
        if (remaining > 0.5) box.scrollTop += Math.max(1, remaining * EASE);
      }
      frame = requestAnimationFrame(step);
    };
    box.addEventListener("wheel", unpinOnUp, { passive: true });
    box.addEventListener("touchmove", unpin, { passive: true });
    box.addEventListener("scroll", repin, { passive: true });
    frame = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(frame);
      box.removeEventListener("wheel", unpinOnUp);
      box.removeEventListener("touchmove", unpin);
      box.removeEventListener("scroll", repin);
    };
  }, [ref]);

  // Pin again, e.g. when the reader sends a new request.
  return useCallback(() => {
    stuck.current = true;
  }, []);
}
