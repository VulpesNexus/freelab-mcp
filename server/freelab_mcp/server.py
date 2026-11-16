"""FreeLab MCP server: exposes the FontLab listener's operations as MCP tools.

This process never imports fontlab. It runs in an ordinary Python 3, speaks
MCP over stdio to the AI app, and passes each tool call to the listener.
Tools are async and run the blocking socket call in a worker thread, so the
server keeps answering pings and cancellations while FontLab works.
"""

from __future__ import annotations

import functools
import json
import time
from typing import Annotated, Any

import anyio
from mcp.server.fastmcp import FastMCP
from pydantic import Strict

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
        "FreeLab reads and edits fonts open in FontLab through a listener script that runs inside FontLab. "
        "Start with fontlab_status; if FontLab is not running and the user wants it, launch_fontlab starts it. "
        "list_fonts gives each open font's id. Read tools act on the font named by font (its id, path, "
        "PostScript name, or full name), or on the font in front when none is named. Write tools need font: "
        "they never guess which font to change, and font_name (its path or name) confirms an id. Each write is one step of FontLab's History (Edit > Undo, or "
        "the undo tool, takes it back), marks the font modified, and returns what changed; nothing is ever "
        "saved, exported, or closed. Make a write only when the user asked for that change. Reads that FontLab "
        "could not answer are listed under 'unreadable' and must not be reported as empty or zero. Outline "
        "coordinates are shape-local: each shape's transform is reported, not applied. run_python executes "
        "code inside FontLab with full access to the open fonts; prefer the dedicated tools."
    ),
)

WRITE_TIMEOUT_S = 120.0  # a History step waits on the clipboard when another program holds it (17 s each, seen live)
# Numbers are strict: true is not 1. Optional names are str with "" for left out, not str | None: FastMCP
# parses a string given for any other type as JSON first, so a glyph named null became None.
Number = Annotated[float, Strict()]
Int = Annotated[int, Strict()]
FontRef = Int | str  # (strict: true is no font id)


async def _call(op: str, args: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> Any:
    try:
        return await anyio.to_thread.run_sync(functools.partial(call, op, args, timeout=timeout))
    except ListenerError as exc:
        extra = f" {json.dumps(exc.data, ensure_ascii=False)}" if exc.data is not None else ""
        raise RuntimeError(f"[{exc.code}] {exc.message}{extra}") from None


async def _write(op: str, args: dict[str, Any]) -> Any:
    return await _call(op, {k: v for k, v in args.items() if v is not None and v != ""}, timeout=WRITE_TIMEOUT_S)


def _named(font_name: str, args: dict[str, Any], font: FontRef) -> dict[str, Any]:
    """A write's arguments: the font, and font_name to confirm it by its path or name when given."""
    args["font_name"] = font_name
    return _font(args, font)


def _font(args: dict[str, Any], font: FontRef | None) -> dict[str, Any]:
    if font is not None:
        args["font"] = font
    return args


def _glyph_args(name: str, unicode: int | None, layer: str, key: str = "name") -> dict[str, Any]:
    name, layer = name or None, layer or None
    if (name is None) == (unicode is None):
        raise RuntimeError(f"[bad_args] Give exactly one of {key} or unicode.")
    args: dict[str, Any] = {}
    if name is not None:
        args[key] = name
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
async def list_fonts() -> dict[str, Any]:
    """List the open fonts: each one's id (what the other tools take as font), names, path, masters, glyph
    count, and whether it is the font in front."""
    return await _call("fonts")


@mcp.tool()
async def get_current_font(font: FontRef | None = None) -> dict[str, Any]:
    """Describe a font: id, names, path, units per em, glyph count, masters, version, unsaved changes.

    font: its id from list_fonts, its path, PostScript name, or full name; the font in front when left out.
    """
    return await _call("font_info", _font({}, font))


@mcp.tool()
async def list_glyphs(offset: Int = 0, limit: Int = 500, font: FontRef | None = None) -> dict[str, Any]:
    """List a font's glyph names, a page at a time (limit 1–5000), in the font's order."""
    return await _call("glyph_names", _font({"offset": offset, "limit": limit}, font))


@mcp.tool()
async def get_glyph(name: str = "", unicode: Int | None = None, layer: str = "",
                    font: FontRef | None = None) -> dict[str, Any]:
    """Describe one glyph, found by name or by Unicode code point (an integer, e.g. 65 for A).

    Returns its Unicode values, layers, and, for one layer: advance width, ink box, sidebearings (from the
    ink box, as FontLab measures them), shape and node counts, components (each one's glyph and transform),
    and anchors. bounding_box is FontLab's stored box of all the nodes (off-curve points included), which can
    lag behind after an edit to a component's glyph; ink_box is measured when asked. By default the layer
    named after the font's current master is read, else the active layer; the reply says which.
    """
    return await _call("glyph", _font(_glyph_args(name, unicode, layer), font))


@mcp.tool()
async def get_glyph_outline(
    name: str = "",
    unicode: Int | None = None,
    layer: str = "",
    max_nodes: Int = 2000,
    font: FontRef | None = None,
) -> dict[str, Any]:
    """Return a glyph's shapes, contours, and nodes (x, y, type, smooth) on one layer.

    Coordinates are shape-local; each shape's transform [m11, m12, m21, m22, dx, dy] is reported
    but not applied. A component shape names the glyph it draws (component). Large glyphs are cut at
    max_nodes: the reply and any cut contour are marked truncated.
    """
    args = _glyph_args(name, unicode, layer)
    args["max_nodes"] = max_nodes
    return await _call("outline", _font(args, font))


@mcp.tool()
async def get_kerning(
    font: FontRef | None = None,
    master: str = "",
    left: str = "",
    right: str = "",
    offset: Int = 0,
    limit: Int = 500,
    classes: bool = False,
) -> dict[str, Any]:
    """Read a master's kerning (the font's current master when master is left out).

    With left and right (glyph names, or @ and a class name such as @V): that pair's own value, null when
    the font has no such pair. Without them: a page of the master's own pairs, classes written with @.
    Values are those FontLab stores; class pairs are not expanded to glyph pairs. classes=True adds the
    kerning classes: each one's side (KernLeft, KernRight, KernBothSide) and glyphs.
    """
    args: dict[str, Any] = {"offset": offset, "limit": limit, "classes": classes}
    for key, value in (("master", master), ("left", left), ("right", right)):
        if value:
            args[key] = value
    return await _call("kerning", _font(args, font))


@mcp.tool()
async def get_features(font: FontRef | None = None, tag: str = "", prefix: bool = False) -> dict[str, Any]:
    """List a font's OpenType feature tags; with tag, return that feature's code as FontLab keeps it (the
    whole block); with prefix=True, the code before the features (languagesystem lines, classes)."""
    args: dict[str, Any] = {"prefix": prefix}
    if tag:
        args["tag"] = tag
    return await _call("features", _font(args, font))


# -- Writes: each one names its font, is one History step, and returns what changed --------------------


@mcp.tool()
async def set_kerning_pair(font: FontRef, left: str, right: str, value: Number, master: str = "", font_name: str = "") -> dict[str, Any]:
    """Set one kerning pair's value in a master (the font's current master when left out).

    left and right are glyph names, or @ and a kerning class's name (@V); a class must kern on that side.
    A glyph whose name is also a class's name is refused: FontLab would write the class's pair. FontLab keeps
    kerning in whole units (-19.5 is stored as -20; the reply says so). Returns the value before (null: a new
    pair) and after. Only the pair's own entry changes; other pairs and the classes are left as they are.
    """
    return await _write("set_kerning", _named(font_name, {"left": left, "right": right, "value": value, "master": master}, font))


@mcp.tool()
async def remove_kerning_pair(font: FontRef, left: str, right: str, master: str = "", font_name: str = "") -> dict[str, Any]:
    """Remove one kerning pair's own entry from a master (glyph names, or @class names, as get_kerning shows them)."""
    return await _write("remove_kerning", _named(font_name, {"left": left, "right": right, "master": master}, font))


@mcp.tool()
async def set_glyph_metrics(
    font: FontRef,
    glyph: str = "",
    unicode: Int | None = None,
    layer: str = "",
    advance_width: Number | None = None,
    left_sidebearing: Number | None = None,
    right_sidebearing: Number | None = None,
    font_name: str = "",
) -> dict[str, Any]:
    """Set a glyph's advance width and sidebearings on one layer (the font's current master by default).

    Give one or two of the three. A new left sidebearing moves the outline, its components and anchors
    with it, and keeps the right sidebearing unless advance_width or right_sidebearing is given; a new
    advance width alone keeps the outline where it is; advance_width with right_sidebearing moves the
    outline so both hold. FontLab keeps whole units: the reply's after shows what it kept. Sidebearings are measured from the ink box, as
    FontLab measures them. Glyphs that use this glyph as a component move with its outline. A layer whose
    metrics come from an expression in FontLab is refused for the value the expression sets.
    """
    args = _glyph_args(glyph, unicode, layer, key="glyph")
    args.update({"advance_width": advance_width, "left_sidebearing": left_sidebearing,
                 "right_sidebearing": right_sidebearing})
    return await _write("set_metrics", _named(font_name, args, font))


@mcp.tool()
async def rename_glyph(font: FontRef, glyph: str, new_name: str, font_name: str = "") -> dict[str, Any]:
    """Rename a glyph. Components that use it follow the new name. Refused while kerning pairs, classes, or
    feature code use the old name: FontLab's rename does not update those."""
    return await _write("rename_glyph", _named(font_name, {"glyph": glyph, "new_name": new_name}, font))


@mcp.tool()
async def set_glyph_unicodes(font: FontRef, unicodes: list[Int], glyph: str = "",
                             unicode: Int | None = None, font_name: str = "") -> dict[str, Any]:
    """Replace a glyph's Unicode values with the code points given (integers; [] clears them). The glyph is
    found by name (glyph) or by one of its current code points (unicode). A code point another glyph has
    is refused."""
    args = _glyph_args(glyph, unicode, None, key="glyph")
    args["unicodes"] = unicodes
    return await _write("set_unicodes", _named(font_name, args, font))


@mcp.tool()
async def add_component_glyph(
    font: FontRef,
    name: str,
    components: list[dict[str, Any]],
    unicodes: list[Int] | None = None,
    advance_width: Number | None = None,
    font_name: str = "",
) -> dict[str, Any]:
    """Add a new glyph made of components: components is a list of {"glyph": name, "x": offset, "y": offset}.

    It is built in every master where each component's glyph has a layer (the others are listed as
    skipped). Its advance width is advance_width, else the first component's in each master. A name or
    code point the font already has is refused. The reply's components are the offsets FontLab stored,
    read back: a font that rounds coordinates rounds them to whole units (said in "rounded").
    """
    return await _write("add_component_glyph", _named(font_name, {"name": name, "components": components, "unicodes": unicodes,
                                                      "advance_width": advance_width}, font))


@mcp.tool()
async def set_anchor(font: FontRef, glyph: str, name: str, x: Number, y: Number, layer: str = "", font_name: str = "") -> dict[str, Any]:
    """Add an anchor to a glyph's layer (the font's current master by default), or move the one of that name."""
    return await _write("set_anchor", _named(font_name, {"glyph": glyph, "name": name, "x": x, "y": y, "layer": layer}, font))


@mcp.tool()
async def remove_anchor(font: FontRef, glyph: str, name: str, layer: str = "", font_name: str = "") -> dict[str, Any]:
    """Remove a glyph's anchor of that name from one layer (the font's current master by default)."""
    return await _write("remove_anchor", _named(font_name, {"glyph": glyph, "name": name, "layer": layer}, font))


@mcp.tool()
async def set_feature_code(font: FontRef, tag: str, code: str, font_name: str = "") -> dict[str, Any]:
    """Set an OpenType feature's code, adding the feature when the font has none of that tag.

    code is the whole block as FontLab keeps it: feature liga { ... } liga; (get_features shows the current
    one). It is stored, not compiled: FontLab compiles feature code when the font is exported, and only
    then reports errors in it.
    """
    return await _write("set_feature", _named(font_name, {"tag": tag, "code": code}, font))


@mcp.tool()
async def undo(font: FontRef, expected_state: Int | None = None, expected_title: str = "",
               font_name: str = "") -> dict[str, Any]:
    """Undo the last step of a font's History, as Edit > Undo does in FontLab.

    FontLab's Undo acts on the font in front, so font must be that font. To undo a write of yours, give its
    history.history_state as expected_state and its history.title as expected_title: nothing is undone if
    anything else has changed the font since, or if the step Undo would take back is not that write (a
    retried undo cannot take back a second step). A write's own "(before)" step (the font recorded before
    its first write) is undone with it, so each write is one undo. The reply names the steps undone and the
    one Undo would take back next.
    """
    return await _write("undo", _font({"expected_state": expected_state, "expected_title": expected_title,
                                       "font_name": font_name}, font))


@mcp.tool()
async def run_python(code: str, persist: bool = False, timeout_s: float = 30.0) -> dict[str, Any]:
    """Run Python inside FontLab (Python 2.7 in FontLab 7) and return its printed output and `result`.

    The namespace holds the fontlab, fontgate, FL, and PythonQt modules. Assign to `result` to return a
    value. With persist=True, names survive between calls; without it, the namespace is cleared when the
    call returns, so a deferred callback (a timer) that needs its names must use persist=True. The code runs on FontLab's main thread, so
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
                           "Scripts > FreeLab > Start MCP Listener.")
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
                       "or choose Scripts > FreeLab > Start MCP Listener.")


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
