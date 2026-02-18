# PatchworkMCP

**Capture what AI agents need but can't do — then use it to improve your MCP
server.**

PatchworkMCP adds a feedback loop to any MCP server. You drop in a single tool
file, agents call it when they hit a wall, and you get a dashboard showing
exactly what's missing. No guessing about what to build next.

**The vision:** Feedback accumulates from every agent session. PatchworkMCP
analyzes the patterns, looks at your server's repo, and gives you actionable
suggestions — eventually as auto-drafted PRs. Right now we're at step one:
capture and review. The analysis layer comes next.

## How It Works

```
┌─────────────────┐     POST /api/feedback     ┌──────────────────┐
│  Your MCP Server │ ─────────────────────────▶ │  Sidecar (8099)  │
│                  │                            │  FastAPI + SQLite │
│  + feedback tool │                            │  + Review UI      │
└─────────────────┘                            └──────────────────┘
      │                                               │
  (agent calls                                  (you browse to
   feedback tool                                 localhost:8099
   when stuck)                                   to review)
```

1. You copy a **single file** (the "drop-in") into your MCP server project
2. The drop-in exposes a `feedback` tool that agents can call
3. When an agent hits a gap — missing tool, wrong format, incomplete results —
   it calls the feedback tool with details about what it needed
4. The feedback goes to a **sidecar service** (a small FastAPI app) that stores
   it in SQLite and serves a review UI
5. You browse the dashboard, triage feedback, add notes, and spot patterns

The drop-in is available for **Python, TypeScript, Go, and Rust**. The sidecar
is the stable contract — every drop-in just POSTs JSON to the same endpoint.

## Quick Start

### 1. Start the sidecar

```bash
# Using uv (recommended)
cd patchworkmcp
uv run server.py

# Using pip
pip install fastapi 'uvicorn[standard]'
uvicorn server:app --port 8099
```

Browse to http://localhost:8099 to see the review UI.

### 2. Add the feedback tool to your MCP server

Pick your stack below, copy the drop-in file, and wire it up. Each drop-in is
a single file with no framework dependencies beyond what you already have.

---

#### Python — FastMCP

Copy `drop-ins/python/feedback_tool.py` into your project.

```bash
uv add httpx    # or: pip install httpx
```

**Option A: One-liner registration**

```python
from mcp.server.fastmcp import FastMCP
from feedback_tool import register_feedback_tool

server = FastMCP("my-server")
register_feedback_tool(server, "my-server")
```

**Option B: Manual wiring** (if you want to customize the handler)

```python
from feedback_tool import FASTMCP_TOOL_KWARGS, send_feedback

@server.tool(**FASTMCP_TOOL_KWARGS)
async def feedback(
    what_i_needed: str,
    what_i_tried: str,
    gap_type: str = "other",
    suggestion: str = "",
    user_goal: str = "",
    resolution: str = "",
    tools_available: list[str] | None = None,
    agent_model: str = "",
    session_id: str = "",
) -> str:
    return await send_feedback(
        {
            "what_i_needed": what_i_needed,
            "what_i_tried": what_i_tried,
            "gap_type": gap_type,
            "suggestion": suggestion,
            "user_goal": user_goal,
            "resolution": resolution,
            "tools_available": tools_available or [],
            "agent_model": agent_model,
            "session_id": session_id,
        },
        server_name="my-server",
    )
```

---

#### Python — Django MCP (e.g. aicostmanager-style)

Copy `drop-ins/python/feedback_tool.py` into your tools directory.

```bash
uv add httpx    # or: pip install httpx
```

If your server uses a `@mcp_tool` decorator with `(credential, arguments)`
handler signatures (like aicostmanager), wire it up like this:

```python
# my_mcp_app/tools/feedback.py
from mcp.tools.registry import mcp_tool
from feedback_tool import TOOL_NAME, TOOL_DESCRIPTION, TOOL_INPUT_SCHEMA, send_feedback_sync

@mcp_tool(
    name=TOOL_NAME,
    description=TOOL_DESCRIPTION,
    input_schema=TOOL_INPUT_SCHEMA,
)
def feedback(credential, arguments):
    return send_feedback_sync(arguments, server_name="my-server")
```

Then add `"feedback"` to your tool modules list so the registry picks it up.

> **Note:** Django MCP servers typically run synchronously. The drop-in
> provides `send_feedback_sync()` for this — it uses `httpx.Client` instead
> of `httpx.AsyncClient`.

---

#### Python — Raw `mcp` SDK

Copy `drop-ins/python/feedback_tool.py` into your project.

```python
from feedback_tool import get_tool_definition, send_feedback

# In your list_tools handler:
tools.append(get_tool_definition())

# In your call_tool handler:
if name == "feedback":
    result = await send_feedback(arguments, server_name="my-server")
```

---

#### TypeScript — `@modelcontextprotocol/sdk`

Copy `drop-ins/typescript/feedback-tool.ts` into your project. No extra
dependencies — uses the built-in `fetch` API (Node 18+).

**Option A: One-liner**

```typescript
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { registerFeedbackTool } from "./feedback-tool.js";

const server = new McpServer({ name: "my-server", version: "1.0.0" });
registerFeedbackTool(server, "my-server");
```

**Option B: Manual**

```typescript
import { TOOL_NAME, TOOL_DESCRIPTION, TOOL_INPUT_SCHEMA, sendFeedback } from "./feedback-tool.js";

server.tool(TOOL_NAME, TOOL_DESCRIPTION, TOOL_INPUT_SCHEMA, async (args) => {
  const message = await sendFeedback(args, "my-server");
  return { content: [{ type: "text", text: message }] };
});
```

---

#### Go — `mcp-go`

Copy `drop-ins/go/feedback_tool.go` into your project. Only depends on
`github.com/mark3labs/mcp-go` and the standard library.

```go
import "your-project/feedback"

s := server.NewMCPServer("my-server", "1.0.0")
feedback.RegisterFeedbackTool(s, "my-server")
```

---

#### Rust

Copy `drop-ins/rust/feedback_tool.rs` into your project.

```toml
# Cargo.toml
[dependencies]
reqwest = { version = "0.12", features = ["json"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
```

The Rust MCP ecosystem is still solidifying. The drop-in provides the payload
types, HTTP submission, and JSON schema constants. Wire `send_feedback()` and
`tool_input_schema()` into your framework's tool registration:

```rust
use feedback_tool::{payload_from_args, send_feedback, TOOL_NAME, TOOL_DESCRIPTION};

// In your tool handler:
let payload = payload_from_args(&args, "my-server");
let message = send_feedback(&payload).await;
```

---

### 3. Test it

Use your MCP server via Claude Desktop, Cursor, Claude Code, etc. Ask the
agent to do something the server can't quite handle. Check if the agent calls
the feedback tool. Browse to http://localhost:8099 to see what it reported.

## What We Capture (and Why)

Every feedback item stores these fields. The schema is designed so a future
analysis layer can look at your GitHub repo and make intelligent suggestions
about what to build, fix, or change.

| Field | Required | Why It Matters |
|---|---|---|
| `what_i_needed` | Yes | The core signal — what capability was missing |
| `what_i_tried` | Yes | Shows the agent's thought process and what exists but fell short |
| `gap_type` | Yes | Categorizes gaps for pattern detection: `missing_tool`, `incomplete_results`, `missing_parameter`, `wrong_format`, `other` |
| `suggestion` | No | The agent's proposed fix — tool signature, parameter, behavior change. Often surprisingly specific. |
| `user_goal` | No | The real-world task that surfaced the gap. Helps prioritize by user impact. |
| `resolution` | No | Did this gap `blocked` the user entirely, was it `worked_around`, or `partial`? Drives severity ranking. |
| `tools_available` | No | What tools the agent could see. Critical for distinguishing "didn't find it" from "it doesn't exist." |
| `agent_model` | No | Which model reported the gap. Helps separate model-specific confusion from real server gaps. |
| `session_id` | No | Groups feedback from the same conversation. Reveals multi-step workflow failures. |

### Notes (append-only)

Notes are stored in a separate table and are **append-only** — you can never
lose a note by accident. Each note gets a timestamp. This matters because
notes are where you annotate feedback with your own context ("this is the
same issue as #42", "blocked by upstream API", "shipped in v2.1") and that
annotation history is valuable for the future analysis layer.

## Configuration

Both the drop-in and sidecar read from environment variables:

| Variable | Default | Description |
|---|---|---|
| `FEEDBACK_SIDECAR_URL` | `http://localhost:8099` | Where the drop-in sends feedback |
| `FEEDBACK_API_KEY` | (none) | Optional shared secret for auth |
| `FEEDBACK_DB_PATH` | `./feedback.db` | SQLite path for sidecar |
| `FEEDBACK_PORT` | `8099` | Port when running `uv run server.py` directly |

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/feedback` | Submit feedback (called by drop-ins) |
| `GET` | `/api/feedback` | List feedback (filterable by `server_name`, `gap_type`, `reviewed`, `resolution`, `session_id`) |
| `GET` | `/api/feedback/{id}` | Single feedback item with full notes |
| `PATCH` | `/api/feedback/{id}` | Toggle reviewed status |
| `POST` | `/api/feedback/{id}/notes` | Add a note (append-only) |
| `GET` | `/api/stats` | Counts by server, gap type, resolution |
| `GET` | `/` | Review UI |

## Adding a Drop-in for a New Language

The sidecar API is the stable contract. Any language can participate by
implementing a file that:

1. Defines the tool schema (name, description, input properties)
2. POSTs a JSON payload to `{SIDECAR_URL}/api/feedback`
3. Provides a framework-specific registration helper

The payload shape:

```json
{
  "server_name": "my-server",
  "what_i_needed": "...",
  "what_i_tried": "...",
  "gap_type": "missing_tool",
  "suggestion": "...",
  "user_goal": "...",
  "resolution": "blocked",
  "tools_available": ["tool_a", "tool_b"],
  "agent_model": "claude-sonnet-4-20250514",
  "session_id": "abc-123"
}
```

Only `what_i_needed`, `what_i_tried`, and `gap_type` are required. Everything
else has sensible defaults.

If you build a drop-in for a new stack, open a PR. The pattern is: one file,
zero extra dependencies beyond the MCP SDK for that language, a
`registerFeedbackTool()` one-liner for the most popular framework.

## Roadmap

- [x] Feedback capture and review UI
- [x] Drop-ins for Python, TypeScript, Go, Rust
- [x] Append-only notes with timestamps
- [ ] Feedback deduplication and clustering (group similar reports)
- [ ] GitHub repo integration (connect feedback to your codebase)
- [ ] LLM-powered analysis ("based on 23 reports, you should add a `date_range` param to `get_costs`")
- [ ] Auto-drafted PRs from feedback patterns
- [ ] Webhook notifications for new feedback
- [ ] Export to CSV/JSON for external analysis

## What We're Testing

1. **Do agents actually call the feedback tool?** The tool description is
   crafted to trigger when agents hit dead ends — does it work in practice?

2. **Is the feedback useful?** Are agents specific enough about what they
   needed? Are the suggestions actionable?

3. **What patterns emerge?** Missing tools, incomplete data, wrong formats —
   what's the distribution, and does it match what developers would prioritize?
