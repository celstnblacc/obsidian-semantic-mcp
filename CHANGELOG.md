# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] — 2026-03-14

### Added
- Unified MCP server (`src/server.py`) combining semantic search and full vault CRUD (10 tools)
- PostgreSQL + pgvector backend with IVFFlat cosine similarity index
- Ollama local embeddings (`nomic-embed-text`, 768-dim)
- Live file watcher via `watchdog` — re-indexes on create/modify/delete/move
- Background indexing on startup — MCP server responds immediately while indexing runs
- Monitoring dashboard at `http://localhost:8484` (`src/dashboard.py`)
- Docker Compose stack (postgres, ollama, mcp-server, dashboard) with healthchecks
- Auto-pull of `nomic-embed-text` model on first `docker compose up`
- Root entry point `obsidian_semantic_mcp.py` for Claude Desktop native config
- 12 unit tests (`tests/test_unit.py`)

### Fixed
- Empty Ollama embedding (`[]`) now raises `ValueError` instead of inserting invalid vector
- Watchdog thread survives all exceptions — `DataException` and other errors are caught and logged
- Connection leak — all database connections closed via `try/finally`
- Path traversal protection uses `Path.is_relative_to()` instead of `str.startswith()`
- Docker `exec` venv bypass — `ENV PATH="/app/.venv/bin:$PATH"` ensures correct Python
- XSS in dashboard — vault paths rendered via DOM API instead of `innerHTML`
- First-boot search returns "indexing in progress" message instead of misleading "try reindex_vault"

### Security
- Vault paths validated with `_resolve_vault_path()` — no escaping the vault root
- All logging uses lazy `%s` format — no f-string injection in log calls
- `.gitignore` covers `.env.*`, `*.key`, `*.pem`, `*.p12`, secrets files
