"use client";

// The most important screen in the app (Section 1). Appears when the agent
// emits a CaptchaEvent. Fjord attention Notice with an explicit "Approve and
// continue" verb.

type Props = {
  runId: string;
  reason: string;
  resumeToken: string;
  onResumed: () => void;
};

export function CaptchaBanner({ runId, reason, resumeToken, onResumed }: Props) {
  return (
    <div role="alert" className="fj-notice" data-tone="attention">
      <div className="fj-notice-title">The site asked to check that you are human.</div>
      <div className="fj-body">{reason} Solve it in the browser, then continue here.</div>
      <div style={{ display: "flex", gap: "var(--space-2)", marginTop: "var(--space-2)" }}>
        <button
          type="button"
          className="fj-btn fj-btn-primary"
          onClick={async () => {
            await fetch(`/api/runs/${runId}/resume-captcha`, {
              method: "POST",
              headers: { "content-type": "application/json" },
              body: JSON.stringify({ resume_token: resumeToken }),
            });
            onResumed();
          }}
        >
          Continue
        </button>
      </div>
    </div>
  );
}
