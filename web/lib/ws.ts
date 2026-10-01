// Two typed WebSocket streams: `/ws/events` for ordered events and
// `/ws/frames` for lossy screencast frames.
//
// The Python agent server owns these sockets. Next.js cannot proxy WebSocket
// with plain API routes, so the browser connects directly to the agent.
// `NEXT_PUBLIC_AGENT_WS_URL` overrides the default of `ws://127.0.0.1:8787`.

import type { FrameEvent, StreamEvent } from "./events";

export type SocketState = "connecting" | "open" | "closed";

type Cleanup = () => void;

function agentBase(): string {
  const configured = process.env.NEXT_PUBLIC_AGENT_WS_URL;
  if (configured) return configured.replace(/\/$/, "");
  const proto = typeof window !== "undefined" && window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//127.0.0.1:8787`;
}

function connect<T>(
  url: string,
  onMessage: (msg: T) => void,
  onState?: (state: SocketState) => void,
): Cleanup {
  let socket: WebSocket | null = null;
  let stopped = false;
  let backoff = 250;

  const open = () => {
    onState?.("connecting");
    socket = new WebSocket(url);
    socket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data) as T;
        onMessage(payload);
      } catch {
        // Malformed frame — drop and stay connected.
      }
    });
    socket.addEventListener("close", () => {
      onState?.("closed");
      if (stopped) return;
      window.setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, 5000);
    });
    socket.addEventListener("error", () => {
      // Errors always precede `close`; nothing to do here.
    });
    socket.addEventListener("open", () => {
      onState?.("open");
      backoff = 250;
    });
  };

  open();

  return () => {
    stopped = true;
    if (socket) socket.close();
  };
}

export function subscribeEvents(
  runId: string,
  onEvent: (event: StreamEvent) => void,
  onState?: (state: SocketState) => void,
): Cleanup {
  return connect<StreamEvent>(`${agentBase()}/ws/events?run_id=${runId}`, onEvent, onState);
}

export function subscribeFrames(
  runId: string,
  onFrame: (frame: FrameEvent) => void,
  onState?: (state: SocketState) => void,
): Cleanup {
  return connect<FrameEvent>(`${agentBase()}/ws/frames?run_id=${runId}`, onFrame, onState);
}
