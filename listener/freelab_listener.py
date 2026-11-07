#FLM: Start Listener
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
import os
import socket
import sys
import time
import traceback

VERSION = "0.1.0"
PROTOCOL = 1
HOST = "127.0.0.1"
DEFAULT_PORT = 47761
POLL_MS = 25
MAX_REQUEST_BYTES = 4 * 1024 * 1024
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

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


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


def _font_summary(package, reads):
    return {
        "family": reads.prop(package, "tfn"),
        "style": reads.prop(package, "tsn"),
        "postscript_name": reads.prop(package, "psn"),
        "full_name": reads.prop(package, "ffn"),
        "path": reads.prop(package, "path"),
        "units_per_em": reads.prop(package, "upm"),
        "glyph_count": reads.prop(package, "glyphsCount"),
        "master": reads.prop(package, "master"),
        "master_count": reads.prop(package, "mastersCount"),
        "version_major": reads.prop(package, "versionMajor"),
        "version_minor": reads.prop(package, "versionMinor"),
        "has_changes": reads.prop(package, "hasChanges"),
    }


def _find_glyph(package, args):
    name = args.get("name")
    codepoint = args.get("unicode")
    if name is not None and codepoint is not None:
        raise FreeLabError("bad_args", "Give either name or unicode, not both.")
    if name is not None:
        if not isinstance(name, string_types) or not name:
            raise FreeLabError("bad_args", "name must be a non-empty string.")
        glyph = package.findName(name)
        label = u"name %r" % _text(name)
    elif codepoint is not None:
        if not isinstance(codepoint, integer_types) or isinstance(codepoint, bool) or not 0 <= codepoint <= 0x10FFFF:
            raise FreeLabError("bad_args", "unicode must be an integer code point.")
        glyph = package.findUnicode(codepoint)
        label = u"U+%04X" % codepoint
    else:
        raise FreeLabError("bad_args", "Give a glyph name or a unicode code point.")
    if _is_null(glyph, "the glyph"):
        raise FreeLabError("no_glyph", u"The current font has no glyph with %s." % label)
    return glyph


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
            raise FreeLabError("no_layer", u"The glyph has no layer named %r." % _text(wanted))
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
    """(workspace has changes, number of open fonts with unsaved changes), read from FontLab.

    A read that fails raises: FontLab must never be quit on a guess.
    """
    fl = _fontlab()
    workspace = fl.flWorkspace.instance()
    try:
        workspace_changed = bool(workspace.hasChanges)
        fonts = sum(1 for package in _real_list(workspace.packages(), "flWorkspace.packages()") if package.hasChanges)
    except Exception as exc:
        raise FreeLabError("unreadable", "FontLab could not tell whether there are unsaved changes: %s" % _exc_text(exc))
    return workspace_changed, fonts


@op("unsaved")
def op_unsaved(listener, args):
    workspace_changed, fonts = unsaved_work()
    return {"unsaved": workspace_changed or fonts > 0, "workspace": workspace_changed, "fonts": fonts}


@op("quit")
def op_quit(listener, args):
    """Close FontLab as its close button would, a moment after replying; refused over any unsaved work."""
    workspace_changed, fonts = unsaved_work()
    if workspace_changed or fonts:
        parts = (["%d font%s" % (fonts, "" if fonts == 1 else "s")] if fonts else []) + (["the workspace"] if workspace_changed else [])
        raise FreeLabError("unsaved_changes", "FontLab has unsaved changes (%s): save them, or close them in FontLab, "
                           "first. FreeLab never quits FontLab over unsaved work." % " and ".join(parts))
    listener.request_quit()
    return {"quitting": True}


@op("font_info")
def op_font_info(listener, args):
    package, count = current_package()
    reads = _Reads()
    result = _font_summary(package, reads)
    result["fonts_open"] = count
    return reads.attach(result)


@op("glyph_names")
def op_glyph_names(listener, args):
    package, count = current_package()
    offset = _int_arg(args, "offset", 0, 0, 10 ** 9)
    limit = _int_arg(args, "limit", 500, 1, 5000)
    names = _real_list(package.fgPackage.keys(), "fgFont.keys()")
    page = [_text(n) for n in names[offset:offset + limit]]
    return {"total": len(names), "offset": offset, "names": page, "more": offset + len(page) < len(names)}


@op("glyph")
def op_glyph(listener, args):
    package, count = current_package()
    glyph = _find_glyph(package, args)
    reads = _Reads()
    layer, layer_source = _choose_layer(glyph, package, args.get("layer"), reads)
    unicodes = None
    try:
        unicodes = [int(u) for u in _real_list(glyph.fgGlyph.unicodes, "fgGlyph.unicodes")]
    except FreeLabError:
        raise
    except Exception as exc:
        reads.unreadable["unicodes"] = _exc_text(exc)
    bbox = reads.prop(layer, "boundingBox", "bounding_box")
    advance = reads.prop(layer, "advanceWidth", "advance_width")
    result = {
        "name": reads.prop(glyph, "name"),
        "unicodes": unicodes,
        "layers": _layer_names(glyph),
        "layer": reads.prop(layer, "name", "layer"),
        "layer_source": layer_source,
        "advance_width": advance,
        "bounding_box": bbox,
        "shapes": reads.prop(layer, "shapesCount", "shapes"),
        "nodes": reads.prop(layer, "nodesCount", "nodes"),
        "mark": reads.prop(glyph, "mark"),
        "tags": reads.prop(glyph, "tags"),
        "note": reads.prop(glyph, "note"),
        "font": reads.prop(package, "tfn", "font"),
    }
    if isinstance(bbox, dict) and isinstance(advance, (int, float)) and bbox.get("width"):
        result["sidebearings"] = {
            "left": bbox["x"],
            "right": advance - (bbox["x"] + bbox["width"]),
            "method": "layer bounding box; not FontLab's metrics",
        }
    return reads.attach(result)


@op("outline")
def op_outline(listener, args):
    package, count = current_package()
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
        shapes_out.append({
            "index": s_index,
            "name": reads.prop(shape, "name", "shape_%d_name" % s_index),
            "is_component": reads.prop(shape, "isComponent", "shape_%d_is_component" % s_index),
            "transform": reads.prop(shape, "transform", "shape_%d_transform" % s_index),
            "contours": contours_out,
        })
    result = {
        "name": reads.prop(glyph, "name"),
        "layer": reads.prop(layer, "name", "layer"),
        "layer_source": layer_source,
        "coordinates": "shape-local; each shape's transform [m11, m12, m21, m22, dx, dy] is not applied",
        "shapes": shapes_out,
        "node_count": total,
        "truncated": truncated,
        "font": reads.prop(package, "tfn", "font"),
    }
    return reads.attach(result)


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
    reply = {"output": captured.value(), "result": to_jsonable(namespace.get("result"))}
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
            size = ctypes.windll.kernel32.GetEnvironmentVariableW(name, None, 0)
            if not size:
                return None
            buffer = ctypes.create_unicode_buffer(size)
            ctypes.windll.kernel32.GetEnvironmentVariableW(name, buffer, size)
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

    # -- quitting FontLab ----------------------------------------------------
    def request_quit(self, delay_ms=QUIT_DELAY_MS):
        """Close FontLab's main window shortly, after this request's reply has gone out.

        Closing the window, rather than ending the application, runs FontLab's own
        shutdown: its preferences are saved, and it may still ask something on screen.
        """
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
            windows[0].close()
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
        try:
            payload = _bytes(json.dumps(reply)) + b"\n"
        except Exception:
            payload = _LAST_RESORT
        try:
            client.sock.setblocking(True)
            client.sock.settimeout(CLIENT_TIMEOUT_S)
            client.sock.sendall(payload)
        except socket.error:
            pass
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
                raise FreeLabError("unknown_op", u"Unknown operation %r." % (_text(name),))
            args = request.get("args")
            if args is None:
                args = {}
            if not isinstance(args, dict):
                raise FreeLabError("bad_args", "args must be a JSON object.")
            self.requests += 1
            result = handler(self, args)
            return {"id": request_id, "ok": True, "result": to_jsonable(result)}
        except FreeLabError as exc:
            return {"id": request_id, "ok": False, "error": {"code": exc.code, "message": _text(exc.message)}}
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
