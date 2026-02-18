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
import re
import uuid
import json
import base64
import sqlite3
from datetime import datetime, timezone
from contextlib import asynccontextmanager, contextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def _migrate_db():
    with get_db() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(feedback)").fetchall()}
        if "pr_url" not in cols:
            conn.execute("ALTER TABLE feedback ADD COLUMN pr_url TEXT DEFAULT ''")
        if "client_type" not in cols:
            conn.execute("ALTER TABLE feedback ADD COLUMN client_type TEXT DEFAULT ''")


# ── App ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _migrate_db()
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
    client_type: str = ""


class ReviewUpdate(BaseModel):
    reviewed: bool = True


class NoteIn(BaseModel):
    content: str


# ── Helpers ──────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["reviewed"] = bool(d["reviewed"])
    d.setdefault("pr_url", "")
    d.setdefault("client_type", "")
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


# ── Settings helpers ─────────────────────────────────────────────────────────

_DB_SETTINGS_KEYS = {"github_repo", "default_branch", "llm_provider", "llm_model"}
_ENV_KEYS = {"github_pat", "anthropic_api_key", "openai_api_key"}
_ALL_SETTINGS_KEYS = _DB_SETTINGS_KEYS | _ENV_KEYS

_PROVIDER_DEFAULTS = {
    "anthropic": "claude-opus-4-6",
    "openai": "GPT-5.2-Codex",
}

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), ".env")


def _mask(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def _read_env() -> dict[str, str]:
    """Read key=value pairs from .env file."""
    values: dict[str, str] = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip("\"'")
                if key in _ENV_KEYS:
                    values[key] = val
    except FileNotFoundError:
        pass
    return values


def _write_env(updates: dict[str, str]):
    """Merge updates into .env, preserving comments and unknown keys."""
    lines: list[str] = []
    seen: set[str] = set()
    try:
        with open(ENV_PATH) as f:
            for line in f:
                raw = line.rstrip("\n")
                stripped = raw.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.partition("=")[0].strip()
                    if key in updates:
                        seen.add(key)
                        if updates[key]:
                            lines.append(f"{key}={updates[key]}")
                        continue
                lines.append(raw)
    except FileNotFoundError:
        pass
    for key, val in updates.items():
        if key not in seen and val:
            lines.append(f"{key}={val}")
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


def _get_settings() -> dict[str, str]:
    """Read all settings from SQLite (prefs) + .env (secrets)."""
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    result = {r["key"]: r["value"] for r in rows}
    result.update(_read_env())
    return result


# ── GitHub Client ────────────────────────────────────────────────────────────

_SOURCE_EXTS = {".py", ".ts", ".js", ".go", ".rs", ".tsx", ".jsx", ".rb"}
_MCP_PATTERNS = ["tool", "server", "mcp", "handler", "schema", "resource", "prompt"]


def _score_file(path: str, server_name: str) -> int:
    lower = path.lower()
    ext = os.path.splitext(path)[1]
    if ext not in _SOURCE_EXTS:
        return -1
    # skip vendored / generated
    for skip in ("node_modules/", "vendor/", ".git/", "__pycache__/", "dist/", "build/"):
        if skip in lower:
            return -1
    score = 0
    for pat in _MCP_PATTERNS:
        if pat in lower:
            score += 10
    if server_name and server_name.lower().replace("-", "_") in lower.replace("-", "_"):
        score += 20
    if ext == ".py":
        score += 2
    elif ext in (".ts", ".tsx"):
        score += 2
    return score


class GitHubClient:
    def __init__(self, token: str, repo: str):
        self.repo = repo  # "owner/repo"
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def close(self):
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code == 401:
            raise HTTPException(502, "GitHub: invalid or expired PAT")
        if resp.status_code == 403:
            raise HTTPException(502, "GitHub: PAT lacks required permissions (need repo scope)")
        if resp.status_code == 404:
            raise HTTPException(502, f"GitHub: not found — check that repo '{self.repo}' exists")
        if resp.status_code >= 400:
            raise HTTPException(502, f"GitHub API error {resp.status_code}: {resp.text[:200]}")
        return resp

    async def get_tree(self, branch: str) -> list[str]:
        resp = await self._request(
            "GET", f"/repos/{self.repo}/git/trees/{branch}",
            params={"recursive": "1"},
        )
        tree = resp.json().get("tree", [])
        return [item["path"] for item in tree if item["type"] == "blob"]

    async def read_file(self, path: str, ref: str) -> str:
        resp = await self._request(
            "GET", f"/repos/{self.repo}/contents/{path}",
            params={"ref": ref},
        )
        data = resp.json()
        content_b64 = data.get("content", "")
        return base64.b64decode(content_b64).decode("utf-8", errors="replace")

    async def get_file_sha(self, path: str, ref: str) -> Optional[str]:
        """Get the blob SHA for a file (needed for updates)."""
        resp = await self._client.request(
            "GET", f"/repos/{self.repo}/contents/{path}",
            params={"ref": ref},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise HTTPException(502, f"GitHub API error {resp.status_code}")
        return resp.json().get("sha")

    async def get_branch_sha(self, branch: str) -> str:
        resp = await self._request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}")
        return resp.json()["object"]["sha"]

    async def create_branch(self, name: str, from_sha: str):
        await self._request(
            "POST", f"/repos/{self.repo}/git/refs",
            json={"ref": f"refs/heads/{name}", "sha": from_sha},
        )

    async def upsert_file(self, path: str, content: str, message: str, branch: str, sha: Optional[str] = None):
        body: dict = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        await self._request("PUT", f"/repos/{self.repo}/contents/{path}", json=body)

    async def create_draft_pr(self, title: str, body: str, branch: str, base: str) -> str:
        resp = await self._request(
            "POST", f"/repos/{self.repo}/pulls",
            json={"title": title, "body": body, "head": branch, "base": base, "draft": True},
        )
        return resp.json()["html_url"]


# ── LLM API ──────────────────────────────────────────────────────────────────

_LLM_SYSTEM_PROMPT = """You improve MCP servers based on agent feedback. You receive feedback about a gap, the repo file tree, and relevant source files.

Rules:
- For "modify" action: include the COMPLETE updated file content, not a diff
- For "create" action: provide the full new file content
- commit_message: imperative mood ("Add X" not "Added X"), under 72 chars
- pr_body: reference the original feedback
- Keep changes minimal and focused on the reported gap"""

_PR_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "action": {"type": "string", "enum": ["modify", "create"]},
        "content": {"type": "string"},
        "commit_message": {"type": "string"},
        "pr_title": {"type": "string"},
        "pr_body": {"type": "string"},
    },
    "required": ["file_path", "action", "content", "commit_message", "pr_title", "pr_body"],
    "additionalProperties": False,
}


def _build_user_message(feedback: dict, tree: list[str], file_contents: dict[str, str]) -> str:
    tree_listing = "\n".join(tree[:30])
    if len(tree) > 30:
        tree_listing += f"\n... and {len(tree) - 30} more files"

    files_section = ""
    for path, content in file_contents.items():
        lines = content.split("\n")[:500]
        files_section += f"\n\n### {path}\n```\n" + "\n".join(lines) + "\n```"

    notes_section = ""
    notes = feedback.get("notes", [])
    if notes:
        notes_section = "\n\n## Developer notes\nThese are human-written annotations from the developer reviewing this feedback. They provide critical context — prioritize them.\n"
        for n in notes:
            notes_section += f"\n- [{n.get('timestamp', '')}] {n.get('content', '')}"

    return f"""## Feedback about MCP server: {feedback.get('server_name', 'unknown')}

**Gap type:** {feedback.get('gap_type', 'other')}
**What the agent needed:** {feedback.get('what_i_needed', '')}
**What the agent tried:** {feedback.get('what_i_tried', '')}
**Suggestion:** {feedback.get('suggestion', '')}
**User goal:** {feedback.get('user_goal', '')}
**Resolution:** {feedback.get('resolution', '')}
**Client type:** {feedback.get('client_type', '')}
{notes_section}
## Repository file tree
{tree_listing}

## Relevant source files
{files_section}"""


def _get_llm_config(settings: dict) -> tuple[str, str, str]:
    """Return (provider, model, api_key) from settings."""
    provider = settings.get("llm_provider") or "anthropic"
    model = settings.get("llm_model") or _PROVIDER_DEFAULTS.get(provider, "claude-opus-4-6")
    if provider == "openai":
        return provider, model, settings["openai_api_key"]
    return provider, model, settings["anthropic_api_key"]


async def _call_anthropic(api_key: str, model: str, user_msg: str) -> str:
    """Call Anthropic API with structured output (output_config.format)."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 16384,
                "system": _LLM_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}],
                "output_config": {
                    "format": {
                        "type": "json_schema",
                        "schema": _PR_SCHEMA,
                    }
                },
            },
        )

    if resp.status_code == 401:
        raise ValueError("Anthropic: invalid API key")
    if resp.status_code >= 400:
        raise ValueError(f"Anthropic API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    if data.get("stop_reason") == "refusal":
        raise ValueError("Anthropic: model refused the request")
    if data.get("stop_reason") == "max_tokens":
        raise ValueError("Anthropic: response truncated (file too large for max_tokens)")
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block["text"]
    return text


async def _call_openai(api_key: str, model: str, user_msg: str) -> str:
    """Call OpenAI API with structured output (response_format json_schema)."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 16384,
                "messages": [
                    {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "pr_suggestion",
                        "schema": _PR_SCHEMA,
                        "strict": True,
                    },
                },
            },
        )

    if resp.status_code == 401:
        raise ValueError("OpenAI: invalid API key")
    if resp.status_code >= 400:
        raise ValueError(f"OpenAI API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        raise ValueError("OpenAI returned no choices")
    msg = choices[0].get("message", {})
    if msg.get("refusal"):
        raise ValueError(f"OpenAI: model refused — {msg['refusal']}")
    return msg.get("content", "")


def _parse_llm_json(text: str) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("LLM returned an empty response")

    # Try 1: direct parse (ideal case — pure JSON response)
    try:
        result = json.loads(text)
        return _validate_llm_result(result)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try 2: strip markdown code fences
    stripped = re.sub(r"^```(?:json)?\s*", "", text)
    stripped = re.sub(r"\s*```$", "", stripped).strip()
    try:
        result = json.loads(stripped)
        return _validate_llm_result(result)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try 3: extract first JSON object from mixed text (model wrote preamble)
    match = re.search(r"\{", text)
    if match:
        depth = 0
        start = match.start()
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                escape = True
                continue
            if c == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        result = json.loads(candidate)
                        return _validate_llm_result(result)
                    except (json.JSONDecodeError, ValueError):
                        break

    preview = text[:300] + ("..." if len(text) > 300 else "")
    raise ValueError(f"Could not extract JSON from LLM response.\nResponse was: {preview}")


def _validate_llm_result(result: dict) -> dict:
    for field in ("file_path", "content", "commit_message", "pr_title", "pr_body"):
        if field not in result:
            raise ValueError(f"LLM response missing required field: {field}")
    result.setdefault("action", "modify")
    return result


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
                 tools_available, session_id, client_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                feedback.client_type,
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


@app.get("/api/settings")
async def get_settings():
    settings = _get_settings()
    masked = {}
    for key in _ALL_SETTINGS_KEYS:
        val = settings.get(key, "")
        if key in _ENV_KEYS and val:
            masked[key] = _mask(val)
        else:
            masked[key] = val
    provider = settings.get("llm_provider", "anthropic")
    api_key_field = "anthropic_api_key" if provider == "anthropic" else "openai_api_key"
    configured = bool(
        settings.get("github_pat")
        and settings.get("github_repo")
        and settings.get(api_key_field)
    )
    return {**masked, "configured": configured}


class SettingsUpdate(BaseModel):
    github_pat: str = ""
    github_repo: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    default_branch: str = ""
    llm_provider: str = ""
    llm_model: str = ""


@app.put("/api/settings")
async def update_settings(body: SettingsUpdate):
    now = datetime.now(timezone.utc).isoformat()
    updates = body.model_dump()
    # Preferences → SQLite
    with get_db() as conn:
        for key, value in updates.items():
            if key in _DB_SETTINGS_KEYS and value:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )
    # Secrets → .env
    env_updates = {k: v for k, v in updates.items() if k in _ENV_KEYS and v}
    if env_updates:
        _write_env(env_updates)
    return {"status": "saved"}


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _sse_json(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/feedback/{feedback_id}/draft-pr")
async def draft_pr(feedback_id: str, force: bool = Query(False)):
    # Pre-validate before starting the stream
    with get_db() as conn:
        row = conn.execute("SELECT * FROM feedback WHERE id = ?", (feedback_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Feedback not found")
        fb = _row_to_dict(row)
        if fb.get("pr_url") and not force:
            raise HTTPException(409, "A draft PR already exists for this feedback")
        # Attach notes so the LLM gets developer context
        _attach_notes(conn, [fb])

    settings = _get_settings()
    provider, model, llm_api_key = _get_llm_config(settings)
    if not all([settings.get("github_pat"), settings.get("github_repo"), llm_api_key]):
        raise HTTPException(400, f"Settings not configured — add GitHub PAT, repo, and {provider.title()} API key")

    async def generate():
        repo = settings["github_repo"]
        base_branch = settings.get("default_branch") or "main"
        gh = GitHubClient(settings["github_pat"], repo)

        try:
            yield _sse("step", f"Fetching file tree from {repo}...")

            tree = await gh.get_tree(base_branch)
            yield _sse("step", f"Found {len(tree)} files in repo")

            # Score & select relevant files
            scored = [(path, _score_file(path, fb.get("server_name", ""))) for path in tree]
            scored = [(p, s) for p, s in scored if s >= 0]
            scored.sort(key=lambda x: x[1], reverse=True)
            top_files = [p for p, _ in scored[:8]]

            yield _sse("step", f"Reading {len(top_files)} relevant source files...")

            file_contents = {}
            for path in top_files:
                try:
                    content = await gh.read_file(path, base_branch)
                    file_contents[path] = content
                    yield _sse("detail", f"  read {path}")
                except Exception:
                    continue

            notes = fb.get("notes", [])
            if notes:
                yield _sse("step", f"Including {len(notes)} developer note(s) for context")

            yield _sse("step", f"Calling {provider.title()} ({model}) with structured output...")

            # Call LLM with schema-enforced JSON
            user_msg = _build_user_message(fb, tree, file_contents)
            if provider == "openai":
                raw_text = await _call_openai(llm_api_key, model, user_msg)
            else:
                raw_text = await _call_anthropic(llm_api_key, model, user_msg)

            yield _sse("step", "Validating response...")
            result = _parse_llm_json(raw_text)

            yield _sse("step", f"LLM suggests: {result.get('action', 'modify')} {result['file_path']}")

            # Create branch (timestamp suffix ensures uniqueness on re-drafts)
            branch_suffix = fb.get("gap_type", "fix").replace("_", "-")
            ts = datetime.now(timezone.utc).strftime("%m%d%H%M")
            branch_name = f"patchwork/feedback-{feedback_id[:8]}-{branch_suffix}-{ts}"

            yield _sse("step", f"Creating branch {branch_name}...")

            base_sha = await gh.get_branch_sha(base_branch)
            await gh.create_branch(branch_name, base_sha)

            # Commit
            yield _sse("step", f"Committing: {result['commit_message']}")

            file_sha = None
            if result["action"] == "modify":
                file_sha = await gh.get_file_sha(result["file_path"], base_branch)

            await gh.upsert_file(
                result["file_path"],
                result["content"],
                result["commit_message"],
                branch_name,
                sha=file_sha,
            )

            # Open PR
            yield _sse("step", "Opening draft pull request...")

            pr_url = await gh.create_draft_pr(
                result["pr_title"],
                result["pr_body"],
                branch_name,
                base_branch,
            )

            with get_db() as conn:
                conn.execute("UPDATE feedback SET pr_url = ? WHERE id = ?", (pr_url, feedback_id))

            yield _sse_json("done", {"pr_url": pr_url, "branch": branch_name})

        except ValueError as e:
            yield _sse("error", str(e))
        except HTTPException as e:
            yield _sse("error", e.detail)
        except Exception as e:
            yield _sse("error", f"Unexpected error: {e}")
        finally:
            await gh.close()

    return StreamingResponse(generate(), media_type="text/event-stream")


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
  /* ── Theme tokens ── */
  :root {
    --bg: #f5f5f5; --bg-surface: #fff; --bg-inset: #f0f0f0;
    --border: #ddd; --border-strong: #ccc;
    --text: #1a1a1a; --text-heading: #000; --text-muted: #666; --text-faint: #999;
    --accent-blue-bg: #e8f0fe; --accent-blue: #1a6ede;
    --accent-green-bg: #e6f4e6; --accent-green: #1a8a1a;
    --accent-red-bg: #fde8e8; --accent-red: #c0392b;
    --accent-orange-bg: #fef3e0; --accent-orange: #c57600;
    --accent-yellow-bg: #fefce8; --accent-yellow: #8a7a00;
    --accent-teal-bg: #e0f7f5; --accent-teal: #00796b;
    --accent-purple-bg: #eee8f5; --accent-purple: #5b48a2;
    --btn-bg: #fff; --btn-hover: #f0f0f0; --btn-active-bg: #e6f4e6; --btn-active-border: #4a8a4a;
    --input-bg: #fff; --input-border: #ccc; --input-placeholder: #aaa;
    --modal-backdrop: rgba(0,0,0,0.3); --modal-bg: #fff;
    --save-bg: #e6f4e6; --save-border: #4a8a4a; --save-text: #1a8a1a;
    --pr-btn-bg: #e8f0fe; --pr-btn-border: #a0c4f0; --pr-btn-text: #1a6ede;
  }
  [data-theme="dark"] {
    --bg: #0a0a0a; --bg-surface: #161616; --bg-inset: #111;
    --border: #2a2a2a; --border-strong: #333;
    --text: #e0e0e0; --text-heading: #fff; --text-muted: #888; --text-faint: #666;
    --accent-blue-bg: #1a2a3a; --accent-blue: #6ab0f3;
    --accent-green-bg: #1a3a1a; --accent-green: #6af36a;
    --accent-red-bg: #3a1a1a; --accent-red: #f36a6a;
    --accent-orange-bg: #3a2a1a; --accent-orange: #f3b06a;
    --accent-yellow-bg: #3a3a1a; --accent-yellow: #e8f36a;
    --accent-teal-bg: #1a3a3a; --accent-teal: #6af3e8;
    --accent-purple-bg: #1a1a2a; --accent-purple: #8888cc;
    --btn-bg: #161616; --btn-hover: #222; --btn-active-bg: #2a4a2a; --btn-active-border: #4a8a4a;
    --input-bg: #0a0a0a; --input-border: #333; --input-placeholder: #555;
    --modal-backdrop: rgba(0,0,0,0.7); --modal-bg: #161616;
    --save-bg: #1a3a1a; --save-border: #4a8a4a; --save-text: #6af36a;
    --pr-btn-bg: #1a2a3a; --pr-btn-border: #3a6a9a; --pr-btn-text: #6ab0f3;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: var(--bg); color: var(--text); padding: 2rem; }
  .top-bar { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 1rem; }
  .top-bar-left { flex: 1; }
  h1 { font-size: 1.4rem; margin-bottom: 0.5rem; color: var(--text-heading); }
  .subtitle { color: var(--text-muted); font-size: 0.9rem; }
  .theme-toggle { background: var(--btn-bg); border: 1px solid var(--border); color: var(--text); padding: 0.35rem 0.65rem; border-radius: 6px; font-size: 1rem; cursor: pointer; line-height: 1; }
  .theme-toggle:hover { background: var(--btn-hover); }

  /* Settings bar */
  .settings-bar { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1.5rem; }
  .settings-toggle { background: var(--btn-bg); border: 1px solid var(--border); color: var(--text); padding: 0.4rem 0.75rem; border-radius: 6px; font-size: 0.85rem; cursor: pointer; display: flex; align-items: center; gap: 0.4rem; }
  .settings-toggle:hover { background: var(--btn-hover); }
  .config-status { font-size: 0.8rem; padding: 0.2rem 0.6rem; border-radius: 4px; }
  .config-status.ok { background: var(--accent-green-bg); color: var(--accent-green); }
  .config-status.missing { background: var(--accent-red-bg); color: var(--accent-red); }
  .settings-panel { background: var(--bg-surface); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 1.5rem; display: none; }
  .settings-panel.open { display: block; }
  .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; margin-bottom: 1rem; }
  .settings-field label { display: block; font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.3rem; }
  .settings-field input, .settings-field select { width: 100%; background: var(--input-bg); border: 1px solid var(--input-border); color: var(--text); padding: 0.5rem 0.75rem; border-radius: 6px; font-size: 0.85rem; font-family: monospace; }
  .settings-field input::placeholder { color: var(--input-placeholder); }
  .settings-field.full { grid-column: 1 / -1; }
  .settings-field.hidden { display: none; }
  .settings-actions { display: flex; gap: 0.5rem; align-items: center; }
  .settings-actions .save-btn { background: var(--save-bg); border-color: var(--save-border); color: var(--save-text); }
  .settings-actions .save-btn:hover { filter: brightness(1.1); }
  .settings-saved { font-size: 0.8rem; color: var(--accent-green); display: none; }

  .stats { display: flex; gap: 1.5rem; margin-bottom: 2rem; flex-wrap: wrap; }
  .stat { background: var(--bg-surface); border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.5rem; }
  .stat-num { font-size: 1.8rem; font-weight: 700; color: var(--text-heading); }
  .stat-label { font-size: 0.8rem; color: var(--text-muted); margin-top: 0.25rem; }
  .filters { display: flex; gap: 0.75rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
  select, button { background: var(--btn-bg); border: 1px solid var(--border); color: var(--text); padding: 0.5rem 0.75rem; border-radius: 6px; font-size: 0.85rem; cursor: pointer; }
  button:hover { background: var(--btn-hover); }
  .btn-active { background: var(--btn-active-bg); border-color: var(--btn-active-border); }
  .card { background: var(--bg-surface); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem; margin-bottom: 0.75rem; }
  .card.reviewed { opacity: 0.5; }
  .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem; }
  .badges { display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap; }
  .server-badge { background: var(--accent-blue-bg); color: var(--accent-blue); padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.75rem; }
  .gap-badge { padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.75rem; }
  .gap-badge.missing_tool { background: var(--accent-red-bg); color: var(--accent-red); }
  .gap-badge.incomplete_results { background: var(--accent-orange-bg); color: var(--accent-orange); }
  .gap-badge.missing_parameter { background: var(--accent-yellow-bg); color: var(--accent-yellow); }
  .gap-badge.wrong_format { background: var(--accent-teal-bg); color: var(--accent-teal); }
  .gap-badge.other { background: var(--bg-inset); color: var(--text-muted); }
  .resolution-badge { padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.75rem; }
  .resolution-badge.blocked { background: var(--accent-red-bg); color: var(--accent-red); }
  .resolution-badge.worked_around { background: var(--accent-green-bg); color: var(--accent-green); }
  .resolution-badge.partial { background: var(--accent-orange-bg); color: var(--accent-orange); }
  .field { margin-bottom: 0.6rem; }
  .field-label { font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.15rem; }
  .field-value { font-size: 0.9rem; line-height: 1.4; }
  .meta-row { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 0.6rem; }
  .meta-item { font-size: 0.75rem; color: var(--text-faint); }
  .meta-item span { color: var(--text-muted); }
  .tools-list { display: flex; gap: 0.3rem; flex-wrap: wrap; margin-top: 0.2rem; }
  .tool-chip { background: var(--accent-purple-bg); color: var(--accent-purple); padding: 0.1rem 0.5rem; border-radius: 3px; font-size: 0.75rem; font-family: monospace; }
  .timestamp { font-size: 0.75rem; color: var(--text-faint); }
  .pr-link { font-size: 0.8rem; color: var(--accent-green); text-decoration: none; }
  .pr-link:hover { text-decoration: underline; }
  .notes-section { margin-top: 0.75rem; border-top: 1px solid var(--border); padding-top: 0.75rem; }
  .notes-header { font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.4rem; }
  .note-item { background: var(--bg-inset); border-left: 2px solid var(--border-strong); padding: 0.4rem 0.6rem; margin-bottom: 0.4rem; border-radius: 0 4px 4px 0; }
  .note-time { font-size: 0.7rem; color: var(--text-faint); }
  .note-content { font-size: 0.85rem; line-height: 1.3; margin-top: 0.15rem; }
  .card-actions { margin-top: 0.75rem; display: flex; gap: 0.5rem; align-items: center; }
  .card-actions button { font-size: 0.8rem; padding: 0.35rem 0.75rem; }
  .btn-draft-pr { background: var(--pr-btn-bg); border-color: var(--pr-btn-border); color: var(--pr-btn-text); }
  .btn-draft-pr:hover { filter: brightness(1.1); }
  .btn-draft-pr:disabled { opacity: 0.5; cursor: wait; }
  .btn-redraft { font-size: 0.75rem; padding: 0.25rem 0.5rem; opacity: 0.7; background: var(--pr-btn-bg); border-color: var(--pr-btn-border); color: var(--pr-btn-text); }
  .btn-redraft:hover { opacity: 1; filter: brightness(1.1); }
  .empty { text-align: center; color: var(--text-faint); padding: 3rem; }

  /* Progress modal */
  .modal-overlay { position: fixed; inset: 0; background: var(--modal-backdrop); display: flex; align-items: center; justify-content: center; z-index: 100; }
  .modal { background: var(--modal-bg); border: 1px solid var(--border); border-radius: 10px; padding: 1.5rem; width: 520px; max-height: 80vh; display: flex; flex-direction: column; }
  .modal-title { font-size: 1rem; font-weight: 600; color: var(--text-heading); margin-bottom: 1rem; }
  .modal-log { flex: 1; overflow-y: auto; font-family: monospace; font-size: 0.8rem; line-height: 1.6; min-height: 120px; max-height: 50vh; }
  .modal-log .step { color: var(--accent-blue); }
  .modal-log .detail { color: var(--text-faint); }
  .modal-log .error { color: var(--accent-red); }
  .modal-log .done { color: var(--accent-green); font-weight: 600; }
  .modal-spinner { color: var(--text-faint); font-size: 0.8rem; font-family: monospace; }
  .modal-spinner::after { content: ''; animation: dots 1.5s steps(4, end) infinite; }
  @keyframes dots { 0% { content: ''; } 25% { content: '.'; } 50% { content: '..'; } 75% { content: '...'; } }
  .modal-footer { margin-top: 1rem; display: flex; justify-content: flex-end; gap: 0.5rem; }
  .modal-footer a { color: var(--accent-green); text-decoration: none; font-size: 0.85rem; }
  .modal-footer a:hover { text-decoration: underline; }
  .modal-footer button { font-size: 0.85rem; }
  .note-textarea { width: 100%; min-height: 120px; background: var(--input-bg); border: 1px solid var(--input-border); color: var(--text); padding: 0.75rem; border-radius: 6px; font-size: 0.85rem; font-family: inherit; resize: vertical; line-height: 1.5; box-sizing: border-box; }
  .note-textarea:focus { outline: none; border-color: var(--accent-blue); }
  .note-textarea::placeholder { color: var(--input-placeholder); }
  .note-hint { font-size: 0.75rem; color: var(--text-faint); margin-top: 0.5rem; }
</style>
</head>
<body>
<div class="top-bar">
  <div class="top-bar-left">
    <h1>PatchworkMCP</h1>
    <p class="subtitle">What are agents trying to do that they can't?</p>
  </div>
  <button class="theme-toggle" onclick="toggleTheme()" id="themeBtn" title="Toggle theme"></button>
</div>

<div class="settings-bar">
  <button class="settings-toggle" onclick="toggleSettings()">Settings</button>
  <span class="config-status" id="configStatus"></span>
</div>

<div class="settings-panel" id="settingsPanel">
  <div class="settings-grid">
    <div class="settings-field">
      <label for="settingPat">GitHub PAT</label>
      <input type="password" id="settingPat" placeholder="ghp_...">
    </div>
    <div class="settings-field">
      <label for="settingRepo">Repository (owner/repo)</label>
      <input type="text" id="settingRepo" placeholder="owner/repo">
    </div>
    <div class="settings-field">
      <label for="settingProvider">LLM Provider</label>
      <select id="settingProvider" onchange="onProviderChange()">
        <option value="anthropic">Anthropic</option>
        <option value="openai">OpenAI</option>
      </select>
    </div>
    <div class="settings-field">
      <label for="settingModel">Model</label>
      <input type="text" id="settingModel" placeholder="claude-opus-4-6">
    </div>
    <div class="settings-field" id="fieldAnthropicKey">
      <label for="settingAnthropicKey">Anthropic API Key</label>
      <input type="password" id="settingAnthropicKey" placeholder="sk-ant-...">
    </div>
    <div class="settings-field hidden" id="fieldOpenaiKey">
      <label for="settingOpenaiKey">OpenAI API Key</label>
      <input type="password" id="settingOpenaiKey" placeholder="sk-...">
    </div>
    <div class="settings-field">
      <label for="settingBranch">Default branch (optional)</label>
      <input type="text" id="settingBranch" placeholder="main">
    </div>
  </div>
  <div class="settings-actions">
    <button class="save-btn" onclick="saveSettings()">Save</button>
    <span class="settings-saved" id="settingsSaved">Saved</span>
  </div>
</div>

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
// Theme
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  document.getElementById('themeBtn').textContent = theme === 'dark' ? '\\u2600' : '\\u263E';
}
function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  localStorage.setItem('patchwork-theme', next);
  applyTheme(next);
}
applyTheme(localStorage.getItem('patchwork-theme') || 'light');

let showReviewed = true;
let currentData = [];
let settingsConfigured = false;

const PROVIDER_DEFAULTS = { anthropic: 'claude-opus-4-6', openai: 'GPT-5.2-Codex' };

async function loadSettings() {
  const r = await fetch('/api/settings');
  const s = await r.json();
  settingsConfigured = s.configured;
  const el = document.getElementById('configStatus');
  if (s.configured) {
    el.className = 'config-status ok';
    el.textContent = 'Configured';
  } else {
    el.className = 'config-status missing';
    el.textContent = 'Not configured';
  }
  document.getElementById('settingRepo').placeholder = s.github_repo || 'owner/repo';
  document.getElementById('settingBranch').placeholder = s.default_branch || 'main';
  const provider = s.llm_provider || 'anthropic';
  document.getElementById('settingProvider').value = provider;
  document.getElementById('settingModel').placeholder = s.llm_model || PROVIDER_DEFAULTS[provider] || '';
  onProviderChange();
}

function toggleSettings() {
  document.getElementById('settingsPanel').classList.toggle('open');
}

function onProviderChange() {
  const provider = document.getElementById('settingProvider').value;
  const anthropicField = document.getElementById('fieldAnthropicKey');
  const openaiField = document.getElementById('fieldOpenaiKey');
  if (provider === 'openai') {
    anthropicField.classList.add('hidden');
    openaiField.classList.remove('hidden');
  } else {
    anthropicField.classList.remove('hidden');
    openaiField.classList.add('hidden');
  }
  document.getElementById('settingModel').placeholder = PROVIDER_DEFAULTS[provider] || '';
}

async function saveSettings() {
  const body = {};
  const pat = document.getElementById('settingPat').value.trim();
  const repo = document.getElementById('settingRepo').value.trim();
  const anthropicKey = document.getElementById('settingAnthropicKey').value.trim();
  const openaiKey = document.getElementById('settingOpenaiKey').value.trim();
  const branch = document.getElementById('settingBranch').value.trim();
  const provider = document.getElementById('settingProvider').value;
  const model = document.getElementById('settingModel').value.trim();
  if (pat) body.github_pat = pat;
  if (repo) body.github_repo = repo;
  if (anthropicKey) body.anthropic_api_key = anthropicKey;
  if (openaiKey) body.openai_api_key = openaiKey;
  if (branch) body.default_branch = branch;
  body.llm_provider = provider;
  if (model) body.llm_model = model;
  await fetch('/api/settings', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  document.getElementById('settingPat').value = '';
  document.getElementById('settingAnthropicKey').value = '';
  document.getElementById('settingOpenaiKey').value = '';
  const saved = document.getElementById('settingsSaved');
  saved.style.display = 'inline';
  setTimeout(() => saved.style.display = 'none', 2000);
  loadSettings();
}

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
      ${f.agent_model || f.session_id || f.client_type ? `
        <div class="meta-row">
          ${f.agent_model ? `<div class="meta-item">Model: <span>${esc(f.agent_model)}</span></div>` : ''}
          ${f.client_type ? `<div class="meta-item">Client: <span>${esc(f.client_type)}</span></div>` : ''}
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
        ${f.pr_url
          ? `<a class="pr-link" href="${esc(f.pr_url)}" target="_blank">View PR &rarr;</a>
             <button class="btn-redraft" id="pr-btn-${f.id}" onclick="draftPR('${f.id}', true)">Re-draft</button>`
          : (settingsConfigured
            ? `<button class="btn-draft-pr" id="pr-btn-${f.id}" onclick="draftPR('${f.id}')">Draft PR</button>`
            : '')
        }
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

function addNote(id) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-title">Add note</div>
      <textarea class="note-textarea" id="noteInput" placeholder="Add context for the LLM — what to focus on, how to approach the fix, what the real issue is..." autofocus></textarea>
      <div class="note-hint">Notes are sent to the LLM when drafting PRs. They're append-only — you can't lose them.</div>
      <div class="modal-footer">
        <button onclick="this.closest('.modal-overlay').remove()">Cancel</button>
        <button class="btn-draft-pr" onclick="submitNote('${id}')">Save note</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const textarea = document.getElementById('noteInput');
  textarea.focus();
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { submitNote(id); }
    if (e.key === 'Escape') { overlay.remove(); }
  });
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
}

async function submitNote(id) {
  const textarea = document.getElementById('noteInput');
  const text = textarea?.value?.trim();
  if (!text) return;
  const btn = textarea.closest('.modal').querySelector('.btn-draft-pr');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
  await fetch(`/api/feedback/${id}/notes`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({content: text}),
  });
  textarea.closest('.modal-overlay').remove();
  loadAll();
}

async function draftPR(id, force = false) {
  const btn = document.getElementById('pr-btn-' + id);
  if (btn) { btn.disabled = true; btn.textContent = 'Creating PR...'; }

  // Create modal
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-title">Drafting PR...</div>
      <div class="modal-log" id="prLog"></div>
      <div class="modal-footer" id="prFooter"></div>
    </div>`;
  document.body.appendChild(overlay);
  const log = document.getElementById('prLog');
  const footer = document.getElementById('prFooter');

  let spinner = null;
  function removeSpinner() { if (spinner) { spinner.remove(); spinner = null; } }
  function showSpinner() {
    removeSpinner();
    spinner = document.createElement('div');
    spinner.className = 'modal-spinner';
    spinner.textContent = 'Working';
    log.appendChild(spinner);
    log.scrollTop = log.scrollHeight;
  }

  function addLine(cls, text) {
    removeSpinner();
    const div = document.createElement('div');
    div.className = cls;
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    if (cls === 'step' || cls === 'detail') showSpinner();
  }

  try {
    const url = `/api/feedback/${id}/draft-pr` + (force ? '?force=true' : '');
    const resp = await fetch(url, { method: 'POST' });

    if (!resp.ok && resp.headers.get('content-type')?.includes('application/json')) {
      const err = await resp.json();
      addLine('error', err.detail || 'Request failed');
      footer.innerHTML = '<button onclick="this.closest(\\'.modal-overlay\\').remove()">Close</button>';
      if (btn) { btn.disabled = false; btn.textContent = 'Draft PR'; }
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let prUrl = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split('\\n');
      buffer = lines.pop();

      let currentEvent = null;
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7);
        } else if (line.startsWith('data: ') && currentEvent) {
          const data = JSON.parse(line.slice(6));
          if (currentEvent === 'step') addLine('step', data);
          else if (currentEvent === 'detail') addLine('detail', data);
          else if (currentEvent === 'error') addLine('error', data);
          else if (currentEvent === 'done') {
            prUrl = data.pr_url;
            addLine('done', 'PR created successfully!');
          }
          currentEvent = null;
        }
      }
    }

    if (prUrl) {
      footer.innerHTML = `<a href="${esc(prUrl)}" target="_blank">View PR &rarr;</a> <button onclick="this.closest('.modal-overlay').remove(); loadAll()">Close</button>`;
      if (btn) btn.textContent = 'PR Created!';
    } else {
      footer.innerHTML = '<button onclick="this.closest(\\'.modal-overlay\\').remove()">Close</button>';
      if (btn) { btn.disabled = false; btn.textContent = 'Draft PR'; }
    }
  } catch (e) {
    addLine('error', 'Network error: ' + e.message);
    footer.innerHTML = '<button onclick="this.closest(\\'.modal-overlay\\').remove()">Close</button>';
    if (btn) { btn.disabled = false; btn.textContent = 'Draft PR'; }
  }
}

function toggleReviewedFilter() {
  showReviewed = !showReviewed;
  document.getElementById('filterReviewed').classList.toggle('btn-active', !showReviewed);
  document.getElementById('filterReviewed').textContent = showReviewed ? 'Hide reviewed' : 'Showing unreviewed';
  loadFeedback();
}

async function loadAll() { loadStats(); await loadSettings(); loadFeedback(); }

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
