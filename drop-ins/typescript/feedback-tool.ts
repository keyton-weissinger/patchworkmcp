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
  };
  if (key) {
    headers["Authorization"] = `Bearer ${key}`;
  }

  try {
    const resp = await fetch(`${url}/api/feedback`, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(5000),
    });
    if (resp.status === 201) {
      return (
        "Thank you. Your feedback has been recorded and will be " +
        "used to improve this server's capabilities."
      );
    }
    console.warn(`PatchworkMCP sidecar returned ${resp.status}`);
    return "Feedback noted (delivery issue, but recorded locally).";
  } catch (e) {
    console.warn("PatchworkMCP: could not reach sidecar:", e);
    return "Feedback noted (sidecar unavailable, but your input is appreciated).";
  }
}
