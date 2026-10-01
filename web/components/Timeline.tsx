"use client";

// One tile per executed step, tinted by outcome. Fjord palette — pine-tint
// for progress, danger-tint for a no-change step. Click to seek.

import type { OutcomeEvent } from "@/lib/events";

type Step = {
  seq: number;
  outcome: OutcomeEvent;
};

type Props = {
  steps: Step[];
  activeSeq: number | null;
  onSelect: (seq: number) => void;
};

export function Timeline({ steps, activeSeq, onSelect }: Props) {
  return (
    <ol
      style={{
        display: "flex",
        gap: "var(--space-1)",
        listStyle: "none",
        padding: 0,
        margin: 0,
        overflowX: "auto",
      }}
    >
      {steps.map((step) => {
        const passed = step.outcome.page_changed;
        const active = step.seq === activeSeq;
        return (
          <li key={step.seq}>
            <button
              type="button"
              onClick={() => onSelect(step.seq)}
              aria-label={`Step ${step.seq}`}
              className="fj-btn fj-btn-sm"
              style={{
                width: 20,
                height: 20,
                padding: 0,
                background: passed ? "var(--pine-tint)" : "var(--danger-tint)",
                borderColor: active ? "var(--pine)" : passed ? "var(--pine-tint)" : "var(--danger-tint)",
                borderWidth: active ? 2 : 1,
              }}
            />
          </li>
        );
      })}
    </ol>
  );
}
