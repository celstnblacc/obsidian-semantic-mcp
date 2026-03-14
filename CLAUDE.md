# Obsidian Semantic MCP

Python MCP server — semantic search across an Obsidian vault using pgvector + Ollama.

## Build & Run

```bash
# Full stack (postgres, ollama, mcp-server, dashboard)
OBSIDIAN_VAULT="/path/to/vault" docker compose up -d

# Rebuild after code changes
docker compose up -d --build mcp-server dashboard

# Wipe all data and re-index from scratch
docker compose down -v
```

## Test

```bash
uv run pytest tests/ -q
```

## Key Commands

```bash
# Install deps
uv sync

# Run server natively
OBSIDIAN_VAULT="/path/to/vault" DATABASE_URL="postgresql://localhost/obsidian_brain" uv run python3 src/server.py

# Run dashboard
OBSIDIAN_VAULT="/path/to/vault" uv run python3 src/dashboard.py
```

## Project Conventions

- All database connections closed via `try/finally conn.close()` — no context manager shortcut
- `_handle_upsert` must catch all exceptions — watchdog thread must never die
- Empty Ollama embeddings (`[]`) raise `ValueError` — never insert invalid vectors
- `_resolve_vault_path()` enforces vault root — no path traversal
- Logging uses `%s` lazy format — no f-strings in log calls
- `_INDEXING_IN_PROGRESS` flag gates first-boot search messages
