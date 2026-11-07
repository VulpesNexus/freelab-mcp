"""Start FontLab 7 from the MCP server, and tell whether it runs, on Windows.

Closing goes through the listener instead (quit_fontlab), which refuses while there is
unsaved work. The listener answers after a launch only when FontLab's startup script
starts it (tools/install-fontlab.ps1 -ListenerAtLaunch).
"""

from __future__ import annotations

import os
import subprocess
import sys

DEFAULT_EXE = r"C:\Program Files\Fontlab\FontLab 7\FontLab 7.exe"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: tasklist runs without a console flashing
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: FontLab outlives the server


def fontlab_exe() -> str:
    return os.environ.get("FREELAB_FONTLAB_EXE") or DEFAULT_EXE


def image_name() -> str:
    """The program's file name, as the process list shows it (FREELAB_FONTLAB_EXE may name another)."""
    return os.path.basename(fontlab_exe())


def program_found() -> bool:
    """True when the FontLab program file exists, so a start can be tried."""
    return os.path.isfile(fontlab_exe())


def _session_id() -> int | None:
    """This process's Windows session: another user's FontLab runs in another one."""
    try:
        import ctypes
        session = ctypes.c_ulong()
        kernel32 = ctypes.WinDLL("kernel32")
        if kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)):
            return session.value
    except (OSError, AttributeError):
        pass
    return None


def fontlab_running() -> bool:
    """True when a FontLab 7 process runs in this user's session."""
    if sys.platform != "win32":
        return False
    name = image_name()
    filters = ["/FI", "IMAGENAME eq %s" % name]
    session = _session_id()
    if session is not None:
        filters += ["/FI", "SESSION eq %d" % session]
    out = subprocess.run(
        ["tasklist", *filters, "/FO", "CSV", "/NH"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW,
        timeout=30,
    ).stdout
    return ('"%s"' % name).lower() in out.lower()


def launch() -> int:
    """Start FontLab and return its process id. Raises FileNotFoundError when the program is missing."""
    exe = fontlab_exe()
    if not os.path.isfile(exe):
        raise FileNotFoundError(exe)
    # FontLab embeds its own Python 2.7: variables set for this Python 3 must not reach it.
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    process = subprocess.Popen(
        [exe], cwd=os.path.dirname(exe), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=DETACHED if sys.platform == "win32" else 0, close_fds=True,
    )
    return process.pid
