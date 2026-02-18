"""
PatchworkMCP — Sidecar Service

Accepts feedback from MCP server drop-ins, stores it in SQLite, and serves
a review UI for browsing what agents report.

Run:
    uv run server.py
    # or: uvicorn server:app --port 8099

Configure:
    FEEDBACK_DB_PATH  - default: ./feedback.db
    FEEDBACK_API_KEY  - optional shared secret (must match drop-in)
    FEEDBACK_PORT     - default: 8099 (only used with `uv run server.py`)
"""

import os
import uuid
import json
import sqlite3
from datetime import datetime, timezone
from contextlib import asynccontextmanager, contextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


# ── Config ───────────────────────────────────────────────────────────────────

DB_PATH = os.environ.get("FEEDBACK_DB_PATH", "feedback.db")
API_KEY = os.environ.get("FEEDBACK_API_KEY", "")


# ── Database ─────────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY,
                server_name TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                what_i_needed TEXT NOT NULL,
                what_i_tried TEXT NOT NULL,
                gap_type TEXT NOT NULL,
                suggestion TEXT DEFAULT '',
                user_goal TEXT DEFAULT '',
                resolution TEXT DEFAULT '',
                agent_model TEXT DEFAULT '',
                tools_available TEXT DEFAULT '[]',
                session_id TEXT DEFAULT '',
                reviewed INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback_notes (
                id TEXT PRIMARY KEY,
                feedback_id TEXT NOT NULL REFERENCES feedback(id),
                timestamp TEXT NOT NULL,
                content TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_feedback_server
            ON feedback(server_name)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_feedback_timestamp
            ON feedback(timestamp DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_feedback_gap_type
            ON feedback(gap_type)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_notes_feedback_id
            ON feedback_notes(feedback_id)
        """)


# ── App ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(
    title="PatchworkMCP",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth ─────────────────────────────────────────────────────────────────────

def check_auth(authorization: Optional[str] = Header(None)):
    if not API_KEY:
        return
    if not authorization or authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Models ───────────────────────────────────────────────────────────────────

class FeedbackIn(BaseModel):
    server_name: str = "unknown"
    what_i_needed: str
    what_i_tried: str
    gap_type: str = "other"
    suggestion: str = ""
    user_goal: str = ""
    resolution: str = ""
    agent_model: str = ""
    tools_available: list[str] = Field(default_factory=list)
    session_id: str = ""


class ReviewUpdate(BaseModel):
    reviewed: bool = True


class NoteIn(BaseModel):
    content: str


# ── Helpers ──────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["reviewed"] = bool(d["reviewed"])
    if "tools_available" in d:
        try:
            d["tools_available"] = json.loads(d["tools_available"])
        except (json.JSONDecodeError, TypeError):
            d["tools_available"] = []
    return d


def _attach_notes(conn, items: list[dict]) -> list[dict]:
    """Fetch notes for a batch of feedback items and attach them."""
    if not items:
        return items
    ids = [item["id"] for item in items]
    placeholders = ",".join("?" * len(ids))
    note_rows = conn.execute(
        f"SELECT * FROM feedback_notes WHERE feedback_id IN ({placeholders}) "
        "ORDER BY timestamp ASC",
        ids,
    ).fetchall()

    notes_by_id: dict[str, list] = {}
    for n in note_rows:
        notes_by_id.setdefault(n["feedback_id"], []).append({
            "id": n["id"],
            "timestamp": n["timestamp"],
            "content": n["content"],
        })
    for item in items:
        item["notes"] = notes_by_id.get(item["id"], [])
    return items


# ── Routes ───────────────────────────────────────────────────────────────────

@app.post("/api/feedback", status_code=201)
async def create_feedback(
    feedback: FeedbackIn,
    authorization: Optional[str] = Header(None),
):
    check_auth(authorization)

    row_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO feedback
                (id, server_name, timestamp, what_i_needed, what_i_tried,
                 gap_type, suggestion, user_goal, resolution, agent_model,
                 tools_available, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                feedback.server_name,
                now,
                feedback.what_i_needed,
                feedback.what_i_tried,
                feedback.gap_type,
                feedback.suggestion,
                feedback.user_goal,
                feedback.resolution,
                feedback.agent_model,
                json.dumps(feedback.tools_available),
                feedback.session_id,
            ),
        )

    return {"id": row_id, "status": "recorded"}


@app.get("/api/feedback")
async def list_feedback(
    server_name: Optional[str] = Query(None),
    gap_type: Optional[str] = Query(None),
    reviewed: Optional[bool] = Query(None),
    resolution: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    with get_db() as conn:
        query = "SELECT * FROM feedback WHERE 1=1"
        params: list = []

        if server_name:
            query += " AND server_name = ?"
            params.append(server_name)
        if gap_type:
            query += " AND gap_type = ?"
            params.append(gap_type)
        if reviewed is not None:
            query += " AND reviewed = ?"
            params.append(int(reviewed))
        if resolution:
            query += " AND resolution = ?"
            params.append(resolution)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        items = [_row_to_dict(r) for r in rows]
        return _attach_notes(conn, items)


@app.get("/api/feedback/{feedback_id}")
async def get_feedback(feedback_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM feedback WHERE id = ?", (feedback_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        items = _attach_notes(conn, [_row_to_dict(row)])
        return items[0]


@app.patch("/api/feedback/{feedback_id}")
async def update_feedback(feedback_id: str, update: ReviewUpdate):
    with get_db() as conn:
        result = conn.execute(
            "UPDATE feedback SET reviewed = ? WHERE id = ?",
            (int(update.reviewed), feedback_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Not found")
    return {"status": "updated"}


@app.post("/api/feedback/{feedback_id}/notes", status_code=201)
async def add_note(feedback_id: str, note: NoteIn):
    with get_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM feedback WHERE id = ?", (feedback_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Feedback item not found")

        note_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO feedback_notes (id, feedback_id, timestamp, content) "
            "VALUES (?, ?, ?, ?)",
            (note_id, feedback_id, now, note.content),
        )
    return {"id": note_id, "status": "recorded"}


@app.get("/api/stats")
async def stats():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM feedback").fetchone()["c"]
        unreviewed = conn.execute(
            "SELECT COUNT(*) as c FROM feedback WHERE reviewed = 0"
        ).fetchone()["c"]
        note_count = conn.execute(
            "SELECT COUNT(*) as c FROM feedback_notes"
        ).fetchone()["c"]

        by_server = conn.execute("""
            SELECT server_name, COUNT(*) as count
            FROM feedback GROUP BY server_name ORDER BY count DESC
        """).fetchall()

        by_type = conn.execute("""
            SELECT gap_type, COUNT(*) as count
            FROM feedback GROUP BY gap_type ORDER BY count DESC
        """).fetchall()

        by_resolution = conn.execute("""
            SELECT resolution, COUNT(*) as count
            FROM feedback WHERE resolution != '' GROUP BY resolution ORDER BY count DESC
        """).fetchall()

    return {
        "total": total,
        "unreviewed": unreviewed,
        "note_count": note_count,
        "by_server": [dict(r) for r in by_server],
        "by_gap_type": [dict(r) for r in by_type],
        "by_resolution": [dict(r) for r in by_resolution],
    }


# ── Review UI ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def review_ui():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PatchworkMCP</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0; padding: 2rem; }
  h1 { font-size: 1.4rem; margin-bottom: 0.5rem; color: #fff; }
  .subtitle { color: #888; margin-bottom: 2rem; font-size: 0.9rem; }
  .stats { display: flex; gap: 1.5rem; margin-bottom: 2rem; flex-wrap: wrap; }
  .stat { background: #161616; border: 1px solid #2a2a2a; border-radius: 8px; padding: 1rem 1.5rem; }
  .stat-num { font-size: 1.8rem; font-weight: 700; color: #fff; }
  .stat-label { font-size: 0.8rem; color: #888; margin-top: 0.25rem; }
  .filters { display: flex; gap: 0.75rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
  select, button { background: #161616; border: 1px solid #2a2a2a; color: #e0e0e0; padding: 0.5rem 0.75rem; border-radius: 6px; font-size: 0.85rem; cursor: pointer; }
  button:hover { background: #222; }
  .btn-active { background: #2a4a2a; border-color: #4a8a4a; }
  .card { background: #161616; border: 1px solid #2a2a2a; border-radius: 8px; padding: 1.25rem; margin-bottom: 0.75rem; }
  .card.reviewed { opacity: 0.5; }
  .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; }
  .badges { display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap; }
  .server-badge { background: #1a2a3a; color: #6ab0f3; padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.75rem; }
  .gap-badge { padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.75rem; }
  .gap-badge.missing_tool { background: #3a1a1a; color: #f36a6a; }
  .gap-badge.incomplete_results { background: #3a2a1a; color: #f3b06a; }
  .gap-badge.missing_parameter { background: #3a3a1a; color: #e8f36a; }
  .gap-badge.wrong_format { background: #1a3a3a; color: #6af3e8; }
  .gap-badge.other { background: #2a2a2a; color: #aaa; }
  .resolution-badge { padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.75rem; }
  .resolution-badge.blocked { background: #3a1a1a; color: #f36a6a; }
  .resolution-badge.worked_around { background: #2a3a1a; color: #b0f36a; }
  .resolution-badge.partial { background: #3a2a1a; color: #f3b06a; }
  .field { margin-bottom: 0.6rem; }
  .field-label { font-size: 0.75rem; color: #888; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.15rem; }
  .field-value { font-size: 0.9rem; line-height: 1.4; }
  .meta-row { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 0.6rem; }
  .meta-item { font-size: 0.75rem; color: #666; }
  .meta-item span { color: #999; }
  .tools-list { display: flex; gap: 0.3rem; flex-wrap: wrap; margin-top: 0.2rem; }
  .tool-chip { background: #1a1a2a; color: #8888cc; padding: 0.1rem 0.5rem; border-radius: 3px; font-size: 0.75rem; font-family: monospace; }
  .timestamp { font-size: 0.75rem; color: #666; }
  .notes-section { margin-top: 0.75rem; border-top: 1px solid #222; padding-top: 0.75rem; }
  .notes-header { font-size: 0.75rem; color: #888; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.4rem; }
  .note-item { background: #111; border-left: 2px solid #333; padding: 0.4rem 0.6rem; margin-bottom: 0.4rem; border-radius: 0 4px 4px 0; }
  .note-time { font-size: 0.7rem; color: #555; }
  .note-content { font-size: 0.85rem; line-height: 1.3; margin-top: 0.15rem; }
  .card-actions { margin-top: 0.75rem; display: flex; gap: 0.5rem; }
  .card-actions button { font-size: 0.8rem; padding: 0.35rem 0.75rem; }
  .empty { text-align: center; color: #666; padding: 3rem; }
</style>
</head>
<body>
<h1>PatchworkMCP</h1>
<p class="subtitle">What are agents trying to do that they can't?</p>

<div class="stats" id="stats"></div>

<div class="filters">
  <select id="filterServer"><option value="">All servers</option></select>
  <select id="filterType">
    <option value="">All types</option>
    <option value="missing_tool">Missing tool</option>
    <option value="incomplete_results">Incomplete results</option>
    <option value="missing_parameter">Missing parameter</option>
    <option value="wrong_format">Wrong format</option>
    <option value="other">Other</option>
  </select>
  <select id="filterResolution">
    <option value="">All resolutions</option>
    <option value="blocked">Blocked</option>
    <option value="worked_around">Worked around</option>
    <option value="partial">Partial</option>
  </select>
  <button id="filterReviewed" onclick="toggleReviewedFilter()">Hide reviewed</button>
  <button onclick="loadAll()">Refresh</button>
</div>

<div id="feed"></div>

<script>
let showReviewed = true;
let currentData = [];

async function loadStats() {
  const r = await fetch('/api/stats');
  const s = await r.json();
  document.getElementById('stats').innerHTML = `
    <div class="stat"><div class="stat-num">${s.total}</div><div class="stat-label">Total feedback</div></div>
    <div class="stat"><div class="stat-num">${s.unreviewed}</div><div class="stat-label">Unreviewed</div></div>
    <div class="stat"><div class="stat-num">${s.note_count}</div><div class="stat-label">Notes</div></div>
    <div class="stat"><div class="stat-num">${s.by_server.length}</div><div class="stat-label">Servers</div></div>
  `;
  const sel = document.getElementById('filterServer');
  const current = sel.value;
  sel.innerHTML = '<option value="">All servers</option>';
  s.by_server.forEach(sv => {
    sel.innerHTML += `<option value="${sv.server_name}">${sv.server_name} (${sv.count})</option>`;
  });
  sel.value = current;
}

async function loadFeedback() {
  const server = document.getElementById('filterServer').value;
  const type = document.getElementById('filterType').value;
  const resolution = document.getElementById('filterResolution').value;
  let url = '/api/feedback?limit=100';
  if (server) url += `&server_name=${encodeURIComponent(server)}`;
  if (type) url += `&gap_type=${encodeURIComponent(type)}`;
  if (resolution) url += `&resolution=${encodeURIComponent(resolution)}`;
  if (!showReviewed) url += `&reviewed=false`;

  const r = await fetch(url);
  currentData = await r.json();
  render();
}

function render() {
  const feed = document.getElementById('feed');
  if (!currentData.length) {
    feed.innerHTML = '<div class="empty">No feedback yet. Agents haven\\'t reported any gaps.</div>';
    return;
  }
  feed.innerHTML = currentData.map(f => `
    <div class="card ${f.reviewed ? 'reviewed' : ''}" id="card-${f.id}">
      <div class="card-header">
        <div class="badges">
          <span class="server-badge">${esc(f.server_name)}</span>
          <span class="gap-badge ${f.gap_type}">${f.gap_type.replace(/_/g, ' ')}</span>
          ${f.resolution ? `<span class="resolution-badge ${f.resolution}">${f.resolution.replace(/_/g, ' ')}</span>` : ''}
        </div>
        <span class="timestamp">${new Date(f.timestamp).toLocaleString()}</span>
      </div>
      <div class="field">
        <div class="field-label">What they needed</div>
        <div class="field-value">${esc(f.what_i_needed)}</div>
      </div>
      <div class="field">
        <div class="field-label">What they tried</div>
        <div class="field-value">${esc(f.what_i_tried)}</div>
      </div>
      ${f.suggestion ? `<div class="field"><div class="field-label">Suggestion</div><div class="field-value">${esc(f.suggestion)}</div></div>` : ''}
      ${f.user_goal ? `<div class="field"><div class="field-label">User goal</div><div class="field-value">${esc(f.user_goal)}</div></div>` : ''}
      ${f.agent_model || f.session_id ? `
        <div class="meta-row">
          ${f.agent_model ? `<div class="meta-item">Model: <span>${esc(f.agent_model)}</span></div>` : ''}
          ${f.session_id ? `<div class="meta-item">Session: <span>${esc(f.session_id)}</span></div>` : ''}
        </div>
      ` : ''}
      ${f.tools_available && f.tools_available.length ? `
        <div class="field">
          <div class="field-label">Tools available</div>
          <div class="tools-list">${f.tools_available.map(t => `<span class="tool-chip">${esc(t)}</span>`).join('')}</div>
        </div>
      ` : ''}
      ${renderNotes(f)}
      <div class="card-actions">
        ${!f.reviewed ? `<button onclick="markReviewed('${f.id}')">Mark reviewed</button>` : `<button onclick="markUnreviewed('${f.id}')">Unmark</button>`}
        <button onclick="addNote('${f.id}')">Add note</button>
      </div>
    </div>
  `).join('');
}

function renderNotes(f) {
  if (!f.notes || !f.notes.length) return '';
  const items = f.notes.map(n => `
    <div class="note-item">
      <div class="note-time">${new Date(n.timestamp).toLocaleString()}</div>
      <div class="note-content">${esc(n.content)}</div>
    </div>
  `).join('');
  return `
    <div class="notes-section">
      <div class="notes-header">Notes (${f.notes.length})</div>
      ${items}
    </div>
  `;
}

function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

async function markReviewed(id) {
  await fetch(`/api/feedback/${id}`, { method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify({reviewed: true}) });
  loadAll();
}

async function markUnreviewed(id) {
  await fetch(`/api/feedback/${id}`, { method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify({reviewed: false}) });
  loadAll();
}

async function addNote(id) {
  const text = prompt('Add a note (this will be appended to the history):');
  if (!text) return;
  await fetch(`/api/feedback/${id}/notes`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({content: text}),
  });
  loadAll();
}

function toggleReviewedFilter() {
  showReviewed = !showReviewed;
  document.getElementById('filterReviewed').classList.toggle('btn-active', !showReviewed);
  document.getElementById('filterReviewed').textContent = showReviewed ? 'Hide reviewed' : 'Showing unreviewed';
  loadFeedback();
}

function loadAll() { loadStats(); loadFeedback(); }

document.getElementById('filterServer').addEventListener('change', loadFeedback);
document.getElementById('filterType').addEventListener('change', loadFeedback);
document.getElementById('filterResolution').addEventListener('change', loadFeedback);
loadAll();
</script>
</body>
</html>"""


# ── Run directly ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("FEEDBACK_PORT", "8099"))
    uvicorn.run(app, host="0.0.0.0", port=port)
