"""FreeLab MCP server: exposes the FontLab listener's operations as MCP tools.

This process never imports fontlab. It runs in an ordinary Python 3, speaks
MCP over stdio to the AI app, and passes each tool call to the listener.
Tools are async and run the blocking socket call in a worker thread, so the
server keeps answering pings and cancellations while FontLab works.
"""

from __future__ import annotations

import functools
import time
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

from . import __version__, launcher
from .client import DEFAULT_TIMEOUT_S, ListenerError, call

MAX_TIMEOUT_S = 600.0
QUIT_WAIT_S = 60.0  # how long a restart waits for FontLab to close
# Ping errors that mean no listener answers (FontLab closed, or its listener not started), and those
# met while a freshly launched FontLab is still starting (old state file, busy loading).
LISTENER_ABSENT = ("[not_running]", "[connection]", "[empty_reply]")
LISTENER_STARTING = ("[timeout]", "[bad_state]", "[bad_token]")

mcp = FastMCP(
    "freelab",
    instructions=(
        "FreeLab reads the font open in FontLab through a listener script the user starts inside FontLab. "
        "Start with fontlab_status; if FontLab is not running and the user wants it, launch_fontlab starts it. Tools act on the font in front. Reads that FontLab could not answer are "
        "listed under 'unreadable' and must not be reported as empty or zero. Outline coordinates are "
        "shape-local: each shape's transform is reported, not applied. run_python executes code inside "
        "FontLab with full access to the open fonts; prefer the read tools, and never save or close fonts "
        "unless the user asks."
    ),
)


async def _call(op: str, args: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> Any:
    try:
        return await anyio.to_thread.run_sync(functools.partial(call, op, args, timeout=timeout))
    except ListenerError as exc:
        raise RuntimeError(f"[{exc.code}] {exc.message}") from None


def _glyph_args(name: str | None, unicode: int | None, layer: str | None) -> dict[str, Any]:
    if (name is None) == (unicode is None):
        raise RuntimeError("[bad_args] Give exactly one of name or unicode.")
    args: dict[str, Any] = {}
    if name is not None:
        args["name"] = name
    if unicode is not None:
        args["unicode"] = unicode
    if layer is not None:
        args["layer"] = layer
    return args


@mcp.tool()
async def fontlab_status() -> dict[str, Any]:
    """Check the FontLab connection: listener and Python versions, open font count, and the font in front."""
    ping = await _call("ping")
    status = await _call("status")
    return {"server": __version__, "listener": ping, **status}


@mcp.tool()
async def get_current_font() -> dict[str, Any]:
    """Return the font in front: names, path, units per em, glyph count, master, version, unsaved changes."""
    return await _call("font_info")


@mcp.tool()
async def list_glyphs(offset: int = 0, limit: int = 500) -> dict[str, Any]:
    """List glyph names of the font in front, a page at a time (limit 1–5000)."""
    return await _call("glyph_names", {"offset": offset, "limit": limit})


@mcp.tool()
async def get_glyph(name: str | None = None, unicode: int | None = None, layer: str | None = None) -> dict[str, Any]:
    """Describe one glyph, found by name or by Unicode code point (an integer, e.g. 65 for A).

    Returns its Unicode values, layers, and, for one layer, its advance width, bounding box, and shape
    and node counts. By default the layer named after the font's current master is read, else the
    active layer; the reply says which. Sidebearings, when present, are derived from the bounding box,
    not from FontLab's metrics.
    """
    return await _call("glyph", _glyph_args(name, unicode, layer))


@mcp.tool()
async def get_glyph_outline(
    name: str | None = None,
    unicode: int | None = None,
    layer: str | None = None,
    max_nodes: int = 2000,
) -> dict[str, Any]:
    """Return a glyph's shapes, contours, and nodes (x, y, type, smooth) on one layer.

    Coordinates are shape-local; each shape's transform [m11, m12, m21, m22, dx, dy] is reported
    but not applied. Large glyphs are cut at max_nodes: the reply and any cut contour are marked
    truncated.
    """
    args = _glyph_args(name, unicode, layer)
    args["max_nodes"] = max_nodes
    return await _call("outline", args)


@mcp.tool()
async def run_python(code: str, persist: bool = False, timeout_s: float = 30.0) -> dict[str, Any]:
    """Run Python inside FontLab (Python 2.7 in FontLab 7) and return its printed output and `result`.

    The namespace holds the fontlab, fontgate, FL, and PythonQt modules. Assign to `result` to return a
    value. With persist=True, names survive between calls. The code runs on FontLab's main thread, so
    FontLab is busy until it finishes; the server waits up to timeout_s (at most 600), and code still
    running then keeps running. It can change or lose work in open fonts:
    use it for reads and probes, and change a font only when the user asked for that change.
    """
    if not 0 < timeout_s <= MAX_TIMEOUT_S:
        raise RuntimeError(f"[bad_args] timeout_s must be above 0 and at most {MAX_TIMEOUT_S:g}.")
    return await _call("run_python", {"code": code, "persist": persist}, timeout=timeout_s)


@mcp.tool()
async def launch_fontlab(wait_s: float = 120.0) -> dict[str, Any]:
    """Start FontLab 7 when it is not running, then wait for its listener (only when the user asked to start FontLab).

    The listener answers after a launch only when FontLab's startup script starts it (installed with
    tools/install-fontlab.ps1 -ListenerAtLaunch). To close FontLab, use quit_fontlab or restart_fontlab,
    which are refused while there is unsaved work.
    """
    if not 0 < wait_s <= MAX_TIMEOUT_S:
        raise RuntimeError(f"[bad_args] wait_s must be above 0 and at most {MAX_TIMEOUT_S:g}.")
    try:
        return {"started": False, "listener": await _call("ping", timeout=5.0)}
    except RuntimeError as exc:
        if not str(exc).startswith(LISTENER_ABSENT):
            raise  # it answered, or is there but busy: a launch would not help
    if await anyio.to_thread.run_sync(launcher.fontlab_running):
        raise RuntimeError("[listener_not_running] FontLab is running, but its listener is not: in FontLab, choose "
                           "Scripts > FreeLab > Start Listener.")
    try:
        pid = await anyio.to_thread.run_sync(launcher.launch)
    except FileNotFoundError as exc:
        raise RuntimeError(f"[not_found] FontLab 7 is not at {exc}; set FREELAB_FONTLAB_EXE to its program file.") from None
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        await anyio.sleep(2.0)
        try:
            return {"started": True, "pid": pid, "listener": await _call("ping", timeout=5.0)}
        except RuntimeError as exc:
            if str(exc).startswith(LISTENER_ABSENT + LISTENER_STARTING):
                continue
            raise
    raise RuntimeError(f"[no_listener] FontLab started (process {pid}), but its listener did not answer within "
                       f"{wait_s:g} s. Install with tools/install-fontlab.ps1 -ListenerAtLaunch to start it at launch, "
                       "or choose Scripts > FreeLab > Start Listener.")


@mcp.tool()
async def quit_fontlab(wait_s: float = 60.0) -> dict[str, Any]:
    """Close FontLab (only when the user asked), as its close button would.

    Refused when FontLab reports unsaved changes. A font edited since it was opened makes FontLab
    ask on screen whether to save it: the question is left there for the user, and this tool stops
    and returns it ([save_prompt]); it never answers it.
    """
    if not 0 < wait_s <= MAX_TIMEOUT_S:
        raise RuntimeError(f"[bad_args] wait_s must be above 0 and at most {MAX_TIMEOUT_S:g}.")
    await _call("quit")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not await anyio.to_thread.run_sync(launcher.fontlab_running):
            return {"quit": True}
        await anyio.sleep(1.0)
        question = await _open_dialog()
        if question is not None:
            code = "save_prompt" if question.get("class") == SAVE_PROMPT_CLASS else "dialog_open"
            words = " ".join(question.get("text") or []) or question.get("title") or question.get("class")
            raise RuntimeError(f"[{code}] FontLab is asking: {words} (buttons: {', '.join(question.get('buttons') or [])}). "
                               "It was left on screen for the user to answer; FontLab stays open until then.")
    left = await _windows_left()
    if left is not None and left.get("main_window") is False:
        others = ", ".join(left.get("windows") or []) or "none visible"
        raise RuntimeError(f"[still_running] FontLab's main window closed, but its process is still running after "
                           f"{wait_s:g} s: a window without a parent (a script's dialog) keeps it open ({others}). "
                           "Close that window to end FontLab.")
    raise RuntimeError(f"[still_running] FontLab was asked to close but is still running after {wait_s:g} s.")


SAVE_PROMPT_CLASS = "DlgClosePackage"  # FontLab 7.2's "Save changes to ... before closing?"


async def _windows_left() -> dict[str, Any] | None:
    """Whether FontLab's main window is open and which other windows are, or None (no listener)."""
    try:
        return await _call("dialog", timeout=5.0)
    except RuntimeError:
        return None


async def _open_dialog() -> dict[str, Any] | None:
    """The dialog FontLab is waiting on, or None (none, or the listener already gone)."""
    try:
        return (await _call("dialog", timeout=5.0)).get("dialog")
    except RuntimeError:
        return None


@mcp.tool()
async def restart_fontlab(wait_s: float = 120.0) -> dict[str, Any]:
    """Close FontLab and start it again (only when the user asked), then wait for the listener.

    Refused while FontLab reports unsaved changes. A font edited since it was opened makes FontLab ask on
    screen whether to save it: the question is left there for the user, and the restart stops ([save_prompt]).
    The listener answers after the restart only when FontLab's startup script starts it
    (tools/install-fontlab.ps1 -ListenerAtLaunch).
    """
    # Everything that could stop the start is checked before FontLab is closed.
    if not 0 < wait_s <= MAX_TIMEOUT_S:
        raise RuntimeError(f"[bad_args] wait_s must be above 0 and at most {MAX_TIMEOUT_S:g}.")
    if not await anyio.to_thread.run_sync(launcher.program_found):
        raise RuntimeError(f"[not_found] FontLab 7 is not at {launcher.fontlab_exe()}; set FREELAB_FONTLAB_EXE to its "
                           "program file. FontLab was left open.")
    await quit_fontlab(wait_s=QUIT_WAIT_S)  # closing is not hurried by a short wait for the listener
    return await launch_fontlab(wait_s=wait_s)


def main() -> None:
    mcp.run()
