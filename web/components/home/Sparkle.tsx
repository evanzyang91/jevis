// The agent's mark: a four-point star in the product gradient. It turns
// while the agent works and rests when it is done.

export function Sparkle({ size = 24, active = false }: { size?: number; active?: boolean }) {
  return (
    <svg
      className={`sparkle ${active ? "active" : ""}`}
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="sparkle-fill" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="var(--g1)" />
          <stop offset="0.55" stopColor="var(--g2)" />
          <stop offset="1" stopColor="var(--g3)" />
        </linearGradient>
      </defs>
      <path
        fill="url(#sparkle-fill)"
        d="M12 1.5c.5 5.6 4.9 10 10.5 10.5-5.6.5-10 4.9-10.5 10.5C11.5 16.9 7.1 12.5 1.5 12 7.1 11.5 11.5 7.1 12 1.5Z"
      />
    </svg>
  );
}
