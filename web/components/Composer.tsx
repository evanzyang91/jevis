"use client";

// Where a person describes a task. Fjord TaskComposer: multi-line lead input,
// a slim bar underneath with the primary Start task button. No suggestion
// chips or sample prompts around it.

import { useState } from "react";

type Props = {
  onStart: (goal: string, url: string) => void;
  disabled?: boolean;
};

export function Composer({ onStart, disabled }: Props) {
  const [goal, setGoal] = useState("");
  const [url, setUrl] = useState("");

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (goal.trim()) onStart(goal.trim(), url.trim());
      }}
      className="fj-panel fj-panel-pad"
      style={{ display: "grid", gap: "var(--space-4)" }}
    >
      <label>
        <span className="fj-field-label">Task</span>
        <textarea
          className="fj-textarea"
          rows={3}
          value={goal}
          onChange={(event) => setGoal(event.target.value)}
          placeholder="Describe what you want the agent to do."
          disabled={disabled}
        />
      </label>
      <label>
        <span className="fj-field-label">Start URL (optional)</span>
        <input
          className="fj-input"
          type="url"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://example.com"
          disabled={disabled}
        />
      </label>
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <button
          type="submit"
          className="fj-btn fj-btn-primary"
          disabled={disabled || !goal.trim()}
        >
          Start task
        </button>
      </div>
    </form>
  );
}
