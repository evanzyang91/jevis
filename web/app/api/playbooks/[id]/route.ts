import { NextResponse } from "next/server";

const AGENT_URL = process.env.AGENT_URL ?? "http://127.0.0.1:8787";

export async function DELETE(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const response = await fetch(`${AGENT_URL}/playbooks/${id}`, { method: "DELETE" });
  const payload = await response.json();
  return NextResponse.json(payload, { status: response.status });
}
