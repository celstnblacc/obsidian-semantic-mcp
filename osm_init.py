#!/usr/bin/env python3
"""
osm_init.py — Obsidian Semantic MCP setup wizard.

Usage (after uv sync):
  python3 osm_init.py init      Interactive setup wizard
  python3 osm_init.py status    Check service health
  python3 osm_init.py rebuild   Rebuild Docker images

Or via the scripts/osm wrapper:
  scripts/osm init
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


# ── Terminal output ───────────────────────────────────────────────────────────

_TTY = sys.stdout.isatty()


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def ok(msg):    print(f"  {_c('92', '✓')}  {msg}")
def warn(msg):  print(f"  {_c('93', '⚠')}  {msg}")
def fail(msg):  print(f"  {_c('91', '✗')}  {msg}")
def info(msg):  print(f"  {_c('94', '→')}  {msg}")
def header(msg): print(f"\n{_c('1', msg)}")
def hr():        print("─" * 60)


# ── Subprocess helpers ────────────────────────────────────────────────────────

def run(cmd, check=True, capture=False, env=None):
    kwargs = {"shell": isinstance(cmd, str), "check": check}
    if env:
        kwargs["env"] = env
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return subprocess.run(cmd, **kwargs)


def cmd_exists(name):
    return shutil.which(name) is not None


# ── Prompts ───────────────────────────────────────────────────────────────────

def prompt(question, default=None, choices=None):
    hint = f" [{default}]" if default else ""
    if choices:
        hint = f" ({'/'.join(choices)})"
    while True:
        try:
            answer = input(f"  {question}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if not answer and default is not None:
            return default
        if choices and answer not in choices:
            print(f"  Please enter one of: {', '.join(choices)}")
            continue
        if answer:
            return answer
        print("  Please enter a value.")


def confirm(question, default="y"):
    return prompt(question, default=default, choices=["y", "n"]).lower() == "y"


def prompt_vault():
    existing = os.environ.get("OBSIDIAN_VAULT", "")
    print()
    if existing:
        info(f"OBSIDIAN_VAULT is already set: {existing}")
        if confirm("Use this vault?"):
            return existing
    while True:
        raw = prompt("Absolute path to your Obsidian vault")
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            return str(p)
        fail(f"Directory not found: {p}")


def prompt_pg_password():
    return prompt("Postgres password (used for the local Docker DB)", default="obsidian")


# ── Prerequisite checks ───────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.resolve()


def check_docker():
    if not cmd_exists("docker"):
        fail("Docker not found — install Docker Desktop: https://docs.docker.com/get-docker/")
        return False
    r = run("docker info", check=False, capture=True)
    if r.returncode != 0:
        fail("Docker daemon is not running — start Docker Desktop first")
        return False
    ok("Docker is running")
    return True


def check_compose():
    r = run("docker compose version", check=False, capture=True)
    if r.returncode != 0:
        fail("docker compose v2 not found — upgrade Docker Desktop")
        return False
    ok("docker compose v2 available")
    return True


def check_ollama_at(host, port=11434):
    url = f"http://{host}:{port}/api/tags"
    try:
        urllib.request.urlopen(url, timeout=4)
        ok(f"Ollama reachable at {host}:{port}")
        return True
    except Exception:
        fail(f"Ollama not reachable at {host}:{port}")
        return False


# ── .env writer (runtime only — gitignored) ───────────────────────────────────

def write_env(vault, pg_password, ollama_url):
    """Write .env in the project root at runtime. This file is gitignored."""
    env_path = PROJECT_ROOT / ".env"
    content = "\n".join([
        f"OBSIDIAN_VAULT={vault}",
        f"POSTGRES_PASSWORD={pg_password}",
        f"OLLAMA_URL={ollama_url}",
        "",
    ])
    env_path.write_text(content)
    ok(f"Wrote {env_path}")


# ── Claude Desktop config ─────────────────────────────────────────────────────

def _claude_cfg_path():
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Linux":
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    return None


def update_claude_config(entry):
    path = _claude_cfg_path()
    if not path:
        warn("Unknown platform — update claude_desktop_config.json manually")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except json.JSONDecodeError:
            warn(f"Could not parse {path} — mcpServers section will be reset")
    cfg.setdefault("mcpServers", {})["obsidian-semantic"] = entry
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    ok(f"Updated {path}")
    info("Restart Claude Desktop to pick up the new server")


def _docker_entry():
    """Claude Desktop config entry for all Docker-based installs."""
    container = f"{PROJECT_ROOT.name}-mcp-server-1"
    return {
        "command": "docker",
        "args": ["exec", "-i", container, "python3", "src/server.py"],
        "env": {},
    }


def _native_entry(vault, db_url):
    """Claude Desktop config entry for native install."""
    return {
        "command": str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
        "args": [str(PROJECT_ROOT / "src" / "server.py")],
        "env": {
            "OBSIDIAN_VAULT": vault,
            "DATABASE_URL": db_url,
        },
    }


# ── Docker compose helpers ────────────────────────────────────────────────────

def compose(cmd, env=None):
    full = f"docker compose --project-directory {PROJECT_ROOT} {cmd}"
    return run(full, env=env)


def compose_up(services=None, env=None):
    svc = " ".join(services) if services else ""
    compose(f"up -d {svc}".strip(), env=env)


def wait_for_postgres(timeout=90):
    info("Waiting for postgres to be healthy…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = run(
            f"docker compose --project-directory {PROJECT_ROOT} exec -T postgres "
            "pg_isready -U obsidian -d obsidian_brain",
            check=False, capture=True,
        )
        if r.returncode == 0:
            ok("Postgres is ready")
            return True
        time.sleep(3)
    fail(f"Postgres did not become ready within {timeout}s")
    return False


# ── Install modes ─────────────────────────────────────────────────────────────

def mode_native_macos():
    header("Native install  (Homebrew + local Postgres + local Ollama)")
    hr()

    if not cmd_exists("brew"):
        fail("Homebrew not found — install from https://brew.sh")
        sys.exit(1)
    ok("Homebrew found")

    vault = prompt_vault()

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    header("PostgreSQL + pgvector")
    if not cmd_exists("psql"):
        info("Installing postgresql@17 and pgvector via Homebrew…")
        run("brew install postgresql@17 pgvector")
        run("brew services start postgresql@17")
        time.sleep(3)
    else:
        ok("psql already installed")

    r = run("psql postgres -lqt", check=False, capture=True)
    if "obsidian_brain" not in (r.stdout or ""):
        run("createdb obsidian_brain")
        run('psql obsidian_brain -c "CREATE EXTENSION IF NOT EXISTS vector;"')
        ok("Created database: obsidian_brain")
    else:
        ok("Database obsidian_brain already exists")

    db_url = "postgresql://localhost/obsidian_brain"

    # ── Ollama ────────────────────────────────────────────────────────────────
    header("Ollama + embedding model")
    if not cmd_exists("ollama"):
        info("Installing ollama via Homebrew…")
        run("brew install ollama")

    if not check_ollama_at("localhost"):
        info("Starting ollama serve in background…")
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)

    info("Pulling nomic-embed-text (first run may take a few minutes)…")
    run("ollama pull nomic-embed-text")
    ok("Model ready: nomic-embed-text")

    # ── Python env ────────────────────────────────────────────────────────────
    header("Python environment")
    if not cmd_exists("uv"):
        fail("uv not found — install from https://github.com/astral-sh/uv")
        sys.exit(1)
    run(f"uv sync --project {PROJECT_ROOT}")
    ok("Dependencies installed in .venv")

    # ── Claude Desktop config ─────────────────────────────────────────────────
    header("Claude Desktop configuration")
    update_claude_config(_native_entry(vault, db_url))

    _done_native(vault)


def mode_full_docker():
    header("Full Docker  (Postgres + Ollama + MCP server all in containers)")
    hr()

    if not check_docker() or not check_compose():
        sys.exit(1)

    vault = prompt_vault()
    pg_pw = prompt_pg_password()
    write_env(vault, pg_pw, "http://ollama:11434")

    header("Starting all services")
    env = {**os.environ, "OBSIDIAN_VAULT": vault, "POSTGRES_PASSWORD": pg_pw}
    compose_up(env=env)
    wait_for_postgres()

    header("Claude Desktop configuration")
    update_claude_config(_docker_entry())
    _done_docker()


def mode_docker_host_ollama():
    header("Docker + host Ollama  (Postgres in Docker, Ollama already running on this machine)")
    hr()

    if not check_docker() or not check_compose():
        sys.exit(1)

    if not check_ollama_at("localhost"):
        fail("Ollama is not running — start it first:  ollama serve")
        info("Then re-run:  osm init")
        sys.exit(1)

    # host.docker.internal resolves to the Docker host on macOS and Windows;
    # on Linux you may need to pass --add-host or use the bridge IP.
    system = platform.system()
    ollama_host = "host.docker.internal" if system in ("Darwin", "Windows") else "172.17.0.1"
    ollama_url  = f"http://{ollama_host}:11434"

    vault = prompt_vault()
    pg_pw = prompt_pg_password()
    write_env(vault, pg_pw, ollama_url)

    header("Starting services (postgres, mcp-server, dashboard)")
    env = {**os.environ, "OBSIDIAN_VAULT": vault, "POSTGRES_PASSWORD": pg_pw, "OLLAMA_URL": ollama_url}
    compose_up(services=["postgres", "mcp-server", "dashboard"], env=env)
    wait_for_postgres()

    header("Claude Desktop configuration")
    update_claude_config(_docker_entry())
    _done_docker()


def mode_docker_remote_ollama():
    header("Docker + remote Ollama  (Postgres in Docker, Ollama on another host)")
    hr()

    if not check_docker() or not check_compose():
        sys.exit(1)

    print()
    remote_host = prompt("Remote Ollama hostname or IP address")
    remote_port = prompt("Remote Ollama port", default="11434")

    if not check_ollama_at(remote_host, int(remote_port)):
        if not confirm("Ollama not reachable — continue anyway?", default="n"):
            sys.exit(0)

    ollama_url = f"http://{remote_host}:{remote_port}"

    vault = prompt_vault()
    pg_pw = prompt_pg_password()
    write_env(vault, pg_pw, ollama_url)

    header("Starting services (postgres, mcp-server, dashboard)")
    env = {**os.environ, "OBSIDIAN_VAULT": vault, "POSTGRES_PASSWORD": pg_pw, "OLLAMA_URL": ollama_url}
    compose_up(services=["postgres", "mcp-server", "dashboard"], env=env)
    wait_for_postgres()

    header("Claude Desktop configuration")
    update_claude_config(_docker_entry())
    _done_docker()


# ── Summary printers ──────────────────────────────────────────────────────────

def _done_native(vault):
    print()
    hr()
    ok(_c("1", "Setup complete!"))
    print()
    info(f"Vault: {vault}")
    info("Restart Claude Desktop — server starts automatically")
    hr()


def _done_docker():
    print()
    hr()
    ok(_c("1", "Setup complete!"))
    print()
    info("Dashboard:  http://localhost:8484")
    info("Logs:       docker compose logs -f mcp-server")
    info("Restart Claude Desktop — server starts automatically")
    hr()


# ── Status command ────────────────────────────────────────────────────────────

def cmd_status():
    header("OSM Status")
    hr()

    r = run(
        f"docker compose --project-directory {PROJECT_ROOT} ps --format table",
        check=False, capture=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        print(r.stdout)
    else:
        info("No Docker services running")

    check_ollama_at("localhost")

    cfg_path = _claude_cfg_path()
    if cfg_path and cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            if "obsidian-semantic" in cfg.get("mcpServers", {}):
                ok("Claude Desktop: obsidian-semantic configured")
            else:
                warn("Claude Desktop: obsidian-semantic NOT configured — run osm init")
        except json.JSONDecodeError:
            warn("Claude Desktop config could not be parsed")
    else:
        warn("Claude Desktop config not found")


# ── Rebuild command ───────────────────────────────────────────────────────────

def cmd_rebuild():
    header("Rebuilding Docker images")
    hr()
    compose("up -d --build mcp-server dashboard")
    ok("Rebuild complete")
    info("Dashboard:  http://localhost:8484")


# ── Install mode tables ───────────────────────────────────────────────────────

MODES_MACOS = {
    "1": ("Native",                  "Homebrew + local Postgres + local Ollama",         mode_native_macos),
    "2": ("Docker + host Ollama",    "Postgres in Docker, Ollama already on this Mac",   mode_docker_host_ollama),
    "3": ("Full Docker",             "Everything in containers  (recommended)",           mode_full_docker),
    "4": ("Docker + remote Ollama",  "Postgres in Docker, Ollama on another machine",    mode_docker_remote_ollama),
}

MODES_LINUX = {
    "1": ("Docker + host Ollama",    "Postgres in Docker, Ollama on this machine",       mode_docker_host_ollama),
    "2": ("Full Docker",             "Everything in containers  (recommended)",           mode_full_docker),
    "3": ("Docker + remote Ollama",  "Postgres in Docker, Ollama on another machine",    mode_docker_remote_ollama),
}


# ── Init command ──────────────────────────────────────────────────────────────

def cmd_init():
    print()
    hr()
    print(_c("1", "  Obsidian Semantic MCP — Setup Wizard"))
    hr()

    system = platform.system()
    if system == "Darwin":
        ver  = platform.mac_ver()[0]
        arch = platform.machine()
        print(f"\n  Detected: macOS {ver} ({arch})\n")
        modes = MODES_MACOS
    elif system == "Linux":
        distro = "Linux"
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    distro = line.split("=", 1)[1].strip('"')
        except Exception:
            pass
        print(f"\n  Detected: {distro}\n")
        modes = MODES_LINUX
    else:
        fail(f"Platform {system!r} not yet supported by this wizard")
        info("Follow the manual steps in README.md")
        sys.exit(1)

    print("  Installation modes:\n")
    for key, (name, desc, _) in modes.items():
        print(f"    {_c('1', key)})  {_c('1', name)}")
        print(f"         {desc}")
    print()

    choice = prompt("Choose", choices=list(modes.keys()))
    _, _, handler = modes[choice]
    handler()


# ── Entry point ───────────────────────────────────────────────────────────────

COMMANDS = {
    "init":    (cmd_init,    "Interactive setup wizard"),
    "status":  (cmd_status,  "Check service health"),
    "rebuild": (cmd_rebuild, "Rebuild Docker images and restart"),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"\n  {_c('1', 'osm')} — Obsidian Semantic MCP CLI\n")
        for name, (_, desc) in COMMANDS.items():
            print(f"    osm {name:<10}  {desc}")
        print()
        sys.exit(0 if len(sys.argv) == 1 else 1)

    COMMANDS[sys.argv[1]][0]()


if __name__ == "__main__":
    main()
