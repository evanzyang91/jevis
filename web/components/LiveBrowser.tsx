"use client";

// The controlled tab, live: the screencast with the agent's cursor drawn over it.

import type { FrameEvent } from "@/lib/events";

import type { CursorRegistry } from "./cursor";
import { CursorLayer } from "./CursorLayer";
import { ScreencastFrame } from "./ScreencastFrame";

type Props = {
  frame: FrameEvent | null;
  url: string;
  title?: string;
  live: boolean;
  cursor: CursorRegistry;
  footer?: boolean;
};

export function LiveBrowser({ frame, url, title, live, cursor, footer }: Props) {
  const w = frame?.capture_w ?? 1280;
  const h = frame?.capture_h ?? 800;
  return (
    <div className="browser-panel">
      <div className="browser-bar">
        <div className="traffic">
          <b />
          <b />
          <b />
        </div>
        <span className="url-bar" title={url}>
          {url || "Live browser"}
        </span>
        <span className={`live-tag ${live ? "on" : ""}`}>{live ? "LIVE" : "ENDED"}</span>
      </div>
      <div className="viewport" style={{ aspectRatio: `${w} / ${h}` }}>
        {frame ? (
          <>
            <ScreencastFrame frame={frame} />
            <CursorLayer captureW={w} captureH={h} onEvent={cursor} />
          </>
        ) : (
          <div className="empty">
            <p className="empty-symbol">[ ↗ ]</p>
            <h2>Frames appear here</h2>
            <p>The browser shows here once the agent opens it.</p>
          </div>
        )}
      </div>
      {footer && (
        <div className="browser-footer">
          <span>{title || "Agent tab"}</span>
          <span>accessibility tree → typed actions → CDP</span>
        </div>
      )}
    </div>
  );
}
