"""
Self-healing sync agent — uses Claude Opus 4.7 to diagnose and patch
persistent sync failures so they don't recur on the next run.

Errors are fingerprinted; already-healed patterns are skipped.
Transient failures (network, rate-limits) are filtered before Claude is called.
Writes are sandboxed to src/sync/ and src/db/ only.
Code changes take effect on the next app restart.
"""

from __future__ import annotations

import ast
import hashlib
import json
import textwrap
import traceback
from datetime import datetime, timezone
from pathlib import Path

from src.core.config import BASE_DIR, LOG_DIR, get_env, get_secret, logger

HEALER_LOG  = LOG_DIR / "healer.jsonl"
_SAFE_WRITE = frozenset({"src/sync", "src/db"})

_TRANSIENT_PATTERNS = frozenset({
    "rate_limit", "ratelimit", "429",
    "timeout", "timed out",
    "connectionerror", "connection refused", "connection reset",
    "httperror", "requests.exceptions",
    "socket.timeout", "ssl",
    "network", "temporary",
})


def _is_transient(exc_type: str, exc_msg: str) -> bool:
    combined = (exc_type + " " + exc_msg).lower()
    return any(t in combined for t in _TRANSIENT_PATTERNS)


def _fingerprint(exc_type: str, exc_msg: str) -> str:
    key = f"{exc_type}::{exc_msg[:120]}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _load_seen_fingerprints() -> set[str]:
    seen: set[str] = set()
    if not HEALER_LOG.exists():
        return seen
    try:
        with open(HEALER_LOG, encoding="utf-8") as fh:
            for line in fh:
                try:
                    seen.add(json.loads(line)["fingerprint"])
                except Exception:
                    pass
    except Exception:
        pass
    return seen


def _record_healing(fp: str, exc_type: str, exc_msg: str, description: str):
    HEALER_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "fingerprint": fp,
        "error_type":  exc_type,
        "error_msg":   exc_msg[:200],
        "healed_at":   datetime.now(tz=timezone.utc).isoformat(),
        "description": description,
    }
    with open(HEALER_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ── Tool implementations ──────────────────────────────────────────────────────

def _tool_read_file(path: str) -> str:
    p = (BASE_DIR / path).resolve()
    try:
        p.relative_to(BASE_DIR / "src")
    except ValueError:
        return "ERROR: path must be under src/"
    if not p.exists():
        return f"ERROR: file not found: {path}"
    try:
        return p.read_text(encoding="utf-8")
    except Exception as exc:
        return f"ERROR: {exc}"


def _tool_list_source_files(directory: str = "src") -> str:
    d = (BASE_DIR / directory).resolve()
    try:
        d.relative_to(BASE_DIR / "src")
    except ValueError:
        return "ERROR: directory must be under src/"
    if not d.exists():
        return f"ERROR: directory not found: {directory}"
    files = sorted(str(p.relative_to(BASE_DIR)) for p in d.rglob("*.py"))
    return "\n".join(files) if files else "(no Python files found)"


def _tool_write_file(path: str, content: str) -> str:
    p = (BASE_DIR / path).resolve()
    try:
        rel = p.relative_to(BASE_DIR)
    except ValueError:
        return f"ERROR: path must be inside the project root"

    parts = rel.parts
    if len(parts) < 2 or "/".join(parts[:2]) not in _SAFE_WRITE:
        return f"ERROR: writes are only allowed inside src/sync/ and src/db/ (got {path})"

    if p.suffix == ".py":
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return f"ERROR: syntax error in proposed code: {exc}"

    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
        return f"OK: wrote {path} ({len(content)} chars)"
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return f"ERROR writing file: {exc}"


def _tool_test_database_query(db_id: str) -> str:
    try:
        import importlib
        import sys
        # Reload sync module to pick up any file changes made this session
        for mod_name in list(sys.modules):
            if mod_name.startswith("src.sync") or mod_name.startswith("src.db"):
                del sys.modules[mod_name]

        from src.db.database import DatabaseManager
        from src.sync.engine import SyncEngine
        db  = DatabaseManager()
        if not db.is_connected():
            return "ERROR: database not connected"
        eng = SyncEngine(db)
        pages = eng._query_pages(db_id)
        return f"OK: queried {db_id}, got {len(pages)} pages."
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


# ── Tool schemas ──────────────────────────────────────────────────────────────

_TOOLS: list[dict] = [
    {
        "name": "read_file",
        "description": (
            "Read any source file under src/. "
            "Always read a file before writing it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to project root, e.g. src/sync/engine.py",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_source_files",
        "description": "List Python files in a directory under src/.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Directory relative to project root, default 'src'",
                },
            },
            "required": [],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Overwrite a source file with a corrected version. "
            "ONLY allowed in src/sync/ or src/db/. "
            "Write the COMPLETE corrected file, not just the patch. "
            "Syntax is validated before writing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to project root, e.g. src/sync/engine.py",
                },
                "content": {
                    "type": "string",
                    "description": "Full corrected file contents.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "test_database_query",
        "description": (
            "Retry querying a Notion database using the current (possibly just-patched) "
            "code to verify the fix works. Reloads sync modules before testing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "db_id": {
                    "type": "string",
                    "description": "The Notion database UUID to query",
                },
            },
            "required": ["db_id"],
        },
    },
]


# ── Stable system prompt (prompt-cached) ─────────────────────────────────────

_SYSTEM = textwrap.dedent("""
    You are a self-healing agent for a Notion → PostgreSQL sync application built in Python.
    Your job: diagnose Python errors in the sync pipeline, patch the source files,
    and verify the fix so the error never recurs on the next run.

    ## Application layout
    - src/sync/engine.py   — SyncEngine: pulls from Notion via notion-client SDK + raw HTTP fallback
    - src/db/database.py   — DatabaseManager: wraps psycopg2
    - src/db/indexer.py    — rebuild_index(): builds page_index table after every sync
    - src/db/backup.py     — BackupManager: nightly pg_dump
    - src/mcp/server.py    — MCP server exposing search/get_page to Claude Desktop

    ## Notion SDK quirks — read carefully
    - This notion-client version exposes `data_sources.query(data_source_id=...)`,
      NOT `databases.query(database_id=...)`.
    - SDK responses can be non-dict (e.g. strings). Always guard: `isinstance(result, dict)`.
    - Raw HTTP fallback: POST /databases/{id}/query with Authorization + Notion-Version headers.
    - Notion-Version header value: "2022-06-28".
    - `get_env("NOTION_API_BASE")` returns the base URL (default https://api.notion.com/v1).
    - `get_secret("NOTION_TOKEN", encrypted_key="NOTION_TOKEN_ENCRYPTED")` returns the token.

    ## Safety rules — non-negotiable
    - You may ONLY write files inside src/sync/ or src/db/.
    - Always read_file before write_file.
    - Write the COMPLETE corrected file, not just the diff.
    - After patching engine.py, call test_database_query to confirm the fix.
    - If an error is caused by missing credentials or Notion permissions — not a code bug —
      say so clearly and stop without writing any files.

    ## Approach
    1. Read the traceback to identify the exact file, function, and line.
    2. read_file that file (and any helpers it calls).
    3. Identify the minimal correct fix.
    4. write_file with the complete corrected content.
    5. test_database_query on the affected db_id to verify.
    6. Report what you changed and why.
""").strip()


# ── Healer ────────────────────────────────────────────────────────────────────

class SyncHealer:
    MAX_ITERATIONS = 14

    def __init__(self):
        try:
            import anthropic as _anthropic
            self._anthropic = _anthropic
        except ImportError:
            self._anthropic = None

    def is_available(self) -> bool:
        if self._anthropic is None:
            return False
        if not get_env("ANTHROPIC_HEALER_ENABLED", "1").strip() not in {"0", "false", "off", "no"}:
            return False
        key = get_secret("ANTHROPIC_API_KEY", encrypted_key="ANTHROPIC_API_KEY_ENCRYPTED")
        return bool(key)

    def _client(self):
        if self._anthropic is None:
            logger.warning("Healer: anthropic package not installed.")
            return None
        enabled = get_env("ANTHROPIC_HEALER_ENABLED", "1").strip().lower()
        if enabled in {"0", "false", "off", "no"}:
            logger.info("Healer: disabled via ANTHROPIC_HEALER_ENABLED.")
            return None
        key = get_secret("ANTHROPIC_API_KEY", encrypted_key="ANTHROPIC_API_KEY_ENCRYPTED")
        if not key:
            logger.info("Healer: ANTHROPIC_API_KEY not configured.")
            return None
        return self._anthropic.Anthropic(api_key=key)

    def _dispatch(self, name: str, inputs: dict) -> str:
        if name == "read_file":
            return _tool_read_file(inputs.get("path", ""))
        if name == "list_source_files":
            return _tool_list_source_files(inputs.get("directory", "src"))
        if name == "write_file":
            return _tool_write_file(inputs.get("path", ""), inputs.get("content", ""))
        if name == "test_database_query":
            return _tool_test_database_query(inputs.get("db_id", ""))
        return f"ERROR: unknown tool '{name}'"

    def heal(self, errors: list[tuple[str, str, str, str]]) -> bool:
        """
        Attempt to diagnose and fix errors.

        errors: list of (db_id, exc_type, exc_msg, traceback_str)
        Returns True if healing was attempted and files were written.
        """
        client = self._client()
        if client is None:
            return False

        seen_fps = _load_seen_fingerprints()

        new_errors: list[tuple] = []
        for db_id, exc_type, exc_msg, tb_str in errors:
            if _is_transient(exc_type, exc_msg):
                logger.debug(f"Healer: skipping transient error: {exc_type}: {exc_msg[:60]}")
                continue
            fp = _fingerprint(exc_type, exc_msg)
            if fp in seen_fps:
                logger.debug(f"Healer: skipping already-healed error {fp}")
                continue
            new_errors.append((db_id, exc_type, exc_msg, tb_str, fp))

        if not new_errors:
            return False

        logger.info(f"Healer: diagnosing {len(new_errors)} new error(s) with Claude Opus 4.7…")

        sections = []
        for i, (db_id, exc_type, exc_msg, tb_str, _) in enumerate(new_errors, 1):
            sections.append(
                f"### Error {i}  (db_id={db_id})\n"
                f"Type: {exc_type}\n"
                f"Message: {exc_msg}\n"
                f"Traceback:\n```\n{tb_str}\n```"
            )

        user_msg = (
            "The following sync errors occurred during the last pull from Notion.\n"
            "Please diagnose each error, read the relevant source files, patch the code, "
            "and verify the fix.\n\n"
            + "\n\n---\n\n".join(sections)
        )

        messages: list[dict] = [{"role": "user", "content": user_msg}]
        healed_any = False

        try:
            for iteration in range(self.MAX_ITERATIONS):
                response = client.messages.create(
                    model="claude-opus-4-7",
                    max_tokens=8192,
                    thinking={"type": "adaptive"},
                    system=[{
                        "type":          "text",
                        "text":          _SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    tools=_TOOLS,
                    messages=messages,
                )

                messages.append({"role": "assistant", "content": response.content})

                if response.stop_reason == "end_turn":
                    logger.info(f"Healer: agent finished after {iteration + 1} iteration(s).")
                    break

                if response.stop_reason != "tool_use":
                    logger.warning(f"Healer: unexpected stop_reason={response.stop_reason}")
                    break

                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    result = self._dispatch(block.name, block.input)
                    logger.info(f"Healer [{block.name}]: {result[:160]}")
                    if block.name == "write_file" and result.startswith("OK:"):
                        healed_any = True
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result,
                    })

                messages.append({"role": "user", "content": tool_results})

            if healed_any:
                for db_id, exc_type, exc_msg, tb_str, fp in new_errors:
                    _record_healing(
                        fp, exc_type, exc_msg,
                        f"Auto-healed by Claude Opus 4.7 (db_id={db_id})"
                    )
                logger.info("Healer: source files patched — changes take effect on next restart.")

        except Exception as exc:
            logger.error(f"Healer: agent failed: {exc}\n{traceback.format_exc()}")

        return healed_any
