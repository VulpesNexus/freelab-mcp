"""FreeLab MCP server: exposes the FontLab listener's operations as MCP tools.

This process never imports fontlab. It runs in an ordinary Python 3, speaks
MCP over stdio to the AI app, and passes each tool call to the listener.
Tools are async and run the blocking socket call in a worker thread, so the
server keeps answering pings and cancellations while FontLab works.
"""

from __future__ import annotations

import functools
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

import time

from . import __version__, launcher
from .client import DEFAULT_TIMEOUT_S, ListenerError, call

MAX_TIMEOUT_S = 600.0

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
    FontLab is busy until it finishes (timeout_s, at most 600). It can change or lose work in open fonts:
    use it for reads and probes, and change a font only when the user asked for that change.
    """
    if not 0 < timeout_s <= MAX_TIMEOUT_S:
        raise RuntimeError(f"[bad_args] timeout_s must be above 0 and at most {MAX_TIMEOUT_S:g}.")
    return await _call("run_python", {"code": code, "persist": persist}, timeout=timeout_s)


@mcp.tool()
async def launch_fontlab(wait_s: float = 120.0) -> dict[str, Any]:
    """Start FontLab 7 when it is not running, then wait for its listener (only when the user asked to start FontLab).

    The listener answers after a launch only when FontLab's startup script starts it (installed with
    tools/install-fontlab.ps1 -ListenerAtLaunch). FreeLab never quits or restarts FontLab: that could lose
    unsaved work, so ask the user to close it.
    """
    if not 0 < wait_s <= MAX_TIMEOUT_S:
        raise RuntimeError(f"[bad_args] wait_s must be above 0 and at most {MAX_TIMEOUT_S:g}.")
    try:
        return {"started": False, "listener": await _call("ping", timeout=5.0)}
    except RuntimeError:
        pass
    if await anyio.to_thread.run_sync(launcher.fontlab_running):
        raise RuntimeError("[listener_not_running] FontLab is running, but its listener is not: in FontLab, choose "
                           "Scripts > FreeLab: Start Listener.")
    try:
        pid = await anyio.to_thread.run_sync(launcher.launch)
    except FileNotFoundError as exc:
        raise RuntimeError(f"[not_found] FontLab 7 is not at {exc}; set FREELAB_FONTLAB_EXE to its program file.") from None
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        await anyio.sleep(2.0)
        try:
            return {"started": True, "pid": pid, "listener": await _call("ping", timeout=5.0)}
        except RuntimeError:
            continue
    raise RuntimeError(f"[no_listener] FontLab started (process {pid}), but its listener did not answer within "
                       f"{wait_s:g} s. Install with tools/install-fontlab.ps1 -ListenerAtLaunch to start it at launch, "
                       "or choose Scripts > FreeLab: Start Listener.")


async def _wait_for_exit(wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not await anyio.to_thread.run_sync(launcher.fontlab_running):
            return True
        await anyio.sleep(1.0)
    return False


@mcp.tool()
async def quit_fontlab(wait_s: float = 60.0) -> dict[str, Any]:
    """Close FontLab (only when the user asked). Refused while any font or the workspace has unsaved changes.

    FontLab closes as its close button would: it saves its preferences, and may still ask something
    on screen, in which case it stays open and this tool says so.
    """
    if not 0 < wait_s <= MAX_TIMEOUT_S:
        raise RuntimeError(f"[bad_args] wait_s must be above 0 and at most {MAX_TIMEOUT_S:g}.")
    await _call("quit")
    if not await _wait_for_exit(wait_s):
        raise RuntimeError(f"[still_running] FontLab was asked to close but is still running after {wait_s:g} s; "
                           "it may be asking something on screen.")
    return {"quit": True}


@mcp.tool()
async def restart_fontlab(wait_s: float = 120.0) -> dict[str, Any]:
    """Close FontLab and start it again (only when the user asked), then wait for the listener.

    Refused while any font or the workspace has unsaved changes. The listener answers after the restart
    only when FontLab's startup script starts it (tools/install-fontlab.ps1 -ListenerAtLaunch).
    """
    await quit_fontlab(wait_s=min(wait_s, 60.0))
    return await launch_fontlab(wait_s=wait_s)


def main() -> None:
    mcp.run()
