import { getBrokerContext } from "./broker";
import {
  buildPrompt,
  promptTemplateId,
  type InferenceAction,
} from "./prompts";

const MAX_RETRIES = 2;
const GEMINI_MODEL = "gemini-2.5-flash";
const GEMINI_URL = `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent`;
const INFERENCE_PROVIDER = (
  process.env.INFERENCE_PROVIDER || "gemini"
).toLowerCase();

interface ChatCompletion {
  id: string;
  choices: { message: { content: string } }[];
}

export interface InferenceProvenance {
  action: InferenceAction;
  provider: "0g" | "gemini";
  model_id: string;
  prompt_template_id: string;
}

interface InferenceTextResult {
  content: string;
  provider: "0g" | "gemini";
  model: string;
}

/**
 * Call inference backend and return raw text.
 */
export async function infer(prompt: string): Promise<string | null> {
  return (await inferWithProvenance(prompt))?.content ?? null;
}

async function inferWithProvenance(
  prompt: string
): Promise<InferenceTextResult | null> {
  // Default to Gemini. 0G is opt-in via INFERENCE_PROVIDER=0g.
  if (INFERENCE_PROVIDER !== "0g") {
    return inferFallback(prompt);
  }

  // 0G path with Gemini fallback if 0G fails.
  const zeroGResult = await infer0G(prompt);
  if (zeroGResult !== null) return zeroGResult;
  return inferFallback(prompt);
}

async function infer0G(prompt: string): Promise<InferenceTextResult | null> {
  let ctx;
  try {
    ctx = await getBrokerContext();
  } catch (e) {
    console.warn("[0G] broker init failed:", e);
    return null;
  }

  let lastErr: unknown;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const headers = await ctx.broker.inference.getRequestHeaders(
        ctx.providerAddress,
        prompt
      );

      const reqHeaders: Record<string, string> = {
        "Content-Type": "application/json",
      };
      for (const [k, v] of Object.entries(headers)) {
        if (typeof v === "string") reqHeaders[k] = v;
      }

      const res = await fetch(`${ctx.endpoint}/chat/completions`, {
        method: "POST",
        headers: reqHeaders,
        body: JSON.stringify({
          model: ctx.model,
          messages: [{ role: "user", content: prompt }],
          temperature: 0.1,
        }),
      });

      if (!res.ok) {
        throw new Error(`0G inference failed with status ${res.status}`);
      }

      const data: ChatCompletion = await res.json();
      const content = data.choices?.[0]?.message?.content || "";

      // settle fee / verify response
      try {
        await ctx.broker.inference.processResponse(
          ctx.providerAddress,
          data.id,
          content
        );
      } catch (e) {
        console.warn("[0G] processResponse warning:", e);
      }

      return { content, provider: "0g", model: ctx.model };
    } catch (e) {
      lastErr = e;
      console.warn(`[0G] inference attempt ${attempt + 1} failed:`, e);
      if (attempt < MAX_RETRIES) {
        await new Promise((r) => setTimeout(r, 2000 * (attempt + 1)));
      }
    }
  }

  console.error("[0G] all inference attempts failed:", lastErr);
  return null;
}

async function inferFallback(prompt: string): Promise<InferenceTextResult | null> {
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) return null;

  try {
    const res = await fetch(`${GEMINI_URL}?key=${apiKey}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        contents: [{ parts: [{ text: prompt }] }],
        generationConfig: {
          temperature: 0.1,
          responseMimeType: "application/json",
        },
      }),
    });

    if (!res.ok) {
      throw new Error(`Gemini inference failed with status ${res.status}`);
    }

    const data = await res.json();
    const content = data.candidates?.[0]?.content?.parts?.[0]?.text || "";

    console.log("[GEMINI] inference OK, response length:", content.length);
    return { content, provider: "gemini", model: GEMINI_MODEL };
  } catch (e) {
    console.warn("[GEMINI] inference failed:", e);
    return null;
  }
}

/**
 * Build prompt from action + params, call 0G, parse JSON from response.
 */
export async function inferAction(
  action: InferenceAction,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  params: Record<string, any>
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): Promise<any> {
  return (await inferActionWithProvenance(action, params)).result;
}

export async function inferActionWithProvenance(
  action: InferenceAction,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  params: Record<string, any>
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): Promise<{ result: any; provenance: InferenceProvenance | null }> {
  const prompt = buildPrompt(action, params);
  const inferred = await inferWithProvenance(prompt);
  const result = parseJsonResponse(inferred?.content ?? null);
  return {
    result,
    provenance: inferred
      ? {
          action,
          provider: inferred.provider,
          model_id: inferred.model,
          prompt_template_id: promptTemplateId(action),
        }
      : null,
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function parseJsonResponse(raw: string | null): any {
  if (!raw) return null;
  try {
    return JSON.parse(raw.trim());
  } catch {
    // strip markdown code fences
    let cleaned = raw.trim();
    if (cleaned.startsWith("```")) {
      const lines = cleaned.split("\n");
      lines.shift();
      if (lines.length && lines[lines.length - 1].trim() === "```") {
        lines.pop();
      }
      cleaned = lines.join("\n");
    }
    try {
      return JSON.parse(cleaned);
    } catch {
      // try to extract first JSON object
      const match = cleaned.match(/\{[\s\S]*\}/);
      if (match) {
        try {
          return JSON.parse(match[0]);
        } catch {
          /* fall through */
        }
      }
      console.warn("[INFER] failed to parse model response as JSON");
      return null;
    }
  }
}
