"use client";

// Draws the latest JPEG frame as an <img> data URL. The browser only swaps
// pixels after the new frame decodes, so there is no intermediate blank
// state — canvas-based updates flickered because resetting canvas.width
// clears the buffer during the async image decode.

import { useMemo } from "react";
import type { FrameEvent } from "@/lib/events";

type Props = {
  frame: FrameEvent | null;
};

export function ScreencastFrame({ frame }: Props) {
  const src = useMemo(
    () => (frame ? `data:image/jpeg;base64,${frame.data_b64}` : undefined),
    [frame],
  );
  if (!src) {
    return <div style={{ width: "100%", height: "100%", background: "#000" }} />;
  }
  return (
    <img
      src={src}
      alt=""
      draggable={false}
      style={{
        width: "100%",
        height: "100%",
        display: "block",
        background: "#000",
        objectFit: "contain",
        userSelect: "none",
      }}
    />
  );
}
