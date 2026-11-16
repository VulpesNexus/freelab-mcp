#FLM: Start MCP Listener
# -*- coding: utf-8 -*-
"""FreeLab listener: runs inside FontLab and answers the FreeLab MCP server.

Run this file from FontLab's Scripting panel or its Scripts menu. It opens a
TCP socket on 127.0.0.1 only, polls it from a QTimer on FontLab's main thread,
and answers one JSON request per connection. Running it again restarts it.

The file must stay valid Python 2.7 (FontLab 7 embeds 2.7) and Python 3
(FontLab 8). The test suite runs it under both.
"""

from __future__ import absolute_import, division, print_function

import binascii
import errno
import hmac
import json
import math
import os
import re
import socket
import sys
import time
import traceback

VERSION = "1.0.0"
PROTOCOL = 1
HOST = "127.0.0.1"
DEFAULT_PORT = 47761
POLL_MS = 25
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_REPLY_BYTES = 64 * 1024 * 1024  # a larger reply is replaced by a too_large error (ask for less: a page, max_nodes)
SEND_BYTES_PER_TICK = 4 * 1024 * 1024  # a reply goes out without blocking, at most this much per tick
SEND_TIMEOUT_S = 10.0  # a client that has not taken its whole reply by then is dropped
MAX_CLIENTS = 16
CLIENT_TIMEOUT_S = 5.0
MAX_ACCEPTS_PER_TICK = 8
DEFAULT_MAX_NODES = 2000
MAX_NODES_LIMIT = 100000
MODULE_NAME = "freelab_listener"
QUIT_DELAY_MS = 300  # the main window closes from a later timer tick, once the quit request is handled
MAX_ERRORS = 100  # errors kept for tests and probes (an error repeating every tick must not grow without end)

PY2 = sys.version_info[0] == 2
if PY2:  # pragma: no cover - exercised by the Python 2.7 test run
    text_type = unicode  # noqa: F821
    string_types = (str, unicode)  # noqa: F821
    integer_types = (int, long)  # noqa: F821
else:
    text_type = str
    string_types = (str,)
    integer_types = (int,)

_WOULD_BLOCK = set([errno.EAGAIN, errno.EWOULDBLOCK, 10035])  # 10035: WSAEWOULDBLOCK
_LAST_RESORT = b'{"ok": false, "error": {"code": "unserializable", "message": "The reply could not be encoded."}}\n'


class FreeLabError(Exception):
    """An expected failure with a stable code, reported to the client as is."""

    def __init__(self, code, message, data=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.data = data  # more for the client, such as the History step a failed write left (error["data"])


# ---------------------------------------------------------------------------
# Text and value conversion
# ---------------------------------------------------------------------------

def _text(value):
    """Any value as text. Bytes that are not UTF-8 (cp932 paths under Python 2) are replaced, not fatal."""
    if isinstance(value, text_type):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    try:
        return text_type(value)
    except Exception:
        try:
            return str(value).decode("utf-8", "replace") if PY2 else repr(value)
        except Exception:
            return repr(value)


def _exc_text(exc):
    return u"%s: %s" % (type(exc).__name__, _text(exc))


def _format_exc():
    return _text(traceback.format_exc())


def to_jsonable(value, depth=0):
    """Convert a FontLab or Qt value to something json.dumps accepts.

    Qt value types are recognized by class name only; arbitrary native objects
    are never probed for methods, because a wrong call on a native FontLab
    object can crash the application.
    """
    if depth > 8:
        return "<too deep>"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, integer_types):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return text_type(value)
        return value
    if isinstance(value, string_types) or isinstance(value, bytes):
        return _text(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return dict((_text(k), to_jsonable(v, depth + 1)) for k, v in value.items())
    name = type(value).__name__
    if name in ("QRectF", "QRect"):
        return {"x": value.x(), "y": value.y(), "width": value.width(), "height": value.height()}
    if name in ("QPointF", "QPoint"):
        return {"x": value.x(), "y": value.y()}
    if name in ("QSizeF", "QSize"):
        return {"width": value.width(), "height": value.height()}
    if name == "QTransform":
        return [value.m11(), value.m12(), value.m21(), value.m22(), value.dx(), value.dy()]
    try:
        return u"<%s %s>" % (name, _text(repr(value))[:200])
    except Exception:
        return u"<%s>" % name


class _Reads(object):
    """Collects guarded attribute reads; a failed read is reported, never hidden."""

    def __init__(self):
        self.unreadable = {}

    def prop(self, obj, name, key=None):
        """Read a documented property. A zero-argument getter is called."""
        key = key or name
        try:
            value = getattr(obj, name)
            if callable(value):
                value = value()
            return to_jsonable(value)
        except Exception as exc:
            self.unreadable[key] = _exc_text(exc)
            return None

    def attach(self, result):
        if self.unreadable:
            result["unreadable"] = dict(self.unreadable)
        return result


def _int_arg(args, key, default, low, high):
    value = args.get(key, default)
    if not isinstance(value, integer_types) or isinstance(value, bool) or not low <= value <= high:
        raise FreeLabError("bad_args", "%s must be an integer from %d to %d." % (key, low, high))
    return value


# ---------------------------------------------------------------------------
# FontLab access
# ---------------------------------------------------------------------------

def _fontlab():
    try:
        import fontlab  # provided by FontLab's embedded Python
    except ImportError:
        raise FreeLabError("not_in_fontlab", "The fontlab module is not available: this is not running inside FontLab.")
    return fontlab


def _is_null(obj, what):
    """True for None or a null FontLab object. A failed isNull read is an error, not 'not null'."""
    if obj is None:
        return True
    try:
        return bool(getattr(obj, "isNull"))
    except Exception as exc:
        raise FreeLabError("unreadable", "Could not tell whether %s is null (%s)." % (what, _exc_text(exc)))


def _real_list(value, what):
    """Accept only real Python containers; native containers are never indexed or iterated."""
    if isinstance(value, (list, tuple)):
        return value
    raise FreeLabError("unexpected_type", "%s is a %s, not a list; it was not read." % (what, type(value).__name__))


def _fonts_open_text(count):
    return "1 font is open" if count == 1 else "%d fonts are open" % count


def current_package():
    """Return (package, open_font_count) for the font in front, or raise."""
    fl = _fontlab()
    count = fl.CountFonts()
    if count < 1:
        raise FreeLabError("no_font", "No font is open in FontLab.")
    package = fl.flWorkspace.instance().currentPackage
    if _is_null(package, "the current font"):
        raise FreeLabError("no_current_font", "%s, but FontLab reports no current font." % _fonts_open_text(count))
    return package, count


def _font_id(package):
    """FontLab's number for an open font (fgPackage.id), the same through every wrapper of it."""
    try:
        return int(package.fgPackage.id)
    except Exception as exc:
        raise FreeLabError("unreadable", "Could not read the font's id (%s)." % _exc_text(exc))


def open_packages():
    """Every open font, as flPackage wrappers of the items flWorkspace.packages() lists."""
    fl = _fontlab()
    items = _real_list(fl.flWorkspace.instance().packages(), "flWorkspace.packages()")
    packages = []
    for item in items:
        package = fl.flPackage(item)
        if not _is_null(package, "an open font"):
            packages.append(package)
    return packages


def _same_path(a, b):
    def norm(path):
        return os.path.normcase(os.path.normpath(_text(path).replace(u"/", os.sep).replace(u"\\", os.sep)))
    return bool(a) and bool(b) and norm(a) == norm(b)


def _font_label(package):
    try:
        name = _text(package.ffn)
    except Exception:
        name = u"?"
    try:
        return u"%s (id %d)" % (name, _font_id(package))
    except FreeLabError:
        return name


def _font_matches(package, wanted, names_only=False):
    """Whether wanted names the font: its id (an integer, or digits that may also be a name), its
    PostScript or full name, or its path."""
    if isinstance(wanted, integer_types):
        return not names_only and _font_id(package) == wanted
    if not names_only and ASCII_DIGITS.match(wanted) and _font_id(package) == int(wanted):
        return True  # (not isdigit: it is true of "²", which int() refuses)
    for key in ("psn", "ffn"):
        try:
            if _text(getattr(package, key)) == wanted:
                return True
        except Exception:
            pass
    try:
        return _same_path(package.path, wanted)
    except Exception:
        return False


ASCII_DIGITS = re.compile(r"^[0-9]+\Z")


def target_package(args, required=False):
    """(package, open font count) of the font args["font"] names: its id (from list_fonts), its path,
    PostScript name, or full name. Without one, the font in front, unless required: a write never
    guesses which font it changes."""
    wanted = args.get("font")
    if wanted is None:
        if required:
            raise FreeLabError("bad_args", "Name the font to change (font): its id from list_fonts, its path, its "
                                           "PostScript name, or its full name.")
        return current_package()
    if isinstance(wanted, bool) or not isinstance(wanted, integer_types + string_types) or \
            (isinstance(wanted, string_types) and not wanted.strip()):
        raise FreeLabError("bad_args", "font must be a font id (an integer from list_fonts) or a non-empty path or name.")
    if isinstance(wanted, string_types):
        wanted = _text(wanted).strip()
    packages = open_packages()
    if not packages:
        raise FreeLabError("no_font", "No font is open in FontLab.")
    found = [package for package in packages if _font_matches(package, wanted)]
    if not found:
        raise FreeLabError("no_such_font", u"No open font is '%s'. Open fonts: %s." % (
            wanted, u"; ".join(_font_label(package) for package in packages)))
    if len(found) > 1:
        raise FreeLabError("ambiguous_font", u"%d open fonts match '%s': %s. Name one by its id." % (
            len(found), wanted, u"; ".join(_font_label(package) for package in found)))
    return found[0], len(packages)


def _font_ref(package, reads=None):
    """The font a reply is about: its id and full name."""
    reads = reads or _Reads()
    return {"id": _font_id(package), "name": reads.prop(package, "ffn", "font_name")}


def _font_summary(package, reads):
    try:
        font_id = _font_id(package)
    except FreeLabError as exc:
        font_id = None
        reads.unreadable["id"] = exc.message
    return {
        "id": font_id,
        "family": reads.prop(package, "tfn"),
        "style": reads.prop(package, "tsn"),
        "postscript_name": reads.prop(package, "psn"),
        "full_name": reads.prop(package, "ffn"),
        "path": reads.prop(package, "path"),
        "units_per_em": reads.prop(package, "upm"),
        "glyph_count": reads.prop(package, "glyphsCount"),
        "master": reads.prop(package, "master"),
        "master_count": reads.prop(package, "mastersCount"),
        "masters": reads.prop(package, "masters"),
        "version_major": reads.prop(package, "versionMajor"),
        "version_minor": reads.prop(package, "versionMinor"),
        "has_changes": reads.prop(package, "hasChanges"),
    }


def _find_glyph(package, args, name_key="name"):
    name = args.get(name_key)
    codepoint = args.get("unicode")
    if name is not None and codepoint is not None:
        raise FreeLabError("bad_args", "Give either %s or unicode, not both." % name_key)
    if name is not None:
        if not isinstance(name, string_types) or not name:
            raise FreeLabError("bad_args", "%s must be a non-empty string." % name_key)
        glyph = glyph_named(package, name)
        label = u"name '%s'" % _text(name)
    elif codepoint is not None:
        if not isinstance(codepoint, integer_types) or isinstance(codepoint, bool) or not 0 <= codepoint <= 0x10FFFF:
            raise FreeLabError("bad_args", "unicode must be an integer code point.")
        glyph = _glyph_with(package, codepoint)
        label = u"U+%04X" % codepoint
    else:
        raise FreeLabError("bad_args", "Give a glyph name or a unicode code point.")
    if _is_null(glyph, "the glyph"):
        raise FreeLabError("no_glyph", u"%s has no glyph with %s." % (_font_label(package), label))
    return glyph


def _named(glyph, name):
    return not _is_null(glyph, "the glyph") and _text(glyph.name) == _text(name)


def glyph_named(package, name):
    """The font's glyph of that name, or None. FontLab's lookups can lag behind a change (an Undo, a
    script's edit): a glyph found under another name, or none, is looked up again once the font's maps
    are rebuilt."""
    glyph = package.findName(name)
    if _named(glyph, name):
        return glyph
    package.fgPackage.rebuildMaps()
    glyph = package.findName(name)
    return glyph if _named(glyph, name) else None


def _has_code(glyph, codepoint):
    if _is_null(glyph, "the glyph"):
        return False
    try:
        return codepoint in [int(u) for u in _real_list(glyph.fgGlyph.unicodes, "fgGlyph.unicodes")]
    except FreeLabError:
        raise
    except Exception:
        return False


def _glyph_with(package, codepoint):
    """The font's glyph with that code point, or None, checked as glyph_named is."""
    glyph = package.findUnicode(codepoint)
    if _has_code(glyph, codepoint):
        return glyph
    package.fgPackage.rebuildMaps()
    glyph = package.findUnicode(codepoint)
    return glyph if _has_code(glyph, codepoint) else None


def _choose_layer(glyph, package, layer_name, reads):
    """Pick a layer by matching names in glyph.layers; never creates one."""
    layers = _real_list(glyph.layers, "glyph.layers")
    if layer_name is not None and not isinstance(layer_name, string_types):
        raise FreeLabError("bad_args", "layer must be a string.")
    wanted = layer_name
    source = "requested"
    if wanted is None:
        wanted = reads.prop(package, "master")
        source = "font master"
    if wanted:
        for layer in layers:
            if _text(layer.name) == _text(wanted):
                return layer, source
        if source == "requested":
            raise FreeLabError("no_layer", u"Glyph '%s' has no layer '%s'; its layers: %s. (A font made from a variable "
                               u"font has a glyph's layers only in the masters where it is drawn.)" % (
                                   _text(glyph.name), _text(wanted), u", ".join(_text(layer.name) for layer in layers)))
    active = glyph.activeLayer
    if _is_null(active, "the active layer"):
        raise FreeLabError("no_layer", "The glyph has no layer matching the master and no active layer.")
    return active, "active layer"


def _layer_names(glyph):
    return [_text(layer.name) for layer in _real_list(glyph.layers, "glyph.layers")]


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

OPS = {}


def op(name):
    def register(func):
        OPS[name] = func
        return func
    return register


@op("ping")
def op_ping(listener, args):
    info = {
        "listener": VERSION,
        "protocol": PROTOCOL,
        "python": sys.version.split()[0],
        "in_fontlab": False,
    }
    try:
        fl = _fontlab()
        info["in_fontlab"] = True
        try:
            info["application_path"] = _text(fl.ApplicationPath())
        except Exception as exc:
            info["unreadable"] = {"application_path": _exc_text(exc)}
    except FreeLabError:
        pass
    return info


@op("status")
def op_status(listener, args):
    fl = _fontlab()
    count = fl.CountFonts()
    result = {"fonts_open": count, "current_font": None}
    if count >= 1:
        reads = _Reads()
        package = fl.flWorkspace.instance().currentPackage
        if not _is_null(package, "the current font"):
            result["current_font"] = _font_summary(package, reads)
        reads.attach(result)
    return result


def unsaved_work():
    """(workspace has changes, fonts with changes, fonts edited since opening), read from FontLab.

    FontLab 7.2 reported an edit to a font only through hasHistoryStates (its undo history):
    hasChanges stayed False, though FontLab itself asked to save it on closing. So an edited font
    does not refuse a quit here: closing goes through FontLab's own question, which the server
    reports (see op_dialog). A read that fails raises: FontLab must never be quit on a guess.
    """
    fl = _fontlab()
    workspace = fl.flWorkspace.instance()
    try:
        workspace_changed = bool(workspace.hasChanges)
        packages = _real_list(workspace.packages(), "flWorkspace.packages()")
        changed = sum(1 for package in packages if package.hasChanges)
        edited = sum(1 for package in packages if package.hasChanges or package.hasHistoryStates)
    except Exception as exc:
        raise FreeLabError("unreadable", "FontLab could not tell whether there are unsaved changes: %s" % _exc_text(exc))
    return workspace_changed, changed, edited


@op("unsaved")
def op_unsaved(listener, args):
    workspace_changed, changed, edited = unsaved_work()
    return {"unsaved": workspace_changed or changed > 0, "workspace": workspace_changed, "fonts": changed,
            "edited": edited}


@op("quit")
def op_quit(listener, args):
    """Close FontLab as its close button would, a moment after replying.

    Refused when FontLab reports unsaved changes outright. A font that was only edited goes through
    FontLab's own question on closing ("Save changes to ... before closing?"), which stays on screen
    for the user; the server reads it through op_dialog and says so.
    """
    if listener.quit_in_progress():
        raise FreeLabError("quit_in_progress", "FontLab is already closing (it may be asking on screen whether to save): "
                           "answer it in FontLab, or wait.")
    workspace_changed, changed, edited = unsaved_work()
    if workspace_changed or changed:
        parts = (["%d font%s" % (changed, "" if changed == 1 else "s")] if changed else []) + (["the workspace"] if workspace_changed else [])
        raise FreeLabError("unsaved_changes", "FontLab has unsaved changes (%s): save them, or close them in FontLab, "
                           "first. FreeLab never quits FontLab over unsaved work." % " and ".join(parts))
    if listener.deadline is not None and time.time() > listener.deadline:
        raise FreeLabError("expired", "The client gave up while FontLab checked for unsaved work; FontLab was not closed.")
    listener.request_quit()
    return {"quitting": True, "edited": edited}


def _prop(obj, name):
    """A Qt property: PythonQt gives most as values, some as methods."""
    value = getattr(obj, name)
    return value() if callable(value) else value


_TAGS = re.compile(r"<[^>]+>")


@op("dialog")
def op_dialog(listener, args):
    """The window FontLab is waiting on (its active modal dialog): class, title, texts, and buttons. Reads only."""
    try:
        from PythonQt.QtGui import QApplication  # only inside FontLab
    except ImportError:
        return {"dialog": None}
    app = QApplication.instance()
    main_window, others = _top_windows(app)
    dialog = app.activeModalWidget()
    if dialog is None:
        return {"dialog": None, "main_window": main_window, "windows": others}
    texts, buttons = [], []

    def walk(widget):
        for child in widget.children():
            if not child.isWidgetType() or not child.isVisible():
                continue
            if child.inherits("QAbstractButton"):
                if not (child.inherits("QCheckBox") or child.inherits("QRadioButton")):
                    text = _MNEMONIC.sub(r"\1", _prop(child, "text") or u"").strip()  # "&Save" -> "Save", "&&" -> "&"
                    if text:
                        buttons.append(text)
            elif child.inherits("QLabel"):
                text = _label_text(child)
                if text:
                    texts.append(text)
            walk(child)
    walk(dialog)
    return {"dialog": {"class": dialog.metaObject().className(), "title": _prop(dialog, "windowTitle"),
                       "text": texts, "buttons": buttons}, "main_window": main_window, "windows": others}


def _top_windows(app):
    """(whether FontLab's main window is open, the titles of the other visible top-level windows)."""
    main_window, others = False, []
    try:
        for widget in app.topLevelWidgets():
            if not widget.isVisible():
                continue
            if widget.metaObject().className() == "MainWindow":
                main_window = True
            else:
                others.append(_text(_prop(widget, "windowTitle") or widget.metaObject().className()))
    except Exception:
        pass
    return main_window, others


_MNEMONIC = re.compile(r"&(.)")
_RICH = re.compile(r"^[^\n]*<[A-Za-z!/][^>]*>")  # a tag before the first line break: Qt's mightBeRichText, roughly
_ENTITIES = ((u"&lt;", u"<"), (u"&gt;", u">"), (u"&quot;", u'"'), (u"&#39;", u"'"), (u"&nbsp;", u" "), (u"&amp;", u"&"))


def _label_text(label):
    """A label's text as shown: rich text without its tags and entities, plain text word for word."""
    text = _prop(label, "text") or u""
    try:
        text_format = int(_prop(label, "textFormat"))  # Qt::PlainText 0, RichText 1, AutoText 2
    except Exception:
        text_format = 2
    if text_format == 1 or (text_format == 2 and _RICH.search(text)):
        text = _TAGS.sub(u"", text)
        for entity, character in _ENTITIES:
            text = text.replace(entity, character)
    return text.strip()


@op("fonts")
def op_fonts(listener, args):
    """Every open font: its id (what the tools take as font), names, path, masters, and whether it is in front."""
    fl = _fontlab()
    current = fl.flWorkspace.instance().currentPackage
    current_id = None
    try:
        if not _is_null(current, "the current font"):
            current_id = _font_id(current)
    except FreeLabError:
        pass
    reads = _Reads()
    fonts = []
    for package in open_packages():
        entry = _font_summary(package, reads)
        entry["in_front"] = entry["id"] == current_id
        fonts.append(entry)
    return reads.attach({"fonts": fonts})


@op("font_info")
def op_font_info(listener, args):
    package, count = target_package(args)
    reads = _Reads()
    result = _font_summary(package, reads)
    result["fonts_open"] = count
    return reads.attach(result)


@op("glyph_names")
def op_glyph_names(listener, args):
    package, count = target_package(args)
    offset = _int_arg(args, "offset", 0, 0, 10 ** 9)
    limit = _int_arg(args, "limit", 500, 1, 5000)
    names = _real_list(package.fgPackage.keys(), "fgFont.keys()")
    page = [_text(n) for n in names[offset:offset + limit]]
    return {"font": _font_ref(package), "total": len(names), "offset": offset, "names": page,
            "more": offset + len(page) < len(names)}


def _unicodes(glyph, reads):
    try:
        return [int(u) for u in _real_list(glyph.fgGlyph.unicodes, "fgGlyph.unicodes")]
    except FreeLabError:
        raise
    except Exception as exc:
        reads.unreadable["unicodes"] = _exc_text(exc)
        return None


def _ink_box(layer, reads):
    """The layer's ink box: its outline's extremes, curves measured, as FontLab's sidebearings use it
    (boundingBox is the box of every node, off-curve points included, widened by 0.001)."""
    try:
        return to_jsonable(layer.inkBox())
    except Exception as exc:
        reads.unreadable["ink_box"] = _exc_text(exc)
        return None


def _anchors(layer, reads):
    try:
        return [{"name": _text(anchor.name), "x": anchor.point.x(), "y": anchor.point.y()}
                for anchor in _real_list(layer.anchors, "layer.anchors")]
    except FreeLabError:
        raise
    except Exception as exc:
        reads.unreadable["anchors"] = _exc_text(exc)
        return None


def _component_glyph(shape):
    """The glyph a component shape draws, or None (an outline of its own, or not readable)."""
    try:
        if not shape.isComponent:
            return None
        return _text(shape.shapeData.componentGlyph) or None
    except Exception:
        return None


@op("glyph")
def op_glyph(listener, args):
    package, count = target_package(args)
    glyph = _find_glyph(package, args)
    reads = _Reads()
    layer, layer_source = _choose_layer(glyph, package, args.get("layer"), reads)
    unicodes = _unicodes(glyph, reads)
    bbox = reads.prop(layer, "boundingBox", "bounding_box")
    ink = _ink_box(layer, reads)
    advance = reads.prop(layer, "advanceWidth", "advance_width")
    components = []
    try:
        for shape in _real_list(layer.shapes, "layer.shapes"):
            base = _component_glyph(shape)
            if base is not None:
                components.append({"glyph": base, "transform": to_jsonable(shape.transform)})
    except FreeLabError:
        raise
    except Exception as exc:
        reads.unreadable["components"] = _exc_text(exc)
        components = None
    result = {
        "name": reads.prop(glyph, "name"),
        "unicodes": unicodes,
        "layers": _layer_names(glyph),
        "layer": reads.prop(layer, "name", "layer"),
        "layer_source": layer_source,
        "advance_width": advance,
        "bounding_box": bbox,
        "ink_box": ink,
        "shapes": reads.prop(layer, "shapesCount", "shapes"),
        "nodes": reads.prop(layer, "nodesCount", "nodes"),
        "components": components,
        "anchors": _anchors(layer, reads),
        "mark": reads.prop(glyph, "mark"),
        "tags": reads.prop(glyph, "tags"),
        "note": reads.prop(glyph, "note"),
        "font": _font_ref(package, reads),
    }
    if isinstance(ink, dict) and isinstance(advance, (int, float)) and ink.get("width"):
        result["sidebearings"] = {"left": ink["x"], "right": advance - (ink["x"] + ink["width"])}
    return reads.attach(result)


@op("outline")
def op_outline(listener, args):
    package, count = target_package(args)
    glyph = _find_glyph(package, args)
    reads = _Reads()
    layer, layer_source = _choose_layer(glyph, package, args.get("layer"), reads)
    max_nodes = _int_arg(args, "max_nodes", DEFAULT_MAX_NODES, 1, MAX_NODES_LIMIT)
    shapes_out = []
    total = 0
    truncated = False
    for s_index, shape in enumerate(_real_list(layer.shapes, "layer.shapes")):
        if total >= max_nodes:
            truncated = True
            break
        contours_out = []
        for contour in _real_list(shape.contours, "shape.contours"):
            if total >= max_nodes:
                truncated = True
                break
            nodes = _real_list(contour.nodes(), "contour.nodes()")
            room = max_nodes - total
            nodes_out = [{
                "x": node.x,
                "y": node.y,
                "type": _text(node.type),
                "smooth": bool(node.smooth),
            } for node in nodes[:room]]
            total += len(nodes_out)
            entry = {"closed": bool(contour.closed), "nodes": nodes_out}
            if len(nodes) > room:
                entry["truncated"] = True
                truncated = True
            contours_out.append(entry)
        entry = {
            "index": s_index,
            "name": reads.prop(shape, "name", "shape_%d_name" % s_index),
            "is_component": reads.prop(shape, "isComponent", "shape_%d_is_component" % s_index),
            "transform": reads.prop(shape, "transform", "shape_%d_transform" % s_index),
            "contours": contours_out,
        }
        if entry["is_component"]:
            entry["component"] = _component_glyph(shape)
        shapes_out.append(entry)
    result = {
        "name": reads.prop(glyph, "name"),
        "layer": reads.prop(layer, "name", "layer"),
        "layer_source": layer_source,
        "coordinates": "shape-local; each shape's transform [m11, m12, m21, m22, dx, dy] is not applied",
        "shapes": shapes_out,
        "node_count": total,
        "truncated": truncated,
        "font": _font_ref(package, reads),
    }
    return reads.attach(result)


# ---------------------------------------------------------------------------
# Kerning and features (reads)
# ---------------------------------------------------------------------------

KERN_SIDES = {"left": ("KernLeft", "KernBothSide"), "right": ("KernRight", "KernBothSide")}
MAX_KERN = 10000
TAG_RE = re.compile(r"^[A-Za-z0-9]{4}\Z")  # (\Z: $ also matches before a final newline)


def _master_arg(package, args):
    """The master args["master"] names (the font's current master when none is given), checked against the font."""
    masters = [_text(m) for m in _real_list(package.masters, "flPackage.masters")]
    master = args.get("master")
    if master is None:
        return _text(package.master), masters
    if not isinstance(master, string_types) or _text(master) not in masters:
        raise FreeLabError("no_master", u"%s has no master '%s'; its masters: %s." % (
            _font_label(package), master, u", ".join(masters)))
    return _text(master), masters


def _kerning(package, master):
    kerning = package.kerning(master)
    if kerning is None:
        raise FreeLabError("unreadable", u"FontLab gave no kerning for master '%s'." % master)
    groups = kerning.groups.asDict()
    if not isinstance(groups, dict):
        raise FreeLabError("unexpected_type", "The kerning classes are a %s, not a dict; they were not read." % type(groups).__name__)
    # (keys as text once: Python 2 compares a byte-string key with unicode text only when it is ASCII)
    return kerning, dict((_text(name), entry) for name, entry in groups.items())


def _kern_side(package, groups, value, side):
    """(the key FontLab takes, the side as shown) for one side of a pair: a glyph name, or @name for a class."""
    if not isinstance(value, string_types) or not value.strip():
        raise FreeLabError("bad_args", "%s must be a glyph name, or @ and a kerning class's name." % side)
    value = _text(value)
    if value.startswith(u"@"):
        name = value[1:]
        if name not in groups:
            raise FreeLabError("no_class", u"%s has no kerning class '%s' in this master." % (_font_label(package), name))
        flag = _text(_real_list(groups[name], "a kerning class")[1])
        if flag not in KERN_SIDES[side]:
            raise FreeLabError("wrong_side", u"Kerning class '%s' is a %s class: it cannot be the %s side of a pair." % (
                name, flag, side))
        return name, value
    if glyph_named(package, value) is None:
        raise FreeLabError("no_glyph", u"%s has no glyph '%s'." % (_font_label(package), value))
    if value in groups:
        raise FreeLabError("class_name_clash", u"'%s' is the name of a glyph and of a kerning class (FontLab often names a "
                           u"class after its key glyph), and FontLab's scripting reads and writes '%s' in a pair as the "
                           u"class: give @%s to kern the class. A pair of the glyph on its own (an exception) cannot be "
                           u"read or written through scripting while the names are the same." % (value, value, value))
    return value, value


def _kern_value(kerning, left, right):
    value = kerning.get((left, right))
    return None if value is None else float(value)


def _pair_side(item):
    return (u"@" if _text(item.mode) == u"groupMode" else u"") + _text(item.id)


@op("kerning")
def op_kerning(listener, args):
    """One pair's own value, or a page of the master's pairs; with classes, the master's kerning classes."""
    package, count = target_package(args)
    master, masters = _master_arg(package, args)
    kerning, groups = _kerning(package, master)
    result = {"font": _font_ref(package), "master": master}
    left, right = args.get("left"), args.get("right")
    if (left is None) != (right is None):
        raise FreeLabError("bad_args", "Give both left and right for one pair, or neither for a page of pairs.")
    if left is not None:
        left_key, left_shown = _kern_side(package, groups, left, "left")
        right_key, right_shown = _kern_side(package, groups, right, "right")
        result.update({"left": left_shown, "right": right_shown, "value": _kern_value(kerning, left_key, right_key)})
    else:
        offset = _int_arg(args, "offset", 0, 0, 10 ** 9)
        limit = _int_arg(args, "limit", 500, 1, 5000)
        items = _real_list(kerning.items(), "fgKerning.items()")
        page = []
        for pair, value in items[offset:offset + limit]:
            page.append({"left": _pair_side(pair.left), "right": _pair_side(pair.right), "value": float(value)})
        result.update({"total": len(items), "offset": offset, "pairs": page, "more": offset + len(page) < len(items)})
    if args.get("classes"):
        result["classes"] = dict((_text(name), {"side": _text(entry[1]), "glyphs": [_text(n) for n in entry[0]]})
                                 for name, entry in groups.items())
    return result


@op("features")
def op_features(listener, args):
    """The font's feature tags, in FontLab's order; with tag, that feature's code; with prefix, the prefix's."""
    package, count = target_package(args)
    features = package.fgPackage.features
    tags = [_text(t) for t in _real_list(features.keys(), "features.keys()")]
    result = {"font": _font_ref(package), "tags": tags, "compiled": "not checked: FontLab compiles feature code when the font is exported"}
    tag = args.get("tag")
    if tag is not None:
        if not isinstance(tag, string_types) or not TAG_RE.match(tag):
            raise FreeLabError("bad_args", "tag must be four letters or digits, such as liga or ss01.")
        if _text(tag) not in tags:
            raise FreeLabError("no_feature", u"%s has no feature '%s'." % (_font_label(package), tag))
        result["tag"] = _text(tag)
        result["code"] = _text(features.get_feature(tag))
    if args.get("prefix"):
        result["prefix"] = _text(features.get_prefix())
    return result


# ---------------------------------------------------------------------------
# Writes: one undoable History step each
# ---------------------------------------------------------------------------
# FontLab's History, live in 7.2 (docs/FONTLAB7-API.md): a step of notifyChangesApplied(title, objects,
# True) records each object as it is after the change, and Undo puts back each object's state before that
# step; an object with none stays as written. So the first write to a font records the font first, as a
# step of its own ("(before)"), and so does a write after an Undo back to the font's first state. The font
# recorded recursively holds its layers, kerning, and features, not its glyphs' own properties (name,
# Unicode values): the glyphs a write changes are recorded too. A script's write leaves the font unmarked,
# so FontLab could close it without asking: every write marks it (setFlag(2), ofChanged). Nothing here
# saves, exports, or closes a font.

_RECORDED_FONTS = set()  # fonts (by id) whose whole state a step of this listener recorded
MAX_CODE_BYTES = 1024 * 1024
GLYPH_NAME_RE = re.compile(r"^[A-Za-z_.][A-Za-z0-9_.\-]{0,62}\Z")
ANCHOR_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,63}\Z")
MAX_COMPONENTS = 32
MAX_COORD = 32767.0


def _without_state(obj):
    """Whether FontLab's History holds no state of obj to go back to (it is at its first one). True
    when that cannot be read: the font is then recorded once more, which costs one step."""
    try:
        return obj.currentHistoryState() == obj.initialHistoryState()
    except Exception:
        return True


def _number(args, key, low, high, required=True):
    value = args.get(key)
    if value is None:
        if required:
            raise FreeLabError("bad_args", "%s is required." % key)
        return None
    if isinstance(value, bool) or not isinstance(value, integer_types + (float,)) or value != value or \
            not low <= value <= high:
        raise FreeLabError("bad_args", "%s must be a number from %g to %g." % (key, low, high))
    return value


def _whole(value):
    """A value in FontLab's whole units, halves away from zero (FontLab stored kerning of -19.5 as -20)."""
    whole = math.floor(abs(value) + 0.5)
    return float(-whole if value < 0 else whole)


def _new_glyph_name(package, args, key):
    name = args.get(key)
    if not isinstance(name, string_types) or not GLYPH_NAME_RE.match(name):
        raise FreeLabError("bad_args", "%s must be a glyph name of up to 63 letters, digits, periods, underscores, and "
                                       "hyphens, not starting with a digit or hyphen." % key)
    name = _text(name)
    if glyph_named(package, name) is not None:
        raise FreeLabError("name_taken", u"%s already has a glyph '%s'." % (_font_label(package), name))
    return name


def _unicodes_arg(args, key="unicodes"):
    values = args.get(key)
    if values is None:
        return None
    if not isinstance(values, list) or len(values) > 64:
        raise FreeLabError("bad_args", "%s must be a list of at most 64 code points (integers)." % key)
    seen = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, integer_types) or not 0 <= value <= 0x10FFFF or \
                0xD800 <= value <= 0xDFFF:
            raise FreeLabError("bad_args", "%s holds %r: each must be a code point from 0 to 10FFFF (hex), not a "
                                           "surrogate." % (key, value))
        if value in seen:
            raise FreeLabError("bad_args", "%s lists U+%04X twice." % (key, value))
        seen.append(int(value))
    return seen


def _unicodes_free(package, unicodes, glyph=None):
    """Refuses code points another glyph of the font has. The font's lookup is rebuilt first: FontLab does
    not update it when a script changes a glyph's Unicode values (live, 2026-11-16)."""
    package.fgPackage.rebuildMaps()
    for value in unicodes:
        other = package.findUnicode(value)
        if _is_null(other, "the glyph"):
            continue
        if glyph is not None and _text(other.name) == _text(glyph.name):
            continue
        raise FreeLabError("unicode_taken", u"U+%04X is glyph '%s''s in %s: a code point belongs to one glyph." % (
            value, _text(other.name), _font_label(package)))


def _check_deadline(listener):
    if listener.deadline is not None and time.time() > listener.deadline:
        raise FreeLabError("expired", "The client gave up while FontLab checked this change; nothing was changed.")


def _layer_arg(glyph, package, args):
    """The glyph's layer args["layer"] names (default: the font's current master), never created."""
    wanted = args.get("layer")
    if wanted is not None and (not isinstance(wanted, string_types) or not wanted):
        raise FreeLabError("bad_args", "layer must be a layer name, such as a master's.")
    name = _text(wanted) if wanted is not None else _text(package.master)
    for layer in _real_list(glyph.layers, "glyph.layers"):
        if _text(layer.name) == name:
            return layer
    raise FreeLabError("no_layer", u"Glyph '%s' has no layer '%s'; its layers: %s. (A font made from a variable font "
                       u"has a glyph's layers only in the masters where it is drawn.)" % (
                           _text(glyph.name), name, u", ".join(_layer_names(glyph))))


def write_target(args):
    """target_package for a write or undo: font is required, and font_name, when given, must name the same
    font (its path, PostScript name, or full name), so an id FontLab gave a font opened since is caught."""
    package, count = target_package(args, required=True)
    confirm = args.get("font_name")
    if confirm is not None:
        if not isinstance(confirm, string_types) or not confirm.strip():
            raise FreeLabError("bad_args", "font_name must be the font's path, PostScript name, or full name.")
        if not _font_matches(package, _text(confirm).strip(), names_only=True):
            raise FreeLabError("font_mismatch", u"%s is not '%s': nothing was changed." % (_font_label(package), confirm))
    return package


class _NotKept(Exception):
    """A write's read-back showed FontLab did not keep the change as asked."""


class _Write(object):
    """One write: the font recorded first when its History could not take the write back, the change,
    then one History step, the font marked modified, and what happened. A change that fails is put back
    and read back; when that fails too, the change is recorded and marked all the same (fail)."""

    def __init__(self, listener, package, title, glyphs=()):
        self.fl = _fontlab()
        self.listener = listener
        self.package = package
        self.title = u"FreeLab MCP: " + title
        self.glyphs = list(glyphs)
        self.font_id = _font_id(package)

    def begin(self):
        """Records what Undo needs before anything changes; raises (nothing changed) when FontLab cannot."""
        _check_deadline(self.listener)
        objects = []
        if self.font_id not in _RECORDED_FONTS or _without_state(self.package):
            objects.append(self.package)
        objects.extend(glyph for glyph in self.glyphs if _without_state(glyph))
        if objects:
            try:
                self.fl.flItems.notifyChangesApplied(u"%s (before)" % self.title, objects, True)
            except Exception as exc:
                raise FreeLabError("history_failed", u"Nothing was changed: FontLab's History could not record the "
                                   u"font before the change (%s), so Undo could not have taken it back." % _exc_text(exc))
            if objects[0] is self.package:
                _RECORDED_FONTS.add(self.font_id)

    def run(self, change, restore, glyphs=(), added=False):
        """change() makes the change and reads it back (raising _NotKept when FontLab did not keep it);
        its result is returned. On any failure restore() puts the font back and says whether a read-back
        shows it so; the error says what became of the change (fail). A FreeLabError raised inside change()
        (a read-back that could not be read) comes after the change may have been made: it is put back and
        recorded like any other failure, never let out bare (Codex, 2026-11-16)."""
        try:
            return change()
        except _NotKept as exc:
            message = _text(exc)
        except FreeLabError as exc:
            message = u"%s (%s)" % (_text(exc.message).rstrip(u"."), exc.code)
        except Exception as exc:
            message = u"FontLab raised %s during the change" % _exc_text(exc)
        try:
            restored = bool(restore())
        except Exception:
            restored = False
        if restored:
            raise self.fail(message, True)
        try:  # (the glyphs left: a read that fails here must not keep the change from being recorded)
            left = list(glyphs() if callable(glyphs) else glyphs)
        except Exception:
            left = []
        raise self.fail(message, False, left, added)

    def fail(self, message, restored, glyphs=(), added=False):
        """The error for a change FontLab did not keep. Put back: nothing changed. Not put back: the change
        stays, so it is recorded as a step and the font marked, and the error carries that History status."""
        if restored:
            return FreeLabError("not_kept", u"%s. It was put back, and a read-back shows the font as it was." % message)
        status = self.finish(glyphs, added)
        return FreeLabError("not_kept", u"%s. It could not be put back: what FontLab made of the change is recorded as a "
                            u"step of its History (Undo takes it back) and the font is marked modified; check it in "
                            u"FontLab." % message, {"history": status})

    def finish(self, glyphs=(), added=False):
        """The step recorded, the font marked modified; {recorded, marked_modified, history_state, title, ...}."""
        glyphs = self.glyphs + [g for g in glyphs if g is not None and g not in self.glyphs]
        for glyph in glyphs:
            try:
                glyph.update()
            except Exception:
                pass  # (a redraw)
        status = {"recorded": True, "marked_modified": True}
        problems = []
        try:
            self.fl.flItems.notifyChangesApplied(self.title, [self.package] + glyphs if glyphs else self.package, True)
            _RECORDED_FONTS.add(self.font_id)
        except Exception as exc:
            status["recorded"] = False
            _RECORDED_FONTS.discard(self.font_id)
            problems.append(u"FontLab's History did not record the change (%s): Undo cannot take it back; to drop "
                            u"it, close the font without saving." % _exc_text(exc))
        if added:
            try:
                self.fl.flItems.notifyPackageContentUpdated(self.package.fgPackage.id)
            except Exception:
                pass  # (a redraw: the Font window's list of glyphs)
        try:
            self.package.setFlag(2, True)  # ofChanged: FontLab asks to save it before closing it
        except Exception as exc:
            status["marked_modified"] = False
            problems.append(u"FontLab did not mark the font modified (%s): it may close it without asking to save." %
                            _exc_text(exc))
        try:
            status["history_state"] = to_jsonable(self.package.currentHistoryState())
        except Exception:
            status["history_state"] = None
        if problems:
            status["warning"] = u" ".join(problems)
        status["title"] = self.title
        return status


def _write_reply(package, write_status, **fields):
    reply = {"font": _font_ref(package), "changed": True}
    reply.update(fields)
    reply["history"] = write_status
    return reply


def _unchanged(package, **fields):
    reply = {"font": _font_ref(package), "changed": False}
    reply.update(fields)
    return reply


@op("set_kerning")
def op_set_kerning(listener, args):
    package = write_target(args)
    master, masters = _master_arg(package, args)
    kerning, groups = _kerning(package, master)
    left, left_shown = _kern_side(package, groups, args.get("left"), "left")
    right, right_shown = _kern_side(package, groups, args.get("right"), "right")
    value = _number(args, "value", -MAX_KERN, MAX_KERN)
    before = _kern_value(kerning, left, right)
    pair = {"master": master, "left": left_shown, "right": right_shown}
    if before is not None and before == _whole(value):  # (FontLab keeps whole units: nothing would change)
        return _unchanged(package, value=before, **pair)
    write = _Write(listener, package, u"kerning %s %s" % (left_shown, right_shown))
    write.begin()

    def change():
        kerning[(left, right)] = value
        after = _kern_value(kerning, left, right)
        if after is None or abs(after - float(value)) > 0.5 + 1e-6:  # (-19.5 read back -20.0, live)
            raise _NotKept(u"FontLab did not keep the pair (it reads %s)" % after)
        return after

    def restore():
        if before is None:
            kerning.remove((left, right))
        else:
            kerning[(left, right)] = before
        return _kern_value(kerning, left, right) == before

    after = write.run(change, restore)
    reply = _write_reply(package, write.finish(), before=before, value=after, **pair)
    if after != float(value):
        reply["rounded"] = u"FontLab keeps kerning in whole units: %g was stored as %g." % (value, after)
    return reply


@op("remove_kerning")
def op_remove_kerning(listener, args):
    package = write_target(args)
    master, masters = _master_arg(package, args)
    kerning, groups = _kerning(package, master)
    left, left_shown = _kern_side(package, groups, args.get("left"), "left")
    right, right_shown = _kern_side(package, groups, args.get("right"), "right")
    before = _kern_value(kerning, left, right)
    if before is None:
        raise FreeLabError("no_pair", u"%s has no kerning pair %s %s of its own in master '%s'." % (
            _font_label(package), left_shown, right_shown, master))
    write = _Write(listener, package, u"remove kerning %s %s" % (left_shown, right_shown))
    write.begin()

    def change():
        kerning.remove((left, right))
        if _kern_value(kerning, left, right) is not None:
            raise _NotKept(u"FontLab still has the pair after removing it")

    def restore():
        if _kern_value(kerning, left, right) is None:
            kerning[(left, right)] = before
        return _kern_value(kerning, left, right) == before

    write.run(change, restore)
    return _write_reply(package, write.finish(), master=master, left=left_shown, right=right_shown, before=before, value=None)


def _metrics(layer):
    """(advance width, left sidebearing, right sidebearing or None) of a layer, from its ink box."""
    width = float(layer.advanceWidth)
    if not layer.shapesCount:
        return width, None, None
    ink = layer.inkBox()
    if ink.width() <= 0 and ink.height() <= 0:
        return width, None, None
    return width, float(ink.x()), width - float(ink.x() + ink.width())


def _same_metrics(a, b, tolerance=0.01):
    return all((x is None and y is None) or (x is not None and y is not None and abs(x - y) <= tolerance)
               for x, y in zip(a, b))


@op("set_metrics")
def op_set_metrics(listener, args):
    """A layer's advance width and sidebearings. A left sidebearing moves the outline (its components and
    anchors with it) by whole units and keeps the right one unless a width or right sidebearing is given."""
    package = write_target(args)
    glyph = _find_glyph(package, args, name_key="glyph")
    layer = _layer_arg(glyph, package, args)
    width_new = _number(args, "advance_width", 0, 65535, required=False)
    lsb_new = _number(args, "left_sidebearing", -MAX_COORD, MAX_COORD, required=False)
    rsb_new = _number(args, "right_sidebearing", -MAX_COORD, MAX_COORD, required=False)
    given = [v for v in (width_new, lsb_new, rsb_new) if v is not None]
    if not given:
        raise FreeLabError("bad_args", "Give advance_width, left_sidebearing, or right_sidebearing.")
    if len(given) == 3:
        raise FreeLabError("bad_args", "Give at most two of advance_width, left_sidebearing, and right_sidebearing: "
                                       "the third follows from them.")
    width, lsb, rsb = _metrics(layer)
    if lsb is None and (lsb_new is not None or rsb_new is not None):
        raise FreeLabError("no_outline", u"Glyph '%s' has no outline in layer '%s', so it has no sidebearings; only its "
                           u"advance width can be set." % (_text(glyph.name), _text(layer.name)))
    ink_right = None if lsb is None else width - rsb  # (the ink's right edge)
    # The outline moves by whole units, so its nodes stay on FontLab's grid; a sidebearing the ink box makes
    # fractional comes out within half a unit of the one asked, and the reply says so (rounded).
    if lsb_new is not None:
        shift = _whole(float(lsb_new) - lsb)
    elif width_new is not None and rsb_new is not None:
        shift = _whole((float(width_new) - float(rsb_new)) - ink_right)
    else:
        shift = 0.0
    if width_new is not None:
        target_width = float(width_new)
    elif rsb_new is not None:
        target_width = (ink_right + shift) + float(rsb_new)
    else:
        target_width = width + shift  # the right sidebearing kept
    if target_width < 0:
        raise FreeLabError("bad_args", "That would make the advance width %g, below 0." % target_width)
    target = {"advance_width": target_width}
    if lsb is not None:
        target["left_sidebearing"] = lsb + shift
        target["right_sidebearing"] = target_width - (ink_right + shift)
    before = {"advance_width": width, "left_sidebearing": lsb, "right_sidebearing": rsb}
    for key, expression in (("left_sidebearing", "metricsLeft"), ("right_sidebearing", "metricsRight"),
                            ("advance_width", "metricsWidth")):
        try:
            formula = _text(getattr(layer, expression) or u"")
        except Exception:
            formula = u""
        if formula and target.get(key) is not None and abs(target[key] - (before[key] or 0.0)) > 1e-6:
            raise FreeLabError("metrics_expression", u"Layer '%s' of '%s' sets its %s by the expression '%s', which FontLab "
                               u"would apply again: nothing was changed. Change or clear the expression in FontLab." % (
                                   _text(layer.name), _text(glyph.name), key.replace("_", " "), formula))
    # (a shape whose outline data another glyph shares stays put there: moving B's layer left H's outline where it
    # was, live 2026-11-16, though both shapes pointed to the same flShapeData)
    if abs(shift) < 1e-9 and abs(target_width - width) < 1e-9:
        return _unchanged(package, glyph=_text(glyph.name), layer=_text(layer.name), **before)
    from PythonQt.QtGui import QTransform  # only inside FontLab
    write = _Write(listener, package, u"metrics of %s" % _text(glyph.name), [glyph])
    write.begin()

    def change():
        if shift:
            layer.applyTransform(QTransform().translate(shift, 0))
        layer.advanceWidth = target_width
        after = _metrics(layer)
        # FontLab keeps whole advance widths (618.2 read back 618.0, live)
        if abs(after[0] - target_width) > 0.5 + 1e-6 or (after[1] is not None and (
                abs(after[1] - target["left_sidebearing"]) > 0.5 + 1e-6 or
                abs(after[2] - target["right_sidebearing"]) > 1.0 + 1e-6)):
            raise _NotKept(u"FontLab did not keep the metrics (read back: width %s, left %s, right %s)" % after)
        return after

    def restore():  # measured, not assumed: the move may not have happened
        now = _metrics(layer)
        if lsb is not None and now[1] is not None and abs(now[1] - lsb) > 1e-9:
            layer.applyTransform(QTransform().translate(lsb - now[1], 0))
        layer.advanceWidth = width
        return _same_metrics(_metrics(layer), (width, lsb, rsb))

    after_width, after_lsb, after_rsb = write.run(change, restore)
    after = {"advance_width": after_width, "left_sidebearing": after_lsb, "right_sidebearing": after_rsb}
    # what was asked, and what was to stay: the other sidebearing when only one value was given
    expected = {"advance_width": width_new, "left_sidebearing": lsb_new, "right_sidebearing": rsb_new}
    if len(given) == 1 and lsb is not None:
        if width_new is not None or rsb_new is not None:
            expected["left_sidebearing"] = lsb
        else:
            expected["right_sidebearing"] = rsb
    rounded = [key for key, value in expected.items() if value is not None and after[key] is not None and
               abs(after[key] - value) > 1e-6]
    reply = _write_reply(package, write.finish(), glyph=_text(glyph.name), layer=_text(layer.name), before=before,
                         after=after, moved_by=shift)
    if rounded:
        reply["rounded"] = u"FontLab keeps whole advance widths, and outlines move by whole units: %s came out as shown " \
                           u"in after." % u", ".join(key.replace("_", " ") for key in sorted(rounded))
    return reply


def _references(package, name):
    """Where a glyph name is used outside components: kerning pairs and classes in every master, other
    classes, and feature code (as a whole word). FontLab's rename does not follow these (live: the
    pairs were lost on Undo), so a rename is refused while there are any."""
    found = []
    masters = [_text(m) for m in _real_list(package.masters, "flPackage.masters")]
    for master in masters:
        kerning, groups = _kerning(package, master)
        pairs = 0
        for pair in _real_list(kerning.keys(), "fgKerning.keys()"):
            for item in (pair.left, pair.right):
                if _text(item.mode) == u"glyphMode" and _text(item.id) == name:
                    pairs += 1
                    break
        if pairs:
            found.append(u"%d kerning pair%s in master %s" % (pairs, "" if pairs == 1 else "s", master))
        classes = sorted(c for c, entry in groups.items() if name in [_text(n) for n in entry[0]])
        if classes:
            found.append(u"kerning class%s %s in master %s" % ("" if len(classes) == 1 else "es", u", ".join(classes), master))
    others = sorted(set(_text(entry[1]) for entry in _real_list(package.getAllGroups(), "getAllGroups()")
                        if not entry[0] and name in [_text(n) for n in entry[4]]))
    if others:
        found.append(u"class%s %s" % ("" if len(others) == 1 else "es", u", ".join(others)))
    features = package.fgPackage.features
    word = re.compile(u"(?<![A-Za-z0-9_.@\\-])%s(?![A-Za-z0-9_.\\-])" % re.escape(name))  # (@name is a class)
    tags = [t for t in _real_list(features.keys(), "features.keys()") if word.search(_text(features.get_feature(t)))]
    if word.search(_text(features.get_prefix())):
        tags.insert(0, u"prefix")
    if tags:
        found.append(u"feature code (%s)" % u", ".join(_text(t) for t in tags))
    return found


@op("rename_glyph")
def op_rename_glyph(listener, args):
    package = write_target(args)
    glyph = _find_glyph(package, args, name_key="glyph")
    old = _text(glyph.name)
    if args.get("new_name") == old:
        return _unchanged(package, glyph=old)
    new = _new_glyph_name(package, args, "new_name")
    used = _references(package, old)
    if used:
        raise FreeLabError("glyph_in_use", u"Glyph '%s' is used by %s: FontLab's rename would leave those pointing to the "
                           u"old name, so nothing was renamed." % (old, u"; ".join(used)))
    write = _Write(listener, package, u"rename %s to %s" % (old, new), [glyph])
    write.begin()

    def renamed(name, gone):
        package.fgPackage.rebuildMaps()
        return glyph_named(package, name) is not None and glyph_named(package, gone) is None

    def change():
        if not package.renameGlyph(glyph.fgGlyph, new, True):
            raise _NotKept(u"FontLab refused to rename '%s'" % old)
        if not renamed(new, old):
            raise _NotKept(u"FontLab's glyph list does not show '%s' renamed to '%s'" % (old, new))

    def restore():
        if _text(glyph.name) != old:
            package.renameGlyph(glyph.fgGlyph, old, True)
        return renamed(old, new)

    write.run(change, restore)
    return _write_reply(package, write.finish(), glyph=new, before=old,
                        note="Components that use the glyph follow the new name (FontLab updates them).")


@op("set_unicodes")
def op_set_unicodes(listener, args):
    package = write_target(args)
    glyph = _find_glyph(package, args, name_key="glyph")
    unicodes = _unicodes_arg(args)
    if unicodes is None:
        raise FreeLabError("bad_args", "unicodes is required: a list of code points (an empty list clears them).")
    reads = _Reads()
    before = _unicodes(glyph, reads)
    if before is None:
        raise FreeLabError("unreadable", "The glyph's Unicode values could not be read; nothing was changed.")
    if before == unicodes:
        return _unchanged(package, glyph=_text(glyph.name), unicodes=before)
    _unicodes_free(package, unicodes, glyph)
    write = _Write(listener, package, u"Unicode values of %s" % _text(glyph.name), [glyph])
    write.begin()

    def change():
        glyph.fgGlyph.unicodes = unicodes
        package.fgPackage.rebuildMaps()
        after = _unicodes(glyph, _Reads())
        if after != unicodes:
            raise _NotKept(u"FontLab did not keep the Unicode values (read back %s)" % (after,))
        return after

    def restore():
        glyph.fgGlyph.unicodes = before
        package.fgPackage.rebuildMaps()
        return _unicodes(glyph, _Reads()) == before

    after = write.run(change, restore)
    return _write_reply(package, write.finish(), glyph=_text(glyph.name), before=before, unicodes=after)


def _components_arg(package, args, new_name):
    parts = args.get("components")
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_COMPONENTS:
        raise FreeLabError("bad_args", "components must be a list of 1 to %d {glyph, x, y} objects." % MAX_COMPONENTS)
    out = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict) or set(part) - set(["glyph", "x", "y"]):
            raise FreeLabError("bad_args", "components[%d] must be an object with glyph, and x and y (offsets, 0 when "
                                           "left out), and nothing else." % index)
        name = part.get("glyph")
        if not isinstance(name, string_types) or not name:
            raise FreeLabError("bad_args", "components[%d].glyph must be a glyph name." % index)
        name = _text(name)
        if name == new_name:
            raise FreeLabError("bad_args", "A glyph cannot be made of itself.")
        base = glyph_named(package, name)
        if base is None:
            raise FreeLabError("no_glyph", u"%s has no glyph '%s' (components[%d])." % (_font_label(package), name, index))
        x = _number(part, "x", -MAX_COORD, MAX_COORD, required=False) or 0
        y = _number(part, "y", -MAX_COORD, MAX_COORD, required=False) or 0
        out.append((name, base, float(x), float(y)))
    return out


@op("add_component_glyph")
def op_add_component_glyph(listener, args):
    """A new glyph made of components of the font's glyphs, in every master where each of them has a layer."""
    fl = _fontlab()
    package = write_target(args)
    name = _new_glyph_name(package, args, "name")
    parts = _components_arg(package, args, name)
    unicodes = _unicodes_arg(args) or []
    width_given = _number(args, "advance_width", 0, 65535, required=False)
    masters = [_text(m) for m in _real_list(package.masters, "flPackage.masters")]
    plans, skipped = [], []
    for master in masters:
        layers = [base.getLayerByName(master, False) for _n, base, _x, _y in parts]
        if any(_is_null(layer, "a layer") for layer in layers):
            skipped.append(master)
            continue
        width = float(width_given) if width_given is not None else float(layers[0].advanceWidth)
        plans.append((master, width))
    if not plans:
        raise FreeLabError("no_master", u"No master has a layer in every component's glyph (%s); nothing was added." %
                           u", ".join(n for n, _b, _x, _y in parts))
    if unicodes:
        _unicodes_free(package, unicodes)
    from PythonQt.QtGui import QTransform  # only inside FontLab
    write = _Write(listener, package, u"add %s" % name)
    write.begin()

    def change():
        new = fl.flGlyph()
        new.name = name
        package.addGlyph(new)  # FontLab adds a copy without layers: they are made on the font's glyph
        glyph = glyph_named(package, name)
        if glyph is None:
            raise _NotKept(u"FontLab did not add '%s'" % name)
        built, placed = [], []
        for master, width in plans:
            components = []
            for base_name, _base, x, y in parts:
                component = fl.GlyphComponent(base_name)
                component.transform = QTransform().translate(x, y)
                components.append(component)
            layer = glyph.getLayerByName(master, True)
            layer.setGlyphComponents(components, width, package, False)
            if layer.shapesCount != len(parts):
                raise _NotKept(u"FontLab kept %d of the %d components in master %s" % (layer.shapesCount, len(parts), master))
            shapes = _real_list(layer.shapes, "layer.shapes")
            kept = [_component_glyph(shape) for shape in shapes]
            if kept != [n for n, _b, _x, _y in parts]:
                raise _NotKept(u"FontLab kept the components %s in master %s, not those asked" % (kept, master))
            for shape, (base_name, _b, x, y) in zip(shapes, parts):  # (each offset as asked: Codex, 2026-11-16)
                t = shape.transform
                at = (float(t.dx()), float(t.dy()))
                if abs(at[0] - x) > 0.5 or abs(at[1] - y) > 0.5 or (t.m11(), t.m12(), t.m21(), t.m22()) != (1, 0, 0, 1):
                    raise _NotKept(u"FontLab placed component %s at (%g, %g) in master %s, not at (%g, %g)" % (
                        base_name, at[0], at[1], master, x, y))
                if not built:  # (the offsets reported are those FontLab stored, which it may round: live)
                    placed.append({"glyph": base_name, "x": at[0], "y": at[1]})
            if abs(float(layer.advanceWidth) - width) > 0.5:
                raise _NotKept(u"FontLab gave master %s the width %g, not %g" % (master, float(layer.advanceWidth), width))
            built.append({"master": master, "advance_width": float(layer.advanceWidth)})
        if unicodes:  # last: given before the layers are made, FontLab kept none of them (live, 2026-11-11)
            glyph.fgGlyph.unicodes = unicodes
            package.fgPackage.rebuildMaps()
        kept = _unicodes(glyph, _Reads())
        if kept != unicodes:  # (read back as set_unicodes does: Codex, 2026-11-16)
            raise _NotKept(u"FontLab did not keep the Unicode values of '%s' (read back %s)" % (name, kept))
        return glyph, built, placed

    def restore():
        glyph = glyph_named(package, name)
        if glyph is not None:
            package.deleteGlyph(glyph)
            package.fgPackage.rebuildMaps()
        return glyph_named(package, name) is None

    glyph, built, placed = write.run(change, restore, glyphs=lambda: [glyph_named(package, name)], added=True)
    extra = {}
    moved = [p["glyph"] for p, (_n, _b, x, y) in zip(placed, parts) if (p["x"], p["y"]) != (x, y)]
    if moved:
        extra["rounded"] = u"FontLab rounded the offset of %s to whole units (as the font rounds coordinates)." % (
            u", ".join(moved))
    return _write_reply(package, write.finish([glyph], added=True), glyph=name, unicodes=unicodes,
                        components=placed, masters=built, skipped_masters=skipped, **extra)


def _anchor_at(layer, name):
    found = layer.findAnchor(name)
    return None if found is None else (found.point.x(), found.point.y())


@op("set_anchor")
def op_set_anchor(listener, args):
    package = write_target(args)
    glyph = _find_glyph(package, args, name_key="glyph")
    layer = _layer_arg(glyph, package, args)
    name = args.get("name")
    if not isinstance(name, string_types) or not ANCHOR_NAME_RE.match(name):
        raise FreeLabError("bad_args", "name must be an anchor name of 1 to 63 letters, digits, periods, underscores, "
                                       "and hyphens, such as top or _top.")
    name = _text(name)
    x = float(_number(args, "x", -MAX_COORD, MAX_COORD))
    y = float(_number(args, "y", -MAX_COORD, MAX_COORD))
    from PythonQt.QtCore import QPointF  # only inside FontLab
    was = _anchor_at(layer, name)
    before = None if was is None else {"x": was[0], "y": was[1]}
    if was == (x, y):
        return _unchanged(package, glyph=_text(glyph.name), layer=_text(layer.name), anchor=name, x=x, y=y)
    write = _Write(listener, package, u"anchor %s of %s" % (name, _text(glyph.name)), [glyph])
    write.begin()

    def change():
        existing = layer.findAnchor(name)
        if existing is None:
            pin = _fontlab().flPinPoint(QPointF(x, y))
            pin.name = name
            layer.addAnchor(pin)
        else:
            existing.point = QPointF(x, y)
        if _anchor_at(layer, name) != (x, y):
            raise _NotKept(u"FontLab did not keep anchor '%s' at (%g, %g)" % (name, x, y))

    def restore():
        found = layer.findAnchor(name)
        if was is None and found is not None:
            layer.removeAnchor(found)
        elif was is not None and found is not None:
            found.point = QPointF(was[0], was[1])
        return _anchor_at(layer, name) == was

    write.run(change, restore)
    return _write_reply(package, write.finish(), glyph=_text(glyph.name), layer=_text(layer.name), anchor=name,
                        before=before, x=x, y=y)


@op("remove_anchor")
def op_remove_anchor(listener, args):
    package = write_target(args)
    glyph = _find_glyph(package, args, name_key="glyph")
    layer = _layer_arg(glyph, package, args)
    name = args.get("name")
    if not isinstance(name, string_types) or not name:
        raise FreeLabError("bad_args", "name must be the anchor's name.")
    name = _text(name)
    was = _anchor_at(layer, name)
    if was is None:
        raise FreeLabError("no_anchor", u"Layer '%s' of '%s' has no anchor '%s'." % (_text(layer.name), _text(glyph.name), name))
    from PythonQt.QtCore import QPointF  # only inside FontLab
    write = _Write(listener, package, u"remove anchor %s of %s" % (name, _text(glyph.name)), [glyph])
    write.begin()

    def change():
        layer.removeAnchor(layer.findAnchor(name))
        if layer.findAnchor(name) is not None:
            raise _NotKept(u"FontLab still has anchor '%s' after removing it" % name)

    def restore():
        if layer.findAnchor(name) is None:  # a new pin: the removed one may be gone
            pin = _fontlab().flPinPoint(QPointF(was[0], was[1]))
            pin.name = name
            layer.addAnchor(pin)
        return _anchor_at(layer, name) == was

    write.run(change, restore)
    return _write_reply(package, write.finish(), glyph=_text(glyph.name), layer=_text(layer.name), anchor=name,
                        before={"x": was[0], "y": was[1]})


def _feature_block(tag, code):
    """None when code is exactly one top-level block, feature tag { ... } tag; (with comments and blank lines
    around it); else why not. Braces are matched outside comments and strings, and nothing else may stand outside the
    block: another feature, a lookup, a languagesystem line, or FontLab's #> and #< markers would ride
    along into the font's feature code."""
    n, i = len(code), 0

    def skip_space_and_comments(i, markers_allowed):
        while i < n:
            if code[i].isspace():
                i += 1
            elif code[i] == u"#":
                end = code.find(u"\n", i)
                end = n if end < 0 else end
                if not markers_allowed and re.match(u"#\\s*/?\\s*[<>]", code[i:end]):
                    return -1
                i = end
            else:
                break
        return i

    i = skip_space_and_comments(i, False)
    if i < 0:
        return u"FontLab's #> and #< markers belong inside a block, not around it"
    head = re.compile(u"feature\\s+%s\\s*\\{" % tag).match(code, i)
    if head is None:
        return u"it must start with feature %s {" % tag
    i, depth = head.end(), 1
    while i < n and depth:
        c = code[i]
        if c == u"#":
            end = code.find(u"\n", i)
            i = n if end < 0 else end
            continue
        if c == u'"':  # a string (a name's text): its braces and # are text (Codex, 2026-11-16); the
            end = code.find(u'"', i + 1)  # feature syntax has no escaped quote (\0022 stands for one)
            if end < 0:
                return u"a string in it does not close"
            i = end + 1
            continue
        if c == u"{":
            depth += 1
        elif c == u"}":
            depth -= 1
        i += 1
    if depth:
        return u"its braces do not close"
    tail = re.compile(u"\\s*%s\\s*;" % tag).match(code, i)
    if tail is None:
        return u"its closing brace must be followed by %s;" % tag
    i = skip_space_and_comments(tail.end(), False)
    if i < 0:
        return u"FontLab's #> and #< markers belong inside a block, not around it"
    if i < n:
        return u"there is more code after the block (one feature per call)"
    return None


@op("set_feature")
def op_set_feature(listener, args):
    """A feature's code, as FontLab keeps it: the whole block, feature tag { ... } tag;. Not compiled."""
    package = write_target(args)
    tag = args.get("tag")
    if not isinstance(tag, string_types) or not TAG_RE.match(tag):
        raise FreeLabError("bad_args", "tag must be four letters or digits, such as liga or ss01.")
    tag = _text(tag)
    code = args.get("code")
    if not isinstance(code, string_types) or len(_bytes(code)) > MAX_CODE_BYTES:
        raise FreeLabError("bad_args", "code must be text of at most 1 MB.")
    code = _text(code)
    problem = _feature_block(tag, code)
    if problem:
        raise FreeLabError("bad_args", u"code must be one whole feature block, as FontLab keeps it: feature %s { ... } %s; "
                           u"(comments around it allowed), and %s." % (tag, tag, problem))
    features = package.fgPackage.features
    tags = [_text(t) for t in _real_list(features.keys(), "features.keys()")]
    before = _text(features.get_feature(tag)) if tag in tags else None
    if before is not None and _same_code(before, code):
        return _unchanged(package, tag=tag, created=False)
    write = _Write(listener, package, u"feature %s" % tag)
    write.begin()

    def change():
        features.set_feature(tag, code)
        stored = _text(features.get_feature(tag))
        if not _same_code(stored, code):
            raise _NotKept(u"FontLab did not keep the code of %s as given" % tag)
        return stored

    def restore():
        if before is None:
            features.remove(tag)
            return tag not in [_text(t) for t in _real_list(features.keys(), "features.keys()")]
        features.set_feature(tag, before)
        return _text(features.get_feature(tag)) == before

    stored = write.run(change, restore)
    return _write_reply(package, write.finish(), tag=tag, created=before is None, before=before, code=stored,
                        compiled="not checked: FontLab compiles feature code when the font is exported")


def _same_code(stored, given):
    """The same feature code as FontLab keeps it: line breaks as \\n (given as \\r\\n, they may come back
    either way), and a line break at the end (given without one, FontLab adds it: live)."""
    def norm(text):
        text = text.replace(u"\r\n", u"\n").replace(u"\r", u"\n")
        return text if text.endswith(u"\n") else text + u"\n"
    return norm(stored) == norm(given)


def _main_window_action(name):
    from PythonQt.QtGui import QApplication, QAction  # only inside FontLab
    for widget in QApplication.instance().topLevelWidgets():
        if widget.metaObject().className() == "MainWindow":
            for action in widget.findChildren(QAction):
                if _text(_prop(action, "objectName")) == name:
                    return action
    return None


@op("undo")
def op_undo(listener, args):
    """FontLab's Edit > Undo on the font in front, which must be the font named. With expected_state and
    expected_title (a write's history.history_state and history.title), only while the font's History
    is still at that state and the step Undo would take back is that write's."""
    package = write_target(args)
    fl = _fontlab()
    current = fl.flWorkspace.instance().currentPackage
    if _is_null(current, "the current font") or _font_id(current) != _font_id(package):
        raise FreeLabError("not_in_front", u"FontLab's Undo acts on the font in front, and that is not %s: bring it to "
                           u"the front in FontLab first (click its tab)." % _font_label(package))
    expected = args.get("expected_state")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, integer_types)):
        raise FreeLabError("bad_args", "expected_state must be an integer: a history_state a write reported.")
    expected_title = args.get("expected_title")
    if expected_title is not None and (not isinstance(expected_title, string_types) or not expected_title.strip()):
        raise FreeLabError("bad_args", "expected_title must be the history.title a write reported.")
    state = package.currentHistoryState()
    if expected is not None and state != expected:
        raise FreeLabError("history_moved", u"The font's History is at state %s, not %s: something else changed it "
                           u"since; nothing was undone." % (state, expected))
    _check_deadline(listener)
    undo, redo = _main_window_action("actionUndo"), _main_window_action("actionRedo")
    if undo is None or redo is None:
        raise FreeLabError("not_found", "FontLab's Edit > Undo or Redo was not found; nothing was undone.")
    if not _prop(undo, "enabled"):
        raise FreeLabError("nothing_to_undo", "FontLab's Edit > Undo is unavailable: there is nothing to undo.")
    top = _step_title(undo, u"Undo")
    if expected_title is not None and top.lower() != _text(expected_title).strip().lower():
        # (a step that records only glyphs, such as a write's leftover "(before)", leaves the state as it was)
        raise FreeLabError("history_moved", u"The step Undo would take back is '%s', not '%s': nothing was undone." % (
            top, expected_title))
    undone = []
    while True:
        title = _step_title(undo, u"Undo")
        redo_before = _step_title(redo, u"Redo") if _prop(redo, "enabled") else None
        state_before = package.currentHistoryState()
        undo.trigger()
        redo_after = _step_title(redo, u"Redo") if _prop(redo, "enabled") else None
        # each step Undo takes back becomes the one Redo puts back; when Redo named a step of the same title
        # already, the font's state has to move too (two such steps in a row that record glyphs only are
        # the one case this cannot tell apart)
        if redo_after != title or (redo_before == title and package.currentHistoryState() == state_before):
            if undone:
                break
            raise FreeLabError("not_kept", u"FontLab's Undo of '%s' did not take: nothing was undone." % title)
        undone.append(title)
        # A write's "(before)" step only recorded the font before it and changed nothing: it goes with the
        # write, so that each write is one Undo. One left alone (a write refused after it) goes on its own.
        following = _step_title(undo, u"Undo") if _prop(undo, "enabled") else u""
        if _before_step(title) or following.lower() != (title + u" (before)").lower():
            break  # (only the write's own: an earlier one left alone stays a step of its own)
    reply = {"font": _font_ref(package), "undone": undone, "history_state_before": to_jsonable(state),
             "history_state": to_jsonable(package.currentHistoryState()),
             "next_undo": _step_title(undo, u"Undo") if _prop(undo, "enabled") else None}
    if _before_step(undone[0]):
        reply["note"] = (u"That step only recorded the font before a FreeLab MCP write that did not happen; it "
                         u"changed nothing. Undo again to take back the step before it.")
    return reply


def _step_title(action, verb):
    """The History step an Undo or Redo action names ("&Undo FreeLab MCP: kerning A V" -> its title)."""
    text = _MNEMONIC.sub(r"\1", _text(_prop(action, "text") or u"")).strip()
    if text.lower().startswith(verb.lower()):
        text = text[len(verb):].strip()
    return text


def _before_step(title):
    """Whether a step is a FreeLab MCP write's "(before)" step (FontLab shows titles in title case)."""
    lowered = title.lower()
    return lowered.startswith(u"freelab mcp:") and lowered.endswith(u"(before)")


class _Capture(object):
    """A stdout stand-in that accepts both text and bytes (Python 2 print writes str)."""

    encoding = "utf-8"

    def __init__(self):
        self.parts = []

    def write(self, data):
        self.parts.append(_text(data))

    def flush(self):
        pass

    def isatty(self):
        return False

    def value(self):
        return u"".join(self.parts)


@op("run_python")
def op_run_python(listener, args):
    code = args.get("code")
    if not isinstance(code, string_types) or not code.strip():
        raise FreeLabError("bad_args", "code must be a non-empty string.")
    if args.get("persist"):
        namespace = listener.session_namespace
    else:
        namespace = listener.fresh_namespace()
    namespace.pop("result", None)
    captured = _Capture()
    saved = (sys.stdout, sys.stderr)
    sys.stdout = sys.stderr = captured
    error = None
    try:
        try:
            # dont_inherit: the code gets FontLab's own defaults, not this file's __future__ imports.
            compiled = compile(code, "<freelab>", "exec", 0, True)
            exec(compiled, namespace)
        except BaseException:  # SystemExit and KeyboardInterrupt too: nothing may leave into FontLab
            error = _format_exc()
    finally:
        sys.stdout, sys.stderr = saved
    try:
        reply = {"output": captured.value(), "result": to_jsonable(namespace.get("result"))}
    finally:
        if not args.get("persist"):
            # A function the code defined holds this namespace as its globals, a cycle only a later garbage
            # collection breaks. Qt objects the code made would then die in some unrelated request (a crash in
            # FontLab once surfaced in tab docking's timer that way), so they die here, in their own request.
            # A deferred callback that needs these names must run with persist.
            builtins = namespace.get("__builtins__")
            namespace.clear()
            if builtins is not None:
                # Kept: a deferred callback the code left (a timer) still runs with these globals, and without
                # __builtins__ Python 2.7's import fails from its frame. FontLab's script runner imports sys
                # from the calling frame, so a menu action triggered from such a callback crashed FontLab
                # (PyObjToStringList(NULL) in PythonQt, 2026-11-16). Builtins hold none of the code's objects.
                namespace["__builtins__"] = builtins
    if error is not None:
        reply["error"] = error
    return reply


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------

def _environ(name):
    """An environment variable as Unicode. Python 2.7 on Windows reads os.environ through the ANSI
    code page, which turns a profile path it cannot spell into question marks; the wide API does not."""
    if PY2 and os.name == "nt":
        try:
            import ctypes
            get = ctypes.WinDLL("kernel32").GetEnvironmentVariableW
            get.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            get.restype = ctypes.c_uint32
            wide = name.decode("ascii") if isinstance(name, bytes) else name  # a byte string finds nothing
            size = get(wide, None, 0)
            if size:
                buffer = ctypes.create_unicode_buffer(size)
                if get(wide, buffer, size):
                    return buffer.value
        except Exception:
            pass
    return os.environ.get(name)


def state_dir():
    override = _environ("FREELAB_STATE_DIR")
    if override:
        return override
    base = _environ("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "FreeLab")


def state_path():
    return os.path.join(state_dir(), "listener.json")


def _new_token():
    return binascii.hexlify(os.urandom(16)).decode("ascii")


def _bytes(text):
    if isinstance(text, bytes):
        return text
    return text.encode("utf-8")


def _no_inherit(sock):
    """Keep a child process FontLab starts from holding the port (Python 2 sockets are inheritable)."""
    if os.name != "nt" or not PY2:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetHandleInformation(sock.fileno(), 1, 0)  # HANDLE_FLAG_INHERIT
    except Exception:
        pass


class _Client(object):
    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()
        self.started = time.time()
        self.outgoing = None  # the reply being sent (bytes), and how much of it went out
        self.sent = 0
        self.send_deadline = None


class Listener(object):
    def __init__(self, port=None, state_file=None):
        self.port = DEFAULT_PORT if port is None else port
        self.state_file = state_file or state_path()
        self.token = _new_token()
        self.server = None
        self.clients = []
        self.timer = None
        self.session_namespace = self.fresh_namespace()
        self.requests = 0
        self.errors = []
        self._busy = False
        self._reported = set()
        self.quit_timer = None
        self.quit_requested = False
        self.closing = False  # inside the main window's close(), which may wait on FontLab's save question
        self.deadline = None  # the request being handled: its deadline (seconds since the epoch), or None

    # -- quitting FontLab ----------------------------------------------------
    def request_quit(self, delay_ms=QUIT_DELAY_MS):
        """Close FontLab's main window shortly, after this request's reply has gone out.

        Closing the window, rather than ending the application, runs FontLab's own
        shutdown: its preferences are saved, and it may still ask something on screen.
        """
        if self.quit_in_progress():  # never drop a timer whose callback may be running (see quit_in_progress)
            raise FreeLabError("quit_in_progress", "FontLab is already closing.")
        self.quit_requested = True
        from PythonQt.QtCore import QTimer  # only inside FontLab
        timer = QTimer()
        timer.setSingleShot(True)
        timer.setInterval(delay_ms)
        try:
            connected = timer.connect("timeout()", self._close_main_window) is not False
        except Exception:
            connected = False
        if not connected:
            timer.timeout.connect(self._close_main_window)
        timer.start()
        self.quit_timer = timer  # referenced until it fires

    def quit_in_progress(self):
        """A quit is under way: its timer not fired yet, or the main window's close() still running (it
        waits inside FontLab's save question). Replacing self.quit_timer then would let Python free a
        Qt timer whose callback is on the stack."""
        if self.closing:
            return True
        timer = self.quit_timer
        if timer is None:
            return False
        try:
            return bool(_prop(timer, "active"))
        except Exception:
            return True  # cannot tell: refused rather than risked

    def _close_main_window(self):
        """Close FontLab's main window (class MainWindow in FontLab 7.2, seen live).

        Never QApplication.quit(): it ends the event loop without FontLab's own shutdown
        or its last questions. When the window is not found, FontLab stays open, the
        Output panel says why, and quit_fontlab reports that FontLab is still running.
        """
        try:
            from PythonQt.QtGui import QApplication
            app = QApplication.instance()
            windows = [w for w in app.topLevelWidgets() if w.metaObject().className() == "MainWindow"]
            if not windows:
                raise FreeLabError("not_found", "FontLab's main window was not found, so FontLab was not closed.")
            self.closing = True
            try:
                windows[0].close()
            finally:
                self.closing = False
        except BaseException as exc:  # a Qt timer callback must not raise
            self._record("quit", exc)

    # -- namespace for run_python ------------------------------------------
    def fresh_namespace(self):
        namespace = {"__name__": "__freelab__", "listener_version": VERSION}
        for module_name in ("fontlab", "fontgate", "FL", "PythonQt"):
            try:
                namespace[module_name] = __import__(module_name)
            except Exception:
                pass
        return namespace

    # -- lifecycle -----------------------------------------------------------
    def bind(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _no_inherit(server)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            try:
                server.bind((HOST, self.port))
            except socket.error:
                if self.port == 0:
                    raise
                server.bind((HOST, 0))  # the default port is taken: take any free one
            server.listen(8)
            server.setblocking(False)
            self.server = server
            self.port = server.getsockname()[1]
            self._write_state()
        except BaseException:
            self.stop()
            try:
                server.close()
            except Exception:
                pass
            raise
        return self.port

    def _write_state(self):
        folder = os.path.dirname(self.state_file)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, 0o700)  # the token is readable by its owner only (Windows ignores the mode)
        data = {
            "port": self.port,
            "token": self.token,
            "pid": os.getpid(),
            "listener": VERSION,
            "protocol": PROTOCOL,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        temp = "%s.%d.tmp" % (self.state_file, os.getpid())  # its own: another listener may be writing too
        if os.path.exists(temp):  # left by this process's earlier, interrupted write
            os.remove(temp)
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            json.dump(data, handle)
        for attempt in range(20):  # a reader may hold the file for a moment
            try:
                if os.path.exists(self.state_file):
                    os.remove(self.state_file)
                os.rename(temp, self.state_file)
                return
            except OSError:
                if attempt == 19:
                    raise
                time.sleep(0.05)

    def start_timer(self):
        from PythonQt.QtCore import QTimer  # only inside FontLab
        timer = QTimer()
        timer.setInterval(POLL_MS)
        connected = False
        try:
            connected = timer.connect("timeout()", self.tick) is not False
        except Exception:
            connected = False
        if not connected:
            timer.timeout.connect(self.tick)
        timer.start()
        self.timer = timer

    def stop(self):
        if self.timer is not None:
            try:
                self.timer.stop()
            except Exception:
                pass
            # Kept, never dropped: stop() can run inside this timer's own tick (a request whose code runs
            # the startup script again), and PythonQt would then delete the QTimer while it emits.
            retired = getattr(sys, "_freelab_retired_timers", None)
            if retired is None:
                retired = sys._freelab_retired_timers = []
            retired.append(self.timer)
            self.timer = None
        for client in self.clients:
            self._close(client)
        self.clients = []
        if self.server is not None:
            try:
                self.server.close()
            except Exception:
                pass
            self.server = None
        try:
            with open(self.state_file) as handle:
                ours = json.load(handle).get("token") == self.token
            if ours:  # removed after closing: Windows refuses to delete an open file
                os.remove(self.state_file)
        except Exception:
            pass

    # -- polling -------------------------------------------------------------
    def tick(self):
        """QTimer callback: never lets anything escape into FontLab, and never runs re-entrantly.

        Qt fires timers inside nested event loops (a modal dialog opened by
        run_python code, for example); those ticks return at once, so the
        request in progress is not dropped and no second request runs inside it.
        """
        if self._busy:
            return
        self._busy = True
        try:
            self.poll()
        except BaseException as exc:
            self._record("poll", exc)
        finally:
            self._busy = False

    def _record(self, key, exc):
        try:
            self.errors.append(_exc_text(exc))
            del self.errors[:-MAX_ERRORS]
            if key not in self._reported:
                self._reported.add(key)
                print(u"FreeLab listener error (shown once): " + _exc_text(exc))
        except BaseException:
            pass

    def poll(self):
        if self.server is None:
            return
        for _ in range(MAX_ACCEPTS_PER_TICK):
            if len(self.clients) >= MAX_CLIENTS:
                break
            try:
                sock, _addr = self.server.accept()
            except socket.error as exc:
                if not (exc.args and exc.args[0] in _WOULD_BLOCK):
                    self._record("accept", exc)  # e.g. a peer that reset before accept
                break
            _no_inherit(sock)
            sock.setblocking(False)
            self.clients.append(_Client(sock))
        for client in list(self.clients):
            try:
                if client.outgoing is not None:
                    self._flush(client)
                else:
                    self._service(client)
            except Exception as exc:
                self._record("client", exc)
                self._drop(client)

    def _service(self, client):
        while True:
            try:
                chunk = client.sock.recv(65536)
            except socket.error as exc:
                if exc.args and exc.args[0] in _WOULD_BLOCK:
                    break
                self._drop(client)
                return
            if not chunk:
                self._drop(client)
                return
            start = max(0, len(client.buffer) - 1)
            client.buffer.extend(chunk)
            if len(client.buffer) > MAX_REQUEST_BYTES:
                self._reply(client, {"ok": False, "error": {"code": "too_large", "message": "Request too large."}})
                return
            newline = client.buffer.find(b"\n", start)
            if newline >= 0:
                line = bytes(client.buffer[:newline])
                self._reply(client, self.handle_line(line))
                return
        if time.time() - client.started > CLIENT_TIMEOUT_S:
            self._drop(client)

    def _reply(self, client, reply):
        """The reply queued on its client and sent without blocking: what the socket takes now, the rest
        a slice per tick (_flush). A blocking sendall here, from FontLab's GUI timer, froze FontLab for
        up to 5 s behind a client that asked for a large reply and read none of it."""
        try:
            payload = _bytes(json.dumps(reply)) + b"\n"
        except Exception:
            payload = _LAST_RESORT
        if len(payload) > MAX_REPLY_BYTES:
            request_id = reply.get("id") if isinstance(reply, dict) else None
            payload = _bytes(json.dumps({"id": request_id, "ok": False, "error": {
                "code": "too_large", "message": "The reply is %d bytes, more than the %d the listener sends: ask for "
                                                "less (a page of glyph names, fewer outline nodes, a smaller "
                                                "result)." % (len(payload), MAX_REPLY_BYTES)}})) + b"\n"
        client.buffer = bytearray()
        client.outgoing, client.sent = payload, 0
        client.send_deadline = time.time() + SEND_TIMEOUT_S
        self._flush(client)

    def _flush(self, client):
        """As much of the client's reply as its socket takes now, up to SEND_BYTES_PER_TICK; the client
        dropped once it has it all, when its socket fails, or past its deadline (a client that stopped
        reading)."""
        payload, budget = client.outgoing, SEND_BYTES_PER_TICK
        while budget > 0 and client.sent < len(payload):
            end = min(len(payload), client.sent + min(budget, 256 * 1024))
            try:
                count = client.sock.send(payload[client.sent:end])
            except socket.error as exc:
                if exc.args and exc.args[0] in _WOULD_BLOCK:
                    break
                self._drop(client)
                return
            if not count:
                break
            client.sent += count
            budget -= count
        if client.sent >= len(payload) or time.time() > client.send_deadline:
            self._drop(client)

    def _close(self, client):
        try:
            client.sock.close()
        except Exception:
            pass

    def _drop(self, client):
        self._close(client)
        if client in self.clients:
            self.clients.remove(client)

    # -- requests ------------------------------------------------------------
    def handle_line(self, line):
        request_id = None
        try:
            try:
                request = json.loads(_bytes(line).decode("utf-8"))
            except Exception:
                raise FreeLabError("bad_json", "The request is not valid JSON.")
            if not isinstance(request, dict):
                raise FreeLabError("bad_request", "The request must be a JSON object.")
            token = request.get("token")
            if not isinstance(token, string_types) or not hmac.compare_digest(_bytes(token), _bytes(self.token)):
                raise FreeLabError("bad_token", "The token does not match this listener; reread the state file.")
            request_id = request.get("id")  # echoed only once the caller is authenticated
            protocol = request.get("protocol")
            if not isinstance(protocol, int) or isinstance(protocol, bool) or protocol != PROTOCOL:
                raise FreeLabError("bad_protocol", "Protocol %r is not %d." % (request.get("protocol"), PROTOCOL))
            deadline = request.get("deadline")
            if deadline is not None:
                if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
                    raise FreeLabError("bad_request", "deadline must be a number of seconds since the epoch.")
                if time.time() > deadline:
                    raise FreeLabError("expired", "The client gave up before FontLab could run this request; it was not run.")
            name = request.get("op")
            handler = OPS.get(name)
            if handler is None:
                raise FreeLabError("unknown_op", u"Unknown operation '%s'." % (_text(name),))
            args = request.get("args")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise FreeLabError("bad_args", "args must be a JSON object.")
            self.requests += 1
            self.deadline = deadline
            result = handler(self, args)
            return {"id": request_id, "ok": True, "result": to_jsonable(result)}
        except FreeLabError as exc:
            error = {"code": exc.code, "message": _text(exc.message)}
            if exc.data is not None:
                error["data"] = to_jsonable(exc.data)
            return {"id": request_id, "ok": False, "error": error}
        except Exception:
            return {"id": request_id, "ok": False, "error": {"code": "exception", "message": _format_exc()}}


def main(port=None):
    previous = getattr(sys, "_freelab_listener", None)
    if previous is not None:
        previous.stop()
        sys._freelab_listener = None
    listener = Listener(port=port)
    listener.bind()
    try:
        listener.start_timer()
    except BaseException:
        listener.stop()
        raise
    sys._freelab_listener = listener  # survives FontLab clearing the script context
    # FontLab may drop modules from sys.modules; a Python 2 module no longer referenced has its
    # globals set to None, which would break every later tick. Keep this one referenced.
    sys._freelab_listener_module = sys.modules.get(MODULE_NAME)
    print("FreeLab listener %s on %s:%d (Python %s)" % (VERSION, HOST, listener.port, sys.version.split()[0]))
    return listener


def stop():
    previous = getattr(sys, "_freelab_listener", None)
    if previous is not None:
        previous.stop()
        sys._freelab_listener = None
        print("FreeLab listener stopped.")


def load_as_module(path):
    """Load this file as a real module, so the running listener's globals are not FontLab's script namespace."""
    if PY2:
        # open() takes a unicode path through the wide Windows API; imp.load_source does not.
        import imp
        module = imp.new_module(MODULE_NAME)
        module.__file__ = path
        sys.modules[MODULE_NAME] = module
        with open(path, "rb") as handle:
            source = handle.read()
        exec(compile(source, MODULE_NAME + ".py", "exec", 0, True), module.__dict__)
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[MODULE_NAME] = module
        spec.loader.exec_module(module)
    sys.modules[MODULE_NAME] = module
    return module


def _autostart():
    if __name__ == MODULE_NAME:
        return None  # imported as a module: whoever imported it decides when to start
    path = globals().get("__file__")
    if path and os.path.isfile(path):
        return load_as_module(path).main()
    return main()  # no file to load from: run from this namespace


if not os.environ.get("FREELAB_NO_AUTOSTART"):
    _autostart()
