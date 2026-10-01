"use client";

// Filterable event stream, styled as a Fjord flat list — no monospace, no
// boxes per row, hairline dividers only.

import { useMemo, useState } from "react";
import type { StreamEvent } from "@/lib/events";

type Props = {
  events: StreamEvent[];
  selectedSeq: number | null;
  onSelect: (event: StreamEvent) => void;
};

const KINDS = [
  "observation",
  "decision",
  "action",
  "outcome",
  "plan",
  "budget",
  "error",
  "status",
];

export function EventLog({ events, selectedSeq, onSelect }: Props) {
  const [active, setActive] = useState<Set<string>>(new Set(KINDS));
  const filtered = useMemo(
    () => events.filter((event) => active.has(event.kind)),
    [events, active],
  );

  return (
    <div style={{ display: "grid", gridTemplateRows: "auto 1fr", gap: "var(--space-3)", height: "100%", minHeight: 0 }}>
      <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
        {KINDS.map((kind) => {
          const on = active.has(kind);
          return (
            <button
              key={kind}
              type="button"
              className={`fj-btn fj-btn-sm ${on ? "fj-btn-quiet" : "fj-btn-secondary"}`}
              style={on ? { background: "var(--pine-tint)" } : undefined}
              onClick={() => {
                const next = new Set(active);
                if (on) next.delete(kind);
                else next.add(kind);
                setActive(next);
              }}
            >
              {kind}
            </button>
          );
        })}
      </div>
      <div style={{ overflowY: "auto", border: "1px solid var(--line)", background: "var(--panel)" }}>
        <table className="fj-table">
          <tbody>
            {filtered.map((event) => {
              const selected = event.seq === selectedSeq;
              return (
                <tr
                  key={`${event.run_id}:${event.seq}`}
                  data-selected={selected}
                  onClick={() => onSelect(event)}
                  style={{ cursor: "pointer" }}
                >
                  <td className="fj-num" style={{ width: 60, color: "var(--ink-muted)" }}>
                    {event.seq}
                  </td>
                  <td style={{ width: 120 }}>
                    <span className="fj-label">{event.kind}</span>
                  </td>
                  <td>
                    <span className="fj-small">{describe(event)}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function describe(event: StreamEvent): string {
  switch (event.kind) {
    case "observation":
      return `${event.source} · ${event.element_count} elements · ${event.url}`;
    case "decision":
      return `${event.operation} · ${event.target ?? "-"} · p=${event.confidence.toFixed(2)}`;
    case "action":
      return `${event.action_kind} · ${event.target_label}${event.text ? ` · "${event.text.slice(0, 40)}"` : ""}`;
    case "outcome":
      return `changed=${event.page_changed} · load=${event.load_ms}ms`;
    case "plan":
      return `active=${event.active_index}/${event.plan.length}`;
    case "budget":
      return `steps=${event.steps} · $${event.usd.toFixed(4)}`;
    case "error":
      return `${event.layer}: ${event.message}`;
    case "status":
      return `${event.status}${event.reason ? " · " + event.reason : ""}`;
    default:
      return "";
  }
}
