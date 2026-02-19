/**
 * PatchworkMCP — Drop-in feedback tool for TypeScript MCP servers.
 *
 * Copy this single file into your project. Works with:
 *   - @modelcontextprotocol/sdk  → registerFeedbackTool(server, "my-server")
 *   - Manual registration        → TOOL_NAME, TOOL_INPUT_SCHEMA, sendFeedback()
 *
 * No extra dependencies — uses the built-in fetch API (Node 18+).
 *
 * Configuration via environment:
 *   FEEDBACK_SIDECAR_URL  - default: http://localhost:8099
 *   FEEDBACK_API_KEY      - optional shared secret
 */

const SIDECAR_URL =
  process.env.FEEDBACK_SIDECAR_URL ?? "http://localhost:8099";
const API_KEY = process.env.FEEDBACK_API_KEY ?? "";

// ── HTTP Client Config ─────────────────────────────────────────────────────

const MAX_RETRIES = 2;
const INITIAL_BACKOFF_MS = 500; // doubles each retry
const REQUEST_TIMEOUT_MS = 5000;
const USER_AGENT = "PatchworkMCP-TypeScript/1.0";

function isRetryableStatus(status: number): boolean {
  return [429, 500, 502, 503, 504].includes(status);
}

// Prefix makes these log lines greppable in any log aggregator.
const LOG_PREFIX = "PATCHWORKMCP_UNSENT_FEEDBACK";

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Log the full payload at warn level so the hosting environment captures it.
 * The structured JSON is greppable via LOG_PREFIX and can be replayed from
 * whatever log aggregation the containing server uses.
 */
function logUnsentPayload(
  payload: Record<string, unknown>,
  reason: string,
): void {
  console.warn(
    `${LOG_PREFIX} reason=${reason} payload=${JSON.stringify(payload)}`,
  );
}

// ── Tool Schema ─────────────────────────────────────────────────────────────

export const TOOL_NAME = "feedback";

export const TOOL_DESCRIPTION =
  "Report when you cannot find what you need or when available tools don't " +
  "fully address the task. This feedback directly improves this server. " +
  "Call this tool whenever: " +
  "(1) you looked for a tool or resource that doesn't exist, " +
  "(2) a tool returned incomplete or unhelpful results, " +
  "(3) you had to work around a limitation or approximate an answer, " +
  "(4) a new tool or parameter would have made the task easier. " +
  "If you could not fully satisfy the user's request with the available " +
  "tools, call this BEFORE giving your final response.";

export const TOOL_INPUT_SCHEMA = {
  what_i_needed: {
    type: "string" as const,
    description:
      "What capability, data, or tool were you looking for? " +
      "Be specific about the action you wanted to perform.",
  },
  what_i_tried: {
    type: "string" as const,
    description:
      "What tools or approaches did you try? " +
      "Include tool names and brief results.",
  },
  gap_type: {
    type: "string" as const,
    enum: [
      "missing_tool",
      "incomplete_results",
      "missing_parameter",
      "wrong_format",
      "other",
    ],
    description: "The category of gap encountered.",
  },
  suggestion: {
    type: "string" as const,
    description:
      "Your idea for what would have helped. Describe the tool, " +
      "parameter, or change — including what inputs it would " +
      "accept and what it would return.",
  },
  user_goal: {
    type: "string" as const,
    description:
      "The user's original request or goal that led to discovering this gap.",
  },
  resolution: {
    type: "string" as const,
    enum: ["blocked", "worked_around", "partial"],
    description:
      "What happened after hitting the gap? " +
      "'blocked' = could not proceed, " +
      "'worked_around' = found an alternative, " +
      "'partial' = completed incompletely.",
  },
  tools_available: {
    type: "array" as const,
    items: { type: "string" as const },
    description:
      "List the tool names available on this server that you " +
      "considered or tried before submitting feedback.",
  },
  agent_model: {
    type: "string" as const,
    description:
      "Your model identifier, if known (e.g. 'claude-sonnet-4-20250514').",
  },
  session_id: {
    type: "string" as const,
    description:
      "An identifier for the current conversation or session, if available.",
  },
  client_type: {
    type: "string" as const,
    description:
      "The MCP client in use, if known " +
      "(e.g. 'claude-desktop', 'cursor', 'claude-code', 'continue').",
  },
};

// ── MCP SDK Integration ─────────────────────────────────────────────────────

/**
 * One-liner registration for @modelcontextprotocol/sdk servers.
 *
 * Usage:
 *   import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
 *   import { registerFeedbackTool } from "./feedback-tool.js";
 *
 *   const server = new McpServer({ name: "my-server", version: "1.0.0" });
 *   registerFeedbackTool(server, "my-server");
 */
export interface FeedbackToolOptions {
  /** Override FEEDBACK_SIDECAR_URL for this tool instance. */
  sidecarUrl?: string;
  /** Override FEEDBACK_API_KEY for this tool instance. */
  apiKey?: string;
}

export function registerFeedbackTool(
  server: { tool: Function },
  serverName: string = "unknown",
  options: FeedbackToolOptions = {},
): void {
  server.tool(
    TOOL_NAME,
    TOOL_DESCRIPTION,
    TOOL_INPUT_SCHEMA,
    async (args: Record<string, unknown>) => {
      const message = await sendFeedback(args, serverName, options);
      return { content: [{ type: "text", text: message }] };
    },
  );
}

// ── Feedback Submission ─────────────────────────────────────────────────────

/**
 * Send feedback to the sidecar with retry logic.
 *
 * Retries up to MAX_RETRIES times on transient failures (connection errors,
 * 5xx, 429) with exponential backoff. Uses built-in fetch (Node 18+) which
 * handles connection pooling via undici automatically.
 */
export async function sendFeedback(
  args: Record<string, unknown>,
  serverName: string = "unknown",
  options: FeedbackToolOptions = {},
): Promise<string> {
  const url = options.sidecarUrl ?? SIDECAR_URL;
  const key = options.apiKey ?? API_KEY;

  const payload = {
    server_name: serverName,
    what_i_needed: (args.what_i_needed as string) ?? "",
    what_i_tried: (args.what_i_tried as string) ?? "",
    gap_type: (args.gap_type as string) ?? "other",
    suggestion: (args.suggestion as string) ?? "",
    user_goal: (args.user_goal as string) ?? "",
    resolution: (args.resolution as string) ?? "",
    tools_available: (args.tools_available as string[]) ?? [],
    agent_model: (args.agent_model as string) ?? "",
    session_id: (args.session_id as string) ?? "",
    client_type: (args.client_type as string) ?? "",
  };

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "User-Agent": USER_AGENT,
  };
  if (key) {
    headers["Authorization"] = `Bearer ${key}`;
  }

  const endpoint = `${url}/api/feedback`;
  const body = JSON.stringify(payload);
  let lastError: unknown;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const resp = await fetch(endpoint, {
        method: "POST",
        headers,
        body,
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
      if (resp.status === 201) {
        return (
          "Thank you. Your feedback has been recorded and will be " +
          "used to improve this server's capabilities."
        );
      }
      if (isRetryableStatus(resp.status) && attempt < MAX_RETRIES) {
        console.warn(
          `PatchworkMCP sidecar returned ${resp.status}, retrying (${attempt + 1}/${MAX_RETRIES})`,
        );
        await sleep(INITIAL_BACKOFF_MS * 2 ** attempt);
        continue;
      }
      logUnsentPayload(payload, `status_${resp.status}`);
      return `Feedback could not be delivered and was logged. (Server returned ${resp.status})`;
    } catch (e) {
      lastError = e;
      if (attempt < MAX_RETRIES) {
        console.warn(
          `PatchworkMCP: delivery failed (${e}), retrying (${attempt + 1}/${MAX_RETRIES})`,
        );
        await sleep(INITIAL_BACKOFF_MS * 2 ** attempt);
        continue;
      }
    }
  }

  logUnsentPayload(payload, `unreachable:${lastError}`);
  return "Feedback could not be delivered and was logged. (Server unreachable)";
}
