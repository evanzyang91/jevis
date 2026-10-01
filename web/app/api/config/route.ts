import { NextResponse } from "next/server";

const AGENT_URL = process.env.AGENT_URL ?? "http://127.0.0.1:8787";

export async function GET() {
  const response = await fetch(`${AGENT_URL}/config`, { cache: "no-store" });
  const payload = await response.json();
  return NextResponse.json(payload, { status: response.status });
}
