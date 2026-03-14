# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

---

## [0.2.0] — 2026-03-14

### Added
- 183 unit tests covering server, osm CLI wizard, and all user-facing decision paths
- `tests/test_osm_commands.py` — 129 tests for every osm command and install mode
- `tests/conftest.py` — shared `_reset()` helper extracted from both test suites
- Non-interactive `osm init` flags (`--mode`, `--vault`, `--pg-password`, `--persistent`, etc.) for script/agent use
- `--dry-run` flag — preview all actions without making any changes
- `osm remove` command — stop services, wipe volumes and config
- README "Using with Claude" section — example prompts and osm CLI command reference

### Fixed
- README test count updated to reflect current suite (183 tests)
- README Quick Start now mentions `--dry-run` tip
- Path containment check uses `Path.is_relative_to()` instead of `str.startswith()`
- LIMIT clamping assertion checks parameterized query tuple, not SQL string

---

## [0.1.0] — 2026-01-01

### Added
- Initial release
- Semantic search MCP server for Obsidian vaults (pgvector + Ollama)
- PostgreSQL connection pool (`ThreadedConnectionPool(1,5)`)
- Vault file watcher with debounce (watchdog)
- Full CRUD MCP tools: `search_vault`, `simple_search`, `list_files`, `get_file`, `get_files_batch`, `append_content`, `write_file`, `recent_changes`, `list_indexed_notes`, `reindex_vault`
- Monitoring dashboard at `http://localhost:8484`
- Docker Compose full-stack setup (postgres, ollama, mcp-server, dashboard)
- `osm init` interactive wizard — macOS modes 1–4, Linux modes 1–3
- SSH tunnel support for remote Ollama hosts (mode 4)
- sshfs vault mounting for remote vaults
- Persistent bind-mount volumes option (`--persistent`, `--data-dir`)
- Graceful shutdown handling
- Apache 2.0 license
