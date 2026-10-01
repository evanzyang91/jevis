"use client";

// Playbook browser. Table by host with a Forget button per row.

import { useEffect, useState } from "react";

type Entry = {
  id: number;
  host: string;
  path: string;
  previous: string;
  next: string;
  seen: number;
  succeeded: number;
  failed: number;
  last_used: string;
};

export default function Playbooks() {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [host, setHost] = useState<string>("");

  useEffect(() => {
    const query = host ? `?host=${encodeURIComponent(host)}` : "";
    fetch(`/api/playbooks${query}`).then((response) => response.json()).then(setEntries);
  }, [host]);

  return (
    <main style={{ padding: 12, display: "grid", gap: 12 }}>
      <header style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <h1 style={{ margin: 0, fontSize: 16 }}>Playbooks</h1>
        <input
          value={host}
          onChange={(event) => setHost(event.target.value)}
          placeholder="host filter"
          style={{ maxWidth: 240 }}
        />
        <a href="/dev">Back</a>
      </header>
      <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 12 }}>
        <thead>
          <tr>
            {["Host", "Path", "Previous", "Next", "Seen", "Success", "Fail", ""].map((column) => (
              <th key={column} style={{ textAlign: "left", borderBottom: "1px solid #999", padding: "4px 8px" }}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.id}>
              <td style={cell}>{entry.host}</td>
              <td style={cell}>{entry.path}</td>
              <td style={cell}>{entry.previous}</td>
              <td style={cell}>{entry.next}</td>
              <td style={cell}>{entry.seen}</td>
              <td style={cell}>{entry.succeeded}</td>
              <td style={cell}>{entry.failed}</td>
              <td style={cell}>
                <button
                  type="button"
                  onClick={async () => {
                    await fetch(`/api/playbooks/${entry.id}`, { method: "DELETE" });
                    setEntries((prior) => prior.filter((row) => row.id !== entry.id));
                  }}
                >
                  Forget
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}

const cell: React.CSSProperties = { padding: "4px 8px", borderBottom: "1px solid #eee", fontFamily: "monospace" };
