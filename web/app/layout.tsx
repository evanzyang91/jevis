import type { ReactNode } from "react";

import "./globals.css";

export const metadata = {
  title: "Jevis",
  description: "Hand a browser task to an agent and watch it work.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
