"""Start FontLab 7 from the MCP server, on Windows.

Only starting: FreeLab never quits or restarts FontLab, which could lose unsaved work.
The listener answers after a launch only when FontLab's startup script starts it
(tools/install-fontlab.ps1 -ListenerAtLaunch).
"""

from __future__ import annotations

import os
import subprocess
import sys

DEFAULT_EXE = r"C:\Program Files\Fontlab\FontLab 7\FontLab 7.exe"
IMAGE_NAME = "FontLab 7.exe"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: tasklist runs without a console flashing
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: FontLab outlives the server


def fontlab_exe() -> str:
    return os.environ.get("FREELAB_FONTLAB_EXE") or DEFAULT_EXE


def fontlab_running() -> bool:
    """True when a FontLab 7 process exists (any user session this process can see)."""
    if sys.platform != "win32":
        return False
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq %s" % IMAGE_NAME, "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW, timeout=30,
    ).stdout
    return IMAGE_NAME.lower() in out.lower()


def launch() -> int:
    """Start FontLab and return its process id. Raises FileNotFoundError when the program is missing."""
    exe = fontlab_exe()
    if not os.path.isfile(exe):
        raise FileNotFoundError(exe)
    process = subprocess.Popen(
        [exe], cwd=os.path.dirname(exe), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=DETACHED if sys.platform == "win32" else 0, close_fds=True,
    )
    return process.pid
