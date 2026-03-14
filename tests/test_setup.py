#!/usr/bin/env python3
"""
test_setup.py — Checks every dependency before running the MCP server.
Usage:
    source .venv/bin/activate
    OBSIDIAN_VAULT="/path/to/vault" python3 test_setup.py
"""

import os
import sys

VAULT_PATH   = os.environ.get("OBSIDIAN_VAULT", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/obsidian_brain")
OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL  = os.environ.get("EMBEDDING_MODEL", "nomic-embed-text")

OK   = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m⚠\033[0m"

passed = 0
failed = 0

def ok(msg):
    global passed
    passed += 1
    print(f"  {OK}  {msg}")

def fail(msg, hint=""):
    global failed
    failed += 1
    print(f"  {FAIL}  {msg}")
    if hint:
        print(f"       → {hint}")

def warn(msg):
    print(f"  {WARN}  {msg}")

# ─── 1. Python imports ────────────────────────────────────────────────────────
print("\n[1] Python dependencies")
for pkg, import_name in [
    ("mcp",              "mcp"),
    ("psycopg2",         "psycopg2"),
    ("requests",         "requests"),
    ("watchdog",         "watchdog"),
]:
    try:
        __import__(import_name)
        ok(pkg)
    except ImportError:
        fail(pkg, f"pip install {pkg}")

# ─── 2. Vault path ────────────────────────────────────────────────────────────
print("\n[2] Obsidian vault")
if not VAULT_PATH:
    fail("OBSIDIAN_VAULT not set", "export OBSIDIAN_VAULT=/path/to/your/vault")
else:
    from pathlib import Path
    vault = Path(VAULT_PATH)
    if not vault.exists():
        fail(f"Path does not exist: {VAULT_PATH}")
    elif not vault.is_dir():
        fail(f"Not a directory: {VAULT_PATH}")
    else:
        md_files = list(vault.rglob("*.md"))
        ok(f"Vault found: {VAULT_PATH}")
        ok(f"Markdown files: {len(md_files)}")

# ─── 3. PostgreSQL + pgvector ─────────────────────────────────────────────────
print("\n[3] PostgreSQL + pgvector")
try:
    import psycopg2
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT version();")
    version = cur.fetchone()[0].split(",")[0]
    ok(f"Connected: {version}")

    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    conn.commit()
    ok("pgvector extension enabled")

    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
    row = cur.fetchone()
    if row:
        ok(f"pgvector version: {row[0]}")
    else:
        fail("pgvector extension not found", "brew install pgvector")

    cur.close()
    conn.close()
except Exception as e:
    fail(f"PostgreSQL error: {e}",
         "brew services start postgresql@16  &&  createdb obsidian_brain")

# ─── 4. Ollama + embedding model ──────────────────────────────────────────────
print("\n[4] Ollama")
try:
    import requests
    resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
    resp.raise_for_status()
    models = [m["name"] for m in resp.json().get("models", [])]
    ok(f"Ollama running at {OLLAMA_URL}")

    if any(EMBED_MODEL in m for m in models):
        ok(f"Model ready: {EMBED_MODEL}")
    else:
        fail(f"Model not found: {EMBED_MODEL}", f"ollama pull {EMBED_MODEL}")
        if models:
            warn(f"Available models: {', '.join(models)}")
except Exception as e:
    fail(f"Cannot reach Ollama: {e}", "ollama serve")

# ─── 5. Quick embed test ──────────────────────────────────────────────────────
print("\n[5] Embedding smoke test")
try:
    import requests
    resp = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": "test"},
        timeout=30,
    )
    resp.raise_for_status()
    vec = resp.json().get("embedding", [])
    if len(vec) > 0:
        ok(f"Embedding returned {len(vec)}-dim vector")
    else:
        fail("Empty embedding returned")
except Exception as e:
    fail(f"Embedding failed: {e}")

# ─── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{'─'*40}")
total = passed + failed
print(f"  {passed}/{total} checks passed\n")

if failed == 0:
    print("  \033[92mAll good — ready to run the MCP server.\033[0m\n")
    sys.exit(0)
else:
    print("  \033[91mFix the issues above, then re-run this script.\033[0m\n")
    sys.exit(1)
