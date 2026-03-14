#!/usr/bin/env python3
"""
Compatibility entry point for Claude Desktop / claude_desktop_config.json.

Claude Desktop expects the server script at the project root. This file
delegates to src/server.py so both paths work:
  - Direct: python3 src/server.py
  - Via config: python3 /path/to/obsidian-semantic-mcp/obsidian_semantic_mcp.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from server import main  # noqa: E402

if __name__ == "__main__":
    asyncio.run(main())
