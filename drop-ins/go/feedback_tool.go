// PatchworkMCP — Drop-in feedback tool for Go MCP servers.
//
// Copy this file into your project. Works with:
//   - github.com/mark3labs/mcp-go  → RegisterFeedbackTool(server, "my-server")
//   - Manual registration          → NewFeedbackTool(), NewFeedbackHandler()
//
// No extra dependencies beyond mcp-go and the standard library.
//
// Configuration via environment:
//   FEEDBACK_SIDECAR_URL  - default: http://localhost:8099
//   FEEDBACK_API_KEY      - optional shared secret

package feedback

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

var (
	sidecarURL = getEnv("FEEDBACK_SIDECAR_URL", "http://localhost:8099")
	apiKey     = os.Getenv("FEEDBACK_API_KEY")
)

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// ── Tool Schema ─────────────────────────────────────────────────────────────

const ToolName = "feedback"

const ToolDescription = "Report when you cannot find what you need or when available tools don't " +
	"fully address the task. This feedback directly improves this server. " +
	"Call this tool whenever: " +
	"(1) you looked for a tool or resource that doesn't exist, " +
	"(2) a tool returned incomplete or unhelpful results, " +
	"(3) you had to work around a limitation or approximate an answer, " +
	"(4) a new tool or parameter would have made the task easier. " +
	"If you could not fully satisfy the user's request with the available " +
	"tools, call this BEFORE giving your final response."

// NewFeedbackTool returns the MCP tool definition for registration.
func NewFeedbackTool() mcp.Tool {
	return mcp.NewTool(ToolName,
		mcp.WithDescription(ToolDescription),
		mcp.WithString("what_i_needed",
			mcp.Required(),
			mcp.Description("What capability, data, or tool were you looking for?"),
		),
		mcp.WithString("what_i_tried",
			mcp.Required(),
			mcp.Description("What tools or approaches did you try? Include tool names and brief results."),
		),
		mcp.WithString("gap_type",
			mcp.Required(),
			mcp.Description("The category of gap: missing_tool, incomplete_results, missing_parameter, wrong_format, other"),
		),
		mcp.WithString("suggestion",
			mcp.Description("Your idea for what would have helped — inputs, outputs, behavior."),
		),
		mcp.WithString("user_goal",
			mcp.Description("The user's original request that led to discovering this gap."),
		),
		mcp.WithString("resolution",
			mcp.Description("What happened after the gap: blocked, worked_around, partial"),
		),
		mcp.WithString("agent_model",
			mcp.Description("Your model identifier, if known."),
		),
		mcp.WithString("session_id",
			mcp.Description("An identifier for the current conversation or session."),
		),
		mcp.WithString("client_type",
			mcp.Description("The MCP client in use, if known (e.g. 'claude-desktop', 'cursor', 'claude-code')."),
		),
		// Note: tools_available is sent as a comma-separated string in the Go
		// drop-in since mcp-go doesn't have WithArray. The sidecar accepts both
		// array and string formats.
		mcp.WithString("tools_available",
			mcp.Description("Comma-separated list of tool names you considered or tried."),
		),
	)
}

// ── Feedback Submission ─────────────────────────────────────────────────────

type feedbackPayload struct {
	ServerName   string   `json:"server_name"`
	WhatINeeded  string   `json:"what_i_needed"`
	WhatITried   string   `json:"what_i_tried"`
	GapType      string   `json:"gap_type"`
	Suggestion   string   `json:"suggestion"`
	UserGoal     string   `json:"user_goal"`
	Resolution   string   `json:"resolution"`
	AgentModel   string   `json:"agent_model"`
	SessionID    string   `json:"session_id"`
	ClientType   string   `json:"client_type"`
	ToolsAvail   []string `json:"tools_available"`
}

func getString(args map[string]any, key string) string {
	if v, ok := args[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// Options configures the feedback tool's sidecar connection.
// Pass to RegisterFeedbackTool or SendFeedback to override env vars.
type Options struct {
	// SidecarURL overrides FEEDBACK_SIDECAR_URL.
	SidecarURL string
	// APIKey overrides FEEDBACK_API_KEY.
	APIKey string
}

func (o *Options) url() string {
	if o != nil && o.SidecarURL != "" {
		return o.SidecarURL
	}
	return sidecarURL
}

func (o *Options) key() string {
	if o != nil && o.APIKey != "" {
		return o.APIKey
	}
	return apiKey
}

// SendFeedback posts feedback to the sidecar. Best-effort, non-blocking on
// failure. Returns a message suitable as the tool response.
// Pass nil for opts to use environment variable defaults.
func SendFeedback(ctx context.Context, args map[string]any, serverName string, opts *Options) string {
	// Parse tools_available — accept comma-separated string or []any
	var tools []string
	switch v := args["tools_available"].(type) {
	case string:
		if v != "" {
			for _, t := range bytes.Split([]byte(v), []byte(",")) {
				tools = append(tools, string(bytes.TrimSpace(t)))
			}
		}
	case []any:
		for _, t := range v {
			if s, ok := t.(string); ok {
				tools = append(tools, s)
			}
		}
	}

	payload := feedbackPayload{
		ServerName:  serverName,
		WhatINeeded: getString(args, "what_i_needed"),
		WhatITried:  getString(args, "what_i_tried"),
		GapType:     getString(args, "gap_type"),
		Suggestion:  getString(args, "suggestion"),
		UserGoal:    getString(args, "user_goal"),
		Resolution:  getString(args, "resolution"),
		AgentModel:  getString(args, "agent_model"),
		SessionID:   getString(args, "session_id"),
		ClientType:  getString(args, "client_type"),
		ToolsAvail:  tools,
	}
	if payload.GapType == "" {
		payload.GapType = "other"
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return "Feedback noted (encoding error)."
	}

	client := &http.Client{Timeout: 5 * time.Second}
	req, err := http.NewRequestWithContext(ctx, "POST", opts.url()+"/api/feedback", bytes.NewReader(body))
	if err != nil {
		return "Feedback noted (sidecar unavailable, but your input is appreciated)."
	}
	req.Header.Set("Content-Type", "application/json")
	if k := opts.key(); k != "" {
		req.Header.Set("Authorization", "Bearer "+k)
	}

	resp, err := client.Do(req)
	if err != nil {
		return "Feedback noted (sidecar unavailable, but your input is appreciated)."
	}
	defer resp.Body.Close()

	if resp.StatusCode == 201 {
		return "Thank you. Your feedback has been recorded and will be used to improve this server's capabilities."
	}
	return fmt.Sprintf("Feedback noted (sidecar returned %d).", resp.StatusCode)
}

// ── Handler & Registration ──────────────────────────────────────────────────

// NewFeedbackHandler returns a tool handler function bound to a server name.
// Pass nil for opts to use environment variable defaults.
func NewFeedbackHandler(serverName string, opts *Options) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args := req.GetArguments()
		msg := SendFeedback(ctx, args, serverName, opts)
		return mcp.NewToolResultText(msg), nil
	}
}

// RegisterFeedbackTool is a one-liner to add the feedback tool to an MCP server.
// Pass nil for opts to use environment variable defaults.
//
//	s := server.NewMCPServer("my-server", "1.0.0")
//	feedback.RegisterFeedbackTool(s, "my-server", nil)
//
//	// Or point at a specific sidecar:
//	feedback.RegisterFeedbackTool(s, "my-server", &feedback.Options{
//	    SidecarURL: "https://feedback.prod.example.com",
//	})
func RegisterFeedbackTool(s *server.MCPServer, serverName string, opts *Options) {
	s.AddTool(NewFeedbackTool(), NewFeedbackHandler(serverName, opts))
}
