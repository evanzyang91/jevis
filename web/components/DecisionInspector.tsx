"use client";

// Current observation and the winning decision, side by side. Fjord flat
// layout with label/value rows and a probability bar per candidate.

import type { DecisionEvent, ObservationEvent } from "@/lib/events";

type Props = {
  observation: ObservationEvent | null;
  decision: DecisionEvent | null;
};

export function DecisionInspector({ observation, decision }: Props) {
  return (
    <div style={{ display: "grid", gap: "var(--space-5)" }}>
      <section>
        <h3 className="fj-heading" style={{ margin: "0 0 var(--space-3)" }}>Observation</h3>
        {observation ? (
          <dl style={dl}>
            <Term label="URL" value={observation.url} />
            <Term label="Title" value={observation.title || "—"} />
            <Term label="Source" value={observation.source} />
            <Term label="Fingerprint" value={observation.fingerprint.slice(0, 16) + "…"} />
            <Term label="Elements" value={String(observation.element_count)} />
          </dl>
        ) : (
          <p className="fj-body" style={{ color: "var(--ink-muted)", margin: 0 }}>No observation yet.</p>
        )}
      </section>
      <section>
        <h3 className="fj-heading" style={{ margin: "0 0 var(--space-3)" }}>Decision</h3>
        {decision ? (
          <div style={{ display: "grid", gap: "var(--space-3)" }}>
            <dl style={dl}>
              <Term label="Operation" value={decision.operation} />
              <Term label="Target" value={decision.target ?? "—"} />
              <Term label="Confidence" value={decision.confidence.toFixed(2)} />
              <Term label="Latency" value={`${decision.latency_ms}ms`} />
              <Term label="Model" value={decision.model} />
            </dl>
            <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: "var(--space-1)" }}>
              {Object.entries(decision.probabilities)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 8)
                .map(([id, p]) => (
                  <li
                    key={id}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "80px 60px 1fr",
                      gap: "var(--space-2)",
                      alignItems: "center",
                    }}
                  >
                    <span className="fj-small">{id}</span>
                    <span className="fj-small fj-num" style={{ textAlign: "right", color: "var(--ink-muted)" }}>
                      {(p * 100).toFixed(1)}%
                    </span>
                    <span
                      style={{
                        height: 4,
                        background: "var(--pine-tint)",
                        position: "relative",
                      }}
                    >
                      <span
                        style={{
                          position: "absolute",
                          inset: 0,
                          background: "var(--pine)",
                          width: `${Math.max(2, p * 100)}%`,
                        }}
                      />
                    </span>
                  </li>
                ))}
            </ul>
          </div>
        ) : (
          <p className="fj-body" style={{ color: "var(--ink-muted)", margin: 0 }}>No decision yet.</p>
        )}
      </section>
    </div>
  );
}

function Term({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="fj-label">{label}</dt>
      <dd className="fj-small" style={{ margin: 0, wordBreak: "break-all" }}>{value}</dd>
    </>
  );
}

const dl: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "100px 1fr",
  gap: "var(--space-2) var(--space-3)",
  margin: 0,
};
