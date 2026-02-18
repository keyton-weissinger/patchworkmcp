# PatchworkMCP recipes using Astral's `uv` Python environment

# Development server (port 8099)
dev:
    uv run server.py

# Development server with auto-reload
dev-reload:
    uv run uvicorn server:app --host 0.0.0.0 --port 8099 --reload

# Run on a custom port
dev-port port:
    FEEDBACK_PORT={{port}} uv run server.py

# Install dependencies
sync:
    uv sync

# Reset the feedback database (destructive!)
db-reset:
    rm -f feedback.db
    @echo "Database removed. It will be recreated on next server start."

# Show feedback stats
stats:
    @curl -s http://localhost:8099/api/stats | python3 -m json.tool

# Show unreviewed feedback count
unreviewed:
    @curl -s http://localhost:8099/api/stats | python3 -c "import sys,json; s=json.load(sys.stdin); print(f'{s[\"unreviewed\"]} unreviewed of {s[\"total\"]} total')"

# Export all feedback as JSON
export file="feedback_export.json":
    @curl -s 'http://localhost:8099/api/feedback?limit=200' | python3 -m json.tool > {{file}}
    @echo "Exported to {{file}}"
