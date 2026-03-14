#!/usr/bin/env python3
"""
server.py — Unified Obsidian MCP server.

Combines semantic search (pgvector) with full vault CRUD operations.
Replaces both obsidian-semantic AND mcp-obsidian with a single server
that works without Obsidian running (direct filesystem access).

Stack:
  - PostgreSQL + pgvector : vector storage
  - Ollama (nomic-embed-text) : local embeddings
  - watchdog : live file watcher
  - mcp : Model Context Protocol server

Environment variables:
  OBSIDIAN_VAULT    absolute path to your vault (required)
  DATABASE_URL      postgres connection string  (overrides POSTGRES_* vars)
  POSTGRES_HOST     postgres host               (default: localhost)
  POSTGRES_PORT     postgres port               (default: 5432)
  POSTGRES_DB       postgres database           (default: obsidian_brain)
  POSTGRES_USER     postgres user               (default: obsidian)
  POSTGRES_PASSWORD postgres password           (default: empty)
  OLLAMA_URL        ollama API endpoint         (default: http://localhost:11434)
  EMBEDDING_MODEL   ollama model name           (default: nomic-embed-text)
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.pool
import requests
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from config import build_dsn


# ─────────────────────────────────── Config ─────────────────────────────────

VAULT_PATH  = os.environ.get("OBSIDIAN_VAULT", "")
OLLAMA_URL  = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

DATABASE_URL = build_dsn()

MAX_EMBED_CHARS = 2000  # nomic-embed-text context limit (approx 512 tokens)
_TIMESTAMP_FMT  = "%Y-%m-%d %H:%M"
_DEBOUNCE_SECS  = 0.5   # collapse rapid saves from Obsidian autosave

# Set during background_init so search_vault can return a useful message
# instead of the misleading "No indexed notes found. Try running reindex_vault."
# threading.Event is used rather than a bare bool to avoid any cross-thread
# visibility issues without relying on the GIL.
_INDEXING_IN_PROGRESS = threading.Event()

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ──────────────────────────────── Database ───────────────────────────────────

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

# Watcher observer — held here so the shutdown handler can stop it cleanly.
_observer: Observer | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Return the shared connection pool, initialising it on first call."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL)
    return _pool


@contextlib.contextmanager
def db_conn():
    """Acquire a connection from the pool and return it on exit.

    On exception the connection is discarded (close=True) so any open
    transaction is rolled back and the pool gets a fresh connection next time.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
    except Exception:
        # Return the connection as broken so the pool replaces it rather than
        # recycling a connection that may have an aborted transaction.
        pool.putconn(conn, close=True)
        raise
    else:
        pool.putconn(conn)


def init_db():
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS notes (
                        id         SERIAL PRIMARY KEY,
                        path       TEXT UNIQUE NOT NULL,
                        content    TEXT NOT NULL,
                        hash       TEXT NOT NULL,
                        embedding  vector(768),
                        indexed_at TIMESTAMP DEFAULT NOW()
                    );
                """)
                # IVFFlat index for fast approximate nearest-neighbour search
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS notes_embedding_idx
                    ON notes USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = 100);
                """)
    log.info("Database initialised")


# ──────────────────────────────── Embeddings ─────────────────────────────────

def _vec_to_str(vec: list[float]) -> str:
    """Format a float list as a pgvector literal, e.g. '[0.1,0.2,...]'."""
    if not vec:
        raise ValueError("Cannot convert empty list to vector literal")
    return "[" + ",".join(str(v) for v in vec) + "]"


def embed(text: str) -> list[float]:
    """Embed text with Ollama. Truncates to MAX_EMBED_CHARS to stay within model limits."""
    text = text[:MAX_EMBED_CHARS]
    resp = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
        timeout=30,
    )
    resp.raise_for_status()
    vec = resp.json().get("embedding", [])
    if not vec:
        raise ValueError(f"Empty embedding returned by Ollama for text: {text[:50]!r}")
    return vec


# ───────────────────────────────── Indexing ──────────────────────────────────

def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _is_system_path(path: Path) -> bool:
    """Skip hidden/system directories (.obsidian, .trash, .git) relative to vault root."""
    vault = _vault_root()
    try:
        rel = path.relative_to(vault)
    except ValueError:
        return True  # outside vault — skip
    return any(part.startswith(".") for part in rel.parts)


def index_note(path: str, content: str):
    """Embed a single note and upsert into the database. Skips unchanged files."""
    h = file_hash(content)
    for attempt in range(3):
        try:
            with db_conn() as conn:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT hash FROM notes WHERE path = %s", (path,))
                        row = cur.fetchone()
                        if row and row[0] == h:
                            return  # unchanged — skip embedding call

                        vec = embed(content)
                        cur.execute("""
                            INSERT INTO notes (path, content, hash, embedding, indexed_at)
                            VALUES (%s, %s, %s, %s::vector, NOW())
                            ON CONFLICT (path) DO UPDATE
                                SET content    = EXCLUDED.content,
                                    hash       = EXCLUDED.hash,
                                    embedding  = EXCLUDED.embedding,
                                    indexed_at = NOW()
                        """, (path, content, h, _vec_to_str(vec)))
            log.info("Indexed: %s", path)
            return
        except psycopg2.Error as e:
            if e.pgcode == "40P01" and attempt < 2:  # deadlock — retry
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


def delete_note(path: str):
    with db_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM notes WHERE path = %s", (path,))
    log.info("Removed: %s", path)


def index_vault(vault: str):
    """Walk the vault and index every markdown file."""
    root = Path(vault)
    md_files = [f for f in root.rglob("*.md") if not _is_system_path(f)]
    log.info("Indexing %d notes in %s…", len(md_files), vault)
    for f in md_files:
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
            index_note(str(f), content)
        except Exception as e:
            log.warning("Skipped %s: %s", f, e)

    # Rebuild IVFFlat index now that data exists — an index built on an empty
    # table has no list centroids and returns zero results.
    try:
        with db_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("REINDEX INDEX notes_embedding_idx;")
        log.info("Rebuilt IVFFlat index")
    except Exception as e:
        log.warning("Index rebuild skipped: %s", e)

    log.info("Vault indexing complete")


# ─────────────────────────────── File Watcher ────────────────────────────────

class VaultEventHandler(FileSystemEventHandler):

    def __init__(self):
        super().__init__()
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _schedule(self, path: str):
        """Debounce rapid events for the same path (e.g. Obsidian autosave)."""
        with self._lock:
            existing = self._timers.pop(path, None)
            if existing:
                existing.cancel()
            t = threading.Timer(_DEBOUNCE_SECS, self._handle_upsert, args=(path,))
            self._timers[path] = t
            t.start()

    def _handle_upsert(self, path: str):
        with self._lock:
            self._timers.pop(path, None)
        if not path.endswith(".md") or _is_system_path(Path(path)):
            return
        try:
            content = Path(path).read_text(encoding="utf-8", errors="ignore")
            index_note(path, content)
        except FileNotFoundError:
            delete_note(path)
        except Exception as e:
            log.warning("Watcher: skipped %s: %s", path, e)

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            delete_note(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            if event.src_path.endswith(".md"):
                delete_note(event.src_path)
            self._schedule(event.dest_path)


def start_watcher(vault: str) -> Observer:
    global _observer
    _observer = Observer()
    _observer.schedule(VaultEventHandler(), vault, recursive=True)
    _observer.start()
    log.info("Watching vault: %s", vault)
    return _observer


# ──────────────────────────── Background Init ────────────────────────────────

def background_init(vault: str):
    """Full index + start watcher — runs in a background thread at startup."""
    time.sleep(1)  # give the MCP server a moment to start
    _INDEXING_IN_PROGRESS.set()
    try:
        init_db()
        index_vault(vault)
        start_watcher(vault)
    except Exception as e:
        log.error("Background init failed: %s", e)
    finally:
        _INDEXING_IN_PROGRESS.clear()


# ─────────────────────────── Shutdown Handler ────────────────────────────────

def _shutdown():
    """Stop the watcher and close the DB pool, then cancel the event loop.

    Called via loop.add_signal_handler() so it runs on the event loop thread,
    making it safe to call asyncio-adjacent code without deadlocking.
    Blocking operations (observer.join) are intentionally absent — the daemon
    thread will be killed when the process exits.
    """
    log.info("Shutting down…")
    if _observer is not None:
        _observer.stop()
    if _pool is not None:
        _pool.closeall()
    asyncio.get_event_loop().stop()


# ──────────────────────────── Vault Filesystem Helpers ───────────────────────

def _vault_root() -> Path:
    return Path(VAULT_PATH)


def _resolve_vault_path(relpath: str) -> Path:
    """Resolve a vault-relative path safely (no escaping the vault)."""
    resolved = (_vault_root() / relpath).resolve()
    vault_resolved = _vault_root().resolve()
    if not resolved.is_relative_to(vault_resolved):
        raise ValueError(f"Path escapes vault: {relpath}")
    return resolved


def _relative(abspath: Path) -> str:
    """Return vault-relative path string."""
    try:
        return str(abspath.relative_to(_vault_root()))
    except ValueError:
        return str(abspath)


# ───────────────────────────────── MCP Server ────────────────────────────────

server = Server("obsidian-semantic")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="search_vault",
            description=(
                "Semantic search across your entire Obsidian vault. "
                "Returns the most relevant note excerpts by meaning, not just keyword matching. "
                "Use this to retrieve context, past decisions, notes, or research from the vault."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results to return (default: 5, max: 20)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_indexed_notes",
            description="List all notes that have been indexed, with their last indexed timestamp.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="reindex_vault",
            description=(
                "Force a full re-index of all notes in the vault. "
                "Runs in the background — use list_indexed_notes to check progress."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        # ── Vault CRUD tools ─────────────────────────────────────────────────
        Tool(
            name="list_files",
            description="List all files and directories in a vault directory. Defaults to vault root.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dirpath": {
                        "type": "string",
                        "description": "Directory path relative to vault root (default: root)",
                        "default": "",
                    },
                },
            },
        ),
        Tool(
            name="get_file",
            description="Read the full content of a file in the vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "File path relative to vault root",
                    },
                },
                "required": ["filepath"],
            },
        ),
        Tool(
            name="get_files_batch",
            description="Read the contents of multiple files at once.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepaths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths relative to vault root",
                    },
                },
                "required": ["filepaths"],
            },
        ),
        Tool(
            name="append_content",
            description="Append content to the end of a file. Creates the file if it doesn't exist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "File path relative to vault root",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to append",
                    },
                },
                "required": ["filepath", "content"],
            },
        ),
        Tool(
            name="write_file",
            description=(
                "Write or overwrite a file in the vault. Creates parent directories if needed. "
                "WARNING: overwrites existing content without confirmation — use append_content "
                "if you want to add to an existing file without replacing it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "File path relative to vault root",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content to write",
                    },
                },
                "required": ["filepath", "content"],
            },
        ),
        Tool(
            name="simple_search",
            description=(
                "Text/keyword search across vault files. "
                "Use search_vault for semantic/meaning-based search, "
                "use this for exact text matching."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Text to search for (case-insensitive)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default: 10)",
                        "default": 10,
                    },
                    "context_length": {
                        "type": "integer",
                        "description": "Characters of context around each match (default: 100)",
                        "default": 100,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="recent_changes",
            description="Get recently modified files in the vault.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max files to return (default: 10)",
                        "default": 10,
                    },
                    "days": {
                        "type": "integer",
                        "description": "Only files modified within this many days (default: 30)",
                        "default": 30,
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):

    # ── search_vault ──────────────────────────────────────────────────────────
    if name == "search_vault":
        query = arguments.get("query", "").strip()
        limit = max(1, min(int(arguments.get("limit", 5)), 20))

        if not query:
            return [TextContent(type="text", text="Please provide a search query.")]

        try:
            loop = asyncio.get_running_loop()
            vec = await loop.run_in_executor(None, embed, query)
            vec_str = _vec_to_str(vec)

            with db_conn() as conn:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT path, content,
                                   1 - (embedding <=> %s::vector) AS similarity
                            FROM notes
                            ORDER BY embedding <=> %s::vector
                            LIMIT %s
                        """, (vec_str, vec_str, limit))
                        rows = cur.fetchall()

            if not rows:
                if _INDEXING_IN_PROGRESS.is_set():
                    return [TextContent(
                        type="text",
                        text="Vault indexing is in progress — no results yet. Try again in a moment.",
                    )]
                return [TextContent(
                    type="text",
                    text="No indexed notes found. Try running reindex_vault first.",
                )]

            parts = []
            for path, content, sim in rows:
                rel = _relative(Path(path))
                preview = content[:600].strip()
                # collapse excess blank lines
                while "\n\n\n" in preview:
                    preview = preview.replace("\n\n\n", "\n\n")
                parts.append(f"**{rel}** _(similarity: {sim:.2f})_\n\n{preview}\n")

            return [TextContent(type="text", text="\n---\n".join(parts))]

        except Exception as e:
            log.error("search_vault error: %s", e)
            return [TextContent(type="text", text=f"Search error: {e}")]

    # ── list_indexed_notes ────────────────────────────────────────────────────
    elif name == "list_indexed_notes":
        try:
            with db_conn() as conn:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT path, indexed_at
                            FROM notes
                            ORDER BY indexed_at DESC
                        """)
                        rows = cur.fetchall()

            if not rows:
                return [TextContent(
                    type="text",
                    text="No notes indexed yet. Run reindex_vault to start.",
                )]

            lines = [f"**{len(rows)} notes indexed**\n"]
            for path, ts in rows:
                rel = _relative(Path(path))
                lines.append(f"- {rel}  _(indexed {ts.strftime(_TIMESTAMP_FMT)})_")

            return [TextContent(type="text", text="\n".join(lines))]

        except Exception as e:
            log.error("list_indexed_notes error: %s", e)
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── reindex_vault ─────────────────────────────────────────────────────────
    elif name == "reindex_vault":
        if not VAULT_PATH:
            return [TextContent(
                type="text",
                text="OBSIDIAN_VAULT environment variable is not set.",
            )]

        threading.Thread(
            target=index_vault,
            args=(VAULT_PATH,),
            daemon=True,
        ).start()

        return [TextContent(
            type="text",
            text=(
                f"Re-indexing started in background for vault: {VAULT_PATH}\n"
                "Use list_indexed_notes to check progress."
            ),
        )]

    # ── list_files ─────────────────────────────────────────────────────────────
    elif name == "list_files":
        try:
            dirpath = arguments.get("dirpath", "")
            target = _resolve_vault_path(dirpath) if dirpath else _vault_root()
            if not target.is_dir():
                return [TextContent(type="text", text=f"Not a directory: {dirpath}")]

            entries = sorted(target.iterdir())
            lines = []
            for e in entries:
                if e.name.startswith("."):
                    continue
                rel = _relative(e)
                prefix = "📁 " if e.is_dir() else "📄 "
                lines.append(f"{prefix}{rel}")

            return [TextContent(type="text", text="\n".join(lines) or "Empty directory")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── get_file ──────────────────────────────────────────────────────────────
    elif name == "get_file":
        try:
            filepath = arguments.get("filepath", "")
            target = _resolve_vault_path(filepath)
            if not target.is_file():
                return [TextContent(type="text", text=f"File not found: {filepath}")]
            content = target.read_text(encoding="utf-8", errors="ignore")
            return [TextContent(type="text", text=content)]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── get_files_batch ───────────────────────────────────────────────────────
    elif name == "get_files_batch":
        try:
            filepaths = arguments.get("filepaths", [])
            parts = []
            for fp in filepaths:
                target = _resolve_vault_path(fp)
                if target.is_file():
                    content = target.read_text(encoding="utf-8", errors="ignore")
                    parts.append(f"--- {fp} ---\n{content}")
                else:
                    parts.append(f"--- {fp} ---\n[File not found]")
            return [TextContent(type="text", text="\n\n".join(parts))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── append_content ────────────────────────────────────────────────────────
    elif name == "append_content":
        try:
            filepath = arguments.get("filepath", "")
            content = arguments.get("content", "")
            target = _resolve_vault_path(filepath)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(content)
            log.info("Appended to: %s", filepath)
            return [TextContent(type="text", text=f"Appended to {filepath}")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── write_file ────────────────────────────────────────────────────────────
    elif name == "write_file":
        try:
            filepath = arguments.get("filepath", "")
            content = arguments.get("content", "")
            target = _resolve_vault_path(filepath)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            log.info("Wrote: %s", filepath)
            return [TextContent(type="text", text=f"Wrote {filepath} ({len(content)} chars)")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── simple_search ─────────────────────────────────────────────────────────
    elif name == "simple_search":
        try:
            query = arguments.get("query", "").strip()
            limit = max(1, min(int(arguments.get("limit", 10)), 50))
            ctx_len = max(1, int(arguments.get("context_length", 100)))
            if not query:
                return [TextContent(type="text", text="Please provide a search query.")]

            query_lower = query.lower()
            results = []
            root = _vault_root()
            for f in root.rglob("*.md"):
                if _is_system_path(f):
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                text_lower = text.lower()
                idx = text_lower.find(query_lower)
                if idx == -1:
                    continue
                # Collect match contexts
                matches = []
                search_from = 0
                while len(matches) < 3:
                    idx = text_lower.find(query_lower, search_from)
                    if idx == -1:
                        break
                    start = max(0, idx - ctx_len)
                    end = min(len(text), idx + len(query) + ctx_len)
                    matches.append(text[start:end].strip())
                    search_from = idx + len(query)

                results.append((_relative(f), matches))
                if len(results) >= limit:
                    break

            if not results:
                return [TextContent(type="text", text=f"No matches for: {query}")]

            parts = []
            for rel, matches in results:
                match_text = "\n".join(f"  ...{m}..." for m in matches)
                parts.append(f"**{rel}**\n{match_text}")
            return [TextContent(type="text", text="\n\n".join(parts))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    # ── recent_changes ────────────────────────────────────────────────────────
    elif name == "recent_changes":
        try:
            limit = min(int(arguments.get("limit", 10)), 100)
            days = int(arguments.get("days", 30))
            cutoff = time.time() - (days * 86400)
            root = _vault_root()

            files = []
            for f in root.rglob("*.md"):
                if _is_system_path(f):
                    continue
                try:
                    mtime = f.stat().st_mtime
                    if mtime >= cutoff:
                        files.append((mtime, f))
                except Exception:
                    continue

            files.sort(key=lambda x: x[0], reverse=True)
            files = files[:limit]

            if not files:
                return [TextContent(type="text", text=f"No files modified in the last {days} days.")]

            lines = [f"**{len(files)} recently modified files** (last {days} days)\n"]
            for mtime, f in files:
                dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                lines.append(f"- {_relative(f)}  _{dt.strftime(_TIMESTAMP_FMT)}_")

            return [TextContent(type="text", text="\n".join(lines))]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ──────────────────────────────── Entry Point ────────────────────────────────

async def main():
    if not VAULT_PATH:
        log.error("OBSIDIAN_VAULT is not set. Export it before running.")
        sys.exit(1)

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, _shutdown)
    loop.add_signal_handler(signal.SIGINT, _shutdown)

    # Full index + watcher starts in background — server is immediately ready
    threading.Thread(
        target=background_init,
        args=(VAULT_PATH,),
        daemon=True,
    ).start()

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
