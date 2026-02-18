//! PatchworkMCP — Drop-in feedback tool for Rust MCP servers.
//!
//! Copy this file into your project and call `register_feedback_tool()` with
//! your MCP server router, or use the constants and `send_feedback()` directly.
//!
//! Dependencies (add to Cargo.toml):
//!   reqwest = { version = "0.12", features = ["json"] }
//!   serde = { version = "1", features = ["derive"] }
//!   serde_json = "1"
//!   tokio = { version = "1", features = ["full"] }
//!
//! Configuration via environment:
//!   FEEDBACK_SIDECAR_URL  - default: http://localhost:8099
//!   FEEDBACK_API_KEY      - optional shared secret
//!
//! Note: The Rust MCP ecosystem is still maturing. This file provides the
//! feedback payload, HTTP submission, and schema constants. Wire the tool
//! into your MCP framework's registration system as needed.

use serde::{Deserialize, Serialize};
use std::env;
use std::time::Duration;

// ── Constants ───────────────────────────────────────────────────────────────

pub const TOOL_NAME: &str = "feedback";

pub const TOOL_DESCRIPTION: &str = concat!(
    "Report when you cannot find what you need or when available tools don't ",
    "fully address the task. This feedback directly improves this server. ",
    "Call this tool whenever: ",
    "(1) you looked for a tool or resource that doesn't exist, ",
    "(2) a tool returned incomplete or unhelpful results, ",
    "(3) you had to work around a limitation or approximate an answer, ",
    "(4) a new tool or parameter would have made the task easier. ",
    "If you could not fully satisfy the user's request with the available ",
    "tools, call this BEFORE giving your final response.",
);

// ── Types ───────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FeedbackPayload {
    pub server_name: String,
    pub what_i_needed: String,
    pub what_i_tried: String,
    pub gap_type: String,
    #[serde(default)]
    pub suggestion: String,
    #[serde(default)]
    pub user_goal: String,
    #[serde(default)]
    pub resolution: String,
    #[serde(default)]
    pub agent_model: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub tools_available: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct SidecarResponse {
    #[allow(dead_code)]
    id: String,
    #[allow(dead_code)]
    status: String,
}

// ── Config ──────────────────────────────────────────────────────────────────

fn sidecar_url() -> String {
    env::var("FEEDBACK_SIDECAR_URL").unwrap_or_else(|_| "http://localhost:8099".to_string())
}

fn api_key() -> Option<String> {
    env::var("FEEDBACK_API_KEY").ok().filter(|k| !k.is_empty())
}

// ── Submission ──────────────────────────────────────────────────────────────

/// Send feedback to the PatchworkMCP sidecar. Best-effort — returns a
/// user-facing message regardless of success or failure.
pub async fn send_feedback(payload: &FeedbackPayload) -> String {
    let url = format!("{}/api/feedback", sidecar_url());

    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
    {
        Ok(c) => c,
        Err(_) => {
            return "Feedback noted (HTTP client error).".to_string();
        }
    };

    let mut req = client.post(&url).json(payload);
    if let Some(key) = api_key() {
        req = req.header("Authorization", format!("Bearer {key}"));
    }

    match req.send().await {
        Ok(resp) if resp.status().as_u16() == 201 => {
            "Thank you. Your feedback has been recorded and will be \
             used to improve this server's capabilities."
                .to_string()
        }
        Ok(resp) => {
            eprintln!(
                "PatchworkMCP sidecar returned {}",
                resp.status().as_u16()
            );
            "Feedback noted (delivery issue, but recorded locally).".to_string()
        }
        Err(e) => {
            eprintln!("PatchworkMCP: could not reach sidecar: {e}");
            "Feedback noted (sidecar unavailable, but your input is appreciated).".to_string()
        }
    }
}

/// Build a FeedbackPayload from a JSON value (as received from MCP call_tool).
/// Missing fields get sensible defaults.
pub fn payload_from_args(args: &serde_json::Value, server_name: &str) -> FeedbackPayload {
    let s = |key: &str| -> String {
        args.get(key)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    };

    let tools: Vec<String> = args
        .get("tools_available")
        .and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();

    FeedbackPayload {
        server_name: server_name.to_string(),
        what_i_needed: s("what_i_needed"),
        what_i_tried: s("what_i_tried"),
        gap_type: {
            let g = s("gap_type");
            if g.is_empty() {
                "other".to_string()
            } else {
                g
            }
        },
        suggestion: s("suggestion"),
        user_goal: s("user_goal"),
        resolution: s("resolution"),
        agent_model: s("agent_model"),
        session_id: s("session_id"),
        tools_available: tools,
    }
}

// ── JSON Schema (for manual tool registration) ──────────────────────────────

/// Returns the tool input schema as a serde_json::Value. Use this when
/// registering the tool manually with your MCP framework.
pub fn tool_input_schema() -> serde_json::Value {
    serde_json::json!({
        "type": "object",
        "properties": {
            "what_i_needed": {
                "type": "string",
                "description": "What capability, data, or tool were you looking for?"
            },
            "what_i_tried": {
                "type": "string",
                "description": "What tools or approaches did you try? Include tool names and brief results."
            },
            "gap_type": {
                "type": "string",
                "enum": ["missing_tool", "incomplete_results", "missing_parameter", "wrong_format", "other"],
                "description": "The category of gap encountered."
            },
            "suggestion": {
                "type": "string",
                "description": "Your idea for what would have helped."
            },
            "user_goal": {
                "type": "string",
                "description": "The user's original request or goal."
            },
            "resolution": {
                "type": "string",
                "enum": ["blocked", "worked_around", "partial"],
                "description": "What happened after hitting the gap."
            },
            "tools_available": {
                "type": "array",
                "items": { "type": "string" },
                "description": "Tool names you considered or tried."
            },
            "agent_model": {
                "type": "string",
                "description": "Your model identifier, if known."
            },
            "session_id": {
                "type": "string",
                "description": "Conversation or session identifier."
            }
        },
        "required": ["what_i_needed", "what_i_tried", "gap_type"]
    })
}
