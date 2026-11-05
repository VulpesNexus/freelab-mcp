"""Start the FreeLab MCP server without installing it: python server/run_server.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from freelab_mcp.server import main  # noqa: E402

main()
