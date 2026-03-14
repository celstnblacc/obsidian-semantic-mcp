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


# ── Dry-run state ─────────────────────────────────────────────────────────────

DRY_RUN     = False
_DRY_ACTIONS: list[str] = []   # collects every skipped action for the summary


def _dry(label, detail=""):
    """Print a dry-run notice and record it for the end-of-run summary."""
    line = f"{label}{('  # ' + detail) if detail else ''}"
    print(f"  {_c('90', '[dry-run]')}  {line}")
    _DRY_ACTIONS.append(line)


# ── Subprocess helpers ────────────────────────────────────────────────────────

def run(cmd, check=True, capture=False, env=None):
    if DRY_RUN:
        cmd_str = cmd if isinstance(cmd, str) else " ".join(str(a) for a in cmd)
        _dry(cmd_str)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
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


# ── SSH tunnel helpers ────────────────────────────────────────────────────────

_SSH_KEY_CANDIDATES = [
    ".ssh/id_ed25519",
    ".ssh/id_rsa",
    ".ssh/id_ecdsa",
    ".ssh/id_ecdsa_sk",
]


def _default_ssh_key():
    """Return the first SSH private key found in $HOME/.ssh, or empty string."""
    for name in _SSH_KEY_CANDIDATES:
        p = Path.home() / name
        if p.exists():
            return str(p)
    return ""


def open_ssh_tunnel(user, host, remote_port, local_port, key_path=None):
    """
    Open an SSH port-forward tunnel in the background:
      local_port  ->  host:remote_port

    Uses -o ExitOnForwardFailure so the ssh process exits immediately if
    binding fails, instead of silently hanging.
    """
    cmd = [
        "ssh",
        "-N", "-f",                          # background, no remote command
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ExitOnForwardFailure=yes",
        "-L", f"{local_port}:localhost:{remote_port}",
        f"{user}@{host}",
    ]
    if key_path:
        cmd += ["-i", key_path]

    if DRY_RUN:
        _dry(" ".join(cmd), f"tunnel localhost:{local_port} → {host}:{remote_port}")
        return True
    r = subprocess.run(cmd, check=False)
    if r.returncode == 0:
        ok(f"SSH tunnel open: localhost:{local_port} → {host}:{remote_port}")
        return True
    fail("SSH tunnel failed — check host, user, and key")
    return False


def prompt_ssh_credentials():
    """
    Interactively collect SSH connection details.
    Returns (user, host, remote_port, key_path_or_None).
    """
    print()
    remote_host = prompt("Remote host (IP address or hostname)")
    remote_port = prompt("Remote Ollama port", default="11434")
    ssh_user    = prompt("SSH username", default=os.environ.get("USER", "ubuntu"))

    print()
    print("  SSH authentication:")
    print("    1)  Private key  (recommended)")
    print("    2)  Password / SSH agent")
    auth = prompt("Choose", choices=["1", "2"])

    key_path = None
    if auth == "1":
        default_key = _default_ssh_key()
        default_fallback = str(Path.home() / ".ssh" / "id_ed25519")
        raw = prompt("Path to SSH private key", default=default_key or default_fallback)
        key_path = str(Path(raw).expanduser().resolve())
        if not Path(key_path).exists():
            warn(f"Key file not found: {key_path}")
            if not confirm("Continue anyway?", default="n"):
                sys.exit(0)
    else:
        info("Using SSH agent or password — you may be prompted by ssh")

    return ssh_user, remote_host, int(remote_port), key_path


# ── .env writer (runtime only — gitignored) ───────────────────────────────────

def write_env(vault, pg_password, ollama_url, ssh_params=None):
    """
    Write .env in the project root at runtime. This file is gitignored.

    ssh_params, if provided, is a dict with keys:
      user, host, remote_port, local_port, key_path (optional)
    These are stored as OSM_SSH_* vars so `osm tunnel` can reconnect.
    """
    env_path = PROJECT_ROOT / ".env"
    lines = [
        f"OBSIDIAN_VAULT={vault}",
        f"POSTGRES_PASSWORD={pg_password}",
        f"OLLAMA_URL={ollama_url}",
    ]
    if ssh_params:
        lines += [
            "",
            "# SSH tunnel config — used by: scripts/osm tunnel",
            f"OSM_SSH_USER={ssh_params['user']}",
            f"OSM_SSH_HOST={ssh_params['host']}",
            f"OSM_SSH_REMOTE_PORT={ssh_params['remote_port']}",
            f"OSM_SSH_LOCAL_PORT={ssh_params['local_port']}",
        ]
        if ssh_params.get("key_path"):
            lines.append(f"OSM_SSH_KEY={ssh_params['key_path']}")
    lines.append("")
    if DRY_RUN:
        _dry(f"write {env_path}", "contents shown below")
        print()
        for l in lines:
            print(f"    {_c('90', l)}")
        print()
        return
    env_path.write_text("\n".join(lines))
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
    pretty = json.dumps({"mcpServers": {"obsidian-semantic": entry}}, indent=2)
    if DRY_RUN:
        _dry(f"write {path}", "contents shown below")
        print()
        for l in pretty.splitlines():
            print(f"    {_c('90', l)}")
        print()
        return
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
        if DRY_RUN:
            _dry("ollama serve  (background)")
        else:
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


def _prompt_vault_location(ssh_user, ssh_host, key_path=None):
    """
    Ask whether the vault lives on this machine or the remote host.
    If remote, offer to mount it via sshfs and return the local mount point.
    Returns the local vault path to pass to Docker.
    """
    print()
    print("  Where is your Obsidian vault?\n")
    print("    1)  On this machine  (local path)")
    print("    2)  On the remote machine  (will mount via sshfs)")
    loc = prompt("Choose", choices=["1", "2"])

    if loc == "1":
        return prompt_vault()

    # Remote vault via sshfs
    remote_vault = prompt("Path to vault on remote machine (absolute)")
    default_mount = str(Path.home() / "obsidian-remote-vault")
    mount_point   = prompt("Local mount point", default=default_mount)

    mount_path = Path(mount_point).expanduser().resolve()
    if not DRY_RUN:
        mount_path.mkdir(parents=True, exist_ok=True)

    if not cmd_exists("sshfs"):
        warn("sshfs not found — install it first:")
        if platform.system() == "Darwin":
            info("  brew install --cask macfuse && brew install sshfs")
        else:
            info("  sudo apt install sshfs  (or equivalent)")
        if not confirm("Continue without sshfs mount?", default="n"):
            sys.exit(0)
        # Fall back to asking for a local path
        return prompt_vault()

    header("Mounting remote vault via sshfs")
    sshfs_cmd = ["sshfs", f"{ssh_user}@{ssh_host}:{remote_vault}", str(mount_path)]
    if key_path:
        sshfs_cmd += ["-o", f"IdentityFile={key_path}"]
    sshfs_cmd += ["-o", "StrictHostKeyChecking=accept-new", "-o", "reconnect"]

    r = subprocess.run(sshfs_cmd, check=False)
    if r.returncode == 0:
        ok(f"Mounted {ssh_host}:{remote_vault}  →  {mount_path}")
    else:
        fail("sshfs mount failed — check credentials and remote path")
        if not confirm("Continue with a local vault path instead?", default="n"):
            sys.exit(0)
        return prompt_vault()

    return str(mount_path)


def mode_docker_remote_ollama():
    header("Docker + remote Ollama  (Postgres in Docker, Ollama on another host via SSH)")
    hr()

    if not check_docker() or not check_compose():
        sys.exit(1)

    # ── SSH credentials ───────────────────────────────────────────────────────
    header("Remote host & SSH credentials")
    ssh_user, remote_host, remote_port, key_path = prompt_ssh_credentials()

    # ── SSH tunnel for Ollama ─────────────────────────────────────────────────
    # Use a non-standard local port to avoid clashing with a local Ollama.
    local_tunnel_port = 11435

    header("SSH tunnel")
    tunnel_ok = open_ssh_tunnel(ssh_user, remote_host, remote_port,
                                local_tunnel_port, key_path)
    if tunnel_ok:
        time.sleep(1)
        check_ollama_at("localhost", local_tunnel_port)
    else:
        if not confirm("Tunnel failed — continue anyway?", default="n"):
            sys.exit(0)

    # Docker containers reach the host-side tunnel via host.docker.internal
    # (macOS/Windows Docker Desktop) or the bridge gateway (Linux).
    system      = platform.system()
    tunnel_host = "host.docker.internal" if system in ("Darwin", "Windows") else "172.17.0.1"
    ollama_url  = f"http://{tunnel_host}:{local_tunnel_port}"

    # ── Vault path ────────────────────────────────────────────────────────────
    header("Obsidian vault")
    vault = _prompt_vault_location(ssh_user, remote_host, key_path)

    # ── Write .env with SSH params for future reconnect ───────────────────────
    pg_pw = prompt_pg_password()
    ssh_params = {
        "user":        ssh_user,
        "host":        remote_host,
        "remote_port": remote_port,
        "local_port":  local_tunnel_port,
        "key_path":    key_path,
    }
    write_env(vault, pg_pw, ollama_url, ssh_params=ssh_params)

    # ── Start Docker services ─────────────────────────────────────────────────
    header("Starting services (postgres, mcp-server, dashboard)")
    env = {**os.environ, "OBSIDIAN_VAULT": vault, "POSTGRES_PASSWORD": pg_pw,
           "OLLAMA_URL": ollama_url}
    compose_up(services=["postgres", "mcp-server", "dashboard"], env=env)
    wait_for_postgres()

    header("Claude Desktop configuration")
    update_claude_config(_docker_entry())
    _done_docker_remote(ssh_user, remote_host, remote_port, local_tunnel_port, key_path)


# ── Summary printers ──────────────────────────────────────────────────────────

def _done_docker_remote(ssh_user, ssh_host, remote_port, local_port, key_path):
    key_flag = f" -i {key_path}" if key_path else ""
    tunnel_cmd = (
        f"ssh -N -f -o ExitOnForwardFailure=yes "
        f"-L {local_port}:localhost:{remote_port} "
        f"{ssh_user}@{ssh_host}{key_flag}"
    )
    print()
    hr()
    ok(_c("1", "Setup complete!"))
    print()
    info("Dashboard:  http://localhost:8484")
    info("Logs:       docker compose logs -f mcp-server")
    info("Restart Claude Desktop — server starts automatically")
    print()
    print(f"  {_c('93', '⚠')}  The SSH tunnel must be running for Ollama to work.")
    print(f"     Reconnect with:")
    print(f"\n       {_c('1', tunnel_cmd)}\n")
    info("Or run:  scripts/osm tunnel   (reads .env automatically)")
    hr()


def _done_dry_run():
    print()
    hr()
    print(f"  {_c('93', '⚠')}  {_c('1', 'DRY RUN — no changes were made')}")
    print()
    if _DRY_ACTIONS:
        print(f"  {_c('1', 'Actions that would have run:')}\n")
        for i, action in enumerate(_DRY_ACTIONS, 1):
            print(f"  {_c('90', str(i) + '.')}  {action}")
    else:
        print("  (no actions would have run)")
    print()
    info("Re-run without --dry-run to apply")
    hr()


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


# ── Tunnel command ────────────────────────────────────────────────────────────

def _read_env():
    """Parse .env into a dict (simple KEY=VALUE, ignores comments)."""
    env_path = PROJECT_ROOT / ".env"
    result = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def cmd_tunnel():
    """Re-open the SSH tunnel using credentials stored in .env."""
    header("OSM Tunnel — reconnect SSH tunnel")
    hr()

    env = _read_env()
    user        = env.get("OSM_SSH_USER")
    host        = env.get("OSM_SSH_HOST")
    remote_port = env.get("OSM_SSH_REMOTE_PORT", "11434")
    local_port  = env.get("OSM_SSH_LOCAL_PORT", "11435")
    key_path    = env.get("OSM_SSH_KEY")

    if not user or not host:
        fail("No SSH config found in .env — run osm init first")
        sys.exit(1)

    info(f"Reconnecting: {user}@{host} (tunnel localhost:{local_port} → {host}:{remote_port})")
    ok_flag = open_ssh_tunnel(user, host, int(remote_port), int(local_port), key_path)
    if ok_flag:
        time.sleep(1)
        check_ollama_at("localhost", int(local_port))
    else:
        sys.exit(1)


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


# ── Remove command ────────────────────────────────────────────────────────────

def cmd_remove():
    header("OSM Remove — tear down Obsidian Semantic MCP")
    hr()
    print()
    warn("This will:")
    print("    • Stop and remove all Docker containers and volumes  (all indexed embeddings lost)")
    print("    • Delete .env from this project")
    print("    • Remove obsidian-semantic from claude_desktop_config.json")
    print()

    if not DRY_RUN and not confirm("Continue?", default="n"):
        info("Aborted — nothing changed")
        return

    # ── Docker services + volumes ─────────────────────────────────────────────
    header("Stopping Docker services")
    r = run(
        f"docker compose --project-directory {PROJECT_ROOT} ps -q",
        check=False, capture=True,
    )
    if not DRY_RUN and not (r.stdout or "").strip():
        info("No running Docker services found — skipping")
    else:
        run(f"docker compose --project-directory {PROJECT_ROOT} down -v", check=False)
        if not DRY_RUN:
            ok("Docker services stopped and volumes removed")

    # ── .env ──────────────────────────────────────────────────────────────────
    header("Removing .env")
    env_path = PROJECT_ROOT / ".env"
    if DRY_RUN:
        _dry(f"delete {env_path}")
    elif env_path.exists():
        env_path.unlink()
        ok(f"Deleted {env_path}")
    else:
        info(".env not found — skipping")

    # ── Claude Desktop config ─────────────────────────────────────────────────
    header("Updating Claude Desktop config")
    cfg_path = _claude_cfg_path()
    if not cfg_path:
        warn("Unknown platform — remove obsidian-semantic from claude_desktop_config.json manually")
    elif DRY_RUN:
        _dry(f"remove obsidian-semantic entry from {cfg_path}")
    elif cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            servers = cfg.get("mcpServers", {})
            if "obsidian-semantic" in servers:
                del servers["obsidian-semantic"]
                cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
                ok(f"Removed obsidian-semantic from {cfg_path}")
                info("Restart Claude Desktop to apply")
            else:
                info("obsidian-semantic not found in config — skipping")
        except json.JSONDecodeError:
            warn(f"Could not parse {cfg_path} — remove entry manually")
    else:
        info("Claude Desktop config not found — skipping")

    if not DRY_RUN:
        print()
        hr()
        ok(_c("1", "Removed."))
        info("Run  scripts/osm init  to reinstall")
        hr()


# ── Install mode tables ───────────────────────────────────────────────────────

MODES_MACOS = {
    "1": ("Native",                  "Homebrew + local Postgres + local Ollama",         mode_native_macos),
    "2": ("Docker + host Ollama",    "Postgres in Docker, Ollama already on this Mac",   mode_docker_host_ollama),
    "3": ("Full Docker",             "Everything in containers  (recommended)",           mode_full_docker),
    "4": ("Docker + remote Ollama",  "Postgres in Docker, Ollama on another machine via SSH", mode_docker_remote_ollama),
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


# ── Help command ──────────────────────────────────────────────────────────────

def cmd_help():
    print(f"\n  {_c('1', 'osm')} — Obsidian Semantic MCP CLI\n")
    print(f"  {_c('1', 'Usage:')}  scripts/osm <command> [--dry-run]\n")
    print(f"  {_c('1', 'Commands:')}\n")
    for name, (_, desc) in COMMANDS.items():
        print(f"    {_c('1', f'osm {name:<10}')}  {desc}")
    print()
    print(f"  {_c('1', 'Flags:')}\n")
    print(f"    {_c('1', '--dry-run')}   Print every action that would run — make no changes")
    print()
    print(f"  {_c('1', 'Examples:')}\n")
    print(f"    scripts/osm init              # Interactive setup")
    print(f"    scripts/osm init --dry-run    # Preview setup steps without changes")
    print(f"    scripts/osm status            # Check service health")
    print(f"    scripts/osm tunnel            # Reconnect SSH tunnel (remote Ollama)")
    print(f"    scripts/osm rebuild           # Rebuild Docker images")
    print(f"    scripts/osm remove            # Stop services, wipe volumes and config")
    print(f"    scripts/osm remove --dry-run  # Preview what remove would delete")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

COMMANDS = {
    "init":    (cmd_init,    "Interactive setup wizard"),
    "status":  (cmd_status,  "Check service health"),
    "tunnel":  (cmd_tunnel,  "Reconnect SSH tunnel to remote Ollama host"),
    "rebuild": (cmd_rebuild, "Rebuild Docker images and restart"),
    "remove":  (cmd_remove,  "Stop services, delete volumes and config"),
    "help":    (cmd_help,    "Show this help message"),
}


def main():
    global DRY_RUN

    args = sys.argv[1:]

    # Strip --dry-run from args and activate dry-run mode
    if "--dry-run" in args:
        DRY_RUN = True
        args = [a for a in args if a != "--dry-run"]
        info("Dry-run mode — no changes will be made")
        print()

    # No command or explicit help request
    if not args or args[0] in ("--help", "-h"):
        cmd_help()
        sys.exit(0)

    cmd = args[0]

    if cmd not in COMMANDS:
        fail(f"Unknown command: {cmd!r}")
        print()
        cmd_help()
        sys.exit(1)

    COMMANDS[cmd][0]()

    if DRY_RUN:
        _done_dry_run()


if __name__ == "__main__":
    main()
