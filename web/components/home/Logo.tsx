// Jevis's mark: a browser window with a cursor in it, in one colour. The same
// drawing is the tab icon (app/icon.svg).

export function Logo({ size = 24 }: { size?: number }) {
  return (
    <svg className="logo" viewBox="0 0 32 32" width={size} height={size} fill="none" aria-hidden="true">
      <rect x="3.5" y="5" width="25" height="22" rx="4" stroke="currentColor" strokeWidth="2.6" />
      <path d="M3.5 11.5h25" stroke="currentColor" strokeWidth="2.6" />
      <circle cx="8" cy="8.3" r="1.25" fill="currentColor" />
      <circle cx="11.6" cy="8.3" r="1.25" fill="currentColor" />
      <circle cx="15.2" cy="8.3" r="1.25" fill="currentColor" />
      <path
        d="M12.6 14.95v7.4l2.1-2 1.6 3.2 1.8-.9-1.6-3.1h2.9z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
    </svg>
  );
}
