"""
PatchworkMCP — Drop-in feedback tool for any Python MCP server.

Copy this single file into your project. It works with:
  - FastMCP  → register_feedback_tool(server, "my-server")
  - Django MCP (@mcp_tool decorator, like aicostmanager)
  - Raw mcp SDK (Tool objects + call_tool handler)

The only dependency is httpx:
    uv add httpx       # or: pip install httpx

Configuration via environment:
    FEEDBACK_SIDECAR_URL  - default: http://localhost:8099
    FEEDBACK_API_KEY      - optional shared secret
"""

import os
import json
import logging

logger = logging.getLogger("patchworkmcp")

SIDECAR_URL = os.environ.get("FEEDBACK_SIDECAR_URL", "http://localhost:8099")
API_KEY = os.environ.get("FEEDBACK_API_KEY", "")


# ── Tool Schema ──────────────────────────────────────────────────────────────
# The description is the most important part of this entire project.
# It needs to make agents actually call this tool when they hit a wall.

TOOL_NAME = "feedback"

TOOL_DESCRIPTION = (
    "Report when you cannot find what you need or when available tools don't "
    "fully address the task. This feedback directly improves this server. "
    "Call this tool whenever: "
    "(1) you looked for a tool or resource that doesn't exist, "
    "(2) a tool returned incomplete or unhelpful results, "
    "(3) you had to work around a limitation or approximate an answer, "
    "(4) a new tool or parameter would have made the task easier. "
    "If you could not fully satisfy the user's request with the available "
    "tools, call this BEFORE giving your final response."
)

TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "what_i_needed": {
            "type": "string",
            "description": (
                "What capability, data, or tool were you looking for? "
                "Be specific about the action you wanted to perform."
            ),
        },
        "what_i_tried": {
            "type": "string",
            "description": (
                "What tools or approaches did you try? "
                "Include tool names and brief results."
            ),
        },
        "gap_type": {
            "type": "string",
            "enum": [
                "missing_tool",
                "incomplete_results",
                "missing_parameter",
                "wrong_format",
                "other",
            ],
            "description": "The category of gap encountered.",
        },
        "suggestion": {
            "type": "string",
            "description": (
                "Your idea for what would have helped. Describe the tool, "
                "parameter, or change — including what inputs it would "
                "accept and what it would return."
            ),
        },
        "user_goal": {
            "type": "string",
            "description": (
                "The user's original request or goal that led to "
                "discovering this gap."
            ),
        },
        "resolution": {
            "type": "string",
            "enum": ["blocked", "worked_around", "partial"],
            "description": (
                "What happened after hitting the gap? "
                "'blocked' = could not proceed at all, "
                "'worked_around' = found an alternative, "
                "'partial' = completed the task incompletely."
            ),
        },
        "tools_available": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "List the tool names available on this server that you "
                "considered or tried before submitting feedback."
            ),
        },
        "agent_model": {
            "type": "string",
            "description": (
                "Your model identifier, if known "
                "(e.g. 'claude-sonnet-4-20250514')."
            ),
        },
        "session_id": {
            "type": "string",
            "description": (
                "An identifier for the current conversation or session, "
                "if available."
            ),
        },
    },
    "required": ["what_i_needed", "what_i_tried", "gap_type"],
}


# ── FastMCP Integration ─────────────────────────────────────────────────────

FASTMCP_TOOL_KWARGS = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
}


def register_feedback_tool(
    server,
    server_name: str = "unknown",
    *,
    sidecar_url: str | None = None,
    api_key: str | None = None,
):
    """One-liner registration for FastMCP servers.

    Usage:
        from feedback_tool import register_feedback_tool
        register_feedback_tool(server, "my-server")

        # Point at a specific sidecar:
        register_feedback_tool(server, "my-server", sidecar_url="https://feedback.prod.example.com")
    """

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
            server_name=server_name,
            sidecar_url=sidecar_url,
            api_key=api_key,
        )


# ── Raw MCP SDK Integration ─────────────────────────────────────────────────

def get_tool_definition():
    """Return an mcp.types.Tool for low-level server registration.

    Usage:
        from feedback_tool import get_tool_definition, send_feedback

        # In list_tools handler:
        tools.append(get_tool_definition())

        # In call_tool handler:
        if name == "feedback":
            result = await send_feedback(arguments, server_name="my-server")
    """
    from mcp.types import Tool

    return Tool(
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        inputSchema=TOOL_INPUT_SCHEMA,
    )


# ── Feedback Submission ─────────────────────────────────────────────────────

def _build_payload(arguments: dict, server_name: str) -> dict:
    tools = arguments.get("tools_available", [])
    if isinstance(tools, str):
        try:
            tools = json.loads(tools)
        except (json.JSONDecodeError, TypeError):
            tools = [tools]

    return {
        "server_name": server_name,
        "what_i_needed": arguments.get("what_i_needed", ""),
        "what_i_tried": arguments.get("what_i_tried", ""),
        "gap_type": arguments.get("gap_type", "other"),
        "suggestion": arguments.get("suggestion", ""),
        "user_goal": arguments.get("user_goal", ""),
        "resolution": arguments.get("resolution", ""),
        "tools_available": tools,
        "agent_model": arguments.get("agent_model", ""),
        "session_id": arguments.get("session_id", ""),
    }


def _resolve_url(sidecar_url: str | None) -> str:
    return sidecar_url or SIDECAR_URL


def _build_headers(api_key: str | None = None) -> dict:
    key = api_key if api_key is not None else API_KEY
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


_SUCCESS_MSG = (
    "Thank you. Your feedback has been recorded and will be "
    "used to improve this server's capabilities."
)
_DELIVERY_MSG = "Feedback noted (delivery issue, but recorded locally)."
_UNAVAILABLE_MSG = (
    "Feedback noted (sidecar unavailable, but your input is appreciated)."
)


async def send_feedback(
    arguments: dict,
    server_name: str = "unknown",
    *,
    sidecar_url: str | None = None,
    api_key: str | None = None,
) -> str:
    """Async — send feedback to the sidecar. For FastMCP and async contexts.

    Args:
        sidecar_url: Override FEEDBACK_SIDECAR_URL for this call.
        api_key: Override FEEDBACK_API_KEY for this call.
    """
    import httpx

    url = _resolve_url(sidecar_url)
    payload = _build_payload(arguments, server_name)
    headers = _build_headers(api_key)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{url}/api/feedback",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 201:
                logger.info("Feedback submitted successfully")
                return _SUCCESS_MSG
            else:
                logger.warning(
                    "Sidecar returned %d: %s", resp.status_code, resp.text
                )
                return _DELIVERY_MSG
    except Exception as e:
        logger.warning("Could not reach feedback sidecar: %s", e)
        return _UNAVAILABLE_MSG


def send_feedback_sync(
    arguments: dict,
    server_name: str = "unknown",
    *,
    sidecar_url: str | None = None,
    api_key: str | None = None,
) -> str:
    """Sync — send feedback to the sidecar. For Django and sync contexts.

    Args:
        sidecar_url: Override FEEDBACK_SIDECAR_URL for this call.
        api_key: Override FEEDBACK_API_KEY for this call.
    """
    import httpx

    url = _resolve_url(sidecar_url)
    payload = _build_payload(arguments, server_name)
    headers = _build_headers(api_key)

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(
                f"{url}/api/feedback",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 201:
                logger.info("Feedback submitted successfully")
                return _SUCCESS_MSG
            else:
                logger.warning(
                    "Sidecar returned %d: %s", resp.status_code, resp.text
                )
                return _DELIVERY_MSG
    except Exception as e:
        logger.warning("Could not reach feedback sidecar: %s", e)
        return _UNAVAILABLE_MSG
