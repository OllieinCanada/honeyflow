import { NextResponse } from "next/server";
import { inferActionWithProvenance } from "@/lib/0g/inference";
import type { InferenceAction } from "@/lib/0g";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 60;

const VALID_ACTIONS: InferenceAction[] = [
  "analyze_repo",
  "split_direct_vs_deps",
  "rank_dependency_importance",
  "analyze_package",
  "rank_citation_influence",
  "analyze_paper",
];

export async function POST(req: Request) {
  try {
    const body = await req.json();
    const { action, params } = body;

    if (!action || !VALID_ACTIONS.includes(action)) {
      return NextResponse.json(
        { error: `Invalid action. Must be one of: ${VALID_ACTIONS.join(", ")}` },
        { status: 400 }
      );
    }

    if (!params || typeof params !== "object") {
      return NextResponse.json(
        { error: "params must be an object" },
        { status: 400 }
      );
    }

    const { result, provenance } = await inferActionWithProvenance(
      action as InferenceAction,
      params
    );

    if (result === null) {
      return NextResponse.json(
        { error: "Inference returned no result", result: null },
        { status: 502 }
      );
    }

    return NextResponse.json({ result, provenance });
  } catch (e: unknown) {
    const message = e instanceof Error ? e.message : String(e);
    console.error("[api/inference] error:", message);
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
