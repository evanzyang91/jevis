"use client";

// StepLog-style plan view (Section 6). Numbered items, the current one shown
// as "Working" in pine-ink, completed ones as "Done" in ink. No colours on
// the item text itself.

type Props = {
  plan: string[];
  activeIndex: number;
};

export function PlanView({ plan, activeIndex }: Props) {
  if (!plan.length) return null;
  return (
    <ol className="fj-panel" style={{ listStyle: "none", padding: 0, margin: 0 }}>
      {plan.map((item, index) => {
        const done = index < activeIndex;
        const active = index === activeIndex;
        const label = done ? "Done" : active ? "Working" : "Next";
        const tone = done ? "muted" : active ? "running" : "muted";
        return (
          <li
            key={index}
            aria-current={active ? "step" : undefined}
            style={{
              display: "grid",
              gridTemplateColumns: "40px 1fr 100px",
              gap: "var(--space-3)",
              alignItems: "baseline",
              padding: "var(--space-3) var(--space-4)",
              borderBottom: index < plan.length - 1 ? "1px solid var(--line-faint)" : "none",
              opacity: done ? 0.7 : 1,
            }}
          >
            <span className="fj-label fj-num">{String(index + 1).padStart(2, "0")}</span>
            <span className="fj-body">{item}</span>
            <span className="fj-status" data-tone={tone} style={{ justifyContent: "flex-end" }}>
              <span className="fj-small">{label}</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}
