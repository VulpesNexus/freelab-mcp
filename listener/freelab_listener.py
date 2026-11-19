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
import hashlib
import hmac
import json
import math
import os
import re
import socket
import struct
import sys
import time
import traceback
import zlib

VERSION = "1.3.0"
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
BAD_TOKEN_LIMIT = 20  # this many requests with a wrong token within BAD_TOKEN_WINDOW_S ...
BAD_TOKEN_WINDOW_S = 10.0
THROTTLE_S = 5.0  # ... and every connection is closed unanswered for this long
LOOPBACK = ("127.0.0.1", "::1")

# Risky actions: refused unless the request carries risky: true, which the server sends only while the user
# has turned risky mode on in their own words (docs/RISKY.md). This is the one list: the server reads it from
# this file (a literal, never run), and the listener enforces it, so a client without the flag cannot do
# them. (key, tools, why it is risky, enforced here)
RISKY_ACTIONS = [
    ("close_font.discard_changes", "close_font",
     "Throws away the unsaved changes of a font that open_font did not open.", True),
    ("answer_dialog", "answer_dialog",
     "Clicks a button of a question FontLab is asking the user (Save, Don't Save, Coordinates Rounding, ...).", True),
    ("open_font.answer", "open_font",
     "Answers the question FontLab asks while it opens the font, such as Coordinates Rounding.", True),
    ("quit_fontlab.discard_changes", "quit_fontlab, restart_fontlab",
     "Answers Don't Save to every Save question FontLab asks while it closes: unsaved work is lost.", True),
    ("rename_glyph.update_references", "rename_glyph",
     "Rewrites kerning pairs, kerning classes, and feature code along with the name, in one History step.", True),
    ("delete_glyph.remove_references", "delete_glyph",
     "Removes the glyph's kerning pairs and kerning class memberships along with the glyph.", True),
    ("save_font", "save_font", "Writes the font's file on disk (File > Save, or a UFO or VFB file).", True),
    ("export_font", "export_font", "Writes font files on disk through FontLab's export.", True),
    ("question_control", "click, set_value, click_link",
     "Clicks or changes a control of a question FontLab is asking (a Save question, Coordinates Rounding, ...), "
     "as answer_dialog does.", True),
    ("risky_command", "click, set_value, click_link, run_menu_command",
     "Runs a command of FontLab's File menu, a button or menu command named Save, Export, Close, Quit, Exit, Revert, "
     "or Discard (outside a script's own window), or a control a script marks risky: it may write files or throw "
     "work away.", True),
    ("run_python", "run_python",
     "Always available, not gated: runs any code inside FontLab, with FontLab's access to files and the network.",
     False),
]
_RISKY_REASONS = dict((key, reason) for key, _tools, reason, _enforced in RISKY_ACTIONS)

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


def _bool_arg(args, key, default=False):
    value = args.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise FreeLabError("bad_args", "%s must be true or false." % key)
    return value


def _require_risky(args, key):
    """Refuses a risky action (RISKY_ACTIONS) unless the request says risky mode is on."""
    if args.get("risky") is not True:
        raise FreeLabError("risky_mode_off", u"%s is a risky action, refused while risky mode is off: %s Risky mode is "
                           u"turned on with set_risky_mode, and only when the user has asked for it in their own words. "
                           u"Nothing was changed." % (key, _RISKY_REASONS[key]), {"action": key})


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
        "state_file_private": listener.state_private,
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


@op("risky_actions")
def op_risky_actions(listener, args):
    """The risky actions (RISKY_ACTIONS), for a server that cannot read this file."""
    return {"actions": [{"action": key, "tools": tools, "reason": reason, "enforced": enforced}
                        for key, tools, reason, enforced in RISKY_ACTIONS]}


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
    discard = _bool_arg(args, "discard_changes")
    if discard:
        _require_risky(args, "quit_fontlab.discard_changes")
    workspace_changed, changed, edited = unsaved_work()
    if (workspace_changed or changed) and not discard:
        parts = (["%d font%s" % (changed, "" if changed == 1 else "s")] if changed else []) + (["the workspace"] if workspace_changed else [])
        raise FreeLabError("unsaved_changes", "FontLab has unsaved changes (%s): save them, or close them in FontLab, "
                           "first. FreeLab never quits FontLab over unsaved work." % " and ".join(parts))
    if listener.deadline is not None and time.time() > listener.deadline:
        raise FreeLabError("expired", "The client gave up while FontLab checked for unsaved work; FontLab was not closed.")
    if discard:  # (FontLab asks about each edited font in turn, from inside the main window's close)
        _keep_answering(listener, "quit", u"Don't Save", time.time() + QUIT_ANSWER_S,
                        lambda dialog: dialog.metaObject().className() == SAVE_PROMPT, once=False,
                        keep_going=listener.quit_in_progress)  # (a quit canceled on screen ends it)
    listener.request_quit()
    return {"quitting": True, "edited": edited, "discard_changes": discard}


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
    return {"dialog": _describe_dialog(dialog), "main_window": main_window, "windows": others}


def _describe_dialog(dialog):
    texts, buttons = _dialog_parts(dialog)
    return {"class": dialog.metaObject().className(), "title": _prop(dialog, "windowTitle"), "text": texts,
            "buttons": [text for _button, text in buttons]}


def _dialog_parts(dialog):
    """(the texts of a dialog's visible labels, [(button, its text as shown)] of its visible push buttons)."""
    texts, buttons = [], []

    def walk(widget):
        for child in widget.children():
            if not child.isWidgetType() or not child.isVisible():
                continue
            if child.inherits("QAbstractButton"):
                if not (child.inherits("QCheckBox") or child.inherits("QRadioButton")):
                    text = _MNEMONIC.sub(r"\1", _prop(child, "text") or u"").strip()  # "&Save" -> "Save", "&&" -> "&"
                    if text:
                        buttons.append((child, text))
            elif child.inherits("QLabel"):
                text = _label_text(child)
                if text:
                    texts.append(text)
            walk(child)
    walk(dialog)
    return texts, buttons


def _button_words(text):
    """A button's text as compared: no mnemonic, a curly apostrophe as a straight one, case ignored."""
    return _MNEMONIC.sub(r"\1", _text(text or u"")).replace(u"’", u"'").strip().lower()


def _modal_dialog():
    from PythonQt.QtGui import QApplication  # only inside FontLab
    return QApplication.instance().activeModalWidget()


def _click_modal_button(button, condition=None):
    """Clicks the button of that text (or of the first of several texts it has) on the dialog FontLab waits
    on, when there is one (and condition(dialog) holds); whether it clicked."""
    dialog = _modal_dialog()
    if dialog is None or (condition is not None and not condition(dialog)):
        return False
    buttons = _dialog_parts(dialog)[1]
    for wanted in [_button_words(b) for b in ((button,) if isinstance(button, string_types) else button)]:
        for widget, text in buttons:
            if _button_words(text) == wanted:
                widget.click()
                return True
    return False


ANSWER_POLL_MS = 400  # how often a pending answer looks for its question
QUIT_ANSWER_S = 120.0  # quit_fontlab with discard_changes answers Save questions this long
OPEN_ANSWER_S = 120.0  # open_font with answer looks for the question this long


def _keep_answering(listener, key, button, until, condition=None, once=True, keep_going=None):
    """Clicks button on the dialog FontLab waits on, from a timer every ANSWER_POLL_MS until the time until
    (seconds since the epoch) or until keep_going() is false (the action it answers for is over, so a later
    question of the user's own is never answered): once, or on each such question in turn. Timers fire
    inside a modal dialog's loop too (live), so this answers a question that a call made from another timer
    is waiting on."""
    def look():
        if time.time() > until or listener.stopped:
            return
        try:
            if keep_going is not None and not keep_going():
                return
        except Exception:
            return
        if _click_modal_button(button, condition) and once:
            return
        listener.later(ANSWER_POLL_MS, look, key)
    listener.later(ANSWER_POLL_MS, look, key)


ANSWER_DELAY_MS = 50


@op("answer_dialog")
def op_answer_dialog(listener, args):
    """[risky] Clicks a button of the dialog FontLab waits on, a moment after the reply (the click may start
    what FontLab waited to do), only while it is still that dialog."""
    _require_risky(args, "answer_dialog")
    button = args.get("button")
    if not isinstance(button, string_types) or not button.strip():
        raise FreeLabError("bad_args", "button must be the text of one of the dialog's buttons.")
    expected = args.get("expected_title") or u""
    if not isinstance(expected, string_types):
        raise FreeLabError("bad_args", "expected_title must be the dialog's title.")
    _qt()
    dialog = _modal_dialog()
    if dialog is None:
        raise FreeLabError("not_found", "FontLab is not waiting on a dialog: nothing was clicked.")
    about = _describe_dialog(dialog)
    title = _text(about["title"] or u"")
    if expected.strip() and title.strip().lower() != _text(expected).strip().lower():
        raise FreeLabError("dialog_changed", u"FontLab's dialog is titled '%s', not '%s': nothing was clicked." % (
            title, expected), {"dialog": about})
    if not any(_button_words(text) == _button_words(button) for text in about["buttons"]):
        raise FreeLabError("no_button", u"FontLab's dialog '%s' has no button '%s'; its buttons: %s. Nothing was clicked." % (
            title, button, u", ".join(about["buttons"])), {"dialog": about})
    _check_deadline(listener)
    answer_id = listener.new_answer()

    def same(widget):
        # The very question described: its class, title, texts, and buttons (a Save question's text quotes its
        # font). Two Save questions share class and title, so those alone would let another font's be answered
        # (Codex, 2026-11-19).
        return _describe_dialog(widget) == about

    def click():
        if listener.stopped:
            outcome = u"listener_stopped"
        else:
            now = _modal_dialog()
            if now is None:
                outcome = u"dialog_gone"
            elif not same(now):
                outcome = u"dialog_changed"
            else:
                outcome = u"clicked" if _click_modal_button(button, same) else u"no_button"
        listener.answer_outcomes[answer_id] = outcome
    listener.later(ANSWER_DELAY_MS, click, "answer_dialog")
    return {"answering": _text(button).strip(), "dialog": about, "answer_id": answer_id}


@op("answer_result")
def op_answer_result(listener, args):
    """What became of an answer_dialog click: null while it is still to come, else clicked, dialog_gone,
    dialog_changed (another question was shown by then: nothing was clicked), no_button, or listener_stopped."""
    answer_id = _int_arg(args, "answer_id", None, 1, 10 ** 9)
    if answer_id not in listener.answer_outcomes:
        raise FreeLabError("not_found", "No answer_dialog of that id in this listener.")
    return {"answer_id": answer_id, "outcome": listener.answer_outcomes[answer_id]}


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
_LINE_TAGS = re.compile(r"<br\s*/?>|</?p(?:\s[^>]*)?>|</?div(?:\s[^>]*)?>|</li\s*>", re.I)
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
        # (a line break, or a paragraph's end, is a line of its own: lines ran together, live, 2026-11-19)
        text = _TAGS.sub(u"", _LINE_TAGS.sub(u"\n", text))
        text = u"\n".join(line.strip() for line in text.split(u"\n") if line.strip())
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


def _mapper(transform):
    """A function placing a shape-local point as the shape's QTransform does (QTransform.map: x' = m11 x +
    m21 y + dx, y' = m12 x + m22 y + dy), from the transform's six values read once."""
    m11, m12, m21, m22 = float(transform.m11()), float(transform.m12()), float(transform.m21()), float(transform.m22())
    dx, dy = float(transform.dx()), float(transform.dy())
    return lambda x, y: (m11 * x + m21 * y + dx, m12 * x + m22 * y + dy)


@op("outline")
def op_outline(listener, args):
    package, count = target_package(args)
    glyph = _find_glyph(package, args)
    reads = _Reads()
    layer, layer_source = _choose_layer(glyph, package, args.get("layer"), reads)
    max_nodes = _int_arg(args, "max_nodes", DEFAULT_MAX_NODES, 1, MAX_NODES_LIMIT)
    apply = _bool_arg(args, "apply_transforms")
    shapes_out = []
    total = 0
    truncated = False
    for s_index, shape in enumerate(_real_list(layer.shapes, "layer.shapes")):
        if total >= max_nodes:
            truncated = True
            break
        place = _mapper(shape.transform) if apply else (lambda x, y: (x, y))
        contours_out = []
        for contour in _real_list(shape.contours, "shape.contours"):
            if total >= max_nodes:
                truncated = True
                break
            nodes = _real_list(contour.nodes(), "contour.nodes()")
            room = max_nodes - total
            nodes_out = []
            for node in nodes[:room]:
                x, y = place(node.x, node.y)
                nodes_out.append({"x": x, "y": y, "type": _text(node.type), "smooth": bool(node.smooth)})
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
        "coordinates": "the glyph's: each shape's transform [m11, m12, m21, m22, dx, dy] is applied" if apply else
                       "shape-local; each shape's transform [m11, m12, m21, m22, dx, dy] is not applied",
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
MAX_PLAIN_PAIRS = 500000  # get_kerning resolved asks FontLab to flatten a master's kerning up to this many glyph pairs
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


def _member_classes(groups, name, side):
    """The kerning classes that hold glyph name and kern on that side, sorted."""
    return sorted(c for c, entry in groups.items()
                  if _text(entry[1]) in KERN_SIDES[side] and name in [_text(n) for n in entry[0]])


def _resolved_pair(package, kerning, groups, left, right):
    """The value a glyph pair gets through classes: FontLab's own (getPlainPairsDict, which flattens every
    class pair to glyph pairs) and one computed here from the master's entries, the most specific winning:
    glyph glyph, then glyph @class, @class glyph, @class @class (the UFO order). Names are glyphs here,
    also one named like a class (its own entries are read by mode, which get() cannot)."""
    for value, side in ((left, "left"), (right, "right")):
        if not isinstance(value, string_types) or not value.strip() or value.startswith(u"@"):
            raise FreeLabError("bad_args", "resolved takes glyph names for %s, not classes." % side)
        if glyph_named(package, value) is None:
            raise FreeLabError("no_glyph", u"%s has no glyph '%s'." % (_font_label(package), value))
    left, right = _text(left), _text(right)
    entries = {}
    for pair, value in _real_list(kerning.items(), "fgKerning.items()"):
        entries[(_pair_side(pair.left), _pair_side(pair.right))] = float(value)
    lefts, rights = _member_classes(groups, left, "left"), _member_classes(groups, right, "right")
    order = [(left, right)] + [(left, u"@" + c) for c in rights] + [(u"@" + c, right) for c in lefts] + \
        [(u"@" + l, u"@" + r) for l in lefts for r in rights]
    computed, through = None, None
    for key in order:
        if key in entries:
            computed, through = entries[key], u"%s %s" % key
            break
    reply = {"left": left, "right": right, "resolved": True, "computed": computed, "through": through,
             "left_classes": lefts, "right_classes": rights}
    # getPlainPairsDict flattens every class pair of the master into glyph pairs, on FontLab's main thread:
    # its size is estimated from the classes first, and a larger one is not asked for (Codex, 2026-11-19).
    sizes = dict((_text(name), len(_real_list(entry[0], "a kerning class"))) for name, entry in groups.items())
    expanded = 0
    for left_side, right_side in entries:
        expanded += (sizes.get(left_side[1:], 1) if left_side.startswith(u"@") else 1) * \
            (sizes.get(right_side[1:], 1) if right_side.startswith(u"@") else 1)
    if expanded > MAX_PLAIN_PAIRS:
        reply.update({"value": computed, "resolved_by": "computed", "fontlab": None,
                      "fontlab_skipped": u"FontLab's own value was not asked for: it flattens every class pair of the "
                                         u"master, about %d glyph pairs here (more than %d)." % (expanded, MAX_PLAIN_PAIRS)})
        return reply
    try:
        plain = kerning.getPlainPairsDict()
        if not isinstance(plain, dict):
            raise TypeError("getPlainPairsDict gave a %s, not a dict" % type(plain).__name__)
        fontlab_value = None
        for (l, r), value in plain.items():
            if _text(l) == left and _text(r) == right:
                fontlab_value = float(value)
                break
    except Exception as exc:
        reply.update({"value": computed, "resolved_by": "computed", "fontlab": None,
                      "fontlab_error": _exc_text(exc)})
        return reply
    reply.update({"value": fontlab_value, "resolved_by": "fontlab", "fontlab": fontlab_value,
                  "agree": fontlab_value == computed})
    if len(lefts) > 1 or len(rights) > 1:
        reply["note"] = u"The glyph is in more than one class on a side; the computed value took them in name order."
    return reply


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
    if _bool_arg(args, "resolved"):
        if left is None:
            raise FreeLabError("bad_args", "resolved needs a pair of glyphs: give left and right.")
        result.update(_resolved_pair(package, kerning, groups, left, right))
    elif left is not None:
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
    result = {"font": _font_ref(package), "tags": tags, "compiled": "not checked: compile_features compiles it"}
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
# so FontLab could close it without asking: every write marks it (setFlag(2), ofChanged). No write saves,
# exports, or closes a font (save_font and export_font are risky actions of their own).

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
        self.restore = None  # the write's restore(), once run() has it
        self.previous = None  # the font that was in front, put back once the write is over
        self.moved = False
        self.behind = False  # the font could not be brought to the front: its steps have no title

    def _to_front(self):
        """FontLab gives a History step a title only when its font is in front (live, 2026-11-19: steps
        recorded for a font behind another showed blank in the History panel and in Edit > Undo, so undo
        could not match them). The font is brought forward for the write, as a click on its tab does, and
        the font that was in front is put back afterward (_put_back)."""
        try:
            current = self.fl.flWorkspace.instance().currentPackage
            previous = None if _is_null(current, "the current font") else current
        except Exception:
            previous = None
        try:
            self.moved = bring_to_front(self.package)
            self.previous = previous if self.moved else None
        except Exception:
            self.behind = True  # (no window tab of it: the write goes on, its step untitled)

    def _put_back(self):
        if self.moved and self.previous is not None:
            try:
                bring_to_front(self.previous)
            except Exception:
                pass
        self.moved, self.previous = False, None

    def begin(self, force=False):
        """Records what Undo needs before anything changes; raises (nothing changed) when FontLab cannot.
        force records the font and the write's glyphs whatever their state (a write of many parts, or one
        that deletes a glyph)."""
        _check_deadline(self.listener)
        self._to_front()
        objects = []
        if force or self.font_id not in _RECORDED_FONTS or _without_state(self.package):
            objects.append(self.package)
        objects.extend(glyph for glyph in self.glyphs if force or _without_state(glyph))
        if objects:
            try:
                self.fl.flItems.notifyChangesApplied(u"%s (before)" % self.title, objects, True)
            except Exception as exc:
                self._put_back()
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
        self.restore = restore
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
            self._put_back()
            return FreeLabError("not_kept", u"%s. It was put back, and a read-back shows the font as it was." % message)
        status = self.finish(glyphs, added, strict=False)
        return FreeLabError("not_kept", u"%s. It could not be put back: what FontLab made of the change is recorded as a "
                            u"step of its History (Undo takes it back, when recorded is true) and the font is marked "
                            u"modified; check it in FontLab." % message, {"history": status})

    def finish(self, glyphs=(), added=False, strict=True):
        """The step recorded, the font marked modified; {recorded, marked_modified, history_state, title, ...}.
        strict: a step FontLab's History refuses fails the write (Codex, 2026-11-19): the change is put back
        (history_failed, nothing changed), or, when it cannot be, the font is marked and the error says so."""
        try:
            return self._finish(glyphs, added, strict)
        finally:
            self._put_back()

    def _finish(self, glyphs, added, strict):
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
            reason = _exc_text(exc)
            if strict:
                restored = False
                if self.restore is not None:
                    try:
                        restored = bool(self.restore())
                    except Exception:
                        restored = False
                if restored:
                    raise FreeLabError("history_failed", u"FontLab's History did not record the change (%s), so Undo "
                                       u"could not have taken it back: it was put back, and a read-back shows the font "
                                       u"as it was. Nothing was changed." % reason)
            problems.append(u"FontLab's History did not record the change (%s): Undo cannot take it back; to drop "
                            u"it, close the font without saving." % reason)
        if strict and not status["recorded"]:
            try:
                self.package.setFlag(2, True)
            except Exception:
                status["marked_modified"] = False
            raise FreeLabError("history_failed", u"%s The change could not be put back either: it stays in the font, "
                               u"which is marked modified%s." % (u" ".join(problems), u"" if status["marked_modified"]
                                                                  else u" (FontLab refused that too)"),
                               {"history": status})
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
        if self.behind:
            problems.append(u"The font has no window tab to bring it to the front, and FontLab gives a step of a font "
                            u"behind another no title: undo cannot check this step by its title.")
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


# Feature code, read as the AFDKO syntax FontLab compiles, far enough to tell a glyph name from everything else
# that looks like one (Codex, 2026-11-19: a blanket word match took a feature's tag for a glyph named like it,
# and missed the ends of a range [A-B]). A glyph named like a keyword is written escaped (\sub).
_FEA_TOKEN = re.compile(u'#[^\\n]*|"[^"]*"?|\\\\?[A-Za-z_.][A-Za-z0-9_.*+\\-]*|@[A-Za-z0-9_.\\-]+|[0-9]+(?:\\.[0-9]+)?|\\S')
FEA_KEYWORDS = frozenset(u"""anchor anchorDef anon anonymous base by contourpoint cursive device enum enumerate
    exclude_dflt excludeDFLT feature from ignore IgnoreBaseGlyphs IgnoreLigatures IgnoreMarks include include_dflt
    includeDFLT language languagesystem ligature ligComponent lookup lookupflag mark MarkAttachmentType markClass
    NULL parameters pos position required reversesub RightToLeft rsub script sub substitute subtable table
    useExtension UseMarkFilteringSet valueRecordDef""".split())
# statements whose names are tags, labels, or flags, never glyphs; and tokens a label follows
FEA_LABEL_STATEMENTS = frozenset(u"""anchorDef anon anonymous cvParameters feature featureNames include language
    languagesystem lookup lookupflag name nameid parameters script sizemenuname subtable table
    valueRecordDef""".split())
FEA_LABEL_AFTER = frozenset([u"}", u"<", u"anchor", u"feature", u"lookup", u"script", u"language", u"table"])


def _fea_names(code, glyphs):
    """(glyph names, ranges) of feature code: [(start, end, name, escaped)] of each glyph name written in
    it, and [(start, end, first, last)] of each glyph range in a class ([A-B], [A - B]). glyphs is the font's
    set of glyph names (a bracketed name with a hyphen is that glyph when the font has it, else a range)."""
    names, ranges = [], []
    statement, first, prev, depth, last_name = None, True, None, 0, None
    for match in _FEA_TOKEN.finditer(code):
        token = match.group(0)
        if token.startswith(u"#"):
            continue
        if token in (u";", u"{"):
            statement, first, prev = None, True, token
            continue
        if token == u"}":
            statement, first, prev = u"}", False, token
            continue
        if token == u"[":
            depth, last_name = depth + 1, None
        elif token == u"]":
            depth, last_name = max(depth - 1, 0), None
        is_name = token[:1].isalpha() or token[:1] in (u"_", u".", u"\\")
        if is_name and not token.startswith(u'"'):
            escaped = token.startswith(u"\\")
            name = token[1:] if escaped else token
            if first:
                statement = name  # (a statement starts with a keyword, never a glyph)
            elif escaped or (name not in FEA_KEYWORDS and statement not in FEA_LABEL_STATEMENTS and
                             prev not in FEA_LABEL_AFTER):
                if depth and not escaped and u"-" in name and name not in glyphs:
                    parts = name.split(u"-")
                    ranges.append((match.start(), match.end(), parts[0], u"-".join(parts[1:])))
                    for cut in range(1, len(parts) - 1):  # (a hyphen in a name too: every reading)
                        ranges.append((match.start(), match.end(), u"-".join(parts[:cut]), u"-".join(parts[cut:])))
                elif depth and prev == u"-" and last_name is not None:
                    ranges.append((last_name[0], match.end(), last_name[1], name))
                else:
                    names.append((match.start(), match.end(), name, escaped))
                last_name = (match.start(), name)
        first = False
        prev = token
    return names, ranges


def _may_be_in_range(name, first, last):
    """Whether a range first-last may hold the glyph: an end of it, or of their length and alike wherever
    they are alike (a superset of what the feature syntax expands)."""
    if name in (first, last):
        return True
    if not (len(name) == len(first) == len(last)) or not first or not last:
        return False
    return all(a != b or c == a for a, b, c in zip(first, last, name))


def _fea_uses(code, name, glyphs):
    """(how many times the glyph is named in the code, whether a range there may hold it)."""
    names, ranges = _fea_names(code, glyphs)
    return (sum(1 for _s, _e, n, _x in names if n == name),
            any(_may_be_in_range(name, a, b) for _s, _e, a, b in ranges))


def _fea_rename(code, old, new, glyphs):
    """The code with each glyph name old written new (escaped when it was, or when new is a keyword)."""
    names, _ranges = _fea_names(code, glyphs)
    out, at = [], 0
    for start, end, name, escaped in names:
        if name != old:
            continue
        out.append(code[at:start])
        out.append((u"\\" if escaped or new in FEA_KEYWORDS else u"") + new)
        at = end
    out.append(code[at:])
    return u"".join(out)


def _entries(kerning):
    """{(left, right): value} of a master's own entries, classes written with @."""
    return dict(((_pair_side(pair.left), _pair_side(pair.right)), float(value))
                for pair, value in _real_list(kerning.items(), "fgKerning.items()"))


class _References(object):
    """What rename_glyph's update_references and delete_glyph's remove_references rewrite, planned before
    anything changes: the glyph's own kerning pairs in every master (removed, and for a rename added again
    under the new name with their values), its kerning class memberships, and for a rename the feature
    code and prefix that name it (as a whole word, the rule _references uses). Refuses what FontLab's
    scripting cannot rewrite: a class of the Classes panel (no call to change one is proven), a pair side
    named like a kerning class (FontLab would read it as the class), and for a delete, feature code."""

    def __init__(self, package, old, new):
        self.package, self.old, self.new = package, old, new
        self.pairs = []  # (master, kerning, (left, right) to remove, (left, right) to add or None, value)
        self.classes = []  # (master, kerning, class, members before, members after, side)
        self.features = []  # (tag, code before, code after)
        self.prefix = None  # (prefix before, prefix after)
        label = _font_label(package)
        panel = sorted(set(_text(entry[1]) for entry in _real_list(package.getAllGroups(), "getAllGroups()")
                           if not entry[0] and old in [_text(n) for n in entry[4]]))
        if panel:
            raise FreeLabError("glyph_in_use", u"Glyph '%s' is in the class%s %s of FontLab's Classes panel, which FontLab's "
                               u"scripting offers no proven way to change: take it out there first. Nothing was "
                               u"changed." % (old, "" if len(panel) == 1 else "es", u", ".join(panel)))
        for master in [_text(m) for m in _real_list(package.masters, "flPackage.masters")]:
            kerning, groups = _kerning(package, master)
            for pair, value in _real_list(kerning.items(), "fgKerning.items()"):
                sides = [(_text(item.id), _text(item.mode)) for item in (pair.left, pair.right)]
                if not any(mode == u"glyphMode" and ident == old for ident, mode in sides):
                    continue
                for ident, mode in sides:
                    if mode == u"glyphMode" and ident in groups:
                        raise FreeLabError("class_name_clash", u"The kerning pair %s %s in master %s has a glyph '%s' "
                                           u"named like a kerning class, which FontLab's scripting cannot address: "
                                           u"nothing was changed." % (_pair_side(pair.left), _pair_side(pair.right),
                                                                     master, ident))
                add = None
                if new is not None:
                    if new in groups:
                        raise FreeLabError("class_name_clash", u"'%s' is the name of a kerning class in master %s: "
                                           u"FontLab would write the pairs of the renamed glyph as the class's. "
                                           u"Nothing was changed." % (new, master))
                    add = tuple(new if mode == u"glyphMode" and ident == old else ident for ident, mode in sides)
                self.pairs.append((master, kerning, (sides[0][0], sides[1][0]), add, float(value),
                                   tuple(m == u"groupMode" for _i, m in sides)))
            for name, entry in sorted(groups.items()):
                members = [_text(n) for n in entry[0]]
                if old in members:
                    after = [(new if n == old else n) for n in members if new is not None or n != old]
                    self.classes.append((master, kerning, name, members, after, _text(entry[1])))
        features = package.fgPackage.features
        self.layout = features
        glyphs = set(_text(n) for n in _real_list(package.fgPackage.keys(), "fgFont.keys()"))
        tags = [_text(t) for t in _real_list(features.keys(), "features.keys()")]
        prefix = _text(features.get_prefix())
        parts = [(u"prefix", prefix)] + [(tag, _text(features.get_feature(tag))) for tag in tags]
        uses, in_ranges = [], []
        for tag, code in parts:
            count, ranged = _fea_uses(code, old, glyphs)
            if count:
                uses.append((tag, code))
            if ranged:
                in_ranges.append(tag)
        if in_ranges:
            raise FreeLabError("glyph_in_use", u"Glyph '%s' may be in a glyph range of the feature code of %s (%s), "
                               u"which cannot be rewritten safely: edit it first (set_feature_code). Nothing was "
                               u"changed." % (old, label, u", ".join(in_ranges)))
        if new is None:
            if uses:
                raise FreeLabError("glyph_in_use", u"Glyph '%s' is named in the feature code of %s (%s): taking a name out "
                                   u"of feature code could break it, so edit it first (set_feature_code). Nothing "
                                   u"was changed." % (old, label, u", ".join(tag for tag, _c in uses)))
            return
        for tag, code in uses:
            after = _fea_rename(code, old, new, glyphs)
            if tag == u"prefix":
                self.prefix = (prefix, after)
                continue
            if _feature_block(tag, after) not in (None, _feature_block(tag, code)):
                raise FreeLabError("glyph_in_use", u"Renaming '%s' in feature %s would no longer leave one block of "
                                   u"feature %s: edit it first (set_feature_code). Nothing was changed." % (old, tag, tag))
            self.features.append((tag, code, after))

    @staticmethod
    def _shown(names, groups_mode):
        return tuple((u"@" if is_group else u"") + n for n, is_group in zip(names, groups_mode))

    def remove_pairs(self):
        for _master, kerning, old_key, _new_key, _value, _modes in self.pairs:
            kerning.remove(old_key)

    def add_pairs(self):
        for _master, kerning, _old_key, new_key, value, _modes in self.pairs:
            if new_key is not None:
                kerning[new_key] = value

    def rewrite(self):
        for _master, kerning, name, _before, after, side in self.classes:
            kerning.groups[name] = (after, side)
        for tag, _before, after in self.features:
            self.layout.set_feature(tag, after)
        if self.prefix is not None:
            self.layout.set_prefix(self.prefix[1])

    def problems(self, restored=False):
        """What does not read back as planned (or, restored, as it was before)."""
        found = []
        for master, kerning, old_key, new_key, value, modes in self.pairs:
            entries = _entries(kerning)
            gone, wanted = (new_key, old_key) if restored else (old_key, new_key)
            gone_shown = None if gone is None else self._shown(gone, modes)
            if gone_shown is not None and gone_shown in entries:
                found.append(u"pair %s %s still in master %s" % (gone_shown + (master,)))
            if wanted is not None:
                read = entries.get(self._shown(wanted, modes))
                if read is None or abs(read - value) > 0.5 + 1e-6:
                    found.append(u"pair %s %s in master %s reads %s, not %g" % (self._shown(wanted, modes) + (master, read, value)))
        for master, kerning, name, before, after, _side in self.classes:
            entry = dict((_text(k), v) for k, v in kerning.groups.asDict().items()).get(name)
            members = None if entry is None else [_text(n) for n in entry[0]]
            if members != (before if restored else after):
                found.append(u"kerning class %s in master %s holds %s" % (name, master, members))
        for tag, before, after in self.features:
            if not _same_code(_text(self.layout.get_feature(tag)), before if restored else after):
                found.append(u"feature %s" % tag)
        if self.prefix is not None and not _same_code(_text(self.layout.get_prefix()), self.prefix[0 if restored else 1]):
            found.append(u"the prefix")
        return found

    def put_back(self):
        """Everything but the glyph's name back as it was, each part tried whatever the others do."""
        for tag, before, _after in self.features:
            try:
                self.layout.set_feature(tag, before)
            except Exception:
                pass
        if self.prefix is not None:
            try:
                self.layout.set_prefix(self.prefix[0])
            except Exception:
                pass
        for _master, kerning, name, before, _after, side in self.classes:
            try:
                kerning.groups[name] = (before, side)
            except Exception:
                pass
        for _master, kerning, old_key, new_key, value, _modes in self.pairs:
            try:
                if new_key is not None and new_key != old_key:
                    kerning.remove(new_key)
            except Exception:
                pass

    def put_back_pairs(self):
        for _master, kerning, old_key, _new_key, value, _modes in self.pairs:
            try:
                kerning[old_key] = value
            except Exception:
                pass

    def summary(self):
        return {"kerning_pairs": len(self.pairs),
                "kerning_classes": [u"%s (%s)" % (name, master) for master, _k, name, _b, _a, _s in self.classes],
                "features": [tag for tag, _b, _a in self.features], "prefix": self.prefix is not None}


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
    glyphs = set(_text(n) for n in _real_list(package.fgPackage.keys(), "fgFont.keys()"))
    parts = [(u"prefix", _text(features.get_prefix()))] + [
        (_text(t), _text(features.get_feature(t))) for t in _real_list(features.keys(), "features.keys()")]
    tags, ranged = [], []
    for tag, code in parts:
        count, in_range = _fea_uses(code, name, glyphs)
        if count:
            tags.append(tag)
        if in_range:
            ranged.append(tag)
    if tags:
        found.append(u"feature code (%s)" % u", ".join(tags))
    if ranged:
        found.append(u"a glyph range that may hold it in feature code (%s)" % u", ".join(ranged))
    return found


@op("rename_glyph")
def op_rename_glyph(listener, args):
    package = write_target(args)
    glyph = _find_glyph(package, args, name_key="glyph")
    old = _text(glyph.name)
    update = _bool_arg(args, "update_references")
    if args.get("new_name") == old:
        return _unchanged(package, glyph=old)
    new = _new_glyph_name(package, args, "new_name")
    used = _references(package, old)
    refs = None
    if used:
        if not update:
            raise FreeLabError("glyph_in_use", u"Glyph '%s' is used by %s: FontLab's rename would leave those pointing to "
                               u"the old name, so nothing was renamed. update_references (a risky action) rewrites them "
                               u"in the same step." % (old, u"; ".join(used)))
        _require_risky(args, "rename_glyph.update_references")
        refs = _References(package, old, new)
    write = _Write(listener, package, u"rename %s to %s" % (old, new), [glyph])
    write.begin(force=refs is not None)  # (Undo of a rename lost the glyph's pairs when the font was not recorded)

    def renamed(name, gone):
        package.fgPackage.rebuildMaps()
        return glyph_named(package, name) is not None and glyph_named(package, gone) is None

    def change():
        if refs is not None:
            refs.remove_pairs()  # (while the old name is a glyph's)
        if not package.renameGlyph(glyph.fgGlyph, new, True):
            raise _NotKept(u"FontLab refused to rename '%s'" % old)
        if not renamed(new, old):
            raise _NotKept(u"FontLab's glyph list does not show '%s' renamed to '%s'" % (old, new))
        if refs is not None:
            refs.add_pairs()
            refs.rewrite()
            problems = refs.problems()
            if problems:
                raise _NotKept(u"FontLab did not keep every reference as rewritten (%s)" % u"; ".join(problems))

    def restore():
        if refs is not None:
            refs.put_back()  # (the new pairs out while the new name is still the glyph's)
        if _text(glyph.name) != old:
            package.renameGlyph(glyph.fgGlyph, old, True)
        back = renamed(old, new)
        if refs is not None:
            refs.put_back_pairs()
            back = back and not refs.problems(restored=True)
        return back

    write.run(change, restore)
    extra = {"references": refs.summary()} if refs is not None else {}
    return _write_reply(package, write.finish(), glyph=new, before=old,
                        note="Components that use the glyph follow the new name (FontLab updates them)." + (
                            " Its kerning pairs, kerning classes, and feature code were rewritten in the same step."
                            if refs is not None else ""), **extra)


def _component_users(package, name):
    """The glyphs that use glyph name as a component, in any layer."""
    users = []
    for other in _real_list(package.fgPackage.keys(), "fgFont.keys()"):
        glyph = package.findName(other)
        if _is_null(glyph, "a glyph") or _text(glyph.name) == name:
            continue
        for layer in _real_list(glyph.layers, "glyph.layers"):
            if any(_component_glyph(shape) == name for shape in _real_list(layer.shapes, "layer.shapes")):
                users.append(_text(glyph.name))
                break
    return users


@op("delete_glyph")
def op_delete_glyph(listener, args):
    """Takes a glyph out of the font (package.deleteGlyph), one History step that Undo takes back. Refused
    for a glyph other glyphs use as a component; one used by kerning or classes only with remove_references
    (risky), and one named in feature code always (edit the code first)."""
    package = write_target(args)
    glyph = _find_glyph(package, args, name_key="glyph")
    name = _text(glyph.name)
    remove = _bool_arg(args, "remove_references")
    users = _component_users(package, name)
    if users:
        raise FreeLabError("glyph_in_use", u"Glyph '%s' is a component of %s: delete or decompose %s first. Nothing was "
                           u"deleted." % (name, u", ".join(users[:20]) + (u", ..." if len(users) > 20 else u""),
                                          u"that glyph" if len(users) == 1 else u"those glyphs"))
    used = _references(package, name)
    refs = None
    if used:
        if not remove:
            raise FreeLabError("glyph_in_use", u"Glyph '%s' is used by %s, so nothing was deleted. remove_references (a "
                               u"risky action) takes it out of kerning pairs and kerning classes in the same step." % (
                                   name, u"; ".join(used)))
        _require_risky(args, "delete_glyph.remove_references")
        refs = _References(package, name, None)
    write = _Write(listener, package, u"delete %s" % name, [glyph])
    write.begin(force=True)  # (the font, with its glyph list, and the glyph's own name and code points)
    write.glyphs = []  # (a deleted glyph is not recorded again: the step records the font)

    def change():
        if refs is not None:
            refs.remove_pairs()
            refs.rewrite()
            problems = refs.problems()
            if problems:
                raise _NotKept(u"FontLab did not keep the references as removed (%s)" % u"; ".join(problems))
        deleted = package.deleteGlyph(glyph)
        package.fgPackage.rebuildMaps()
        if glyph_named(package, name) is not None:
            raise _NotKept(u"FontLab did not delete '%s' (deleteGlyph returned %r)" % (name, deleted))

    def restore():
        if glyph_named(package, name) is None:
            return False  # (gone: only Undo brings it back)
        if refs is not None:
            refs.put_back()
            refs.put_back_pairs()
            return not refs.problems(restored=True)
        return True

    write.run(change, restore, added=True)
    extra = {"references": refs.summary()} if refs is not None else {}
    return _write_reply(package, write.finish(added=True), glyph=name, deleted=True, **extra)


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


MAX_OUTLINE_CONTOURS = 2000
MAX_OUTLINE_NODES = 50000
NODE_TYPES = ("on", "off", "curve")  # as get_glyph_outline reads them


def _contours_arg(args):
    """[(closed, [(x, y, type, smooth)])] of args["contours"], checked."""
    contours = args.get("contours")
    if not isinstance(contours, list) or len(contours) > MAX_OUTLINE_CONTOURS:
        raise FreeLabError("bad_args", "contours must be a list of at most %d {closed, nodes} objects ([] clears the "
                                       "shape)." % MAX_OUTLINE_CONTOURS)
    out, total = [], 0
    for c_index, contour in enumerate(contours):
        if not isinstance(contour, dict) or set(contour) - set(["closed", "nodes"]):
            raise FreeLabError("bad_args", "contours[%d] must be an object with nodes, and closed (true when left "
                                           "out), and nothing else." % c_index)
        closed = contour.get("closed", True)
        nodes = contour.get("nodes")
        if not isinstance(closed, bool) or not isinstance(nodes, list) or len(nodes) < 2:
            raise FreeLabError("bad_args", "contours[%d] needs closed (true or false) and at least 2 nodes." % c_index)
        total += len(nodes)
        if total > MAX_OUTLINE_NODES:
            raise FreeLabError("bad_args", "At most %d nodes in all." % MAX_OUTLINE_NODES)
        made = []
        for n_index, node in enumerate(nodes):
            where = "contours[%d].nodes[%d]" % (c_index, n_index)
            if not isinstance(node, dict) or set(node) - set(["x", "y", "type", "smooth"]):
                raise FreeLabError("bad_args", "%s must be an object with x and y, and type and smooth when needed, and "
                                               "nothing else." % where)
            x = float(_number(node, "x", -MAX_COORD, MAX_COORD))
            y = float(_number(node, "y", -MAX_COORD, MAX_COORD))
            kind = node.get("type", "on")
            smooth = node.get("smooth", False)
            if kind not in NODE_TYPES or not isinstance(smooth, bool):
                raise FreeLabError("bad_args", "%s: type must be on, off, or curve (as get_glyph_outline reads them), and "
                                               "smooth true or false." % where)
            made.append((x, y, _text(kind), smooth))
        if not closed and (made[0][2] != "on" or made[-1][2] != "on"):  # (off and curve are off the outline)
            raise FreeLabError("bad_args", "contours[%d] is open: it must start and end on an on-curve node." % c_index)
        out.append((closed, made))
    return out


def _shape_contours(shape):
    """[(closed, [(x, y, type, smooth)])] of a shape as FontLab reads it."""
    return [(bool(c.closed), [(float(n.x), float(n.y), _text(n.type), bool(n.smooth))
                              for n in _real_list(c.nodes(), "contour.nodes()")])
            for c in _real_list(shape.contours, "shape.contours")]


def _contours_differ(read, wanted, tolerance=0.5):
    """Where read differs from wanted, as text, or None when they match."""
    if len(read) != len(wanted):
        return u"%d contour%s, not %d" % (len(read), "" if len(read) == 1 else "s", len(wanted))
    for c_index, ((closed_a, nodes_a), (closed_b, nodes_b)) in enumerate(zip(read, wanted)):
        if closed_a != closed_b:
            return u"contour %d is %s" % (c_index, u"closed" if closed_a else u"open")
        if len(nodes_a) != len(nodes_b):
            return u"contour %d has %d nodes, not %d" % (c_index, len(nodes_a), len(nodes_b))
        for n_index, (a, b) in enumerate(zip(nodes_a, nodes_b)):
            if abs(a[0] - b[0]) > tolerance or abs(a[1] - b[1]) > tolerance or a[2] != b[2] or a[3] != b[3]:
                return u"contour %d node %d reads %g %g %s%s, not %g %g %s%s" % (
                    c_index, n_index, a[0], a[1], a[2], u" smooth" if a[3] else u"", b[0], b[1], b[2],
                    u" smooth" if b[3] else u"")
    return None


def _same_contours(read, wanted, tolerance=0.5):
    return _contours_differ(read, wanted, tolerance) is None


def _own_shapes(layer):
    """[(index, shape)] of a layer's own shapes (not components), read from the layer now."""
    return [(index, shape) for index, shape in enumerate(_real_list(layer.shapes, "layer.shapes"))
            if _component_glyph(shape) is None and not bool(shape.isComponent)]


def _component_count(layer):
    return len(_real_list(layer.shapes, "layer.shapes")) - len(_own_shapes(layer))


@op("set_outline")
def op_set_outline(listener, args):
    """Replaces the contours of one shape of a glyph's layer (not a component), or gives a layer with no
    shape of its own a new one: contours made from QPointF lists (flContour([QPointF...]), every node on
    the curve), then each node's type and smoothness set, and the shape's contours replaced at once
    (clearContours, addContours), as FreeLab Script Suite does live. One History step; read back."""
    fl = _fontlab()
    package = write_target(args)
    glyph = _find_glyph(package, args, name_key="glyph")
    layer = _layer_arg(glyph, package, args)
    wanted = _contours_arg(args)
    shapes = _real_list(layer.shapes, "layer.shapes")
    own = _own_shapes(layer)
    components = _component_count(layer)
    index = args.get("shape")
    if index is not None:
        if isinstance(index, bool) or not isinstance(index, integer_types) or index not in [i for i, _s in own]:
            raise FreeLabError("bad_args", u"shape must be the index of one of the layer's own shapes (not a component), "
                               u"as get_glyph_outline numbers them: %s." % (u", ".join(str(i) for i, _s in own) or u"none"))
        target = dict(own)[index]
    elif len(own) > 1:
        raise FreeLabError("bad_args", u"Layer '%s' of '%s' has %d shapes of its own: give shape, the index of the one "
                           u"to replace (%s)." % (_text(layer.name), _text(glyph.name), len(own),
                                                  u", ".join(str(i) for i, _s in own)))
    else:
        target = own[0][1] if own else None
    own_index = index if index is not None else (own[0][0] if own else None)
    before = _shape_contours(target) if target is not None else []
    if target is not None and _same_contours(before, wanted, 0.0):
        return _unchanged(package, glyph=_text(glyph.name), layer=_text(layer.name), contours=len(before))
    if target is None and not wanted:
        return _unchanged(package, glyph=_text(glyph.name), layer=_text(layer.name), contours=0)
    from PythonQt.QtCore import QPointF  # only inside FontLab
    saved = [c.clone() for c in _real_list(target.contours, "shape.contours")] if target is not None else []

    def made(contours):
        out = []
        for closed, nodes in contours:
            contour = fl.flContour([QPointF(x, y) for x, y, _k, _s in nodes])
            contour.closed = closed
            made_nodes = _real_list(contour.nodes(), "contour.nodes()")
            for node, (_x, _y, kind, _smooth) in zip(made_nodes, nodes):
                if kind != "on":
                    node.type = kind
            # Smoothness after every type (live, 2026-11-19): FontLab refuses smooth on a node whose
            # neighbors are not yet handles (canBeSmooth false), so set in one pass it was lost.
            for node, (_x, _y, _kind, smooth) in zip(made_nodes, nodes):
                if smooth:
                    node.smooth = True
            out.append(contour)
        return out

    write = _Write(listener, package, u"outline of %s" % _text(glyph.name), [glyph])
    write.begin()
    added = []

    def redraw(shape):
        for item in (shape, layer):
            try:
                item.update()  # (a layer keeps its old box until update(), live 2026-11-14)
            except Exception:
                pass

    def change():
        shape = target
        if shape is None:
            shape = fl.flShape()
            shape.addContours(made(wanted), True)
            layer.addShape(shape)
            added.append(shape)
        else:
            shape.clearContours()
            shape.addContours(made(wanted), True)
        redraw(shape)
        # Read back from the layer as it is now, not from the shape made here (Codex, 2026-11-19): a shape
        # the layer did not take, or took a changed copy of, must not read as kept.
        now = _own_shapes(layer)
        if target is None:
            if len(now) != 1:
                raise _NotKept(u"the layer has %d shapes of its own after the new one was added, not 1" % len(now))
            kept = now[0][1]
        else:
            kept = dict(now).get(own_index)
            if kept is None or len(now) != len(own):
                raise _NotKept(u"the layer's own shapes changed (%d, not %d)" % (len(now), len(own)))
        if _component_count(layer) != components:
            raise _NotKept(u"the layer's components changed (%d, not %d)" % (_component_count(layer), components))
        read = _shape_contours(kept)
        differs = _contours_differ(read, wanted)
        if differs is not None:
            raise _NotKept(u"FontLab did not keep the contours as given (%s)" % differs)
        return read

    def restore():
        if target is None:
            for shape in added:
                try:
                    layer.removeShape(shape)
                except Exception:
                    shape.clearContours()
            left = [s for s in _real_list(layer.shapes, "layer.shapes") if _component_glyph(s) is None and
                    not bool(s.isComponent) and _shape_contours(s)]
            return not left
        target.clearContours()
        target.addContours(saved, True)
        redraw(target)
        return _same_contours(_shape_contours(target), before, 0.0)

    read = write.run(change, restore)
    if target is None:
        placed = _own_shapes(layer)
        own_index = placed[0][0] if placed else len(shapes)
    reply = _write_reply(package, write.finish(), glyph=_text(glyph.name), layer=_text(layer.name),
                         shape=own_index,
                         new_shape=target is None, contours_before=len(before), contours=len(read),
                         nodes=sum(len(n) for _c, n in read))
    if not _same_contours(read, wanted, 0.0):
        reply["rounded"] = u"FontLab rounded some coordinates to whole units (as the font rounds coordinates)."
    return reply


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
                        compiled="not checked: compile_features compiles it")


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
    keep = _bool_arg(args, "keep_in_front")
    expected = args.get("expected_state")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, integer_types)):
        raise FreeLabError("bad_args", "expected_state must be an integer: a history_state a write reported.")
    expected_title = args.get("expected_title")
    if expected_title is not None and (not isinstance(expected_title, string_types) or not expected_title.strip()):
        raise FreeLabError("bad_args", "expected_title must be the history.title a write reported.")
    _check_deadline(listener)
    with _InFront(package, keep) as front:
        reply = _undo_in_front(listener, args, package)
    reply.update(front.reply())
    return reply


class _InFront(object):
    """Brings a font to the front for an action FontLab takes on the font in front (Edit > Undo, the Features
    panel's Compile, File > Save), and puts the font that was in front back afterward unless keep is
    given, whatever the action did."""

    def __init__(self, package, keep=False):
        self.package, self.keep = package, keep
        self.previous, self.moved, self.restore = None, False, None

    def __enter__(self):
        current = _fontlab().flWorkspace.instance().currentPackage
        try:
            self.previous = None if _is_null(current, "the current font") else current
        except FreeLabError:
            self.previous = None
        self.moved = bring_to_front(self.package)
        if _current_id() != _font_id(self.package):
            raise FreeLabError("not_in_front", u"%s could not be brought to the front; nothing was done." %
                               _font_label(self.package))
        return self

    def __exit__(self, kind, value, trace):
        if self.moved and not self.keep and self.previous is not None:
            try:
                bring_to_front(self.previous)
                self.restore = {"font": _font_ref(self.previous)}
            except Exception as exc:
                self.restore = {"warning": u"The font that was in front could not be put back (%s): %s is in front "
                                           u"now." % (_exc_text(exc), _font_label(self.package))}
        return False

    def reply(self):
        reply = {"brought_forward": self.moved}
        if self.restore is not None:
            reply["front_restored"] = self.restore
        return reply


def _undo_in_front(listener, args, package):
    expected, expected_title = args.get("expected_state"), args.get("expected_title")
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


# ---------------------------------------------------------------------------
# Fonts and windows: open, close, bring to the front, compile, the Output panel, pictures
# ---------------------------------------------------------------------------
# Each of these was a probe helper first, run live for weeks against test fonts (docs/FONTLAB7-API.md).
# Opening and closing a font run from a timer, after the reply: FontLab's call returns only once any
# question it asks on the way (Coordinates Rounding, Save changes) is answered, and a request must not
# wait on a dialog. The server watches for the result.

OPEN_DELAY_MS = 50
CLOSE_DELAY_MS = 100
DONT_SAVE_MS = (600, 1500, 3000, 6000, 10000)  # when close_font with discard_changes looks for FontLab's Save question
COMPILE_TOOLTIP = u"Compile features ("  # the Features panel's button (FontLab 7.2 has no call to compile)
OUTPUT_VIEW = "textOutput"  # the Output panel's text box (FontLab has two of that name, one hidden and empty)
MAX_OUTPUT_CHARS = 4 * 1024 * 1024
DEFAULT_OUTPUT_CHARS = 100000
SAVE_PROMPT = "DlgClosePackage"  # FontLab 7.2's "Save changes to ... before closing?"
WELCOME = "DlgInitialize"  # FontLab 7.2's Welcome window, modal from the launch when no font is opened (live)


def _qt():
    try:
        from PythonQt import QtCore, QtGui  # only inside FontLab
    except ImportError:
        raise FreeLabError("not_in_fontlab", "Qt is not available: this is not running inside FontLab.")
    return QtCore, QtGui


def _main_window(QtGui):
    for widget in QtGui.QApplication.instance().topLevelWidgets():
        if widget.metaObject().className() == "MainWindow":
            return widget
    raise FreeLabError("not_found", "FontLab's main window was not found.")


def _current_id():
    """The id of the font in front, or None."""
    current = _fontlab().flWorkspace.instance().currentPackage
    try:
        return None if _is_null(current, "the current font") else _font_id(current)
    except FreeLabError:
        return None


def _font_tabs(QtGui):
    """(tab widget, index, text) of each window tab FontLab shows fonts in (Font, Glyph, Metrics...): the
    tabs of a YTabWidget in a YDropTargetContainer; the panels' tab widgets sit elsewhere."""
    found = []
    for top in QtGui.QApplication.instance().topLevelWidgets():
        if not top.isVisible():
            continue
        for tabs in top.findChildren(QtGui.QTabWidget):
            if tabs.metaObject().className() != "YTabWidget":
                continue
            parent = tabs.parentWidget()
            if parent is None or _text(_prop(parent, "objectName")) != "YDropTargetContainer":
                continue
            for index in range(int(_prop(tabs, "count"))):
                found.append((tabs, index, _text(tabs.tabText(index)).strip()))
    return found


def bring_to_front(package):
    """Make the font FontLab's current one by choosing its window tab, as a click on the tab does; whether
    that changed anything. The tab named after the font is tried first, then each other tab; a tab that
    does not make it current is left as it was."""
    target = _font_id(package)
    if _current_id() == target:
        return False
    QtGui = _qt()[1]
    try:
        name = _text(package.ffn).strip()
    except Exception:
        name = None
    tabs = _font_tabs(QtGui)
    for tab_widget, index, text in [t for t in tabs if t[2] == name] + [t for t in tabs if t[2] != name]:
        previous = int(_prop(tab_widget, "currentIndex"))
        if previous == index:
            continue
        tab_widget.setCurrentIndex(index)
        if _current_id() == target:
            return True
        tab_widget.setCurrentIndex(previous)
    raise FreeLabError("not_found", u"No window of %s was found to bring to the front: open one in FontLab (Window > "
                       u"New Font Tab)." % _font_label(package))


@op("front")
def op_front(listener, args):
    """Bring the font named to the front (FontLab's current font: what Undo and the Features panel act on)."""
    package = write_target(args)
    moved = bring_to_front(package)
    return {"font": _font_ref(package), "brought_forward": moved}


# ---------------------------------------------------------------------------
# A font FontLab 7.2 crashes on: single adjustments with no values
# ---------------------------------------------------------------------------
# FontLab 7.2 can crash as it opens a font whose GPOS holds a single adjustment subtable (lookup type 1, or 9
# around it) of format 2 with a ValueCount of 0: it copies the first value of an empty list and follows the
# pointers in whatever bytes its heap left there (docs/FONTLAB7-API.md). open_font looks for such subtables in
# the file's own bytes before FontLab is asked: no fontTools, so it runs in FontLab's Python 2.7 as in Python 3.
# FreeLab Script Suite makes the same check (its kerning/freelab_auto_kerning.py), a copy of this one, since
# neither repository needs the other.

SFNT_VERSIONS = (b"\x00\x01\x00\x00", b"OTTO", b"true")
SAFE_COPIES = "safe-copies"  # beside the state file: the fixed copies open_font opens with safe_copy
WOFF2_TAGS = (  # WOFF2's known table tags, by their index in a table directory entry's flags
    "cmap", "head", "hhea", "hmtx", "maxp", "name", "OS/2", "post", "cvt ", "fpgm", "glyf", "loca", "prep", "CFF ",
    "VORG", "EBDT", "EBLC", "gasp", "hdmx", "kern", "LTSH", "PCLT", "VDMX", "vhea", "vmtx", "BASE", "GDEF", "GPOS",
    "GSUB", "EBSC", "JSTF", "MATH", "CBDT", "CBLC", "COLR", "CPAL", "SVG ", "sbix", "acnt", "avar", "bdat", "bloc",
    "bsln", "cvar", "fdsc", "feat", "fmtx", "fvar", "gvar", "hsty", "just", "lcar", "mort", "morx", "opbd", "prop",
    "trak", "Zapf", "Silf", "Glat", "Gloc", "Feat", "Sill")


class FontFileError(Exception):
    """A font file read no further: it ends early, or an offset points outside it."""


def _u8(data, at):
    try:
        return struct.unpack_from(">B", data, at)[0]
    except struct.error:
        raise FontFileError("an offset points past the end of the data")


def _u16(data, at):
    try:
        return struct.unpack_from(">H", data, at)[0]
    except struct.error:
        raise FontFileError("an offset points past the end of the data")


def _u32(data, at):
    try:
        return struct.unpack_from(">I", data, at)[0]
    except struct.error:
        raise FontFileError("an offset points past the end of the data")


def _coverage_size(data, at):
    """How many glyphs the Coverage table at at covers (None for a format that is not 1 or 2)."""
    form = _u16(data, at)
    if form == 1:
        return _u16(data, at + 2)
    if form == 2:
        return sum(max(0, _u16(data, at + 6 + 6 * k) - _u16(data, at + 4 + 6 * k) + 1)
                   for k in range(_u16(data, at + 2)))
    return None


def empty_single_adjustments(gpos):
    """The single adjustment subtables of format 2 with no values in a GPOS table's bytes, in lookup order:
    [{"lookup", "subtable", "extension", "value_format", "glyphs", "at"}], lookup and subtable counted from 0 as
    fontTools counts them, at the subtable's offset in the table. Raises FontFileError when the table cannot be
    read that far."""
    version = _u16(gpos, 0)
    if version != 1:
        raise FontFileError("its GPOS has version %d, which is not known" % version)
    lookups = _u16(gpos, 8)
    found = []
    if not lookups:
        return found
    for li in range(_u16(gpos, lookups)):
        lookup = lookups + _u16(gpos, lookups + 2 + 2 * li)
        kind = _u16(gpos, lookup)
        if kind not in (1, 9):
            continue
        for si in range(_u16(gpos, lookup + 4)):
            at = lookup + _u16(gpos, lookup + 6 + 2 * si)
            if kind == 9:
                if _u16(gpos, at) != 1 or _u16(gpos, at + 2) != 1:
                    continue  # (an extension of another lookup type)
                at += _u32(gpos, at + 4)
            if _u16(gpos, at) != 2 or _u16(gpos, at + 6) != 0:
                continue
            try:
                glyphs = _coverage_size(gpos, at + _u16(gpos, at + 2))
            except FontFileError:
                glyphs = None
            found.append({"lookup": li, "subtable": si, "extension": kind == 9, "value_format": _u16(gpos, at + 4),
                          "glyphs": glyphs, "at": at})
    return found


def _reader(source):
    """read(at, length) over a file object or bytes; FontFileError when the data ends early."""
    if hasattr(source, "seek"):
        def read(at, length):
            source.seek(at)
            data = source.read(length)
            if len(data) != length:
                raise FontFileError("the file ends before its tables do")
            return data
    else:
        def read(at, length):
            if at + length > len(source):
                raise FontFileError("the file ends before its tables do")
            return source[at:at + length]
    return read


def _sfnt_tables(read, base):
    """{tag: (offset, length, directory entry's offset)} of the font whose offset table is at base."""
    count = _u16(read(base + 4, 2), 0)
    directory = read(base + 12, 16 * count)
    tables = {}
    for k in range(count):
        tag = bytes(directory[16 * k:16 * k + 4])
        tables[tag] = (_u32(directory, 16 * k + 8), _u32(directory, 16 * k + 12), base + 12 + 16 * k)
    return tables


def _sfnt_findings(read, base):
    """The empty single adjustments of the font whose offset table is at base, each at its file offset."""
    tables = _sfnt_tables(read, base)
    if b"GPOS" not in tables:
        return []
    offset, length, _entry = tables[b"GPOS"]
    found = empty_single_adjustments(read(offset, length))
    for finding in found:
        finding["at"] += offset
    return found


def _collection_bases(read):
    """The offset tables of a TrueType or OpenType collection's fonts."""
    count = _u32(read(8, 4), 0)
    offsets = read(12, 4 * count)
    return [_u32(offsets, 4 * k) for k in range(count)]


def _woff_tables(read):
    """(flavor, [(tag, offset, compressed length, length, checksum)]) of a WOFF file."""
    header = read(0, 44)
    count = _u16(header, 12)
    directory = read(44, 20 * count)
    return bytes(header[4:8]), [(bytes(directory[20 * k:20 * k + 4]), _u32(directory, 20 * k + 4),
                                 _u32(directory, 20 * k + 8), _u32(directory, 20 * k + 12),
                                 _u32(directory, 20 * k + 16)) for k in range(count)]


def _woff_table(read, entry):
    """A WOFF table's bytes (zlib, unless stored as is)."""
    _tag, offset, compressed, length, _checksum = entry
    data = read(offset, compressed)
    if compressed < length:
        try:
            data = zlib.decompress(bytes(data))
        except zlib.error as exc:
            raise FontFileError("a table could not be decompressed (%s)" % exc)
    if len(data) != length:
        raise FontFileError("a table is not as long as its directory says")
    return data


def _brotli_decompress():
    """Brotli's decompress, or None: WOFF2 needs it (FontLab's Python has none)."""
    for name in ("brotli", "brotlicffi"):
        try:
            return __import__(name).decompress
        except Exception:
            continue
    return None


def _base128(data, at):
    """A WOFF2 UIntBase128 at at: (value, the offset after it)."""
    value = 0
    for k in range(5):
        byte = _u8(data, at + k)
        if (k == 0 and byte == 0x80) or value & 0xFE000000:
            raise FontFileError("a WOFF2 number is malformed")
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, at + k + 1
    raise FontFileError("a WOFF2 number is malformed")


def _u255(data, at):
    """A WOFF2 255UInt16 at at: (value, the offset after it)."""
    code = _u8(data, at)
    if code == 253:
        return _u16(data, at + 1), at + 3
    if code == 255:
        return _u8(data, at + 1) + 253, at + 2
    if code == 254:
        return _u8(data, at + 1) + 506, at + 2
    return code, at + 1


def _woff2_findings(data):
    """[(font index, findings)] of a WOFF2 file's bytes; None when brotli is missing. Only GPOS is read, which
    WOFF2 never transforms."""
    decompress = _brotli_decompress()
    if decompress is None:
        return None
    flavor, count, compressed = bytes(data[4:8]), _u16(data, 12), _u32(data, 20)
    at, entries = 48, []
    for _k in range(count):
        flags = _u8(data, at)
        at += 1
        if flags & 0x3F == 63:
            tag = bytes(data[at:at + 4])
            at += 4
        else:
            tag = WOFF2_TAGS[flags & 0x3F].encode("ascii")
        length, at = _base128(data, at)
        version = flags >> 6
        if (version == 0) if tag in (b"glyf", b"loca") else (version != 0):
            length, at = _base128(data, at)  # (transformed: its length in the stream)
        entries.append((tag, length))
    fonts = None
    if flavor == b"ttcf":
        at += 4  # (the collection's version)
        number, at = _u255(data, at)
        fonts = []
        for _f in range(number):
            tables, at = _u255(data, at)
            at += 4  # (the font's flavor)
            indexes = []
            for _t in range(tables):
                index, at = _u255(data, at)
                indexes.append(index)
            fonts.append(indexes)
    try:
        stream = decompress(bytes(data[at:at + compressed]))
    except Exception as exc:
        raise FontFileError("its data could not be decompressed (%s)" % _exc_text(exc))
    found, offset = {}, 0
    for index, (tag, length) in enumerate(entries):
        if tag == b"GPOS":
            if offset + length > len(stream):
                raise FontFileError("its GPOS ends past its data")
            subtables = empty_single_adjustments(stream[offset:offset + length])
            for font in ([f for f, indexes in enumerate(fonts) if index in indexes] if fonts is not None else [0]):
                found.setdefault(font, []).extend(dict(s) for s in subtables)
        offset += length
    return sorted(found.items())


def scan_font_file(path):
    """open_font's check: the single adjustments with no values a font file holds.

    {"format": "sfnt", "collection", "woff", "woff2", or None (no binary GPOS to check: a FontLab, UFO, or Glyphs
    source, a folder, or no font), "checked": whether the check was made, "why_not": why not (when it could not
    be), "fonts": [{"font": index in the file, "subtables": [...]}] (the fonts that hold some), "count": how many
    in all}. A collection's fonts are each checked."""
    result = {"format": None, "checked": False, "fonts": [], "count": 0}
    if not os.path.isfile(path):
        return result
    try:
        with open(path, "rb") as handle:
            read = _reader(handle)
            try:
                magic = bytes(read(0, 4))
            except FontFileError:
                return result
            if magic in SFNT_VERSIONS:
                result["format"] = "sfnt"
            elif magic == b"ttcf":
                result["format"] = "collection"
            elif magic == b"wOFF":
                result["format"] = "woff"
            elif magic == b"wOF2":
                result["format"] = "woff2"
            else:
                return result
            try:
                if result["format"] == "sfnt":
                    pairs = [(0, _sfnt_findings(read, 0))]
                elif result["format"] == "collection":
                    pairs = [(k, _sfnt_findings(read, base)) for k, base in enumerate(_collection_bases(read))]
                elif result["format"] == "woff":
                    gpos = [e for e in _woff_tables(read)[1] if e[0] == b"GPOS"]
                    pairs = [(0, empty_single_adjustments(_woff_table(read, gpos[0])) if gpos else [])]
                else:
                    pairs = _woff2_findings(read(0, os.path.getsize(path)))
                    if pairs is None:
                        result["why_not"] = u"WOFF2 needs brotli, which this Python does not have"
                        return result
            except FontFileError as exc:
                result["why_not"] = u"its tables could not be read: %s" % _text(exc)
                return result
    except (IOError, OSError) as exc:
        result["why_not"] = u"it could not be read: %s" % _exc_text(exc)
        return result
    result["checked"] = True
    result["fonts"] = [{"font": font, "subtables": found} for font, found in pairs if found]
    result["count"] = sum(len(entry["subtables"]) for entry in result["fonts"])
    return result


def _listed(items):
    """'a', 'a and b', 'a, b, and c'."""
    items = [_text(item) for item in items]
    if len(items) < 3:
        return u" and ".join(items)
    return u"%s, and %s" % (u", ".join(items[:-1]), items[-1])


def crasher_summary(scan):
    """'2 single adjustment subtables of format 2 with no values (lookups 10 and 11)': what a scan found."""
    parts = []
    several = scan["format"] == "collection" or any(entry["font"] for entry in scan["fonts"])  # (a WOFF2 collection too)
    for entry in scan["fonts"]:
        lookups = sorted(set(s["lookup"] for s in entry["subtables"]))
        where = (u"lookup %d" % lookups[0]) if len(lookups) == 1 else u"lookups %s" % _listed(lookups)
        parts.append(u"font %d: %s" % (entry["font"], where) if several else where)
    count = scan["count"]
    return u"%d single adjustment %s of format 2 with no values (%s)" % (
        count, u"subtable" if count == 1 else u"subtables", u"; ".join(parts))


def crasher_report(scan):
    """What a scan found, for a reply: the subtables without their offsets."""
    return [{"font": entry["font"], "subtables": [dict((k, v) for k, v in s.items() if k != "at")
                                                  for s in entry["subtables"]]} for entry in scan["fonts"]]


def _checksum(data, start, length):
    """OpenType's checksum of length bytes from start (zero padded to 4), 64 KB at a time."""
    total, end = 0, start + length
    for at in range(start, end, 65536):
        block = bytes(data[at:min(at + 65536, end)])
        block += b"\0" * (-len(block) % 4)
        total += sum(struct.unpack(">%dI" % (len(block) // 4), block))
    return total & 0xFFFFFFFF


def _sfnt_from_woff(read):
    """The sfnt a WOFF file holds, as bytes: its tables in its order, each 4-byte aligned, zero padded."""
    flavor, entries = _woff_tables(read)
    count = len(entries)
    selector = 0
    while (1 << (selector + 1)) <= count:
        selector += 1
    out = bytearray(flavor + struct.pack(">HHHH", count, 16 << selector, selector, 16 * count - (16 << selector)))
    out += b"\0" * (16 * count)
    for k, entry in enumerate(sorted(entries)):
        data = _woff_table(read, entry)
        struct.pack_into(">4sIII", out, 12 + 16 * k, entry[0], entry[4], len(out), len(data))
        out += bytes(data) + b"\0" * (-len(data) % 4)
    return out


def fixed_font_bytes(data, fmt):
    """A copy of a font file's bytes (bytearray; an sfnt or a collection) with each single adjustment of format 2
    with no values made format 1 with no ValueFormat, over the same coverage: it positions nothing either, and
    FontLab 7.2 opens it (the bisect, docs/FONTLAB7-API.md). Nothing moves: the subtable's ValueFormat is set to 0
    and its ValueCount, already 0, is left as two unused bytes. Then the GPOS checksums and each font's head
    checkSumAdjustment are made again. Returns (data, how many subtables were changed)."""
    read = _reader(data)
    bases = [0] if fmt == "sfnt" else _collection_bases(read)
    patched = set()
    for base in bases:
        for finding in _sfnt_findings(read, base):
            if finding["at"] not in patched:
                struct.pack_into(">H", data, finding["at"], 1)
                struct.pack_into(">H", data, finding["at"] + 4, 0)
                patched.add(finding["at"])
    for base in bases:
        tables = _sfnt_tables(read, base)
        if b"GPOS" in tables:
            offset, length, entry = tables[b"GPOS"]
            struct.pack_into(">I", data, entry + 4, _checksum(data, offset, length))
    for base in bases:
        tables = _sfnt_tables(read, base)
        if b"head" not in tables:
            continue
        head = tables[b"head"][0]
        struct.pack_into(">I", data, head + 8, 0)
        if fmt == "sfnt":
            total = _checksum(data, 0, len(data))
        else:  # (a collection's font: its offset table and directory, and its tables, as a font of its own)
            total = _checksum(data, base, 12 + 16 * len(tables))
            for offset, length, _entry in tables.values():
                total += _checksum(data, offset, length)
        struct.pack_into(">I", data, head + 8, (0xB1B0AFBA - total) & 0xFFFFFFFF)
    return data, len(patched)


def safe_copy_path(path, scan, folder):
    """Where the fixed copy of path goes: in folder, a folder named after the original's full path (so two files
    of the same name do not share one), and in it the original's name (a WOFF's as .ttf or .otf, by its flavor).
    Raises FontFileError for a WOFF2 (its tables are not rebuilt here)."""
    if scan["format"] == "woff2":
        raise FontFileError("a WOFF2 file is not copied here: convert it to TTF or OTF first, then open that")
    key = hashlib.sha1(_text(os.path.normcase(os.path.abspath(path))).encode("utf-8")).hexdigest()[:10]
    name = os.path.basename(path)
    if scan["format"] == "woff":
        with open(path, "rb") as handle:
            flavor = bytes(_reader(handle)(4, 4))
        name = os.path.splitext(name)[0] + (u".otf" if flavor == b"OTTO" else u".ttf")
    return os.path.join(folder, key, name)


def write_safe_copy(path, scan, target):
    """Writes the fixed copy of a font file a scan found crashers in at target (safe_copy_path): fixed_font_bytes,
    of a WOFF's sfnt decompressed. The original is only read. Returns how many subtables were changed. Raises
    FontFileError for a WOFF2 or a file that cannot be read, OSError for a copy that cannot be written."""
    fmt = scan["format"]
    if fmt == "woff2":
        raise FontFileError("a WOFF2 file is not copied here: convert it to TTF or OTF first, then open that")
    with open(path, "rb") as handle:
        if fmt == "woff":
            data, fmt = _sfnt_from_woff(_reader(handle)), "sfnt"
        else:
            data = bytearray(handle.read())
    data, changed = fixed_font_bytes(data, fmt)
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    temp = u"%s.%d.tmp" % (target, os.getpid())
    with open(temp, "wb") as handle:
        handle.write(bytes(data))
    if os.path.exists(target):
        os.remove(target)
    os.rename(temp, target)
    return changed


def _copies_folder(listener):
    """The fixed copies' folder: beside the state file, or in the system's temporary folder."""
    folder = os.path.join(os.path.dirname(os.path.abspath(listener.state_file)), SAFE_COPIES)
    try:
        if not os.path.isdir(folder):
            os.makedirs(folder)
        return folder
    except OSError:
        import tempfile
        return os.path.join(tempfile.gettempdir(), "FreeLab", SAFE_COPIES)


@op("open_font")
def op_open_font(listener, args):
    """Open a font file as File > Open does, a moment after the reply. A font already open from that path
    is named instead. The reply lists the fonts open before, so the server can tell the new one. A file
    FontLab 7.2 crashes on (scan_font_file) is refused (known_crasher), unless safe_copy is given: a fixed
    copy of it is then written beside the state file and opened instead (the reply's safe_copy says so)."""
    path = args.get("path")
    if not isinstance(path, string_types) or not path.strip():
        raise FreeLabError("bad_args", "path must be the font file's full path.")
    path = _text(path).strip()
    if not os.path.isabs(path):
        raise FreeLabError("bad_args", u"path must be a full path, not '%s'." % path)
    if not os.path.exists(path):
        raise FreeLabError("not_found", u"There is no file at %s." % path)
    answer = args.get("answer")
    if answer is not None:
        if not isinstance(answer, string_types) or not answer.strip():
            raise FreeLabError("bad_args", "answer must be the text of the button to click on FontLab's question.")
        _require_risky(args, "open_font.answer")
    safe_copy = _bool_arg(args, "safe_copy")
    fl = _fontlab()
    package = _open_from(path)
    if package is not None:
        return {"already_open": True, "font": _font_ref(package)}
    scan = scan_font_file(path)  # (FontLab 7.2 may crash on the file: docs/FONTLAB7-API.md)
    copy = None
    name = os.path.basename(path)
    if scan["count"]:
        if not safe_copy:
            raise FreeLabError("known_crasher", (
                u"FontLab 7.2 crashes on opening %s (not every time: it depends on what FontLab's memory held), and a "
                u"crash loses the unsaved work in every open font. Its GPOS holds %s. Nothing was opened. open_font "
                u"with safe_copy=True opens a fixed copy instead, written to a temporary folder: %s nothing, so %s "
                u"made an empty single adjustment of format 1, which FontLab opens, and the font is the same; the "
                u"file itself is not changed.") % (
                    name, crasher_summary(scan),
                    u"such a subtable positions" if scan["count"] == 1 else u"such subtables position",
                    u"it is" if scan["count"] == 1 else u"each is"),
                {"path": path, "format": scan["format"], "count": scan["count"], "fonts": crasher_report(scan)})
        try:
            target = safe_copy_path(path, scan, _copies_folder(listener))
            package = _open_from(target)
            if package is None:
                changed = write_safe_copy(path, scan, target)
        except (FontFileError, IOError, OSError) as exc:
            raise FreeLabError("cannot_copy", u"%s holds %s, on which FontLab 7.2 crashes, and no fixed copy could be "
                               u"made (%s). Nothing was opened." % (name, crasher_summary(scan), _text(exc)),
                               {"path": path, "format": scan["format"], "fonts": crasher_report(scan)})
        copy = {"original": path, "path": target, "fonts": crasher_report(scan)}
        if package is not None:
            copy["note"] = u"The fixed copy of %s, made earlier, is open already; the file itself is not changed." % name
            return {"already_open": True, "font": _font_ref(package), "safe_copy": copy}
        copy["changed"] = changed
        copy["note"] = (u"Opened a fixed copy of %s, written to %s: its %s, on which FontLab 7.2 crashes, %s made an "
                        u"empty single adjustment of format 1, which positions nothing either, so the font is the "
                        u"same. The file itself is not changed.") % (
                            name, target, crasher_summary(scan), u"was" if scan["count"] == 1 else u"were each")
        path = target
    before = [_font_id(package) for package in open_packages()]
    _check_deadline(listener)
    loading = {"running": False, "ended": False}

    def load():
        # requestLoadingFont returns only once the questions it asks are answered (live): its run is the only
        # time an answer may be clicked, and its end ends the answering, whatever became of the open (Codex,
        # 2026-11-19: a canceled open left the answer looking for a later question of the user's own).
        loading["running"] = True
        try:
            fl.flItems.requestLoadingFont(path)
        finally:
            loading["running"], loading["ended"] = False, True
            listener.adopt_opened(path, before)
    if answer is not None:  # (first: it must be waiting while requestLoadingFont asks)
        _keep_answering(listener, "open_font", _text(answer), time.time() + OPEN_ANSWER_S,
                        lambda dialog: loading["running"] and dialog.metaObject().className() not in (SAVE_PROMPT, WELCOME),
                        keep_going=lambda: not loading["ended"])
    listener.later(OPEN_DELAY_MS, load, "open_font")
    reply = {"queued": True, "path": path, "fonts_before": before}
    if answer is not None:
        reply["answer"] = _text(answer)
    if copy is not None:
        reply["safe_copy"] = copy
    elif scan.get("why_not"):
        reply["crash_check"] = (u"Not made: %s. FontLab 7.2 crashes on a font whose GPOS holds a single adjustment of "
                                u"format 2 with no values; whether this one does is not known." % scan["why_not"])
    elif safe_copy and scan["checked"]:
        reply["safe_copy"] = {"original": path, "path": None,
                              "note": u"No copy was needed: %s holds no subtable FontLab 7.2 crashes on." % name}
    return reply


def _open_from(path):
    """The open font FontLab opened from path, or None."""
    for package in open_packages():
        try:
            if _same_path(package.path, path):
                return package
        except Exception:
            continue
    return None


def _edited(package):
    """Whether the font changed since it was opened, as far as FontLab says: its History is past its first
    state, or it is marked changed. A read that fails counts as changed. (hasHistoryStates is no sign: it
    was True for a font just opened, live.)"""
    try:
        return package.currentHistoryState() != package.initialHistoryState() or bool(package.hasChanges)
    except Exception:
        return True


def _opened_here(listener, package):
    """Whether open_font opened this very font: its id, recorded once the open succeeded, and its path. A font
    closed by any means is forgotten (prune), so the same file opened again by hand is not taken for it
    (Codex, 2026-11-19)."""
    listener.prune_opened()
    try:
        font_id, path = _font_id(package), package.path
    except Exception:
        return False
    return any(entry["id"] == font_id and _same_path(path, entry["path"]) for entry in listener.opened)


@op("close_font")
def op_close_font(listener, args):
    """Close a font as File > Close Font does, a moment after the reply. A font with changes is refused,
    unless discard_changes is given for a font open_font opened in this FontLab process, or for any font in
    risky mode: FontLab's Save question for that font is then answered Don't Save. FontLab 7 does not report
    a change made outside its History and left unmarked, so such a font closes without a question."""
    package = write_target(args)
    discard = _bool_arg(args, "discard_changes")
    label = _font_label(package)
    opened_here = _opened_here(listener, package)
    edited = _edited(package)
    if discard and not opened_here:
        try:
            _require_risky(args, "close_font.discard_changes")
        except FreeLabError as exc:
            exc.message = u"%s was not opened with open_font in this FontLab session, and throwing away changes to a font " \
                          u"FreeLab MCP did not open is risky. %s" % (label, exc.message)
            raise
    if edited and not discard:
        raise FreeLabError("unsaved_changes", u"%s has changes that are not saved: save it in FontLab first%s. Nothing was "
                           u"closed." % (label, u", or close it with discard_changes" if opened_here else u""))
    if _main_window_action("actionClose_Fonts") is None:
        raise FreeLabError("not_found", "FontLab's File > Close Font was not found; nothing was closed.")
    _check_deadline(listener)
    bring_to_front(package)
    target = _font_id(package)
    # (FontLab's question names the font in quotes: by its full name, or by its family name alone for a font
    # of several masters, live: Save changes to  "Rubik" before closing? for Rubik Light)
    names = [n for n in (_text(getattr(package, key, u"") or u"").strip() for key in ("ffn", "psn", "tfn")) if n]
    try:
        names.append(os.path.basename(_text(package.path).replace(u"/", os.sep)))
    except Exception:
        pass
    # This close's own run of File > Close Font: its Don't Save answers act only while that trigger() runs
    # (FontLab's question is modal inside it, live) and only once; when it is over, by a close, a Cancel, or
    # a close skipped, the answers left do nothing, so a later close of the same font, by the user, is never
    # answered (Codex, 2026-11-19).
    closing = {"active": False, "over": False, "answered": False}

    def close():
        try:
            if _current_id() != target:
                return  # something else came to the front meanwhile: left open (the server says so)
            closing["active"] = True
            try:
                _main_window_action("actionClose_Fonts").trigger()  # returns once FontLab's question is answered
            finally:
                closing["active"] = False
            listener.prune_opened()
        finally:
            closing["over"] = True

    def answer():
        if closing["over"] or not closing["active"] or closing["answered"] or listener.stopped:
            return
        if _answer_dont_save(target, names):
            closing["answered"] = True

    if discard:
        for delay in DONT_SAVE_MS:
            listener.later(delay, answer, "close_font")
    listener.later(CLOSE_DELAY_MS, close, "close_font")
    return {"closing": True, "font": _font_ref(package), "edited": edited, "discard_changes": discard}


def _answer_dont_save(target, names):
    """Don't Save, on FontLab's Save question about this font only: the font is the one in front and the
    question names it in quotes (live: Save changes to  "Varela Round Regular" before closing?), so a font
    whose name merely starts the same is not taken for it. Whether it was answered."""
    QtGui = _qt()[1]
    if _current_id() != target:
        return False
    for widget in QtGui.QApplication.instance().topLevelWidgets():
        if not widget.isVisible() or widget.metaObject().className() != SAVE_PROMPT:
            continue
        text = u" ".join(_label_text(label) for label in widget.findChildren(QtGui.QLabel))
        if not any(u'"%s"' % name in text for name in names):
            continue
        for button in widget.findChildren(QtGui.QAbstractButton):
            words = _MNEMONIC.sub(r"\1", _text(_prop(button, "text") or u"")).replace(u"\u2019", u"'").strip().lower()
            if words == u"don't save":
                button.click()
                return True
    return False


def _output_view(main, QtGui):
    """FontLab's Output panel's text box: the one shown, else one that holds text, else either (FontLab has
    two of that name, the first hidden and empty), or None."""
    views = [w for w in main.findChildren(QtGui.QTextEdit) if _text(_prop(w, "objectName")) == OUTPUT_VIEW]
    for wanted in (lambda w: w.isVisible(), lambda w: bool(_text(w.toPlainText()))):
        for view in views:
            if wanted(view):
                return view
    return views[0] if views else None


@op("output")
def op_output(listener, args):
    """FontLab's Output panel's text from offset on (the length an earlier read gave); cleared says the
    panel holds less than offset (FontLab clears it, at each compile for one), and the text is then all of it."""
    offset = _int_arg(args, "offset", 0, 0, 10 ** 9)
    limit = _int_arg(args, "max_chars", DEFAULT_OUTPUT_CHARS, 1, MAX_OUTPUT_CHARS)
    QtGui = _qt()[1]
    view = _output_view(_main_window(QtGui), QtGui)
    if view is None:
        raise FreeLabError("not_found", "FontLab's Output panel was not found: open it (Window > Panels > Output).")
    text = _text(view.toPlainText())
    cleared = offset > len(text)
    start = 0 if cleared else offset
    truncated = len(text) - start > limit
    if truncated:
        start = len(text) - limit
    return {"text": text[start:], "start": start, "length": len(text), "cleared": cleared, "truncated": truncated,
            "visible": bool(view.isVisible())}


COMPILE_ERROR = re.compile(r"^\[(F|E)\]")
COMPILE_WARNING = re.compile(r"^\[W\]")


def compile_report(text):
    """What FontLab's compiler printed, sorted: [F] and [E] lines are errors, [W] lines warnings."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if COMPILE_ERROR.match(line)]
    warnings = [line for line in lines if COMPILE_WARNING.match(line)]
    return {"compiled": not errors, "errors": errors, "warnings": warnings}


@op("compile_features")
def op_compile_features(listener, args):
    """Compile a font's feature code as the Features panel's Compile button does, the font brought to the
    front first (the panel compiles the font in front). The compile runs within the click (live: it
    returned with the compiler's messages in place); FontLab clears the Output panel first, and prints
    nothing for code that compiles."""
    package = write_target(args)
    keep = _bool_arg(args, "keep_in_front")
    QtGui = _qt()[1]
    main = _main_window(QtGui)
    opened = []

    def compile_buttons():
        return [b for b in main.findChildren(QtGui.QAbstractButton) if _text(_prop(b, "objectName")) == "buttonCompile"
                and _text(_prop(b, "toolTip") or u"").startswith(COMPILE_TOOLTIP)]
    buttons = compile_buttons()
    if not buttons and _open_panel(FEATURES_PANEL_ACTION):
        opened.append(u"Features")
        buttons = compile_buttons()
    if not buttons:
        raise FreeLabError("not_found", u"FontLab's Features panel was not found%s: open it (Window > Panels > Features), "
                           u"then compile again. Nothing was compiled." % (
                               u", though Window > Panels > Features was chosen" if opened else u""), {"opened_panels": opened})
    if not _prop(buttons[0], "enabled"):
        raise FreeLabError("unavailable", "The Features panel's Compile button is disabled; nothing was compiled.")
    view = _output_view(main, QtGui)
    if view is None and _open_panel(OUTPUT_PANEL_ACTION):
        opened.append(u"Output")
        view = _output_view(main, QtGui)
    if view is None:
        raise FreeLabError("not_found", u"FontLab's Output panel, where its compiler writes, was not found%s: open it "
                           u"(Window > Panels > Output). Nothing was compiled." % (
                               u", though Window > Panels > Output was chosen" if u"Output" in opened else u""),
                           {"opened_panels": opened})
    _check_deadline(listener)
    with _InFront(package, keep) as front:
        started = time.time()
        buttons[0].click()
        text = _text(view.toPlainText())
        seconds = round(time.time() - started, 3)
    reply = {"font": _font_ref(package), "seconds": seconds, "output": text, "output_length": len(text),
             "opened_panels": opened}
    reply.update(front.reply())
    reply.update(compile_report(text))
    located = _locate_errors(package, reply["errors"])
    if located:
        reply["located"] = located[0]
        reply["located_all"] = located
    return reply


FEATURES_PANEL_ACTION = "actionFeaturesPanel"  # Window > Panels > Features (seen live in the menu, 2026-11-19)
OUTPUT_PANEL_ACTION = "actionOutputPanel"  # Window > Panels > Output


def _open_panel(name):
    """Chooses Window > Panels > the panel (its QAction), unless the action is checked already (choosing it
    then would close the panel); whether it was chosen."""
    action = _main_window_action(name)
    if action is None:
        return False
    try:
        if _prop(action, "checked"):
            return False
    except Exception:
        pass
    action.trigger()
    return True


FEATURES_FILE_LINE = re.compile(r'in file "features" at line (\d+)')


def _panel_class_lines(package):
    """The lines get_prefix() starts with for the Classes panel's classes, one "@name = [glyphs];" each (live,
    2026-11-19: every class of the panel, in getAllGroups() order, on Varela Round, Bellefair, and Tinos)."""
    return [u"@%s = [%s];" % (_text(entry[1]), u" ".join(_text(n) for n in entry[4]))
            for entry in _real_list(package.getAllGroups(), "getAllGroups()") if not entry[0]]


def _own_prefix(package):
    """The prefix as FontLab's compiler counts it: get_prefix() without the Classes panel's lines it starts
    with (live: an error's line in FontLab's "features" file was each font's rebuilt line less exactly its
    number of panel classes, on three fonts)."""
    prefix = _text(package.fgPackage.features.get_prefix())
    lines = prefix.split(u"\n")
    classes = _panel_class_lines(package)
    if classes and lines[:len(classes)] == classes:
        return u"\n".join(lines[len(classes):])
    return prefix


def _feature_parts(package):
    """[(part, text ending with a line break)] of a font's feature code as FontLab puts it together for its
    compiler (live, 2026-11-19): the prefix without the Classes panel's lines, then each feature in keys()
    order."""
    features = package.fgPackage.features
    parts = [(u"prefix", _own_prefix(package))]
    for tag in _real_list(features.keys(), "features.keys()"):
        parts.append((_text(tag), _text(features.get_feature(tag))))
    return [(key, text if not text or text.endswith(u"\n") else text + u"\n") for key, text in parts]


def _locate_line(parts, line):
    first = 1
    for key, text in parts:
        count = text.count(u"\n")
        if first <= line < first + count:
            return {"tag": key, "line_in_feature": line - first + 1}
        first += count
    return {"tag": None, "line_in_feature": None, "lines_rebuilt": first - 1}


def _locate_errors(package, errors):
    """Each compiler error that names a line of FontLab's combined "features" file, mapped to a part of the
    font's code (a feature tag, or the prefix) and a line within it. Rebuilt from the parts as FontLab was
    seen to put them together (matched live on three fonts), so each mapping says certain: false."""
    found = []
    for error in errors:
        match = FEATURES_FILE_LINE.search(error)
        if match is None:
            continue
        line = int(match.group(1))
        try:
            entry = {"line": line, "certain": False}
            entry.update(_locate_line(_feature_parts(package), line))
            entry["note"] = (u"Rebuilt here from the prefix (without the Classes panel's classes) and each feature in "
                             u"order, as FontLab was seen to put them together; FontLab may add code of its own.")
        except Exception as exc:
            entry = {"line": line, "certain": False, "tag": None, "line_in_feature": None,
                     "note": u"Could not rebuild the feature code to locate it (%s)." % _exc_text(exc)}
        found.append(entry)
    return found


@op("locate_feature_line")
def op_locate_feature_line(listener, args):
    """A line of FontLab's combined "features" file (as a compiler error names it), mapped as compile_features
    maps it: an estimate."""
    package, count = target_package(args)
    line = _int_arg(args, "line", None, 1, 10 ** 7)
    located = _locate_errors(package, [u'in file "features" at line %d' % line])
    return {"font": _font_ref(package), "located": located[0]}


MAX_SCREENSHOT_WIDTH = 16384


@op("screenshot")
def op_screenshot(listener, args):
    """A PNG of one of FontLab's windows, drawn by Qt as it is now (another program over it is not in it):
    the main window, or the window whose title is given (exactly, else as the only one that contains it).
    max_width scales a wider picture down."""
    wanted = args.get("window") or u""
    if not isinstance(wanted, string_types):
        raise FreeLabError("bad_args", "window must be a window title.")
    max_width = _int_arg(args, "max_width", 0, 0, MAX_SCREENSHOT_WIDTH)
    QtCore, QtGui = _qt()
    windows = [w for w in QtGui.QApplication.instance().topLevelWidgets() if w.isVisible()]
    titles = [_text(_prop(w, "windowTitle") or u"") for w in windows]
    wanted = _text(wanted).strip()
    if not wanted:
        found = [_main_window(QtGui)]
    else:
        found = [w for w, t in zip(windows, titles) if t == wanted] or \
                [w for w, t in zip(windows, titles) if wanted.lower() in t.lower()]
    if not found:
        raise FreeLabError("not_found", u"No FontLab window is titled '%s'. Its windows: %s." % (
            wanted, u"; ".join(t for t in titles if t) or u"none shown"))
    if len(found) > 1:
        raise FreeLabError("ambiguous_window", u"%d FontLab windows match '%s': %s. Give the whole title." % (
            len(found), wanted, u"; ".join(_text(_prop(w, "windowTitle")) for w in found)))
    window = found[0]
    picture = window.grab()
    if max_width and picture.width() > max_width:
        picture = picture.scaledToWidth(max_width, 1)  # Qt::SmoothTransformation
    data = QtCore.QByteArray()
    buffer = QtCore.QBuffer(data)
    buffer.open(QtCore.QIODevice.WriteOnly)
    if not picture.save(buffer, "PNG"):
        raise FreeLabError("failed", "Qt could not make a PNG of the window.")
    png = bytes(data.data())
    return {"window": _text(_prop(window, "windowTitle") or u""), "class": window.metaObject().className(),
            "width": picture.width(), "height": picture.height(),
            "png_base64": binascii.b2a_base64(png).decode("ascii").strip()}


# ---------------------------------------------------------------------------
# Windows and their controls: generic tools over any Qt window, FontLab's own and scripts' alike
# ---------------------------------------------------------------------------
# Reads run inline and only read. An action (a click, a value set, a link followed, a menu command) is
# checked here, then run from a timer a moment after the reply, as open_font is: it may open a modal dialog
# or start long work, and a request must not wait on either. It is checked again when its timer fires, and
# window_state and action_result say what became of it (the server's wait_idle polls them).
#
# Optional conventions a script can follow to be driven more surely (docs/WINDOW-TOOLS.md):
#   - a control's objectName, unique in its window, is its address;
#   - the dynamic property mcpBusy on a window is truthy while it works (a text names the step);
#   - mcpScriptWindow on a window (and so on the windows it holds) says it is a script's: its controls are
#     ordinary actions; a dialog shown modal is a script's own question only when it carries it itself, since
#     FontLab may parent a question of its own to a script's window;
#   - mcpRisky on a control or a menu command (or on a widget holding controls): its click may write files or
#     throw work away, so it needs risky mode.

BUSY_PROPERTY = "mcpBusy"
SCRIPT_WINDOW_PROPERTY = "mcpScriptWindow"
RISKY_PROPERTY = "mcpRisky"
ACTION_DELAY_MS = 50
ACTION_LOST_S = 30.0  # an action still queued this long after it was asked never ran (its timer was lost)
MAX_ACTIONS_KEPT = 50
MAX_WALK = 20000  # widgets visited in one window
MAX_DEPTH = 64
DEFAULT_CONTROLS = 300
MAX_CONTROLS = 5000
DEFAULT_ROWS = 100
MAX_ROWS = 1000
MAX_TREE_ROWS = 20000  # rows a tree is walked for, to count them (a walk holds FontLab: kept short)
MAX_COLUMNS = 64
MAX_CHOICES = 200
MAX_LINKS = 2000
SHORT_TEXT = 160
CAPTION_CHARS = 48
DEFAULT_TEXT_CHARS = 100000
MAX_TEXT_CHARS = 4 * 1024 * 1024
DISPLAY_ROLE, CHECK_STATE_ROLE, HORIZONTAL = 0, 10, 1  # Qt::DisplayRole, Qt::CheckStateRole, Qt::Horizontal
USER_CHECKABLE = 16  # Qt::ItemIsUserCheckable
INSTANT_POPUP = 2  # QToolButton::InstantPopup: a press opens the menu (modal), so a click needs an entry
# A command named so may write files or throw work away: risky, unless a script's window says otherwise.
RISKY_WORDS = re.compile(u"(?<![a-z])(save|export|close|quit|exit|revert|discard)(?![a-z])")
EXTERNAL_LINK = re.compile(u"^(https?|ftp|mailto|file|javascript):", re.I)
SKIPPED_WINDOWS = ("QMenu", "QTipLabel", "QComboBoxPrivateContainer")  # popups, not windows to drive
_KIND_OF_CLASS = {
    "QPushButton": "button", "QToolButton": "button", "QCommandLinkButton": "button", "QCheckBox": "check",
    "QRadioButton": "radio", "QComboBox": "combo", "QFontComboBox": "combo", "QSpinBox": "spin",
    "QDoubleSpinBox": "spin", "QLineEdit": "field", "QTextEdit": "text", "QPlainTextEdit": "text",
    "QTextBrowser": "log", "QLabel": "label", "QTabWidget": "tabs", "QTabBar": "tabs", "QTreeWidget": "tree",
    "QTreeView": "tree", "QListWidget": "list", "QListView": "list", "QTableWidget": "table", "QTableView": "table",
    "QProgressBar": "progress", "QSlider": "slider"}
_CONTAINERS = frozenset([
    "QWidget", "QFrame", "QSplitter", "QSplitterHandle", "QScrollArea", "QStackedWidget", "QGroupBox", "QDockWidget",
    "QToolBar", "QMainWindow", "QDialog", "QStatusBar", "QMenuBar", "QScrollBar", "QSizeGrip", "QRubberBand",
    "QHeaderView", "QToolBox"])
_KIND_ORDER = (  # for other classes (FontLab's own, a script's subclass): the first Qt class it inherits
    ("QHeaderView", None), ("QScrollBar", None), ("QMenuBar", None), ("QTextBrowser", "log"), ("QTextEdit", "text"),
    ("QPlainTextEdit", "text"), ("QLineEdit", "field"), ("QAbstractSpinBox", "spin"), ("QComboBox", "combo"),
    ("QCheckBox", "check"), ("QRadioButton", "radio"), ("QAbstractButton", "button"), ("QTabWidget", "tabs"),
    ("QTabBar", "tabs"), ("QTreeView", "tree"), ("QTableView", "table"), ("QAbstractItemView", "list"),
    ("QLabel", "label"), ("QProgressBar", "progress"), ("QAbstractSlider", "slider"))
CONTROL_KINDS = ("button", "check", "radio", "combo", "spin", "field", "text", "log", "tabs", "list", "tree", "table",
                 "label", "progress", "slider")
ROW_KINDS = ("list", "tree", "table")
_LINK = re.compile(u'<a\\s[^>]*?href\\s*=\\s*(?:"([^"]*)"|\'([^\']*)\')[^>]*>(.*?)</a\\s*>', re.I | re.S)
_NUMERIC_ENTITY = re.compile(u"&#([0-9]+);")
_BREAK = re.compile(u"<br\\s*/?>", re.I)
_unichr = unichr if PY2 else chr  # noqa: F821
_lazy_range = xrange if PY2 else range  # noqa: F821  (a model may report millions of rows)


def _same(a, b):
    """Whether two wrappers are one Qt object (PythonQt keeps one wrapper per object while it is held)."""
    if a is b:
        return True
    try:
        return a is not None and b is not None and bool(a == b)
    except Exception:
        return False


def _try(read, default=None):
    try:
        return read()
    except Exception:
        return default


def _character(number):
    """A code point as text; above U+FFFF in Python 2.7's narrow build (FontLab 7's), as a surrogate pair."""
    if not 0 < number < 0x110000:
        return u"�"
    try:
        return _unichr(number)
    except ValueError:
        return (b"\\U%08x" % number).decode("unicode-escape")


def _unescape(text):
    text = _NUMERIC_ENTITY.sub(lambda m: _character(int(m.group(1))), text)
    for entity, character in _ENTITIES:
        text = text.replace(entity, character)
    return text


def _plain(text):
    """Rich text as plain text: tags out, line breaks kept, entities decoded."""
    return _unescape(_TAGS.sub(u"", _BREAK.sub(u"\n", _text(text))))


def _shorten(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + u"\u2026"


def _caption(text, limit=CAPTION_CHARS):
    """A text as a caption: plain, no mnemonic, on one line, without a closing colon, shortened."""
    if text is None:
        return u""
    text = _text(text)
    text = _plain(text) if _RICH.search(text) else _MNEMONIC.sub(r"\1", text)
    return _shorten(u" ".join(text.split()).rstrip(u":\uff1a").strip(), limit)


def _first_line(text):
    for line in _text(text or u"").splitlines():
        if line.strip():
            return line
    return u""


def _entry_words(text):
    """A menu entry's or a choice's text as compared: no mnemonic, no shortcut after a tab, no closing
    ellipsis, a curly apostrophe as a straight one, case ignored."""
    text = _MNEMONIC.sub(r"\1", _text(text or u"")).split(u"\t")[0].replace(u"\u2019", u"'").strip()
    for tail in (u"\u2026", u"...", u"."):  # (a caption that is a sentence ends with its period: found without it)
        if text.endswith(tail):
            text = text[:-len(tail)].rstrip()
            break
    return u" ".join(text.split()).lower()


def _object_name(obj):
    return _try(lambda: _text(_prop(obj, "objectName") or u""), u"")


def _class_name(obj):
    return _try(lambda: _text(obj.metaObject().className()), u"")


def _window_title(widget):
    return _try(lambda: _text(_prop(widget, "windowTitle") or u"").strip(), u"")


def _flagged(widget, name):
    """Whether a dynamic property (a convention) is set, truthy, on the widget or a widget holding it."""
    for _ in range(MAX_DEPTH):
        if widget is None:
            return False
        try:
            if widget.property(name):
                return True
            widget = widget.parentWidget()
        except Exception:
            return False
    return False


def _script_window(widget, waiting):
    """Whether a window is a script's own (mcpScriptWindow): the dialog FontLab waits on only when it says so
    itself (FontLab may parent a question of its own to a script's window), any other window also when a window
    holding it does."""
    if waiting:
        return bool(_try(lambda: widget.property(SCRIPT_WINDOW_PROPERTY), False))
    return _flagged(widget, SCRIPT_WINDOW_PROPERTY)


def _risky_named(*texts):
    """Whether a text names a command that may write files or throw work away (an objectName's words too:
    actionSaveFont). Each text is read whole, as shown: its mnemonics out (Sa&ve is Save), rich text's tags
    out."""
    for text in texts:
        if not text:
            continue
        text = _MNEMONIC.sub(r"\1", _TAGS.sub(u" ", _text(text)))
        if RISKY_WORDS.search(re.sub(u"([a-z])([A-Z])", u"\\1 \\2", text).lower()):
            return True
    return False


def _actions_risky(actions, named=True):
    """Whether any of these QActions (a command and the menus it is in, or a button's default action) is marked
    mcpRisky, or, with named, has a text, tooltip, or objectName naming a command that may write files or throw
    work away."""
    for action in actions:
        if action is None:
            continue
        if _try(lambda: action.property(RISKY_PROPERTY)):
            return True
        if named and _risky_named(_try(lambda: _prop(action, "text")), _try(lambda: _prop(action, "toolTip")),
                                  _object_name(action)):
            return True
    return False


def _default_action(widget, kind=None):
    """A tool button's default action (what its click triggers), or None."""
    if kind not in (None, "button") or not _try(lambda: widget.inherits("QToolButton"), False):
        return None
    return _try(lambda: widget.defaultAction())


# -- windows -------------------------------------------------------------------

def _window_entries(QtGui):
    """The visible top-level windows, each with its key: "main" for FontLab's main window, else its title (its
    objectName or class when untitled), with #1, #2, ... when several share it."""
    entries = []
    for widget in QtGui.QApplication.instance().topLevelWidgets():
        try:
            if not widget.isVisible():
                continue
        except Exception:
            continue
        cls = _class_name(widget)
        if not cls or cls in SKIPPED_WINDOWS:
            continue
        entries.append({"widget": widget, "title": _window_title(widget), "class": cls, "name": _object_name(widget),
                        "main": cls == "MainWindow"})
    groups = {}
    for entry in entries:
        base = u"main" if entry["main"] else (entry["title"] or entry["name"] or entry["class"])
        groups.setdefault(base, []).append(entry)
    for base, group in groups.items():
        # A key names one window for good: several windows of one name are told apart by a number each window
        # keeps (WINDOW_ID_PROPERTY, given here once), never by their order, which Qt may change between two
        # calls (#1 would then name the other one). A window of another kind named main or dialog is numbered
        # too, so that it never takes the place of FontLab's main window or the dialog waiting.
        reserved = not group[0]["main"] and base.lower() in (u"main", u"dialog")
        if len(group) == 1 and not reserved:
            group[0]["key"] = base
            continue
        for entry in group:  # (the numbers the group holds first: a number is never given twice)
            _window_number(entry["widget"], give=False)
        for entry in group:
            entry["key"] = u"main" if entry["main"] else u"%s#%d" % (base, _window_number(entry["widget"]))
    return entries


WINDOW_ID_PROPERTY = "freelabMcpWindow"  # a number the listener gives a window that shares its name with others
_WINDOW_NUMBERS = [0]  # the last number given (numbers are never given twice in a FontLab session)


def _window_number(widget, give=True):
    """The window's own number (WINDOW_ID_PROPERTY), given now if it has none (unless give is False: None): a
    dynamic property on the window itself, so the listener holds no reference to it (a script's window must die
    once its script lets it go), and a listener started again goes on from the numbers windows hold."""
    number = _try(lambda: widget.property(WINDOW_ID_PROPERTY))
    try:
        if number is not None and int(number) > 0:
            _WINDOW_NUMBERS[0] = max(_WINDOW_NUMBERS[0], int(number))
            return int(number)
    except Exception:
        pass
    if not give:
        return None
    _WINDOW_NUMBERS[0] += 1
    widget.setProperty(WINDOW_ID_PROPERTY, _WINDOW_NUMBERS[0])
    return _WINDOW_NUMBERS[0]


def _find_window(QtGui, wanted, modal):
    """The window entry wanted names: its key (list_windows), "main", "dialog" (the dialog FontLab waits on), a
    title (exact, else the only one containing it), an objectName, or a class."""
    if not isinstance(wanted, string_types) or not wanted.strip():
        raise FreeLabError("bad_args", "window must be a window's key from list_windows, \"main\" for FontLab's main "
                                       "window, or \"dialog\" for the dialog FontLab waits on.")
    wanted = _text(wanted).strip()
    entries = _window_entries(QtGui)
    if wanted.lower() == u"dialog":
        if modal is None:
            raise FreeLabError("not_found", "No dialog is waiting.")
        for entry in entries:
            if _same(entry["widget"], modal):
                return entry
        return {"widget": modal, "key": u"dialog", "title": _window_title(modal), "class": _class_name(modal),
                "name": _object_name(modal), "main": False}
    low = wanted.lower()
    for test in (lambda e: e["key"] == wanted, lambda e: e["title"] == wanted,
                 lambda e: e["key"].lower() == low or e["title"].lower() == low,
                 lambda e: e["name"] == wanted or e["class"] == wanted, lambda e: low in e["title"].lower()):
        found = [entry for entry in entries if test(entry)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise FreeLabError("ambiguous_window", u"%d windows match '%s': %s. Give one's key." % (
                len(found), wanted, u"; ".join(entry["key"] for entry in found)))
    raise FreeLabError("not_found", u"No window is '%s'. The windows shown: %s." % (
        wanted, u"; ".join(entry["key"] for entry in entries) or u"none"))


def _window_busy(widget, main, QtGui):
    """Whether a window works: its mcpBusy property when it has one (a text names the step), else, but for
    FontLab's main window and its panels (a panel may show a bar for good), a progress bar shown and not full
    (an empty range is a busy indicator)."""
    value = _try(lambda: widget.property(BUSY_PROPERTY))
    if value is not None:
        busy = {"busy": bool(value), "busy_by": "property"}
        if busy["busy"] and isinstance(value, string_types) and value.strip():
            busy["step"] = _text(value).strip()
        return busy
    if not main and not _try(lambda: widget.inherits("QDockWidget"), True):
        for bar in _try(lambda: _real_list(widget.findChildren(QtGui.QProgressBar), "findChildren()"), []):
            try:
                if not bar.isVisible():
                    continue
                low, high, value = int(_prop(bar, "minimum")), int(_prop(bar, "maximum")), int(_prop(bar, "value"))
            except Exception:
                continue
            if high == low or value < high:
                busy = {"busy": True, "busy_by": "progress_bar"}
                if high > low:
                    busy["progress"] = round(max(0.0, float(value - low) / (high - low)), 3)
                return busy
    return {"busy": False}


def _pending_actions(listener, own=None):
    """The listener's actions still queued or running, oldest first (one queued long ago never ran: skipped)."""
    now, found = time.time(), []
    for key in sorted(listener.actions):
        record = listener.actions[key]
        if record["id"] == own or record["state"] not in ("queued", "running"):
            continue
        if record["state"] == "queued" and now - record.get("asked", now) > ACTION_LOST_S:
            record.update({"state": "skipped", "code": "lost", "reason": u"Its timer never fired."})
            continue
        found.append(record)
    return found


def _describe_window(entry, modal, QtGui, listener):
    widget = entry["widget"]
    waiting = modal is not None and _same(widget, modal)
    script = _script_window(widget, waiting)
    about = {"window": entry["key"], "title": entry["title"], "class": entry["class"], "main": entry["main"],
             "modal": bool(_try(lambda: _prop(widget, "modal"), False)), "waiting": waiting,
             "question": waiting and not script, "script_window": script}
    if entry["name"]:
        about["object_name"] = entry["name"]
    about.update(_window_busy(widget, entry["main"], QtGui))
    running = [r["id"] for r in _pending_actions(listener) if r.get("window") == entry["key"]]
    if running:
        about["action_running"] = running[0]
        if not about["busy"]:
            about.update({"busy": True, "busy_by": "action"})
    return about


@op("windows")
def op_windows(listener, args):
    """The visible top-level windows: each one's key (what the window tools take), title, class, whether it is
    modal, whether FontLab waits on it (and whether it is a question of FontLab's), and whether it is busy."""
    QtGui = _qt()[1]
    modal = QtGui.QApplication.instance().activeModalWidget()
    return {"windows": [_describe_window(entry, modal, QtGui, listener) for entry in _window_entries(QtGui)],
            "dialog_waiting": modal is not None}


# -- controls ------------------------------------------------------------------

class _Control(object):
    """A control of a window: its widget, kind, the tabs, group boxes, and panels around it, its caption,
    objectName, and address."""

    def __init__(self, widget, kind, context):
        self.widget, self.kind, self.context = widget, kind, context
        self.name = _object_name(widget)
        self.caption = None
        self.address = None


_CLASS_INFO = {}  # a class name -> (its kind, "group" or "dock" for a group box or a docked panel): one look each


def _class_info(widget):
    """(the control kind of a widget, or None; "group" or "dock" when it is a group box or a docked panel, whose
    title names what it holds), looked up once per class: a window of FontLab's holds thousands of widgets."""
    cls = _class_name(widget)
    if cls not in _CLASS_INFO:
        kind, holder = _KIND_OF_CLASS.get(cls), None
        if kind is None and cls not in _CONTAINERS:
            for qt_class, found in _KIND_ORDER:
                if widget.inherits(qt_class):
                    kind = found
                    break
        if kind is None:
            holder = "group" if widget.inherits("QGroupBox") else "dock" if widget.inherits("QDockWidget") else None
        _CLASS_INFO[cls] = (kind, holder)
    return _CLASS_INFO[cls]


def _walk_controls(window):
    """Every control of a window, shown or not, in Qt's order, each with the tabs (of a QTabWidget), group
    boxes, and docked panels around it; and whether the walk stopped at MAX_WALK widgets. Windows of their own
    (a dialog the window parents) and the inside of a control (a spin box's field) are not walked."""
    found, seen = [], [0]

    def visit(widget, context, depth):
        children = _try(lambda: _real_list(widget.children(), "children()"), [])
        for child in children:
            seen[0] += 1
            if seen[0] > MAX_WALK:
                return
            try:
                if not child.isWidgetType() or child.isWindow():
                    continue
                kind, holder = _class_info(child)
            except Exception:
                continue
            if kind == "tabs" and _try(lambda: child.inherits("QTabWidget"), False):
                found.append(_Control(child, kind, context))
                for index in range(_try(lambda: int(_prop(child, "count")), 0)):
                    page = _try(lambda: child.widget(index))
                    if page is not None and depth < MAX_DEPTH:
                        tab = _caption(_try(lambda: child.tabText(index), u"")) or u"tab %d" % (index + 1)
                        visit(page, context + [tab], depth + 1)
                continue
            if kind is not None:
                if not _object_name(child).startswith(u"qt_"):  # (Qt's own parts: a tab widget's bar)
                    found.append(_Control(child, kind, context))
                if kind in ROW_KINDS and depth < MAX_DEPTH:
                    # A list's cells may hold controls of their own (setItemWidget: a dropdown in each row, as
                    # FreeLab's Feature Code has): they are the viewport's children, named after the list
                    segment = _object_name(child) or kind
                    for inner in _try(lambda: _real_list(child.children(), "children()"), []):
                        if _try(lambda: _object_name(inner) == u"qt_scrollarea_viewport", False):
                            visit(inner, context + [segment], depth + 1)
                continue
            segment = None
            if holder == "group":
                segment = _caption(_try(lambda: _prop(child, "title")))
            elif holder == "dock":
                segment = _caption(_window_title(child))
            if depth < MAX_DEPTH:
                visit(child, context + [segment] if segment else context, depth + 1)
    visit(window, [], 0)
    return found, seen[0] > MAX_WALK


def _previous_in_layout(layout, out, depth=0):
    """Appends (widget, the widget just before it, or None) for every widget of a layout, nested layouts walked
    (a nested layout's first widget takes the one before the nested layout): each layout is walked once."""
    previous = None
    for index in range(int(layout.count())):
        item = layout.itemAt(index)
        if item is None:
            continue
        inner = item.widget()
        if inner is not None:
            out.append((inner, previous))
            previous = inner
            continue
        nested = item.layout()
        if nested is not None and depth < 8:
            found = []
            _previous_in_layout(nested, found, depth + 1)
            out.extend((each, before if before is not None else previous) for each, before in found)
    return out


def _layout_caption(widget, cache=None):
    """The text of the label just before a control in its layout (Master: before a dropdown), or u"". cache: {id of
    the parent: (the parent, [(widget, the one before)], {id of a widget: the one before})}, filled once per
    parent for one listing (a layout scanned again for each of its controls made a listing quadratic)."""
    try:
        parent = widget.parentWidget()
        if parent is None:
            return u""
        cache = {} if cache is None else cache
        held = cache.get(id(parent))
        if held is None or held[0] is not parent:
            layout = parent.layout()
            pairs = _previous_in_layout(layout, []) if layout is not None else []
            held = cache[id(parent)] = (parent, pairs, dict((id(each), before) for each, before in pairs))
        if id(widget) in held[2]:
            before = held[2][id(widget)]
        else:  # (another wrapper of the same widget)
            before = next((b for each, b in held[1] if _same(each, widget)), None)
        if before and before.inherits("QLabel"):
            return _caption(_label_text(before))
    except Exception:
        pass
    return u""

def _action_text(action):
    return u"" if action is None else _text(_prop(action, "text") or u"")


def _control_caption(control, buddies, cache=None):
    """What names a control for its address: a button's text (else its accessible name, its action's text, or
    the first line of its tooltip); another control's accessible name, the label whose buddy it is, or the
    label just before it in its layout. Labels have none: their text is what they show."""
    widget, kind = control.widget, control.kind
    if kind == "label":
        return None
    reads = []
    if kind in ("button", "check", "radio"):
        reads.append(lambda: _prop(widget, "text"))
    reads.append(lambda: _prop(widget, "accessibleName"))
    if kind == "button":
        reads.append(lambda: _action_text(widget.defaultAction()) if widget.inherits("QToolButton") else u"")
        reads.append(lambda: _first_line(_prop(widget, "toolTip")))
    for read in reads:
        text = _caption(_try(read, u""))
        if text:
            return text
    if kind == "button":
        return None
    for buddy, label in buddies:
        if _same(buddy, widget):
            text = _caption(_try(lambda: _label_text(label), u""))
            if text:
                return text
    return _layout_caption(widget, cache) or None


def _controls(window):
    """A window's controls with their addresses (see _Control), and whether the walk was cut short. An address
    is the control's objectName when no other control of the window has it, else the tabs, group boxes, and
    panels around it, its caption, and its kind, joined by " > ", with #1, #2, ... when several share that."""
    controls, truncated = _walk_controls(window)
    buddies = []
    for control in controls:
        if control.kind == "label":
            buddy = _try(lambda: control.widget.buddy())
            if buddy is not None:
                buddies.append((buddy, control.widget))
    names, cache = {}, {}
    for control in controls:
        control.caption = _control_caption(control, buddies, cache)
        if control.name:
            names[control.name] = names.get(control.name, 0) + 1
    groups, order = {}, []
    for control in controls:
        if control.name and names[control.name] == 1:
            control.address = control.name
            continue
        base = u" > ".join(list(control.context) + ([control.caption] if control.caption else []) + [control.kind])
        if base not in groups:
            groups[base] = []
            order.append(base)
        groups[base].append(control)
    for base in order:
        for number, control in enumerate(groups[base]):
            control.address = base if len(groups[base]) == 1 else u"%s#%d" % (base, number + 1)
    return controls, truncated


def _shown(control):
    return bool(_try(lambda: control.widget.isVisible(), False))


def _find_control(controls, wanted, window_key):
    """The control wanted names: its address, its objectName, or its caption (one control's; the one shown when
    only one of several is)."""
    if not isinstance(wanted, string_types) or not wanted.strip():
        raise FreeLabError("bad_args", "control must be a control's address from list_window_controls, its "
                                       "objectName, or its caption.")
    wanted = _text(wanted).strip()
    words = _entry_words(wanted)
    for caption, test in ((False, lambda c: c.address == wanted), (False, lambda c: c.name == wanted),
                          (False, lambda c: c.address.lower() == wanted.lower()),
                          (True, lambda c: bool(c.caption) and _entry_words(c.caption) == words)):
        found = [control for control in controls if test(control)]
        if len(found) > 1 and caption:  # (a caption names the one shown of several; an address or a name never
            # picks by what is shown: an objectName equal to another control's address is refused as ambiguous)
            shown = [control for control in found if _shown(control)]
            found = shown if len(shown) == 1 else found
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise FreeLabError("ambiguous_control", u"%d controls of '%s' match '%s': %s. Give one's address." % (
                len(found), window_key, wanted, u"; ".join(control.address for control in found[:20])))
    near = [c.address for c in controls if words and words in c.address.lower()] or \
        [c.address for c in controls if _shown(c)]
    raise FreeLabError("not_found", u"The window '%s' has no control '%s'%s." % (
        window_key, wanted, (u"; controls: " + u"; ".join(near[:25])) if near else u""))


def _button_menu(widget):
    """A push or tool button's menu, or None."""
    if widget.inherits("QToolButton") or widget.inherits("QPushButton"):
        return widget.menu()
    return None


def _needs_entry(widget, menu):
    """Whether a click on the button would only open its menu (modal): a push button's, or a tool button's
    shown at the press (InstantPopup). A menu that opens on a held press or from its arrow does not."""
    if menu is None:
        return False
    if widget.inherits("QPushButton"):
        return True
    return _try(lambda: int(_prop(widget, "popupMode")), INSTANT_POPUP) == INSTANT_POPUP


def _menu_entries(menu):
    """[(action, its text as shown)] of a menu's entries, separators and hidden entries left out."""
    entries = []
    for action in _real_list(menu.actions(), "actions()"):
        try:
            if action.isSeparator() or not _prop(action, "visible"):
                continue
        except Exception:
            continue
        entries.append((action, _MNEMONIC.sub(r"\1", _text(_prop(action, "text") or u"")).split(u"\t")[0].strip()))
    return entries


def _menu_at(level, path, where):
    """(action, [entry texts], [actions]) of the entry path names in a menu or menu bar: entries' texts joined by
    ">"; actions: the entry's and those of the menus it is in, outermost first (their risk counts too)."""
    parts = [part.strip() for part in _text(path).split(u">")]
    if not parts or not all(parts):
        raise FreeLabError("bad_args", u"A menu path names each entry, joined by \">\": File > Open Fonts.")
    trail, chain = [], []
    for number, part in enumerate(parts):
        entries = _menu_entries(level)
        found = [(action, text) for action, text in entries if _entry_words(text) == _entry_words(part)]
        if not found:
            raise FreeLabError("not_found", u"%s has no entry '%s'; its entries: %s." % (
                u" > ".join(trail) or where, part, u", ".join(text for _a, text in entries) or u"none"))
        if len(found) > 1:
            raise FreeLabError("ambiguous_control", u"%s has %d entries '%s'." % (
                u" > ".join(trail) or where, len(found), part))
        action, text = found[0]
        trail.append(text)
        chain.append(action)
        if number < len(parts) - 1:
            submenu = action.menu()
            if submenu is None:
                raise FreeLabError("not_found", u"%s is a command, not a menu." % u" > ".join(trail))
            level = submenu
    return action, trail, chain


def _number_value(value):
    if isinstance(value, bool) or value is None:
        raise ValueError
    if isinstance(value, integer_types + (float,)):
        number = float(value)
    else:
        number = float(_text(value).strip())
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError
    return number


def _describe_control(control, question, script):
    """A control as list_window_controls lists it: address, kind, caption, objectName, shown, enabled, whether
    a click needs risky mode, and what it holds (a choice, a value, a text shortened, rows, tabs)."""
    widget, kind, reads = control.widget, control.kind, _Reads()
    entry = {"address": control.address, "kind": kind, "visible": _shown(control), "enabled": reads.prop(widget, "enabled")}
    if control.caption:
        entry["caption"] = control.caption
    if control.name:
        entry["object_name"] = control.name
    try:
        if kind in ("button", "check", "radio"):
            entry["text"] = _caption(reads.prop(widget, "text"), SHORT_TEXT)
            if kind == "button":
                if reads.prop(widget, "checkable"):
                    entry["checked"] = reads.prop(widget, "checked")
                menu = _button_menu(widget)
                if menu is not None:
                    entry["menu"] = [text for _action, text in _menu_entries(menu)][:MAX_CHOICES]
                    entry["click_needs_entry"] = _needs_entry(widget, menu)
            else:
                entry["checked"] = reads.prop(widget, "checked")
        elif kind == "combo":
            count = int(_prop(widget, "count"))
            entry["choices"] = [_text(widget.itemText(i)) for i in range(min(count, MAX_CHOICES))]
            if count > MAX_CHOICES:
                entry["choices_count"] = count
            entry["current"] = reads.prop(widget, "currentText")
            entry["current_index"] = reads.prop(widget, "currentIndex")
        elif kind in ("spin", "progress", "slider"):
            for key in ("value", "minimum", "maximum"):
                entry[key] = reads.prop(widget, key)
            if kind == "spin":
                entry["read_only"] = reads.prop(widget, "readOnly")
        elif kind == "field":
            entry["text"] = _shorten(_text(reads.prop(widget, "text") or u""), SHORT_TEXT)
            entry["read_only"] = reads.prop(widget, "readOnly")
        elif kind in ("text", "log"):
            text = _text(widget.toPlainText())
            entry["length"] = len(text)
            entry["preview"] = _shorten(u" ".join(text[:SHORT_TEXT * 2].split()), SHORT_TEXT)
            entry["read_only"] = reads.prop(widget, "readOnly")
        elif kind == "tabs":
            entry["tabs"] = [_caption(widget.tabText(i), SHORT_TEXT) for i in range(int(_prop(widget, "count")))]
            entry["current"] = reads.prop(widget, "currentIndex")
        elif kind in ROW_KINDS:
            QtCore = _qt()[0]
            model = widget.model()
            root = QtCore.QModelIndex()
            columns = min(int(model.columnCount(root)), MAX_COLUMNS)
            entry["columns"] = [_cell(model.headerData(c, HORIZONTAL, DISPLAY_ROLE)) for c in range(columns)]
            entry["rows"] = int(model.rowCount(root))
        elif kind == "label":
            raw = _text(reads.prop(widget, "text") or u"")
            entry["text"] = _shorten(_label_text(widget), SHORT_TEXT)
            if u"<a " in raw.lower():
                entry["links"] = len(_links(raw))
    except Exception as exc:
        reads.unreadable[kind] = _exc_text(exc)
    risk = _control_risk(control, question, script, click=True)
    if risk:
        entry["risky"] = True
    return reads.attach(entry)


def _control_risk(control, question, script, click=False, chain=None):
    """The risky action a control's use is (RISKY_ACTIONS), or None: any control of a question FontLab asks;
    a control a script marks mcpRisky (or a widget holding it), a button whose default action is marked, a menu
    entry marked or in a menu marked (chain: the entry's action and its menus'); and, outside a script's window,
    a click on a button, its default action, or a menu entry or a menu it is in, named like Save, Export, Close,
    Quit, Exit, Revert, or Discard (in its text, tooltip, accessible name, or objectName, mnemonics out)."""
    if question:
        return "question_control"
    widget = control.widget
    actions = list(chain) if chain is not None else [_default_action(widget, control.kind)]
    if _flagged(widget, RISKY_PROPERTY) or _actions_risky(actions, named=False):
        return "risky_command"
    if script or not click or control.kind != "button":
        return None
    if chain is not None:  # (a menu's entry: the entry and its menus name the command, not the button)
        return "risky_command" if _actions_risky(chain) else None
    named = [_try(lambda: _prop(widget, "text")), _try(lambda: _prop(widget, "toolTip")),
             _try(lambda: _prop(widget, "accessibleName")), control.name]
    return "risky_command" if _risky_named(*named) or _actions_risky(actions) else None


@op("window_controls")
def op_window_controls(listener, args):
    """A window's controls (shown ones, unless include_hidden), each with its address, a page at a time."""
    QtGui = _qt()[1]
    modal = QtGui.QApplication.instance().activeModalWidget()
    entry = _find_window(QtGui, args.get("window"), modal)
    include_hidden = _bool_arg(args, "include_hidden")
    offset = _int_arg(args, "offset", 0, 0, 10 ** 7)
    limit = _int_arg(args, "limit", DEFAULT_CONTROLS, 1, MAX_CONTROLS)
    kinds = args.get("kinds")
    if kinds is not None and (not isinstance(kinds, list) or not all(k in CONTROL_KINDS for k in kinds)):
        raise FreeLabError("bad_args", u"kinds must be a list of: %s." % u", ".join(CONTROL_KINDS))
    waiting = modal is not None and _same(entry["widget"], modal)
    script = _script_window(entry["widget"], waiting)
    question = waiting and not script
    controls, truncated = _controls(entry["widget"])
    listed = []
    for control in controls:
        if kinds and control.kind not in kinds:
            continue
        if not include_hidden and (not _shown(control) or (
                control.kind == "label" and not _try(lambda: _label_text(control.widget), u""))):
            continue
        listed.append(control)
    page = listed[offset:offset + limit]
    reply = {"window": entry["key"], "title": entry["title"], "class": entry["class"], "question": question,
             "script_window": script, "count": len(listed), "offset": offset, "more": offset + len(page) < len(listed),
             "controls": [_describe_control(control, question, script) for control in page]}
    if truncated:
        reply["walk_truncated"] = True
    reply.update(_window_busy(entry["widget"], entry["main"], QtGui))
    return reply


def _read_target(args, kinds):
    QtGui = _qt()[1]
    entry = _find_window(QtGui, args.get("window"), QtGui.QApplication.instance().activeModalWidget())
    control = _find_control(_controls(entry["widget"])[0], args.get("control"), entry["key"])
    if control.kind not in kinds:
        raise FreeLabError("wrong_kind", u"'%s' is a %s; this reads a %s." % (control.address, control.kind,
                                                                              u", ".join(kinds)))
    return entry, control


def _cell(value):
    """A model's value as JSON: text, a number, a bool, or None (an icon, a color, ...)."""
    if value is None or isinstance(value, bool) or isinstance(value, integer_types + (float,)):
        return to_jsonable(value)
    if isinstance(value, string_types + (bytes,)):
        return _text(value)
    return None


def _model_rows(model, kind, QtCore, start, count, count_all=True):
    """([(row number, parent index, row in parent, depth)] of rows start to start + count, the rows counted,
    whether the count stopped at MAX_TREE_ROWS). A tree's rows go depth first, its children right after each
    row, collapsed or not; other views' rows are their model's top level."""
    root = QtCore.QModelIndex()
    if kind != "tree":
        total = int(model.rowCount(root))
        return [(n, root, n, 0) for n in range(start, min(total, start + count))], total, False
    rows, counter, stopped = [], [0], [False]

    def walk(parent, depth):
        for row in _lazy_range(int(model.rowCount(parent))):  # (Python 2.7: never a list of every row)
            if counter[0] >= MAX_TREE_ROWS or (not count_all and counter[0] >= start + count):
                stopped[0] = True
                return
            number = counter[0]
            counter[0] += 1
            if start <= number < start + count:
                rows.append((number, parent, row, depth))
            if depth < MAX_DEPTH:
                child = model.index(row, 0, parent)
                if int(model.rowCount(child)) > 0:
                    walk(child, depth + 1)
                    if stopped[0]:
                        return
    walk(root, 0)
    return rows, counter[0], stopped[0]


def _row_texts(model, parent, row, columns):
    return [_cell(model.data(model.index(row, column, parent), DISPLAY_ROLE)) for column in columns]


@op("read_list")
def op_read_list(listener, args):
    """Rows of a list, tree, or table: each one's text per column, check state, selection, and, in a tree,
    its depth, child count, and whether it is expanded; a page at a time."""
    entry, control = _read_target(args, ROW_KINDS)
    offset = _int_arg(args, "offset", 0, 0, 10 ** 7)
    limit = _int_arg(args, "limit", DEFAULT_ROWS, 1, MAX_ROWS)
    QtCore = _qt()[0]
    view = control.widget
    model = view.model()
    available = int(model.columnCount(QtCore.QModelIndex()))
    columns = args.get("columns")
    if columns is None:
        columns = list(range(min(available, MAX_COLUMNS)))
    elif not isinstance(columns, list) or not columns or len(columns) > MAX_COLUMNS or not all(
            isinstance(c, integer_types) and not isinstance(c, bool) and 0 <= c < available for c in columns):
        raise FreeLabError("bad_args", u"columns must be a list of column numbers from 0 to %d." % (available - 1))
    rows, total, stopped = _model_rows(model, control.kind, QtCore, offset, limit)
    selection = _try(lambda: view.selectionModel())
    listed = []
    for number, parent, row, depth in rows:
        index = model.index(row, 0, parent)
        item = {"row": number, "texts": _row_texts(model, parent, row, columns)}
        check = _try(lambda: model.data(index, CHECK_STATE_ROLE))
        if isinstance(check, integer_types) and not isinstance(check, bool):
            item["checked"] = {0: False, 2: True}.get(int(check), u"partial")
        if selection is not None:
            item["selected"] = bool(_try(lambda: selection.isSelected(index), False))
        if control.kind == "tree":
            item["depth"] = depth
            children = int(model.rowCount(index))
            if children:
                item["children"] = children
                item["expanded"] = bool(_try(lambda: view.isExpanded(index), False))
        listed.append(item)
    reply = {"window": entry["key"], "control": control.address, "kind": control.kind,
             "columns": [_cell(model.headerData(c, HORIZONTAL, DISPLAY_ROLE)) for c in columns],
             "column_numbers": columns, "rows": listed, "offset": offset, "total": total,
             "more": offset + len(listed) < total}
    if stopped:
        reply["total_counted_to"] = MAX_TREE_ROWS
    return reply


def _links(html):
    """[{"href", "text"}] of the links in rich text, in order."""
    found = []
    for match in _LINK.finditer(_text(html)):
        href = _unescape(match.group(1) if match.group(1) is not None else match.group(2))
        found.append({"href": href, "text": u" ".join(_plain(match.group(3)).split())})
        if len(found) >= MAX_LINKS:
            break
    return found


@op("read_text")
def op_read_text(listener, args):
    """A label's, field's, text box's, or log's whole text (plain), from offset, with the links it holds."""
    entry, control = _read_target(args, ("label", "field", "text", "log", "button", "check", "radio", "combo"))
    offset = _int_arg(args, "offset", 0, 0, 10 ** 9)
    limit = _int_arg(args, "max_chars", DEFAULT_TEXT_CHARS, 1, MAX_TEXT_CHARS)
    widget, kind, links = control.widget, control.kind, None
    if kind == "label":
        raw = _text(_prop(widget, "text") or u"")
        text = _label_text(widget)
        links = _links(raw) if u"<a " in raw.lower() else []
    elif kind in ("text", "log"):
        text = _text(widget.toPlainText())
        if kind == "log":
            links = _links(widget.toHtml())
    elif kind == "combo":
        text = _text(_prop(widget, "currentText") or u"")
    elif kind == "field":
        text = _text(_prop(widget, "text") or u"")
    else:
        text = _MNEMONIC.sub(r"\1", _text(_prop(widget, "text") or u""))
    start = min(offset, len(text))
    reply = {"window": entry["key"], "control": control.address, "kind": kind, "text": text[start:start + limit],
             "start": start, "length": len(text), "truncated": len(text) - start > limit}
    if links is not None:
        reply["links"] = links
        if len(links) >= MAX_LINKS:
            reply["links_truncated"] = True
    return reply


# -- actions -------------------------------------------------------------------

class _Plan(object):
    """An action checked and ready: what it acts on (fingerprint, compared again when its timer fires), what
    the reply says of it, and perform(), which does it and returns what changed."""

    def __init__(self, window, fingerprint, about, perform, target=None, row=None):
        self.window, self.fingerprint, self.about, self.perform = window, fingerprint, about, perform
        self.target = target  # the widget or menu command acted on: the very one again when the timer fires
        self.row = row  # a row acted on: (its model, persistent indexes of it and its parents, its path)

    def same_row(self, again):
        """Whether the row again resolves is the very row this plan found (not one whose text looks the same):
        the same model, and the row this plan found, followed where it moved, is where again found its row."""
        if self.row is None or again.row is None:
            return self.row is None and again.row is None
        model, persistent, path = self.row
        if not _same(model, again.row[0]) or path != again.row[2]:
            return False
        if persistent is None:  # (a Qt without QPersistentModelIndex: the path and the texts alone)
            return True
        try:
            return all(p.isValid() for p in persistent) and [int(p.row()) for p in persistent] == again.row[2]
        except Exception:
            return False


def _refuse_while_waiting(listener, QtGui, modal, waiting, own):
    """Actions wait for one another and for the windows' work: while a dialog waits, only it can be driven
    (Qt itself lets no input reach another window then); while an earlier action is queued or runs, or a window
    works, nothing else is done (a script's work reads the fonts as they are, and blocks input meanwhile)."""
    if waiting:
        return
    if modal is not None:
        about = _describe_dialog(modal)
        raise FreeLabError("dialog_open", u"A dialog is waiting (%s): only it can be driven until it is answered "
                                          u"(window \"dialog\"). Nothing was done." % _dialog_words(about), {"dialog": about})
    pending = _pending_actions(listener, own)
    if pending:
        raise FreeLabError("busy", u"An earlier action (%d, %s) is still %s: wait_idle waits until it is over. Nothing "
                                   u"was done." % (pending[0]["id"], pending[0]["tool"], pending[0]["state"]))
    for entry in _window_entries(QtGui):
        busy = _window_busy(entry["widget"], entry["main"], QtGui)
        if busy["busy"]:
            raise FreeLabError("busy", u"The window '%s' is busy%s: wait_idle waits until its work is over. Nothing "
                                       u"was done." % (entry["key"], u" (%s)" % busy["step"] if busy.get("step") else u""),
                               {"window": entry["key"]})


def _dialog_words(about):
    return u"%s; buttons: %s" % (u" ".join(about.get("text") or []) or about.get("title") or about.get("class"),
                                 u", ".join(about.get("buttons") or []))


def _action_window(listener, args, own):
    """(window entry, whether it is a question of FontLab's, whether it is a script's own window, the fingerprint of
    the window), refused while something else waits or works (_refuse_while_waiting)."""
    QtGui = _qt()[1]
    modal = QtGui.QApplication.instance().activeModalWidget()
    entry = _find_window(QtGui, args.get("window"), modal)
    waiting = modal is not None and _same(entry["widget"], modal)
    script = _script_window(entry["widget"], waiting)
    question = waiting and not script
    _refuse_while_waiting(listener, QtGui, modal, waiting, own)
    fingerprint = (entry["key"], entry["class"], entry["title"])
    if question:  # (that very question, as answer_dialog: another font's Save question is not it)
        fingerprint += (json.dumps(_describe_dialog(entry["widget"]), sort_keys=True),)
    return entry, question, script, fingerprint


def _action_control(entry, args, kinds):
    control = _find_control(_controls(entry["widget"])[0], args.get("control"), entry["key"])
    if control.kind not in kinds:
        raise FreeLabError("wrong_kind", u"'%s' is a %s, which this does not take (it takes a %s)." % (
            control.address, control.kind, u", ".join(kinds)))
    if not _shown(control):
        raise FreeLabError("unavailable", u"'%s' is hidden (on another tab, or folded away): nothing was done." % control.address)
    if not _try(lambda: _prop(control.widget, "enabled"), False):
        raise FreeLabError("disabled", u"'%s' is disabled: nothing was done." % control.address)
    return control


def _require_for(args, risk, what):
    if not risk:
        return
    try:
        _require_risky(args, risk)
    except FreeLabError as exc:
        exc.message = u"%s %s" % (what, exc.message)
        raise


def _start_action(listener, args, tool, plan):
    """Checks an action (plan), then runs it from a timer a moment after the reply, checked again then: what it
    acts on must be as it was (the same window, control, and, for a question, the very question), and nothing
    else may wait or work. action_result says what became of it."""
    first = plan(listener, args, None)
    _check_deadline(listener)
    record = listener.new_action(tool, first.window, first.about)

    def run():
        # A record left queued is the only one that may run: one reported lost (its timer late past
        # ACTION_LOST_S) stays skipped, whatever its timer does later, so nothing said not done is done.
        if record["state"] != "queued":
            return
        if listener.stopped:
            record.update({"state": "skipped", "code": "listener_stopped", "reason": u"The listener stopped."})
            return
        try:
            again = plan(listener, args, record["id"])
            if again.fingerprint != first.fingerprint or not _same(first.target, again.target) or \
                    not first.same_row(again):
                raise FreeLabError("changed", u"By the time of the action, what it was to act on had changed (another "
                                              u"window, control, command, row, or question): nothing was done.")
        except FreeLabError as exc:
            record.update({"state": "skipped", "code": exc.code, "reason": exc.message})
            return
        except BaseException as exc:  # (SystemExit too: the record ends, then the timer's own guard records it)
            record.update({"state": "skipped", "code": "exception", "reason": _exc_text(exc)})
            if not isinstance(exc, Exception):
                raise
            return
        if record["state"] != "queued":  # (lost while it was checked again)
            return
        record["state"] = "running"
        started = time.time()
        try:
            result = again.perform()
            record["state"] = "done"
            if result:
                record["result"] = to_jsonable(result)
        except BaseException as exc:  # (a slot's SystemExit must not leave the action running for good)
            record.update({"state": "failed", "error": _exc_text(exc)})
            if not isinstance(exc, Exception):
                raise
        finally:
            record["seconds"] = round(time.time() - started, 3)
    listener.later(ACTION_DELAY_MS, run, "window_action")
    reply = {"action_id": record["id"], "queued": True, "window": first.window}
    reply.update(first.about)
    return reply


def _row_target(view, kind, args):
    """(model, parent index, row in parent, the row's texts) of the row args["row"] numbers (as read_list does)."""
    QtCore = _qt()[0]
    number = _int_arg(args, "row", None, 0, MAX_TREE_ROWS)
    model = view.model()
    rows = _model_rows(model, kind, QtCore, number, 1, count_all=False)[0]
    if not rows:
        raise FreeLabError("not_found", u"There is no row %d (read_list numbers the rows)." % number)
    _number_, parent, row, _depth = rows[0]
    columns = list(range(min(int(model.columnCount(QtCore.QModelIndex())), MAX_COLUMNS)))
    return model, parent, row, _row_texts(model, parent, row, columns)


ITEM_ENABLED = 32  # Qt::ItemIsEnabled


def _row_checked(model, index, number, address, checkable=False):
    """Refuses a row a user could not use: an invalid one, a disabled one (no ItemIsEnabled), and, to tick, one
    with no check box."""
    if not _try(lambda: index.isValid(), True):
        raise FreeLabError("not_found", u"Row %s of '%s' is gone." % (number, address))
    flags = int(model.flags(index))
    if not flags & ITEM_ENABLED:
        raise FreeLabError("disabled", u"Row %s of '%s' is disabled: nothing was done." % (number, address))
    if checkable and not flags & USER_CHECKABLE:
        raise FreeLabError("wrong_kind", u"Row %s of '%s' has no check box." % (number, address))


def _row_binding(model, index):
    """(model, persistent indexes of the row and of each row holding it, outermost first, or None where Qt has no
    QPersistentModelIndex, the row's path: its number under each parent): what _Plan.same_row compares."""
    chain, path = [], []
    for _ in range(MAX_DEPTH):
        if index is None or not _try(lambda: index.isValid(), False):
            break
        chain.append(index)
        path.append(int(index.row()))
        index = _try(lambda: index.parent())
    chain.reverse()
    path.reverse()
    make = getattr(_qt()[0], "QPersistentModelIndex", None)
    persistent = None
    if make is not None:
        try:
            persistent = [make(each) for each in chain]
        except Exception:
            persistent = None
    return model, persistent, path


def _plan_click(listener, args, own):
    entry, question, script, fingerprint = _action_window(listener, args, own)
    control = _action_control(entry, args, ("button", "check", "radio") + ROW_KINDS)
    widget, kind = control.widget, control.kind
    item = args.get("entry") or u""
    if not isinstance(item, string_types):
        raise FreeLabError("bad_args", "entry must be a menu entry's text (submenus joined by \">\").")
    about = {"control": control.address, "kind": kind}
    fingerprint += (control.address, kind)
    if kind in ROW_KINDS:
        if item:
            raise FreeLabError("bad_args", "entry is for a button's menu; a list's row is given by row.")
        double = _bool_arg(args, "double")
        model, parent, row, texts = _row_target(widget, kind, args)
        column = _int_arg(args, "column", 0, 0, max(0, int(model.columnCount(parent)) - 1))
        index = model.index(row, column, parent)
        _row_checked(model, index, args.get("row"), control.address)
        _require_for(args, _control_risk(control, question, script), u"'%s' is a control of a question FontLab asks, or one "
                                                                      u"marked risky." % control.address)
        about.update({"row": args.get("row"), "texts": texts, "double": double})

        def perform_row():
            widget.setCurrentIndex(index)  # (selects it, as the press of a click does)
            widget.clicked(index)  # (PythonQt emits a signal called like a slot; a tree's itemClicked follows)
            if double:
                widget.doubleClicked(index)
                widget.activated(index)  # (Windows activates an item on a double click)
            return {"row": args.get("row"), "texts": texts, "double": double}
        return _Plan(entry["key"], fingerprint + (tuple(texts), column, double), about, perform_row, widget,
                     _row_binding(model, index))
    if args.get("row") is not None:
        raise FreeLabError("bad_args", "row is for a list, tree, or table.")
    if item:
        menu = _button_menu(widget) if kind == "button" else None
        if menu is None:
            raise FreeLabError("wrong_kind", u"'%s' has no menu: click it without entry." % control.address)
        action, trail, chain = _menu_at(menu, item, control.address)
        if action.menu() is not None:
            raise FreeLabError("wrong_kind", u"%s is a menu: give one of its entries." % u" > ".join(trail))
        if not _prop(action, "enabled"):
            raise FreeLabError("disabled", u"%s > %s is disabled: nothing was done." % (control.address, u" > ".join(trail)))
        risk = _control_risk(control, question, script, click=True, chain=chain)
        _require_for(args, risk, u"'%s' > %s is a command that may write files or throw work away, or a control of a "
                                 u"question FontLab asks." % (control.address, u" > ".join(trail)))
        about["entry"] = u" > ".join(trail)

        def perform_entry():
            action.trigger()
            return {"entry": u" > ".join(trail)}
        return _Plan(entry["key"], fingerprint + (tuple(trail),), about, perform_entry, action)
    menu = _button_menu(widget) if kind == "button" else None
    if _needs_entry(widget, menu):
        raise FreeLabError("wrong_kind", u"A click on '%s' only opens its menu: give entry, one of: %s." % (
            control.address, u", ".join(text for _a, text in _menu_entries(menu))))
    risk = _control_risk(control, question, script, click=True)
    _require_for(args, risk, u"'%s' is a command that may write files or throw work away (by its name, or marked so), "
                             u"or a control of a question FontLab asks." % control.address)

    def perform():
        widget.click()  # (pressed, released, clicked, and toggled, as a user's click)
        result = {"clicked": control.address}
        if kind in ("check", "radio") or _try(lambda: _prop(widget, "checkable"), False):
            result["checked"] = bool(_try(lambda: _prop(widget, "checked"), False))
        return result
    # What the click means is the button's text and, for a tool button, the action it triggers: a named button
    # that turned from Start to Stop, or was given another action, by the time of the click is not clicked
    meaning = (_text(_try(lambda: _prop(widget, "text")) or u""),)
    return _Plan(entry["key"], fingerprint + meaning, about, perform, (widget, _default_action(widget, kind)))


@op("click")
def op_click(listener, args):
    """Clicks a button, check box, or radio button (or a button's menu entry, or a row of a list), as a user's
    click does, from a timer a moment after the reply. Refused for a hidden or disabled control, while
    something else waits or works, and without risky mode for a risky one (_control_risk)."""
    return _start_action(listener, args, "click", _plan_click)


def _truth(value):
    if isinstance(value, bool):
        return value
    words = {u"true": True, u"on": True, u"yes": True, u"checked": True, u"false": False, u"off": False, u"no": False,
             u"unchecked": False}
    if isinstance(value, string_types) and _text(value).strip().lower() in words:
        return words[_text(value).strip().lower()]
    raise FreeLabError("bad_args", "value must be true or false here.")


def _plan_set_value(listener, args, own):
    entry, question, script, fingerprint = _action_window(listener, args, own)
    control = _action_control(entry, args, ("check", "radio", "combo", "spin", "field", "text", "tabs", "slider") + ROW_KINDS)
    widget, kind = control.widget, control.kind
    _require_for(args, _control_risk(control, question, script), u"'%s' is a control of a question FontLab asks, or one "
                                                                  u"marked risky." % control.address)
    if "value" not in args:
        raise FreeLabError("bad_args", "value is missing.")
    value = args.get("value")
    about = {"control": control.address, "kind": kind}
    fingerprint += (control.address, kind)
    if kind in ROW_KINDS:
        wanted = _truth(value)
        model, parent, row, texts = _row_target(widget, kind, args)
        index = model.index(row, 0, parent)
        _row_checked(model, index, args.get("row"), control.address, checkable=True)
        about.update({"row": args.get("row"), "texts": texts, "value": wanted})

        def perform_row():
            before = _try(lambda: model.data(index, CHECK_STATE_ROLE))
            kept = model.setData(index, 2 if wanted else 0, CHECK_STATE_ROLE)  # (as a user's tick: itemChanged)
            after = _try(lambda: model.data(index, CHECK_STATE_ROLE))
            if kept is False or _try(lambda: int(after)) != (2 if wanted else 0):
                raise FreeLabError("not_kept", u"Row %s of '%s' refused the tick (it reads %r)." % (
                    args.get("row"), control.address, after))
            return {"row": args.get("row"), "before": before, "after": after}
        return _Plan(entry["key"], fingerprint + (tuple(texts), wanted), about, perform_row, widget,
                     _row_binding(model, index))
    if args.get("row") is not None:
        raise FreeLabError("bad_args", "row is for a list, tree, or table.")
    if kind in ("check", "radio"):
        wanted = _truth(value)
        about["value"] = wanted
        fingerprint += (wanted,)
    if kind == "check":
        tristate = bool(_try(lambda: _prop(widget, "tristate"), False))

        def state():
            return int(widget.checkState()) if tristate else (2 if _prop(widget, "checked") else 0)

        def perform():
            before = state()
            for _ in range(3):  # (a box of three states goes through them in turn, as clicks do)
                if state() == (2 if wanted else 0):
                    break
                widget.click()
            return {"before": before == 2 if before != 1 else u"partial", "after": state() == 2}
        perform_value = perform
    elif kind == "radio":
        if not wanted:
            raise FreeLabError("bad_args", "A radio button is turned off by choosing another: give true.")

        def perform_value():
            before = bool(_prop(widget, "checked"))
            if not before:
                widget.click()
            return {"before": before, "after": bool(_prop(widget, "checked"))}
    elif kind == "combo":
        count = int(_prop(widget, "count"))
        texts = [_text(widget.itemText(i)) for i in range(count)]
        if isinstance(value, bool):
            raise FreeLabError("bad_args", "value must be the entry's text (or its number).")
        if isinstance(value, integer_types):
            if not 0 <= value < count:
                raise FreeLabError("bad_args", u"'%s' has %d entries: value must be from 0 to %d." % (control.address, count, count - 1))
            index = value
        else:
            matches = [i for i, text in enumerate(texts) if _entry_words(text) == _entry_words(value)]
            if not matches:
                raise FreeLabError("not_found", u"'%s' has no entry '%s'; its entries: %s." % (
                    control.address, _text(value), u", ".join(texts[:MAX_CHOICES])))
            index = matches[0]
        about["value"] = texts[index]
        fingerprint += (index, texts[index])

        def perform_value():
            before = _text(_prop(widget, "currentText") or u"")
            widget.setCurrentIndex(index)  # (currentIndexChanged, if it changed)
            widget.activated(index)  # (what a user's choice emits besides; PythonQt emits a signal called like a slot)
            return {"before": before, "after": _text(_prop(widget, "currentText") or u"")}
    elif kind in ("spin", "slider"):
        if kind == "spin" and _try(lambda: _prop(widget, "readOnly"), False):
            raise FreeLabError("unavailable", u"'%s' is read-only: nothing was done." % control.address)
        if kind == "spin" and not (widget.inherits("QSpinBox") or widget.inherits("QDoubleSpinBox")):
            raise FreeLabError("wrong_kind", u"'%s' is a spin box of another kind (a date, a time): not set." % control.address)
        try:
            number = _number_value(value)
        except ValueError:
            raise FreeLabError("bad_args", "value must be a number here.")
        whole = not widget.inherits("QDoubleSpinBox")
        if whole and number != int(number):
            raise FreeLabError("bad_args", u"'%s' takes whole numbers." % control.address)
        low, high = float(_prop(widget, "minimum")), float(_prop(widget, "maximum"))
        if not low <= number <= high:
            raise FreeLabError("bad_args", u"'%s' takes %g to %g." % (control.address, low, high))
        number = int(number) if whole else number
        about["value"] = number
        fingerprint += (number,)

        def perform_value():
            before = _prop(widget, "value")
            widget.setValue(number)  # (valueChanged, if it changed)
            if kind == "spin":
                widget.editingFinished()  # (as a user's typing ends)
            return {"before": before, "after": _prop(widget, "value")}
    elif kind in ("field", "text"):
        if _try(lambda: _prop(widget, "readOnly"), False):
            raise FreeLabError("unavailable", u"'%s' is read-only: nothing was done." % control.address)
        if isinstance(value, bool) or not isinstance(value, string_types + integer_types + (float,)):
            raise FreeLabError("bad_args", "value must be the text to put in.")
        text = _text(value)
        about["value"] = _shorten(text, SHORT_TEXT)
        fingerprint += (text,)

        def perform_value():
            if kind == "field":
                before = _text(_prop(widget, "text") or u"")
                widget.setText(text)  # (textChanged)
                widget.textEdited(text)  # (what typing emits besides)
                widget.editingFinished()
                return {"before": _shorten(before, SHORT_TEXT), "after": _shorten(_text(_prop(widget, "text") or u""), SHORT_TEXT)}
            widget.setPlainText(text)  # (textChanged)
            return {"length": len(_text(widget.toPlainText()))}
    else:  # tabs
        count = int(_prop(widget, "count"))
        texts = [_text(widget.tabText(i)) for i in range(count)]
        if isinstance(value, bool):
            raise FreeLabError("bad_args", "value must be the tab's text (or its number).")
        if isinstance(value, integer_types):
            if not 0 <= value < count:
                raise FreeLabError("bad_args", u"'%s' has %d tabs: value must be from 0 to %d." % (control.address, count, count - 1))
            index = value
        else:
            matches = [i for i, text in enumerate(texts) if _entry_words(text) == _entry_words(value)]
            if not matches:
                raise FreeLabError("not_found", u"'%s' has no tab '%s'; its tabs: %s." % (
                    control.address, _text(value), u", ".join(_caption(t, SHORT_TEXT) for t in texts)))
            index = matches[0]
        if not widget.isTabEnabled(index):
            raise FreeLabError("disabled", u"The tab '%s' of '%s' is disabled: nothing was done." % (texts[index], control.address))
        about["value"] = _caption(texts[index], SHORT_TEXT)
        fingerprint += (index, texts[index])

        def perform_value():
            before = int(_prop(widget, "currentIndex"))
            widget.setCurrentIndex(index)  # (currentChanged, as a click on the tab)
            return {"before": before, "after": int(_prop(widget, "currentIndex"))}
    return _Plan(entry["key"], fingerprint, about, perform_value, widget)


@op("set_value")
def op_set_value(listener, args):
    """Sets a control as a user's edit does, from a timer a moment after the reply: ticks or unticks a check
    box (or a row's), chooses a radio button, a dropdown's entry (by its text), or a tab, sets a spin box,
    slider, field, or text box; and emits what the user's edit would besides (activated, editingFinished,
    textEdited)."""
    return _start_action(listener, args, "set_value", _plan_set_value)


def _plan_link(listener, args, own):
    entry, question, script, fingerprint = _action_window(listener, args, own)
    control = _action_control(entry, args, ("label", "log"))
    widget = control.widget
    href, text = args.get("href") or u"", args.get("text") or u""
    if not isinstance(href, string_types) or not isinstance(text, string_types) or not (href.strip() or text.strip()):
        raise FreeLabError("bad_args", "Give the link's href, or its text, as read_text lists them.")
    if control.kind == "label":
        if _try(lambda: _prop(widget, "openExternalLinks"), False):
            raise FreeLabError("unavailable", u"'%s' opens its links in a web browser: FreeLab MCP never opens one." % control.address)
        links = _links(_text(_prop(widget, "text") or u""))
    else:
        links = _links(widget.toHtml())
    if href.strip():
        found = [link for link in links if link["href"] == _text(href).strip()]
    else:
        found = [link for link in links if u" ".join(link["text"].split()).lower() == u" ".join(_text(text).split()).lower()]
    if not found:
        raise FreeLabError("not_found", u"'%s' has no link %s; its links: %s." % (
            control.address, u"to '%s'" % href if href.strip() else u"'%s'" % text,
            u"; ".join(u"%s (%s)" % (link["text"], link["href"]) for link in links[:20]) or u"none"))
    if len(set(link["href"] for link in found)) > 1:
        raise FreeLabError("ambiguous_control", u"%d links of '%s' read '%s', to %s: give href." % (
            len(found), control.address, text, u", ".join(sorted(set(link["href"] for link in found))[:10])))
    target = found[0]["href"]
    if EXTERNAL_LINK.match(target):
        raise FreeLabError("unavailable", u"The link goes to %s, outside FontLab: FreeLab MCP never opens one." % target)
    _require_for(args, _control_risk(control, question, script), u"'%s' is a control of a question FontLab asks, or one "
                                                                  u"marked risky." % control.address)
    QtCore = _qt()[0]

    def perform():
        if control.kind == "log":
            widget.anchorClicked(QtCore.QUrl(target))  # (what a click on the link emits; the view is not made to follow it)
        else:
            widget.linkActivated(target)
        return {"link": target}
    return _Plan(entry["key"], fingerprint + (control.address, target), {"control": control.address, "link": target},
                 perform, widget)


@op("click_link")
def op_click_link(listener, args):
    """Follows a link of a log (a text browser) or a label as a click does (anchorClicked, linkActivated), from a
    timer a moment after the reply. A link out of FontLab (a web page, a mail) is never followed."""
    return _start_action(listener, args, "click_link", _plan_link)


def _menu_bar(QtGui):
    main = _main_window(QtGui)
    bars = _real_list(main.findChildren(QtGui.QMenuBar), "findChildren()")
    for bar in bars:
        if _same(_try(lambda: bar.parentWidget()), main):
            return bar
    if not bars:
        raise FreeLabError("not_found", "FontLab's menu bar was not found.")
    return bars[0]


def _menu_risk(chain, trail):
    """A menu command is risky when it is in FontLab's File menu, or it or a menu it is in (chain: their actions,
    outermost first) is named like Save, Export, Close, Quit, Exit, Revert, or Discard (in its text, tooltip, or
    objectName) or marked mcpRisky."""
    if _entry_words(trail[0]) == u"file" or _actions_risky(chain):
        return "risky_command"
    return None


@op("menu")
def op_menu(listener, args):
    """The main window's menus (path ""), or the entries of the menu path names: each one's text, whether it is a
    menu or a command, enabled, checked, and whether running it needs risky mode. A menu FontLab fills only as
    it opens may list nothing until it has been opened once."""
    QtGui = _qt()[1]
    path = args.get("path") or u""
    if not isinstance(path, string_types):
        raise FreeLabError("bad_args", "path must be a menu's path, such as Scripts > FreeLab.")
    bar = _menu_bar(QtGui)
    trail, chain, level = [], [], bar
    if path.strip():
        action, trail, chain = _menu_at(bar, path, u"The menu bar")
        level = action.menu()
        if level is None:
            raise FreeLabError("wrong_kind", u"%s is a command, not a menu: run_menu_command runs it." % u" > ".join(trail))
    entries = []
    for action, text in _menu_entries(level):
        submenu = _try(lambda: action.menu())
        item = {"text": text, "kind": "menu" if submenu is not None else "command",
                "enabled": bool(_try(lambda: _prop(action, "enabled"), False))}
        if submenu is None:
            if _try(lambda: _prop(action, "checkable"), False):
                item["checked"] = bool(_try(lambda: _prop(action, "checked"), False))
            if _menu_risk(chain + [action], trail + [text]):
                item["risky"] = True
        entries.append(item)
    reply = {"path": u" > ".join(trail), "entries": entries}
    if trail and not entries:
        reply["note"] = u"This menu lists no entry: FontLab may fill it only as it opens (open it once on screen)."
    return reply


def _plan_menu(listener, args, own):
    QtGui = _qt()[1]
    _refuse_while_waiting(listener, QtGui, QtGui.QApplication.instance().activeModalWidget(), False, own)
    path = args.get("path")
    if not isinstance(path, string_types) or not path.strip():
        raise FreeLabError("bad_args", "path must be a menu command's path, such as Scripts > FreeLab > Font Check.")
    action, trail, chain = _menu_at(_menu_bar(QtGui), path, u"The menu bar")
    command = u" > ".join(trail)
    if action.menu() is not None:
        raise FreeLabError("wrong_kind", u"%s is a menu: list_menu lists its entries." % command)
    if not _prop(action, "enabled"):
        raise FreeLabError("disabled", u"%s is disabled: nothing was done." % command)
    _require_for(args, _menu_risk(chain, trail), u"%s is in FontLab's File menu, named like a command that may write "
                                                  u"files or throw work away, or marked risky." % command)

    def perform():
        action.trigger()  # (as the menu's click: FontLab runs the command, or the script, now)
        result = {"command": command}
        if _try(lambda: _prop(action, "checkable"), False):
            result["checked"] = bool(_try(lambda: _prop(action, "checked"), False))
        return result
    return _Plan(u"main", ("menu", tuple(trail), _object_name(action)), {"command": command}, perform, action)


@op("run_menu_command")
def op_run_menu_command(listener, args):
    """Runs a command of the main window's menus by its path (Scripts > FreeLab > Font Check), as a click on it
    does, from a timer a moment after the reply. Refused while a dialog waits, an action runs, or a window works,
    and without risky mode for a risky command (_menu_risk)."""
    return _start_action(listener, args, "run_menu_command", _plan_menu)


@op("action_result")
def op_action_result(listener, args):
    """What became of an action: state queued, running, done (with what changed), skipped (checked again when
    its timer fired, it was not done: code and reason), or failed (it raised: error)."""
    action_id = _int_arg(args, "action_id", None, 1, 10 ** 9)
    if action_id not in listener.actions:
        raise FreeLabError("not_found", "No action of that id in this listener.")
    _pending_actions(listener)  # (a lost one marked skipped)
    return {"action": dict(listener.actions[action_id])}


@op("window_state")
def op_window_state(listener, args):
    """What wait_idle polls: the dialog waiting (if any), the windows that work, the listener's actions queued
    or running and the last one, and, for window, whether it is still shown and busy. Reads only."""
    QtGui = _qt()[1]
    modal = QtGui.QApplication.instance().activeModalWidget()
    reply = {"dialog": None, "question": False, "busy_windows": []}
    entries = _window_entries(QtGui)
    if modal is not None:
        reply["dialog"] = _describe_dialog(modal)
        reply["question"] = not _script_window(modal, True)
        keys = [entry["key"] for entry in entries if _same(entry["widget"], modal)]
        reply["dialog_window"] = keys[0] if keys else u"dialog"
    for entry in entries:
        busy = _window_busy(entry["widget"], entry["main"], QtGui)
        if busy["busy"]:
            busy["window"] = entry["key"]
            reply["busy_windows"].append(busy)
    wanted = args.get("window") or u""
    if wanted:
        try:
            reply["window"] = _describe_window(_find_window(QtGui, wanted, modal), modal, QtGui, listener)
        except FreeLabError as exc:
            if exc.code != "not_found":
                raise
            reply["window"] = None
    pending = _pending_actions(listener)
    reply["actions_pending"] = [{"id": r["id"], "tool": r["tool"], "state": r["state"], "window": r.get("window")}
                                for r in pending]
    if listener.actions:
        reply["last_action"] = dict(listener.actions[max(listener.actions)])
    return reply


# ---------------------------------------------------------------------------
# Saving and exporting: risky actions (RISKY_ACTIONS), each run from a timer after the reply
# ---------------------------------------------------------------------------

SAVE_DELAY_MS = 300
EXPORT_DELAY_MS = 300
EXPORT_ANSWER_S = 600.0  # an export of a large font may take minutes before its message shows
SAVE_ACTIONS = ("actionSaveFont",)  # File > Save Font (live, 2026-11-19: text "&Save Font..."; never actionSaveAll)
SAVE_TEXTS = (u"save font", u"save font...", u"save font…")
SAVE_FORMATS = (".ufo", ".vfb")  # fgFont.save writes these (FontLab 7's reference; both live, 2026-11-19)
EXPORT_DONE = "DlgSavePackage"  # FontLab 7.2's Font Exported message after a script's export (live, 2026-11-14 and -18)
EXPORT_BUTTONS = (u"OK", u"Close")


def _save_action():
    """File > Save Font, the main window's QAction: by objectName (live), else by its text."""
    from PythonQt.QtGui import QAction  # only inside FontLab
    actions = _main_window(_qt()[1]).findChildren(QAction)
    for name in SAVE_ACTIONS:
        for action in actions:
            if _text(_prop(action, "objectName")) == name:
                return action
    for action in actions:
        if _button_words(_prop(action, "text")) in SAVE_TEXTS:
            return action
    return None


@op("save_font")
def op_save_font(listener, args):
    """[risky] Saves a font, a moment after the reply: with no path, as File > Save does (the font brought to
    the front; FontLab may ask where, for a font opened from a file it does not save to); with a path, a
    UFO or VFB file written by fgFont.save, refused over an existing file unless overwrite is given."""
    _require_risky(args, "save_font")
    package = write_target(args)
    keep = _bool_arg(args, "keep_in_front")
    overwrite = _bool_arg(args, "overwrite")
    path = args.get("path") or u""
    if not isinstance(path, string_types):
        raise FreeLabError("bad_args", "path must be a full path, or left out to save to the font's own file.")
    path = _text(path).strip()
    target = _font_id(package)
    if path:
        if not os.path.isabs(path):
            raise FreeLabError("bad_args", u"path must be a full path, not '%s'." % path)
        extension = os.path.splitext(path)[1].lower()
        if extension not in SAVE_FORMATS:
            raise FreeLabError("bad_args", u"FontLab's scripting writes a font to a path only as UFO or VFB (fgFont.save); "
                               u"File > Save As asks for its file on screen. Give a .ufo or .vfb path, or no path to "
                               u"save to the font's own file.")
        if not os.path.isdir(os.path.dirname(path)):
            raise FreeLabError("not_found", u"There is no folder %s." % os.path.dirname(path))
        if os.path.exists(path) and not overwrite:
            raise FreeLabError("file_exists", u"%s exists: give overwrite to replace it. Nothing was saved." % path)
        _check_deadline(listener)
        listener.later(SAVE_DELAY_MS, lambda: package.fgPackage.save(path, extension == ".vfb"), "save_font")
        return {"saving": True, "font": _font_ref(package), "path": path, "how": "fgFont.save"}
    action = _save_action()
    if action is None:
        raise FreeLabError("not_found", "FontLab's File > Save was not found; nothing was saved.")
    try:
        own = _text(package.path)
    except Exception:
        own = None
    _check_deadline(listener)
    current = _fontlab().flWorkspace.instance().currentPackage
    previous = None if _is_null(current, "the current font") else current
    moved = bring_to_front(package)

    def save():
        if _current_id() != target:
            return  # something else came to the front meanwhile: not saved (the server says so)
        action.trigger()  # returns once any question FontLab asks on the way is answered
        if moved and not keep and previous is not None:
            bring_to_front(previous)
    listener.later(SAVE_DELAY_MS, save, "save_font")
    return {"saving": True, "font": _font_ref(package), "path": own, "how": u"File > Save (%s)" % _text(
        _prop(action, "objectName")), "brought_forward": moved}


@op("export_font")
def op_export_font(listener, args):
    """[risky] Exports a font into a folder through FontLab's export (flWorkspace.exportFont with an
    ExportControl holding the user's last export settings, the profile and folder set, never stored
    back), a moment after the reply; FontLab names the file. Its Font Exported message is closed."""
    _require_risky(args, "export_font")
    package = write_target(args)
    folder = args.get("folder")
    if not isinstance(folder, string_types) or not folder.strip() or not os.path.isabs(_text(folder).strip()):
        raise FreeLabError("bad_args", "folder must be the full path of an existing folder.")
    folder = _text(folder).strip()
    if not os.path.isdir(folder):
        raise FreeLabError("not_found", u"There is no folder %s." % folder)
    profile = args.get("format") or u""
    if not isinstance(profile, string_types) or len(profile) > 200:
        raise FreeLabError("bad_args", "format must be an export profile's name, such as OpenType TT or OpenType PS.")
    fl = _fontlab()
    control = fl.ExportControl()
    control.load()  # (the user's last export settings; store() would change them, and is never called)
    if profile.strip():
        control.profileName = _text(profile).strip()
    control.destinationMode = 3  # (a folder, as proven live)
    control.destinationFolder = folder.replace(u"\\", u"/").rstrip(u"/") + u"/"
    workspace = fl.flWorkspace.instance()
    _check_deadline(listener)
    progress = {"returned": False}

    def export():
        try:
            workspace.exportFont(package, control)  # (returns once its message is closed)
        finally:
            progress["returned"] = True

    def exporting():  # (once the export returned, only its own message still shown is closed)
        if not progress["returned"]:
            return True
        dialog = _modal_dialog()
        return dialog is not None and dialog.metaObject().className() == EXPORT_DONE
    _keep_answering(listener, "export_font", EXPORT_BUTTONS, time.time() + EXPORT_ANSWER_S,
                    lambda dialog: dialog.metaObject().className() == EXPORT_DONE, keep_going=exporting)
    listener.later(EXPORT_DELAY_MS, export, "export_font")
    try:
        used = _text(control.profileName)
    except Exception:
        used = None
    return {"exporting": True, "font": _font_ref(package), "folder": folder, "format": used}


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


class _Win32Security(object):
    """advapi32 and kernel32 through ctypes, typed for 64-bit handles."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        advapi = self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = ctypes.c_void_p
        advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
        advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ctypes.c_wchar_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        advapi.SetFileSecurityW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, ctypes.c_void_p]

    def failed(self, call):
        return u"%s failed (error %d)" % (call, self.ctypes.get_last_error())

    def descriptor(self):
        """A security descriptor (LocalFree it) with a protected access list of one entry: full access for
        the user this process runs as; or the reason as text."""
        ctypes, wintypes, advapi, kernel = self.ctypes, self.wintypes, self.advapi, self.kernel
        token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return self.failed(u"OpenProcessToken")
        try:
            size = wintypes.DWORD()
            advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            buffer = ctypes.create_string_buffer(max(size.value, 1))
            if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                return self.failed(u"GetTokenInformation")
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # (TOKEN_USER starts with the SID's pointer)
            text = ctypes.c_void_p()
            if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                return self.failed(u"ConvertSidToStringSidW")
            try:
                user = ctypes.wstring_at(text.value)
            finally:
                kernel.LocalFree(text)
        finally:
            kernel.CloseHandle(token)
        descriptor = ctypes.c_void_p()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(u"D:P(A;;FA;;;%s)" % user, 1,
                                                                          ctypes.byref(descriptor), None):
            return self.failed(u"ConvertStringSecurityDescriptorToSecurityDescriptorW")
        return descriptor


def _owner_only(path):
    """Gives an existing file a protected access list with one entry: full access for the user this process
    runs as, so no other account (administrators included) can read it, and nothing is inherited from its
    folder. True when done, the reason as text when not (the file keeps its inherited access then); None
    outside Windows, where the file's 0600 mode does it."""
    if os.name != "nt":
        return None
    try:
        win = _Win32Security()
        descriptor = win.descriptor()
        if not isinstance(descriptor, win.ctypes.c_void_p):
            return descriptor
        try:
            # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
            if not win.advapi.SetFileSecurityW(_text(path), 0x00000004 | 0x80000000, descriptor):
                return win.failed(u"SetFileSecurityW")
        finally:
            win.kernel.LocalFree(descriptor)
        return True
    except Exception as exc:
        return _exc_text(exc)


def _create_private(path):
    """(a text file open for writing, privacy) for a new file at path (refused when one is there). On
    Windows the file is born with the protected one-entry access list (_owner_only's), through CreateFileW
    with it in SECURITY_ATTRIBUTES and no sharing, and written through that handle, so no other account
    ever holds it open (Codex, 2026-11-19): privacy True. When Windows cannot do that, the file is made the
    ordinary way and _owner_only tried, and privacy is the reason it could not. Outside Windows, mode 0600
    and privacy None."""
    if os.name != "nt":
        return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"), None
    reason = None
    try:
        win = _Win32Security()
        descriptor = win.descriptor()
        if isinstance(descriptor, win.ctypes.c_void_p):
            ctypes = win.ctypes

            class SecurityAttributes(ctypes.Structure):
                _fields_ = [("nLength", win.wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                            ("bInheritHandle", win.wintypes.BOOL)]
            try:
                attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
                # GENERIC_WRITE, no sharing, CREATE_NEW, FILE_ATTRIBUTE_NORMAL
                handle = win.kernel.CreateFileW(_text(path), 0x40000000, 0, ctypes.byref(attributes), 1, 0x80, None)
            finally:
                win.kernel.LocalFree(descriptor)
            if handle is None or handle == ctypes.c_void_p(-1).value:
                error = ctypes.get_last_error()
                if error in (80, 183):  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
                    raise OSError(errno.EEXIST, "File exists", path)
                reason = u"CreateFileW failed (error %d)" % error
            else:
                import msvcrt
                try:
                    descriptor_fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
                except Exception:
                    win.kernel.CloseHandle(handle)
                    raise
                return os.fdopen(descriptor_fd, "w"), True
        else:
            reason = descriptor
    except OSError:
        raise
    except Exception as exc:
        reason = _exc_text(exc)
    handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
    fallback = _owner_only(path)
    return handle, (u"%s; the access list was set after the file was made: %s" % (
        reason, u"done" if fallback is True else fallback))


def _loopback(address):
    try:
        host = _text(address[0])
    except Exception:
        return False
    return host in LOOPBACK or host.startswith(u"127.")


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
        self.pending = []  # callbacks waiting on a timer (later): held, since PythonQt drops a slot nothing holds
        self.stopped = False  # stop() ran: answers still on a timer do nothing
        # the fonts open_font opened in this FontLab process, each {"id", "path"} (close_font may discard changes
        # in those without risky mode), kept in a file beside the state file so that a restarted listener still
        # knows them; a font that is no longer open is forgotten (prune_opened)
        self.opened = self._load_opened()
        self.answer_outcomes = {}  # answer_dialog's id -> what became of its click (None: still to come)
        self.answers_made = 0
        self.actions = {}  # the window tools' actions by id: each one's tool, window, state, and outcome
        self.actions_made = 0
        self.state_private = None  # True once the state file is readable by this user only; else why not
        self.bad_tokens = []  # when requests with a wrong token came (BAD_TOKEN_LIMIT)
        self.throttled_until = 0.0
        self.refused_peers = 0  # connections from outside the loopback, closed unanswered

    # -- paths open_font opened --------------------------------------------------
    def opened_file(self):
        return os.path.join(os.path.dirname(self.state_file), "opened.json")

    def _load_opened(self):
        """The fonts open_font opened ({"id", "path"}), when the file was written by this FontLab process (its
        pid): another process's are of fonts this FontLab never opened. Each is bound to a font's id, which
        FontLab does not give again within a process (seen live: ids only grow), and is pruned once that font
        is gone, so a restarted listener grants nothing to a font opened since by hand."""
        try:
            with open(self.opened_file()) as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or data.get("pid") != os.getpid() or not isinstance(data.get("fonts"), list):
                return []
            return [{"id": entry["id"], "path": _text(entry["path"])} for entry in data["fonts"]
                    if isinstance(entry, dict) and isinstance(entry.get("id"), integer_types)
                    and not isinstance(entry.get("id"), bool) and isinstance(entry.get("path"), string_types)]
        except Exception:
            return []

    def adopt_opened(self, path, before):
        """Records the font an open_font call opened, once its requestLoadingFont has returned: the font open now
        that was not before, from that path (or the only new one, when FontLab gives it another path). An open
        that failed or was canceled records nothing."""
        try:
            new = [p for p in open_packages() if _font_id(p) not in before]
            same = [p for p in new if _same_path(_text(p.path), path)]
            chosen = same[:1] or (new if len(new) == 1 else [])
            for package in chosen:
                self.opened.append({"id": _font_id(package), "path": _text(package.path)})
            if chosen:
                self.save_opened()
        except Exception as exc:
            self._record("open_font", exc)

    def prune_opened(self):
        """Forgets the fonts open_font opened that are no longer open, however they were closed."""
        try:
            open_ids = set(_font_id(p) for p in open_packages())
        except Exception:
            return
        kept = [entry for entry in self.opened if entry["id"] in open_ids]
        if len(kept) != len(self.opened):
            self.opened = kept
            self.save_opened()

    def new_action(self, tool, window, about):
        """A window tool's action, queued (see _start_action); the last MAX_ACTIONS_KEPT are kept."""
        self.actions_made += 1
        record = {"id": self.actions_made, "tool": tool, "window": window, "state": "queued", "asked": time.time()}
        record.update(about)
        self.actions[self.actions_made] = record
        for old in [k for k in self.actions if k <= self.actions_made - MAX_ACTIONS_KEPT]:
            del self.actions[old]
        return record

    def new_answer(self):
        self.answers_made += 1
        self.answer_outcomes[self.answers_made] = None
        for old in [k for k in self.answer_outcomes if k <= self.answers_made - 50]:
            del self.answer_outcomes[old]
        return self.answers_made

    def save_opened(self):
        """Writes the fonts open_font opened with this process's pid; whether it was written."""
        target = self.opened_file()
        temp = "%s.%d.tmp" % (target, os.getpid())
        try:
            folder = os.path.dirname(target)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder, 0o700)
            if os.path.exists(temp):
                os.remove(temp)
            handle, _private = _create_private(temp)
            with handle:
                json.dump({"pid": os.getpid(), "fonts": self.opened}, handle)
            if os.path.exists(target):
                os.remove(target)
            os.rename(temp, target)
            return True
        except Exception:
            try:
                if os.path.exists(temp):
                    os.remove(temp)
            except Exception:
                pass
            return False

    # -- work after the reply ------------------------------------------------
    def later(self, delay_ms, func, key):
        """Run func from a single-shot timer, after this request's reply; an error it raises is recorded
        under key (a Qt timer callback must not raise)."""
        from PythonQt.QtCore import QTimer  # only inside FontLab

        def run():
            try:
                func()
            except BaseException as exc:
                self._record(key, exc)
            finally:
                if run in self.pending:
                    self.pending.remove(run)
        self.pending.append(run)
        QTimer.singleShot(delay_ms, run)

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
            # FontLab's Welcome window, modal since the launch when no font was opened, kept FontLab running
            # once the main window had closed (live, 2026-11-19). It holds no work: it is closed first.
            for widget in app.topLevelWidgets():
                if widget.metaObject().className() == WELCOME and widget.isVisible():
                    widget.close()
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
        handle, private = _create_private(temp)  # (born closed to other accounts: Codex, 2026-11-19)
        self.state_private = True if private is None else private  # (None: not Windows, the 0600 mode)
        with handle:
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
        self.stopped = True
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
                sock, address = self.server.accept()
            except socket.error as exc:
                if not (exc.args and exc.args[0] in _WOULD_BLOCK):
                    self._record("accept", exc)  # e.g. a peer that reset before accept
                break
            _no_inherit(sock)
            # (the socket listens on 127.0.0.1 only; a peer from elsewhere is refused all the same, and while
            # wrong tokens keep coming every connection is closed unanswered for a moment)
            if not _loopback(address) or time.time() < self.throttled_until:
                if not _loopback(address):
                    self.refused_peers += 1
                try:
                    sock.close()
                except Exception:
                    pass
                continue
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
    def _bad_token(self):
        """Counts a request with a wrong token; past BAD_TOKEN_LIMIT within BAD_TOKEN_WINDOW_S, connections are
        closed unanswered for THROTTLE_S (a guesser gets no answers, FontLab no stream of requests)."""
        now = time.time()
        self.bad_tokens = [t for t in self.bad_tokens if now - t < BAD_TOKEN_WINDOW_S] + [now]
        if len(self.bad_tokens) >= BAD_TOKEN_LIMIT:
            self.throttled_until = now + THROTTLE_S
            self.bad_tokens = []

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
                self._bad_token()
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
    if listener.state_private is not True:
        print(u"FreeLab listener: its state file keeps its folder's access (%s)." % _text(listener.state_private))
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
