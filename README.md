# Obsidian Semantic MCP

A persistent memory layer for Claude Desktop — semantic search across your entire Obsidian vault using local embeddings and PostgreSQL + pgvector.

## The Problem

AI assistants forget everything between sessions. You repeat context, lose continuity, and start from zero every time. Your notes, projects, and preferences sit in Obsidian but never make it into your AI conversations automatically.

## The Solution

**Obsidian Semantic MCP** turns your vault into a queryable brain for Claude. It:

- Indexes every note as a vector embedding (via Ollama + `nomic-embed-text`)
- Stores embeddings in PostgreSQL with pgvector for fast semantic search
- Watches your vault for changes and re-indexes automatically
- Provides full vault CRUD (read, write, search, list) — works even when Obsidian is closed
- Exposes everything through MCP so Claude can retrieve and manage vault content on the fly

No cloud services. No API keys. Everything runs locally.

## Architecture

```
Claude Desktop
    ↓ MCP protocol (stdio)
src/server.py (unified MCP server)
    ├── Semantic search (pgvector cosine similarity)
    ├── Vault CRUD (direct filesystem access)
    └── Live file watcher (watchdog)
    ↓
PostgreSQL + pgvector (vector storage + IVFFlat index)
    ↓
Ollama / nomic-embed-text (local 768-dim embeddings)
    ↓
Your Obsidian vault ($HOME/.obsidian)
```

## Project Structure

```
obsidian-semantic-mcp/
├── scripts/
│   └── osm                # CLI wrapper — run `scripts/osm init` to set up
├── src/
│   ├── server.py          # MCP server — semantic search + vault CRUD (10 tools)
│   └── dashboard.py       # Monitoring dashboard (http://localhost:8484)
├── tests/
│   ├── test_setup.py      # Prerequisites checker (deps, DB, Ollama)
│   └── test_e2e.py        # End-to-end MCP protocol test
├── osm_init.py            # Interactive setup wizard (used by scripts/osm)
├── Dockerfile             # Python 3.13 + uv
├── docker-compose.yml     # Full stack: postgres, ollama, server, dashboard
├── pyproject.toml         # Project metadata + dependencies (osm script entry)
├── uv.lock                # Pinned lockfile
└── LICENSE                # Apache 2.0
```

## Prerequisites

- An Obsidian vault on your filesystem
- **macOS native:** Homebrew (auto-installs everything else)
- **Docker modes:** Docker Desktop (macOS/Linux/Windows WSL2)

## Quick Start

### 1. Clone

```bash
git clone <repo-url> && cd obsidian-semantic-mcp
```

### 2. Run the setup wizard

```bash
scripts/osm init
```

The wizard detects your OS and asks which installation mode you want:

**macOS:**
```
  1)  Native              Homebrew + local Postgres + local Ollama
  2)  Docker + host Ollama    Postgres in Docker, Ollama already on this Mac
  3)  Full Docker         Everything in containers  (recommended)
  4)  Docker + remote Ollama  Postgres in Docker, Ollama on another machine
```

**Linux:**
```
  1)  Docker + host Ollama    Postgres in Docker, Ollama on this machine
  2)  Full Docker         Everything in containers  (recommended)
  3)  Docker + remote Ollama  Postgres in Docker, Ollama on another machine
```

It then:
- Installs prerequisites (Homebrew packages or Docker images)
- Pulls `nomic-embed-text` if needed
- Writes a `.env` file (gitignored) with your vault path and credentials
- Updates `claude_desktop_config.json` automatically

After the wizard completes, restart Claude Desktop — no further configuration needed.

---

### Manual start (without wizard)

```bash
OBSIDIAN_VAULT="/path/to/your/vault" POSTGRES_PASSWORD=obsidian docker compose up -d
```

> Docker Compose also reads a `.env` file in the repo root (gitignored).

First run pulls all images and the `nomic-embed-text` model automatically. This starts:

| Service | Port | Description |
|---------|------|-------------|
| PostgreSQL + pgvector | 5433 | Vector storage (avoids conflict with host pg) |
| Ollama | 11435 | Local embeddings (auto-pulls model) |
| MCP server | stdio | Claude Desktop connects via `docker exec` |
| Dashboard | 8484 | http://localhost:8484 |

### 3. Configure Claude Desktop

Add to `$HOME/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your platform:

```json
{
  "mcpServers": {
    "obsidian-semantic": {
      "command": "docker",
      "args": ["exec", "-i", "obsidian-semantic-mcp-mcp-server-1", "python3", "src/server.py"],
      "env": {}
    }
  }
}
```

> **Container name note:** The container name `obsidian-semantic-mcp-mcp-server-1` is derived from the directory you cloned into. If you cloned into a different folder name, replace `obsidian-semantic-mcp` in the args with your actual directory name. Run `docker ps` to confirm the exact container name.

### 4. Restart Claude Desktop

The server indexes your vault on first run, then watches for changes automatically. Open the dashboard at http://localhost:8484 to monitor progress.

### Useful commands

```bash
# View server logs
docker compose logs -f mcp-server

# Rebuild after code changes
docker compose up -d --build mcp-server dashboard

# Stop everything
docker compose down

# Stop and wipe all data (re-index from scratch)
# ⚠️  WARNING: -v deletes all indexed embeddings. Re-indexing will restart from scratch.
docker compose down -v
```

### GPU support (optional)

For faster embeddings on Linux with NVIDIA GPU, add to the `ollama` service in `docker-compose.yml`:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - capabilities: [gpu]
```

## MCP Tools

### Semantic Search

| Tool | Description |
|------|-------------|
| `search_vault` | Semantic search by meaning across the entire vault. Returns ranked excerpts with similarity scores. |
| `simple_search` | Exact text/keyword search across vault files. |

### Vault Management

| Tool | Description |
|------|-------------|
| `list_files` | List files and directories in a vault directory. |
| `get_file` | Read the full content of a single file. |
| `get_files_batch` | Read multiple files at once. |
| `append_content` | Append content to a file (creates if missing). |
| `write_file` | Write or overwrite a file in the vault. |
| `recent_changes` | Get recently modified files. |

### Index Management

| Tool | Description |
|------|-------------|
| `list_indexed_notes` | List all indexed notes with their last indexed timestamp. |
| `reindex_vault` | Force a full re-index of all notes. Runs in the background. |

## How It Works

1. **Indexing** — On startup, the server walks your vault, reads each `.md` file, generates a 768-dim embedding via Ollama, and upserts it into PostgreSQL with pgvector. Unchanged files (same SHA-256 hash) are skipped.
2. **Watching** — A file watcher (`watchdog`) monitors the vault for creates, updates, deletes, and moves — re-embedding changed files automatically.
3. **Searching** — When Claude calls `search_vault`, the query is embedded and matched against stored vectors using cosine similarity (IVFFlat index). The top results are returned with similarity scores and content previews.
4. **CRUD** — All file operations use direct filesystem access, so the server works whether Obsidian is open or not. Path traversal outside the vault is blocked.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OBSIDIAN_VAULT` | Absolute path to your Obsidian vault | *required* |
| `POSTGRES_PASSWORD` | PostgreSQL password (Docker) | *required for Docker* |
| `DATABASE_URL` | Full connection string (overrides POSTGRES_* vars) | built from POSTGRES_* vars |
| `POSTGRES_HOST` | PostgreSQL host | `localhost` |
| `POSTGRES_PORT` | PostgreSQL port | `5432` |
| `POSTGRES_DB` | Database name | `obsidian_brain` |
| `POSTGRES_USER` | Database user | `obsidian` |
| `OLLAMA_URL` | Ollama API endpoint | `http://localhost:11434` |
| `EMBEDDING_MODEL` | Ollama model for embeddings | `nomic-embed-text` |

## Monitoring Dashboard

A built-in dashboard is available at http://localhost:8484 (started automatically with Docker). It shows:

- Service health (PostgreSQL, Ollama, embedding model)
- Indexed notes count, vault coverage, DB size
- Recently indexed files
- A "Start Ollama" button if Ollama is down

To run the dashboard without Docker:

```bash
OBSIDIAN_VAULT="/path/to/your/vault" uv run python3 src/dashboard.py
```

## Testing

Two test scripts are included (for native installs):

### `test_setup.py` — Prerequisites check

Verifies Python deps, vault path, PostgreSQL + pgvector, Ollama, and embedding smoke test.

```bash
OBSIDIAN_VAULT="/path/to/your/vault" uv run python3 tests/test_setup.py
```

### `test_e2e.py` — End-to-end MCP test

Launches the server, initializes MCP protocol, waits for indexing, runs semantic search, and verifies results.

```bash
OBSIDIAN_VAULT="/path/to/your/vault" uv run python3 tests/test_e2e.py
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: No module named 'mcp'` | System Python instead of venv | Use `.venv/bin/python3` in config, or use Docker |
| `ModuleNotFoundError: No module named 'psycopg2'` in Docker | Container built before venv PATH fix | `docker compose up -d --build mcp-server` |
| `Search returns 0 results` | IVFFlat index built on empty table | Run `psql obsidian_brain -c "REINDEX INDEX notes_embedding_idx;"` |
| `Vault indexing is in progress — no results yet` | First-boot indexing not complete | Wait for indexing to finish (check `docker compose logs -f mcp-server`) |
| `Cannot reach Ollama` | Ollama not running | Run `ollama serve` or `docker compose up ollama` |
| `Skipped <file>: vector must have at least 1 dimension` | Ollama returned empty embedding (blank/tiny file) | Expected — file is skipped and indexing continues |
| `Skipped <file>: 500 Server Error` | Ollama internal error (file too large or model issue) | Expected — file is skipped; try `ollama pull nomic-embed-text` to refresh model |
| `pgvector extension not found` | Not installed for your PG version | Use Docker, or build from source (see native install) |
| Server crashes on startup | `OBSIDIAN_VAULT` not set | Set the env var in your config or docker compose command |
| Docker container can't see vault | Wrong path or missing volume | Ensure `OBSIDIAN_VAULT` is an absolute path accessible to Docker |

---

## Native Install (macOS)

If you prefer running without Docker:

### 1. Clone and install

```bash
git clone <repo-url> && cd obsidian-semantic-mcp
uv sync
```

### 2. Install system dependencies

```bash
brew install postgresql@17 pgvector ollama
brew services start postgresql@17
ollama serve &
ollama pull nomic-embed-text
```

> **PostgreSQL 16:** Homebrew's `pgvector` bottle requires pg17 or pg18. If you must use pg16, build pgvector from source:
> ```bash
> cd /tmp
> git clone --branch v0.8.2 --depth 1 https://github.com/pgvector/pgvector.git
> cd pgvector
> make PG_CONFIG=$(brew --prefix postgresql@16)/bin/pg_config
> make install PG_CONFIG=$(brew --prefix postgresql@16)/bin/pg_config
> ```

### 3. Set up the database

```bash
createdb obsidian_brain
psql obsidian_brain -c "CREATE EXTENSION vector;"
```

### 4. Verify

```bash
OBSIDIAN_VAULT="/path/to/your/vault" uv run python3 tests/test_setup.py
```

### 5. Configure Claude Desktop

Add to `$HOME/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "obsidian-semantic": {
      "command": "/absolute/path/to/obsidian-semantic-mcp/.venv/bin/python3",
      "args": ["/absolute/path/to/obsidian-semantic-mcp/src/server.py"],
      "env": {
        "OBSIDIAN_VAULT": "/absolute/path/to/your/vault",
        "DATABASE_URL": "postgresql://localhost/obsidian_brain"
      }
    }
  }
}
```

> **Important:** Use `.venv/bin/python3` — not system Python. Homebrew Python won't have the required packages.

### 6. Restart Claude Desktop

The server indexes your vault on first run, then watches for changes automatically.

## Cost

Everything runs locally. No cloud APIs, no subscriptions. The only cost is disk space for the database (~a few MB for most vaults).

## License

Apache 2.0
