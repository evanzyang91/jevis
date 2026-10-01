// One route: start a run. Forwards to the Python agent process, which owns
// the browser and the event stream. Persistence into Postgres happens on the
// event listener side (added in step 8).

import { NextResponse } from "next/server";

const AGENT_URL = process.env.AGENT_URL ?? "http://127.0.0.1:8787";

export async function POST(request: Request) {
  const body = await request.json();
  const response = await fetch(`${AGENT_URL}/runs`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  return NextResponse.json(payload, { status: response.status });
}
