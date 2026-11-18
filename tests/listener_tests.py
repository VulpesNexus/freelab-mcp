# -*- coding: utf-8 -*-
"""Listener tests against spy fakes; they run under Python 2.7 and Python 3."""

from __future__ import absolute_import, division, print_function

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LISTENER_DIR = os.path.join(os.path.dirname(HERE), "listener")
sys.path.insert(0, HERE)
sys.path.insert(0, LISTENER_DIR)
os.environ["FREELAB_NO_AUTOSTART"] = "1"

import fakes  # noqa: E402
import freelab_listener as fl  # noqa: E402

CP932_BYTES = b"\x83e\x83X\x83g"  # the cp932 encoding of a Japanese word: not valid UTF-8


class ListenerCase(unittest.TestCase):
    def setUp(self):
        self.state = fakes.FontLabState()
        fakes.install(self.state)
        self.tmp = tempfile.mkdtemp(prefix="freelab-test-")
        self.listener = fl.Listener(port=0, state_file=os.path.join(self.tmp, "listener.json"))

    def tearDown(self):
        errors = list(self.listener.errors)
        self.listener.stop()
        fakes.uninstall()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(errors, [], "the listener swallowed errors: %r" % errors)

    def open_sample(self, **kw):
        font = fakes.sample_font(**kw)
        self.state.fonts = [font]
        self.state.current = font
        return font

    def request(self, op, args=None, token=None, protocol=fl.PROTOCOL, **extra):
        message = {"protocol": protocol, "id": 7, "token": token or self.listener.token, "op": op, "args": args or {}}
        message.update(extra)
        return self.listener.handle_line(json.dumps(message).encode("utf-8"))

    def ok(self, op, args=None, **extra):
        reply = self.request(op, args, **extra)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["id"], 7)
        json.dumps(reply)  # the reply must serialize
        return reply["result"]

    def fails(self, op, args=None, code=None, **extra):
        reply = self.request(op, args, **extra)
        self.assertFalse(reply["ok"], reply)
        json.dumps(reply)
        if code is not None:
            self.assertEqual(reply["error"]["code"], code, reply)
        return reply["error"]


class CloseMainWindowTests(ListenerCase):
    """_close_main_window with PythonQt faked: FontLab's main window, or nothing at all."""

    def setUp(self):
        ListenerCase.setUp(self)
        self.closed, self.quit = [], []
        test = self

        class Meta(object):
            def __init__(self, name):
                self.name = name

            def className(self):
                return self.name

        class Window(object):
            def __init__(self, name):
                self.meta = Meta(name)

            def metaObject(self):
                return self.meta

            def close(self):
                test.closed.append(self.meta.name)

        class App(object):
            windows = []

            def topLevelWidgets(self):
                return list(App.windows)

            def quit(self):
                test.quit.append(1)

        self.Window, self.App = Window, App
        app = App()
        qtgui = type(sys)("PythonQt.QtGui")
        qtgui.QApplication = type("QApplication", (object,), {"instance": staticmethod(lambda: app)})
        pythonqt = type(sys)("PythonQt")
        pythonqt.QtGui = qtgui
        self._saved_modules = {k: sys.modules.get(k) for k in ("PythonQt", "PythonQt.QtGui")}
        sys.modules["PythonQt"], sys.modules["PythonQt.QtGui"] = pythonqt, qtgui

    def tearDown(self):
        for key, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module
        ListenerCase.tearDown(self)

    def test_closes_the_main_window(self):
        self.App.windows = [self.Window("QDialog"), self.Window("MainWindow")]
        self.listener._close_main_window()
        self.assertEqual((self.closed, self.quit), (["MainWindow"], []))

    def test_without_a_main_window_nothing_is_ended(self):
        self.App.windows = [self.Window("QDialog")]
        self.listener._close_main_window()
        self.assertEqual((self.closed, self.quit), ([], []), "never QApplication.quit()")
        self.assertEqual(len(self.listener.errors), 1)
        del self.listener.errors[:]

    def test_closing_is_marked_while_the_main_window_closes(self):
        seen = []
        listener = self.listener

        class Main(self.Window):
            def close(self):
                seen.append(listener.quit_in_progress())

        self.App.windows = [Main("MainWindow")]
        self.listener._close_main_window()
        self.assertEqual(seen, [True], "a quit asked meanwhile is refused")
        self.assertFalse(self.listener.quit_in_progress(), "and allowed again once close() returns (Cancel)")

    def test_a_second_quit_never_replaces_a_running_timer(self):
        class Timer(object):
            active = True

        self.listener.quit_timer = Timer()
        with self.assertRaises(fl.FreeLabError) as raised:
            self.listener.request_quit()
        self.assertEqual(raised.exception.code, "quit_in_progress")
        Timer.active = False
        self.assertFalse(self.listener.quit_in_progress())
        self.listener.quit_timer = None

    def test_dialog_reads_the_question_as_shown(self):
        class Meta(object):
            def __init__(self, name):
                self.name = name

            def className(self):
                return self.name

        class Widget(object):
            def __init__(self, kind, text=u"", text_format=2, children=()):
                self.kind, self.text, self.textFormat, self._children = kind, text, text_format, list(children)
                self.windowTitle = u"FontLab Warning"

            def children(self):
                return self._children

            def isWidgetType(self):
                return True

            def isVisible(self):
                return True

            def inherits(self, name):
                bases = {"QLabel": ("QLabel",), "QPushButton": ("QPushButton", "QAbstractButton"),
                         "QCheckBox": ("QCheckBox", "QAbstractButton"), "QDialog": ("QDialog",)}
                return name in bases[self.kind]

            def metaObject(self):
                return Meta("DlgClosePackage" if self.kind == "QDialog" else self.kind)

        dialog = Widget("QDialog", children=[
            Widget("QLabel", u'Save changes to "Foo <Bold>" before closing?', 0),
            Widget("QLabel", u"<b>Kerning</b> &amp; classes", 1),
            Widget("QPushButton", u"&Don't Save"), Widget("QPushButton", u"A&&B"),
            Widget("QCheckBox", u"Do not ask again")])

        class Shown(self.Window):
            def isVisible(self):
                return True

            windowTitle = u"FreeLab Auto Kerning"

        self.App.windows = [Shown("QDialog")]
        self.App.activeModalWidget = lambda app: dialog
        try:
            reply = self.ok("dialog")
        finally:
            del self.App.activeModalWidget
        self.assertEqual(reply["dialog"]["text"], [u'Save changes to "Foo <Bold>" before closing?', u"Kerning & classes"],
                         "plain text word for word; rich text without its tags and entities")
        self.assertEqual(reply["dialog"]["buttons"], [u"Don't Save", u"A&B"], "mnemonics dropped, a literal & kept, no check box")
        self.assertEqual((reply["main_window"], reply["windows"]), (False, [u"FreeLab Auto Kerning"]))

    def test_errors_are_capped(self):
        for i in range(fl.MAX_ERRORS + 5):
            self.listener._record("tick", RuntimeError(str(i)))
        self.assertEqual(len(self.listener.errors), fl.MAX_ERRORS)
        self.assertIn(str(fl.MAX_ERRORS + 4), self.listener.errors[-1])
        del self.listener.errors[:]


class QuitTests(ListenerCase):
    def setUp(self):
        ListenerCase.setUp(self)
        self.quits = []
        self.listener.request_quit = lambda delay_ms=0: self.quits.append(delay_ms)

    def test_unsaved_reports_fonts_and_workspace(self):
        self.state.unsaved = [False, True, True]
        self.assertEqual(self.ok("unsaved"), {"unsaved": True, "workspace": False, "fonts": 2, "edited": 2})
        self.state.unsaved = [False]
        self.state.workspace_changed = True
        self.assertEqual(self.ok("unsaved"), {"unsaved": True, "workspace": True, "fonts": 0, "edited": 0})

    def test_quit_is_refused_over_an_unsaved_font(self):
        self.state.unsaved = [True]
        error = self.fails("quit", code="unsaved_changes")
        self.assertIn("1 font", error["message"])
        self.assertEqual(self.quits, [])

    def test_an_edited_font_goes_through_fontlabs_own_question(self):
        # FontLab 7.2 left hasChanges False after an edit (only hasHistoryStates told), and asked to
        # save it on closing: the quit goes ahead, and FontLab asks
        self.state.unsaved = [False, "history"]
        self.assertEqual(self.ok("quit"), {"quitting": True, "edited": 1})
        self.assertEqual(len(self.quits), 1)
        self.assertEqual(self.ok("unsaved"), {"unsaved": False, "workspace": False, "fonts": 0, "edited": 1})

    def test_dialog_outside_fontlab_is_none(self):
        self.assertEqual(self.ok("dialog"), {"dialog": None})

    def test_state_folder_follows_the_environment(self):
        saved = os.environ.get("FREELAB_STATE_DIR")
        os.environ["FREELAB_STATE_DIR"] = self.tmp
        try:
            self.assertEqual(fl.state_dir(), self.tmp)
        finally:
            if saved is None:
                del os.environ["FREELAB_STATE_DIR"]
            else:
                os.environ["FREELAB_STATE_DIR"] = saved

    def test_state_folder_is_under_local_app_data(self):
        saved = os.environ.pop("FREELAB_STATE_DIR", None)
        try:
            if os.environ.get("LOCALAPPDATA"):
                self.assertEqual(os.path.normcase(fl.state_dir()),
                                 os.path.normcase(os.path.join(os.environ["LOCALAPPDATA"], "FreeLab")))
        finally:
            if saved is not None:
                os.environ["FREELAB_STATE_DIR"] = saved

    def test_quit_is_refused_over_an_unsaved_workspace(self):
        self.state.workspace_changed = True
        self.fails("quit", code="unsaved_changes")
        self.assertEqual(self.quits, [])

    def test_quit_is_refused_when_fontlab_cannot_tell(self):
        class Broken(object):
            @property
            def hasChanges(self):
                raise RuntimeError("no")

            def packages(self):
                return []

        fontlab = sys.modules["fontlab"]
        saved = fontlab.flWorkspace
        fontlab.flWorkspace = type("W", (object,), {"instance": staticmethod(lambda: Broken())})
        try:
            self.fails("quit", code="unreadable")
        finally:
            fontlab.flWorkspace = saved
        self.assertEqual(self.quits, [])

    def test_quit_is_refused_when_packages_is_not_a_list(self):
        class Native(object):  # a container FontLab could return: never iterated
            def __iter__(self):
                raise fakes.SpyViolation("a native container was iterated")  # past except Exception

        class Workspace(object):
            hasChanges = False

            def packages(self):
                return Native()

        fontlab = sys.modules["fontlab"]
        saved = fontlab.flWorkspace
        fontlab.flWorkspace = type("W", (object,), {"instance": staticmethod(lambda: Workspace())})
        try:
            error = self.fails("quit", code="unreadable")
        finally:
            fontlab.flWorkspace = saved
        self.assertIn("not a list", error["message"])
        self.assertEqual(self.quits, [])

    def test_a_leftover_temporary_state_file_is_replaced(self):
        leftover = "%s.%d.tmp" % (self.listener.state_file, os.getpid())
        with open(leftover, "w") as handle:
            handle.write("half")
        self.listener.bind()
        self.assertTrue(os.path.isfile(self.listener.state_file))
        self.assertFalse(os.path.exists(leftover))

    def test_protocol_must_be_the_integer(self):
        self.fails("ping", protocol=True, code="bad_protocol")

    def test_quit_is_refused_while_fontlab_is_closing(self):
        self.listener.closing = True
        try:
            self.fails("quit", code="quit_in_progress")
        finally:
            self.listener.closing = False
        self.assertEqual(self.quits, [])

    def test_quit_is_refused_when_the_client_gave_up_meanwhile(self):
        saved = fl.unsaved_work
        listener = self.listener

        def slow():  # FontLab busy: the client's deadline passes while this runs
            listener.deadline = time.time() - 1
            return False, 0, 0

        fl.unsaved_work = slow
        try:
            self.fails("quit", code="expired")
        finally:
            fl.unsaved_work = saved
        self.assertEqual(self.quits, [], "FontLab not closed")

    def test_quit_with_nothing_unsaved(self):
        self.state.unsaved = [False, False]
        self.assertEqual(self.ok("quit"), {"quitting": True, "edited": 0})
        self.assertEqual(len(self.quits), 1)


class ProtocolTests(ListenerCase):
    def test_wrong_token_is_refused_without_echoing_the_id(self):
        reply = self.request("ping", token="0" * 32)
        self.assertEqual(reply["error"]["code"], "bad_token")
        self.assertIsNone(reply["id"])

    def test_missing_token_is_refused(self):
        reply = self.listener.handle_line(json.dumps({"protocol": 1, "op": "ping"}).encode("utf-8"))
        self.assertEqual(reply["error"]["code"], "bad_token")

    def test_wrong_protocol_is_refused(self):
        self.assertEqual(self.request("ping", protocol=99)["error"]["code"], "bad_protocol")

    def test_bad_json(self):
        self.assertEqual(self.listener.handle_line(b"{nope")["error"]["code"], "bad_json")

    def test_non_object_request(self):
        self.assertEqual(self.listener.handle_line(b"[1, 2]")["error"]["code"], "bad_request")

    def test_unknown_op(self):
        self.fails("format_disk", code="unknown_op")

    def test_args_must_be_object(self):
        line = json.dumps({"protocol": 1, "id": 1, "token": self.listener.token, "op": "ping", "args": [1]})
        self.assertEqual(self.listener.handle_line(line.encode("utf-8"))["error"]["code"], "bad_args")

    def test_expired_request_is_not_run(self):
        sys._freelab_ran = 0
        try:
            self.fails("run_python", {"code": "import sys; sys._freelab_ran += 1"}, code="expired", deadline=time.time() - 1)
            self.assertEqual(sys._freelab_ran, 0)
            self.ok("run_python", {"code": "import sys; sys._freelab_ran += 1"}, deadline=time.time() + 30)
            self.assertEqual(sys._freelab_ran, 1)
        finally:
            del sys._freelab_ran

    def test_bad_deadline(self):
        self.fails("ping", code="bad_request", deadline="soon")
        self.fails("ping", code="bad_request", deadline=True)

    def test_ping(self):
        result = self.ok("ping")
        self.assertEqual(result["listener"], fl.VERSION)
        self.assertEqual(result["protocol"], fl.PROTOCOL)
        self.assertTrue(result["in_fontlab"])
        self.assertEqual(result["application_path"], "FontLab 7 (fake)")

    def test_ping_outside_fontlab(self):
        fakes.uninstall()
        result = self.ok("ping")
        self.assertFalse(result["in_fontlab"])

    def test_tokens_differ_between_listeners(self):
        other = fl.Listener(port=0, state_file=os.path.join(self.tmp, "other.json"))
        self.assertNotEqual(other.token, self.listener.token)
        self.assertEqual(len(other.token), 32)

    def test_exception_reply_with_non_utf8_bytes_serializes(self):
        def broken(listener, args):
            raise ValueError(CP932_BYTES)
        fl.OPS["_broken"] = broken
        try:
            error = self.fails("_broken", code="exception")
            self.assertIn("ValueError", error["message"])
        finally:
            del fl.OPS["_broken"]

    def test_freelab_error_with_non_utf8_bytes_serializes(self):
        def broken(listener, args):
            raise fl.FreeLabError("custom", CP932_BYTES)
        fl.OPS["_broken"] = broken
        try:
            self.fails("_broken", code="custom")
        finally:
            del fl.OPS["_broken"]


class FontTests(ListenerCase):
    def test_status_without_font(self):
        result = self.ok("status")
        self.assertEqual(result, {"fonts_open": 0, "current_font": None})

    def test_font_info_without_font(self):
        self.fails("font_info", code="no_font")

    def test_status_with_font(self):
        self.open_sample()
        result = self.ok("status")
        self.assertEqual(result["fonts_open"], 1)
        self.assertEqual(result["current_font"]["family"], "Free Sans")
        self.assertNotIn("unreadable", result)

    def test_font_info(self):
        self.open_sample()
        info = self.ok("font_info")
        self.assertEqual(info["family"], "Free Sans")
        self.assertEqual(info["style"], "Regular")
        self.assertEqual(info["postscript_name"], "FreeSans-Regular")
        self.assertEqual(info["units_per_em"], 1000.0)
        self.assertEqual(info["glyph_count"], 4)
        self.assertEqual(info["master"], "Regular")
        self.assertEqual(info["fonts_open"], 1)

    def test_unreadable_field_is_reported_not_hidden(self):
        self.open_sample(broken_path=True)
        info = self.ok("font_info")
        self.assertIsNone(info["path"])
        self.assertIn("path", info["unreadable"])
        self.assertIn("RuntimeError", info["unreadable"]["path"])

    def test_unreadable_field_with_non_utf8_message(self):
        font = self.open_sample()

        class CP932Path(fakes.flPackage):
            def __getattribute__(self, name):
                if name == "path":
                    raise IOError(CP932_BYTES)
                return fakes.flPackage.__getattribute__(self, name)

        broken = CP932Path("Free Sans", "Regular", font._f_glyphs)
        self.state.fonts = [broken]
        self.state.current = broken
        info = self.ok("font_info")
        self.assertIn("path", info["unreadable"])

    def test_open_font_but_no_current_package(self):
        self.open_sample()
        self.state.current = None
        error = self.fails("font_info", code="no_current_font")
        self.assertIn("1 font is open", error["message"])

    def test_glyph_names_paging(self):
        self.open_sample()
        first = self.ok("glyph_names", {"limit": 2})
        self.assertEqual(first, {"font": {"id": 1, "name": "Free Sans Regular"}, "total": 4, "offset": 0, "names": ["A", "o"],
                                 "more": True})
        rest = self.ok("glyph_names", {"offset": 2, "limit": 2})
        self.assertEqual(rest["names"], ["space", "B"])
        self.assertFalse(rest["more"])

    def test_glyph_names_limits(self):
        self.open_sample()
        for bad in ({"limit": 0}, {"limit": 5001}, {"offset": -1}, {"limit": "many"}, {"limit": None}, {"offset": 1.5}, {"limit": True}):
            self.fails("glyph_names", bad, code="bad_args")


class GlyphTests(ListenerCase):
    def setUp(self):
        ListenerCase.setUp(self)
        self.open_sample()

    def test_glyph_by_name(self):
        g = self.ok("glyph", {"name": "A"})
        self.assertEqual(g["name"], "A")
        self.assertEqual(g["unicodes"], [0x41])
        self.assertEqual(g["layers"], ["Regular", "Regular mask"])
        self.assertEqual(g["layer"], "Regular")
        self.assertEqual(g["layer_source"], "font master")
        self.assertEqual(g["advance_width"], 500.0)
        self.assertEqual(g["bounding_box"], {"x": 50, "y": 0, "width": 400, "height": 400})
        self.assertEqual(g["sidebearings"]["left"], 50)
        self.assertEqual(g["sidebearings"]["right"], 50.0)
        self.assertEqual(g["shapes"], 1)
        self.assertEqual(g["nodes"], 4)
        self.assertNotIn("unreadable", g)

    def test_counts_come_from_the_chosen_layer(self):
        g = self.ok("glyph", {"name": "A", "layer": "Regular mask"})
        self.assertEqual(g["shapes"], 0)
        self.assertEqual(g["nodes"], 0)

    def test_glyph_by_unicode(self):
        self.assertEqual(self.ok("glyph", {"unicode": 0x6F})["name"], "o")

    def test_empty_glyph_has_no_sidebearings(self):
        g = self.ok("glyph", {"name": "space"})
        self.assertEqual(g["advance_width"], 250.0)
        self.assertNotIn("sidebearings", g)

    def test_missing_glyph(self):
        error = self.fails("glyph", {"name": "Zzz"}, code="no_glyph")
        self.assertIn("Zzz", error["message"])
        self.fails("glyph", {"unicode": 0x5A}, code="no_glyph")

    def test_glyph_argument_checks(self):
        self.fails("glyph", {}, code="bad_args")
        self.fails("glyph", {"name": "A", "unicode": 65}, code="bad_args")
        self.fails("glyph", {"name": ""}, code="bad_args")
        self.fails("glyph", {"unicode": -1}, code="bad_args")
        self.fails("glyph", {"unicode": True}, code="bad_args")
        self.fails("glyph", {"unicode": "65"}, code="bad_args")
        self.fails("glyph", {"name": "A", "layer": 3}, code="bad_args")

    def test_requested_layer(self):
        g = self.ok("glyph", {"name": "A", "layer": "Regular mask"})
        self.assertEqual(g["layer"], "Regular mask")
        self.assertEqual(g["layer_source"], "requested")
        self.fails("glyph", {"name": "A", "layer": "Bold"}, code="no_layer")

    def test_active_layer_fallback(self):
        object.__setattr__(self.state.current, "master", "Bold")
        g = self.ok("glyph", {"name": "A"})
        self.assertEqual(g["layer_source"], "active layer")
        self.assertEqual(g["layer"], "Regular")

    def test_unreadable_isnull_is_an_error_not_a_glyph(self):
        font = self.state.current
        odd = fakes.NullUnknown("odd", [fakes.flLayer("Regular")])
        font._f_glyphs.append(odd)
        self.fails("glyph", {"name": "odd"}, code="unreadable")

    def test_outline(self):
        out = self.ok("outline", {"name": "o"})
        self.assertEqual(out["node_count"], 5)
        self.assertFalse(out["truncated"])
        shape = out["shapes"][0]
        self.assertEqual(shape["transform"], [1, 0, 0.2, 1, 10, 0])
        self.assertFalse(shape["is_component"])
        nodes = shape["contours"][0]["nodes"]
        self.assertEqual([n["type"] for n in nodes], ["on", "off", "off", "curve", "on"])
        self.assertTrue(nodes[3]["smooth"])
        self.assertTrue(shape["contours"][0]["closed"])
        self.assertIn("not applied", out["coordinates"])

    def test_outline_truncates_inside_a_contour(self):
        out = self.ok("outline", {"name": "A", "max_nodes": 3})
        self.assertTrue(out["truncated"])
        self.assertEqual(out["node_count"], 3)
        contour = out["shapes"][0]["contours"][0]
        self.assertTrue(contour["truncated"])
        self.assertEqual(len(contour["nodes"]), 3)

    def test_outline_truncates_at_a_contour_boundary_without_a_phantom(self):
        out = self.ok("outline", {"name": "B", "max_nodes": 4})
        self.assertTrue(out["truncated"])
        contours = out["shapes"][0]["contours"]
        self.assertEqual(len(contours), 1)  # the unread second contour is not reported as empty
        self.assertNotIn("truncated", contours[0])
        full = self.ok("outline", {"name": "B"})
        self.assertEqual([len(c["nodes"]) for c in full["shapes"][0]["contours"]], [4, 4])
        self.assertFalse(full["truncated"])

    def test_outline_max_nodes_bounds(self):
        self.fails("outline", {"name": "A", "max_nodes": 0}, code="bad_args")
        self.fails("outline", {"name": "A", "max_nodes": None}, code="bad_args")

    def test_outline_of_empty_glyph(self):
        out = self.ok("outline", {"name": "space"})
        self.assertEqual(out["shapes"], [])
        self.assertEqual(out["node_count"], 0)

    def test_native_list_types_are_checked(self):
        object.__setattr__(self.state.current.findName("A"), "_f_layers", fakes.fgFont([]))
        error = self.fails("glyph", {"name": "A"}, code="unexpected_type")
        self.assertIn("glyph.layers", error["message"])


class RunPythonTests(ListenerCase):
    def test_output_and_result(self):
        result = self.ok("run_python", {"code": "print('hello')\nresult = {'n': 1 + 1}"})
        self.assertEqual(result["output"].strip(), "hello")
        self.assertEqual(result["result"], {"n": 2})
        self.assertNotIn("error", result)

    def test_fontlab_is_in_namespace(self):
        self.open_sample()
        result = self.ok("run_python", {"code": "result = fontlab.CountFonts()"})
        self.assertEqual(result["result"], 1)

    def test_exception_is_returned_and_streams_restored(self):
        before = (sys.stdout, sys.stderr)
        result = self.ok("run_python", {"code": "import sys\nprint('partial')\nsys.stderr.write('warned')\nraise ValueError('boom')"})
        self.assertEqual((sys.stdout, sys.stderr), before)
        self.assertIn("ValueError: boom", result["error"])
        self.assertIn("partial", result["output"])
        self.assertIn("warned", result["output"])

    def test_system_exit_and_keyboard_interrupt_stay_inside(self):
        before = (sys.stdout, sys.stderr)
        for code in ("import sys; sys.exit(3)", "raise SystemExit", "raise KeyboardInterrupt"):
            result = self.ok("run_python", {"code": code})
            self.assertTrue("SystemExit" in result["error"] or "KeyboardInterrupt" in result["error"], result)
        self.assertEqual((sys.stdout, sys.stderr), before)

    def test_listener_future_flags_are_not_inherited(self):
        result = self.ok("run_python", {"code": "result = 7 / 2"})
        self.assertEqual(result["result"], 3 if fl.PY2 else 3.5)  # FontLab 7's own panel divides integers

    def test_capture_looks_like_a_stream(self):
        result = self.ok("run_python", {"code": "import sys\nresult = [sys.stdout.encoding, sys.stdout.isatty()]"})
        self.assertEqual(result["result"], ["utf-8", False])

    def test_syntax_error_is_returned(self):
        result = self.ok("run_python", {"code": "def ("})
        self.assertIn("SyntaxError", result["error"])

    def test_persist(self):
        self.ok("run_python", {"code": "kept = 41", "persist": True})
        self.assertEqual(self.ok("run_python", {"code": "result = kept + 1", "persist": True})["result"], 42)
        fresh = self.ok("run_python", {"code": "result = 'kept' in dir()"})
        self.assertFalse(fresh["result"])

    def test_fresh_namespace_dies_with_its_request(self):
        # Without the clear, f's globals cycle would keep k alive until a later garbage collection.
        code = ("import sys\nclass K(object):\n    def __del__(self, sys=sys):\n        sys._freelab_freed = 1\n"  # 1, not True: 2.7 looks True up in the cleared builtins
                "k = K()\ndef f():\n    return k\nresult = 1")
        sys._freelab_freed = 0
        try:
            self.assertEqual(self.ok("run_python", {"code": code})["result"], 1)
            self.assertEqual(sys._freelab_freed, 1)
        finally:
            del sys._freelab_freed

    def test_a_deferred_callback_keeps_builtins_and_imports(self):
        # A callback left behind (a timer, here held on sys) runs after the clear: it must still reach
        # builtins and import, or FontLab's script runner crashes when it triggers a menu action.
        code = "import sys\ndef later():\n    import os\n    return len(os.sep), True\nsys._freelab_later = later\nresult = 1"
        try:
            self.assertEqual(self.ok("run_python", {"code": code})["result"], 1)
            self.assertEqual(sys._freelab_later(), (1, True))
        finally:
            del sys._freelab_later

    def test_persistent_namespace_is_kept(self):
        self.ok("run_python", {"code": "def f():\n    return 5", "persist": True})
        self.assertEqual(self.ok("run_python", {"code": "result = f()", "persist": True})["result"], 5)

    def test_result_is_cleared_between_persistent_calls(self):
        self.ok("run_python", {"code": "result = 1", "persist": True})
        self.assertIsNone(self.ok("run_python", {"code": "x = 2", "persist": True})["result"])

    def test_unserializable_result_becomes_text(self):
        result = self.ok("run_python", {"code": "result = object()"})
        self.assertTrue(result["result"].startswith("<object"))

    def test_empty_code(self):
        self.fails("run_python", {"code": "  "}, code="bad_args")


class ConversionTests(unittest.TestCase):
    def test_values(self):
        self.assertEqual(fl.to_jsonable(fakes.QRectF(1, 2, 3, 4)), {"x": 1, "y": 2, "width": 3, "height": 4})
        self.assertEqual(fl.to_jsonable(fakes.QTransform(2, 0, 0, 2, 5, 6)), [2, 0, 0, 2, 5, 6])
        self.assertEqual(fl.to_jsonable(float("nan")), "nan")
        self.assertEqual(fl.to_jsonable((1, [b"x", u"é"])), [1, [u"x", u"é"]])
        self.assertEqual(fl.to_jsonable({1: set([2])}), {u"1": [2]})
        json.dumps(fl.to_jsonable(CP932_BYTES))

    def test_native_objects_are_not_probed(self):
        # A spy native object would raise on any member access; repr is all that is used.
        text = fl.to_jsonable(fakes.flNode(1, 2))
        self.assertTrue(text.startswith("<flNode"))

    def test_exception_text_survives_non_ascii_bytes(self):
        text = fl._exc_text(ValueError(CP932_BYTES))
        self.assertTrue(text.startswith(u"ValueError"))
        json.dumps(text)


class ModuleLoadTests(unittest.TestCase):
    def test_load_as_module_gives_real_module_globals(self):
        saved = sys.modules.get(fl.MODULE_NAME)
        try:
            module = fl.load_as_module(os.path.join(LISTENER_DIR, "freelab_listener.py"))
            self.assertIs(sys.modules[fl.MODULE_NAME], module)
            self.assertEqual(module.__name__, fl.MODULE_NAME)
            self.assertIs(module.Listener.tick.__globals__ if not fl.PY2 else module.Listener.tick.im_func.func_globals, module.__dict__)
        finally:
            if saved is not None:
                sys.modules[fl.MODULE_NAME] = saved


class SocketTests(ListenerCase):
    def setUp(self):
        ListenerCase.setUp(self)
        self.listener.bind()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop)
        self.thread.daemon = True
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            self.listener.tick()
            time.sleep(0.005)

    def tearDown(self):
        self.stop_event.set()
        self.thread.join(5)
        ListenerCase.tearDown(self)

    def line(self, op, args=None, request_id=1):
        return json.dumps({"protocol": 1, "id": request_id, "token": self.listener.token, "op": op, "args": args or {}}).encode("utf-8") + b"\n"

    def send_raw(self, payload):
        sock = socket.create_connection(("127.0.0.1", self.listener.port), timeout=5)
        try:
            sock.sendall(payload)
            return self.read_reply(sock)
        finally:
            sock.close()

    def read_reply(self, sock):
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode("utf-8")) if data else None

    def test_state_file(self):
        with open(self.listener.state_file) as handle:
            state = json.load(handle)
        self.assertEqual(state["port"], self.listener.port)
        self.assertEqual(state["token"], self.listener.token)
        self.assertEqual(state["protocol"], fl.PROTOCOL)

    def test_round_trip_in_pieces(self):
        line = self.line("ping", request_id=3)
        sock = socket.create_connection(("127.0.0.1", self.listener.port), timeout=5)
        try:
            sock.sendall(line[:10])
            time.sleep(0.05)
            sock.sendall(line[10:])
            reply = self.read_reply(sock)
        finally:
            sock.close()
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["id"], 3)

    def test_several_clients(self):
        for i in range(5):
            self.assertEqual(self.send_raw(self.line("status", request_id=i))["id"], i)

    def test_unicode_glyph_name(self):
        self.open_sample()
        reply = self.send_raw(self.line("glyph", {"name": u"été"}))
        self.assertEqual(reply["error"]["code"], "no_glyph")

    def test_nested_tick_does_not_drop_the_request_in_progress(self):
        # Code that spins a nested event loop (a modal dialog) fires the timer re-entrantly.
        sys._freelab_test_listener = self.listener
        try:
            code = (
                "import sys\n"
                "L = sys._freelab_test_listener\n"
                "for c in L.clients:\n"
                "    c.started -= 100\n"
                "L.tick()\n"
                "result = 'survived'\n"
            )
            reply = self.send_raw(self.line("run_python", {"code": code}))
        finally:
            del sys._freelab_test_listener
        self.assertIsNotNone(reply, "the client was dropped by a nested tick")
        self.assertEqual(reply["result"]["result"], "survived")

    def test_client_cap(self):
        socks = []
        try:
            for _ in range(fl.MAX_CLIENTS + 4):
                socks.append(socket.create_connection(("127.0.0.1", self.listener.port), timeout=5))
            time.sleep(0.3)
            self.assertLessEqual(len(self.listener.clients), fl.MAX_CLIENTS)
        finally:
            for s in socks:
                s.close()

    def test_silent_client_is_dropped(self):
        sock = socket.create_connection(("127.0.0.1", self.listener.port), timeout=5)
        try:
            deadline = time.time() + 2
            while not self.listener.clients and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(self.listener.clients), 1)
            self.listener.clients[0].started -= fl.CLIENT_TIMEOUT_S + 1
            deadline = time.time() + 2
            while self.listener.clients and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.listener.clients, [])
        finally:
            sock.close()

    def test_a_client_that_stops_reading_never_holds_up_a_tick(self):
        """Review of 2026-11-15: the reply went out through a blocking sendall from the GUI timer, so a
        client that asked for a large reply and read none of it froze FontLab for up to 5 s."""
        ticks = []

        def timed():
            started = time.time()
            self.listener_tick()
            ticks.append(time.time() - started)
        self.listener_tick, self.listener.tick = self.listener.tick, timed
        saved = fl.SEND_TIMEOUT_S
        fl.SEND_TIMEOUT_S = 1.5
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.settimeout(5)
            sock.connect(("127.0.0.1", self.listener.port))
            sock.sendall(self.line("run_python", {"code": "result = 'x' * (24 * 1024 * 1024)"}))  # (never read)
            deadline = time.time() + 10
            while time.time() < deadline and not any(c.outgoing is not None for c in self.listener.clients):
                time.sleep(0.01)
            self.assertTrue(any(c.outgoing is not None for c in self.listener.clients), "the reply is queued")
            ticks[:] = []
            deadline = time.time() + 10
            while self.listener.clients and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.listener.clients, [], "dropped once its reply took too long")
            self.assertTrue(ticks, "the timer kept ticking")
            self.assertLess(max(ticks), 0.5, "no tick waits on the client (longest %.2f s)" % max(ticks))
        finally:
            fl.SEND_TIMEOUT_S = saved
            sock.close()

    def test_a_large_reply_reaches_a_client_that_reads(self):
        reply = self.send_raw(self.line("run_python", {"code": "result = 'y' * (3 * 1024 * 1024)"}))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(len(reply["result"]["result"]), 3 * 1024 * 1024)

    def test_a_reply_past_the_limit_is_an_error(self):
        saved = fl.MAX_REPLY_BYTES
        fl.MAX_REPLY_BYTES = 4096
        try:
            reply = self.send_raw(self.line("run_python", {"code": "result = 'z' * 10000"}, request_id=5))
        finally:
            fl.MAX_REPLY_BYTES = saved
        self.assertFalse(reply["ok"], reply)
        self.assertEqual((reply["id"], reply["error"]["code"]), (5, "too_large"))

    def test_stop_removes_own_state_file(self):
        path = self.listener.state_file
        self.stop_event.set()
        self.thread.join(5)
        self.listener.stop()
        self.assertFalse(os.path.exists(path))

    def test_stop_keeps_a_newer_listeners_state_file(self):
        path = self.listener.state_file
        newer = fl.Listener(port=0, state_file=path)
        newer.bind()
        try:
            self.stop_event.set()
            self.thread.join(5)
            self.listener.stop()
            with open(path) as handle:
                self.assertEqual(json.load(handle)["token"], newer.token)
        finally:
            newer.stop()


class ReplyQueueTests(ListenerCase):
    """The reply sent without blocking, a slice per tick, on a socket that takes a little at a time."""

    class Sock(object):
        def __init__(self, takes):
            self.takes, self.sent, self.closed, self.blocking = list(takes), b"", False, []

        def setblocking(self, flag):
            self.blocking.append(flag)

        def settimeout(self, value):
            self.blocking.append(value is None)

        def sendall(self, data):
            raise AssertionError("sendall blocks until everything is sent")

        def send(self, data):
            take = self.takes.pop(0) if self.takes else 0
            if not take:
                raise socket.error(10035, "would block")
            self.sent += data[:take]
            return len(data[:take])

        def close(self):
            self.closed = True

    def test_the_reply_goes_out_over_several_ticks(self):
        sock = self.Sock([5, 0, 7, 0, 100000])
        client = fl._Client(sock)
        self.listener.clients.append(client)
        self.listener._reply(client, {"ok": True, "result": u"abcdefghijklmnopqrstuvwxyz"})
        self.assertNotIn(True, sock.blocking, "never switched to blocking")
        self.assertEqual(len(sock.sent), 5)
        self.assertIn(client, self.listener.clients, "kept until it has it all")
        self.listener._flush(client)  # (the next tick's)
        self.assertEqual(len(sock.sent), 12)
        self.assertIn(client, self.listener.clients)
        self.listener._flush(client)
        self.assertEqual(json.loads(sock.sent.decode("utf-8"))["result"], u"abcdefghijklmnopqrstuvwxyz")
        self.assertTrue(sock.closed)
        self.assertNotIn(client, self.listener.clients)

    def test_a_reply_not_taken_in_time_drops_the_client(self):
        sock = self.Sock([])
        client = fl._Client(sock)
        self.listener.clients.append(client)
        self.listener._reply(client, {"ok": True, "result": u"x"})
        self.assertIn(client, self.listener.clients)
        client.send_deadline = time.time() - 1
        self.listener._flush(client)
        self.assertTrue(sock.closed)
        self.assertNotIn(client, self.listener.clients)


class FontCase(ListenerCase):
    """Two open fonts: the sample font in front and the two-master write font behind it, with the Qt fakes."""

    def setUp(self):
        ListenerCase.setUp(self)
        self.sample = fakes.sample_font()
        self.font = fakes.write_font()
        self.state.fonts = [self.sample, self.font]
        self.state.current = self.sample
        self.qt = fakes.install_qt(self.state)

    def tearDown(self):
        fakes.uninstall_qt(self.qt)
        ListenerCase.tearDown(self)

    def write(self, op, args=None, **extra):
        payload = {"font": 7}
        payload.update(args or {})
        return self.ok(op, payload, **extra)

    def refused(self, op, args, code):
        payload = {"font": 7}
        payload.update(args)
        steps, flags = len(self.state.steps), len(self.font._f_flags)
        error = self.fails(op, payload, code=code)
        self.assertEqual(len(self.state.steps), steps, "a refused write records nothing")
        self.assertEqual(len(self.font._f_flags), flags, "a refused write does not mark the font")
        return error

    def glyph(self, name):
        return self.font.findName(name)

    def layer(self, name, master="Regular"):
        return self.glyph(name)._f_layer(master)


class FontTargetTests(FontCase):
    def test_reads_default_to_the_font_in_front(self):
        self.assertEqual(self.ok("font_info")["id"], 1)
        self.assertEqual(self.ok("glyph", {"name": "A"})["font"], {"id": 1, "name": "Free Sans Regular"})

    def test_a_font_is_named_by_id_path_or_names(self):
        for wanted in (7, "7", "WriteSans-Regular", "Write Sans Regular", "fixtures/Write.vfc",
                       os.path.join("fixtures", "Write.vfc"), "fixtures\\Write.vfc"):
            self.assertEqual(self.ok("font_info", {"font": wanted})["id"], 7, wanted)
        self.assertEqual(self.ok("glyph", {"name": "Q", "font": 7})["font"]["id"], 7)

    def test_an_unknown_font_lists_the_open_ones(self):
        error = self.fails("font_info", {"font": "Nope-Bold"}, code="no_such_font")
        self.assertIn("Write Sans Regular (id 7)", error["message"])
        self.fails("font_info", {"font": 99}, code="no_such_font")

    def test_two_fonts_with_the_same_name_are_not_guessed(self):
        twin = fakes.write_font(font_id=8, path="fixtures/Twin.vfc")
        self.state.fonts.append(twin)
        error = self.fails("font_info", {"font": "WriteSans-Regular"}, code="ambiguous_font")
        self.assertIn("id 8", error["message"])
        self.assertEqual(self.ok("font_info", {"font": 8})["path"], "fixtures/Twin.vfc")

    def test_digits_other_than_ascii_are_no_id(self):
        self.fails("font_info", {"font": u"\u00b2"}, code="no_such_font")  # (isdigit is true of it; int() refuses it)

    def test_font_must_be_an_id_or_text(self):
        for bad in (True, "", "  ", 1.5, [7], {"id": 7}):
            self.fails("font_info", {"font": bad}, code="bad_args")

    def test_writes_never_guess_the_font(self):
        error = self.fails("set_kerning", {"left": "A", "right": "B", "value": -5}, code="bad_args")
        self.assertIn("Name the font", error["message"])
        self.assertEqual(self.state.steps, [])

    def test_fonts_lists_every_open_font(self):
        fonts = self.ok("fonts")["fonts"]
        self.assertEqual([(f["id"], f["in_front"], f["masters"]) for f in fonts],
                         [(1, True, ["Regular"]), (7, False, ["Regular", "Bold"])])

    def test_no_font_open(self):
        self.state.fonts, self.state.current = [], None
        self.fails("font_info", {"font": 7}, code="no_font")


class ExtendedReadTests(FontCase):
    def test_sidebearings_come_from_the_ink_box(self):
        g = self.ok("glyph", {"name": "O", "font": 7})
        self.assertEqual(g["bounding_box"], {"x": 0, "y": 0, "width": 0, "height": 0})  # (the fake's node box, unset)
        self.assertEqual(g["ink_box"]["x"], 40.4)
        self.assertAlmostEqual(g["sidebearings"]["left"], 40.4)
        self.assertAlmostEqual(g["sidebearings"]["right"], 500 - (40.4 + 419.2))

    def test_components_and_anchors(self):
        g = self.ok("glyph", {"name": "Aacute", "font": 7})
        self.assertEqual(g["components"], [{"glyph": "A", "transform": [1, 0, 0, 1, 0, 0]},
                                           {"glyph": "acute", "transform": [1, 0, 0, 1, 200, 0]}])
        self.assertEqual(self.ok("glyph", {"name": "A", "font": 7})["anchors"], [{"name": "top", "x": 250.0, "y": 400.0}])
        out = self.ok("outline", {"name": "Aacute", "font": 7})
        self.assertEqual([s.get("component") for s in out["shapes"]], ["A", "acute"])
        self.assertNotIn("component", self.ok("outline", {"name": "A", "font": 7})["shapes"][0])

    def test_kerning_pair_and_page(self):
        self.assertEqual(self.ok("kerning", {"font": 7, "left": "A", "right": "B"})["value"], -20.0)
        self.assertEqual(self.ok("kerning", {"font": 7, "left": "@V", "right": "A"})["value"], -50.0)
        self.assertIsNone(self.ok("kerning", {"font": 7, "left": "B", "right": "A"})["value"])
        self.assertEqual(self.ok("kerning", {"font": 7, "master": "Bold", "left": "A", "right": "B"})["value"], -25.0)
        page = self.ok("kerning", {"font": 7, "limit": 2, "classes": True})
        self.assertEqual(page["pairs"], [{"left": "A", "right": "B", "value": -20.0},
                                         {"left": "@V", "right": "A", "value": -50.0}])
        self.assertTrue(page["more"])
        self.assertEqual(page["classes"]["round"], {"side": "KernRight", "glyphs": ["O", "Q"]})

    def test_kerning_reads_refuse_what_fontlab_would_misread(self):
        self.fails("kerning", {"font": 7, "left": "A"}, code="bad_args")
        self.fails("kerning", {"font": 7, "left": "V", "right": "A"}, code="class_name_clash")
        self.fails("kerning", {"font": 7, "left": "@round", "right": "A"}, code="wrong_side")
        self.fails("kerning", {"font": 7, "left": "@nope", "right": "A"}, code="no_class")
        self.fails("kerning", {"font": 7, "left": "Zz", "right": "A"}, code="no_glyph")
        self.fails("kerning", {"font": 7, "master": "Black"}, code="no_master")

    def test_features(self):
        result = self.ok("features", {"font": 7, "tag": "ss01", "prefix": True})
        self.assertEqual(result["tags"], ["liga", "ss01"])
        self.assertIn("sub Q by O", result["code"])
        self.assertIn("languagesystem", result["prefix"])
        self.fails("features", {"font": 7, "tag": "ss02"}, code="no_feature")
        self.fails("features", {"font": 7, "tag": "ss0"}, code="bad_args")


class KerningWriteTests(FontCase):
    def test_a_first_write_records_the_font_before_it(self):
        reply = self.write("set_kerning", {"left": "B", "right": "A", "value": -30})
        self.assertEqual((reply["changed"], reply["before"], reply["value"]), (True, None, -30.0))
        self.assertEqual([title for title, _ in self.state.steps],
                         ["FreeLab MCP: kerning B A (before)", "FreeLab MCP: kerning B A"])
        self.assertIs(self.state.steps[0][1][0], self.font)
        self.assertEqual(self.font._f_flags, [(2, True)], "marked modified")
        self.assertTrue(reply["history"]["recorded"])
        self.assertEqual(reply["history"]["history_state"], self.font.currentHistoryState())
        self.assertEqual(self.font.kerning("Regular").get(("B", "A")), -30.0)
        self.write("set_kerning", {"left": "B", "right": "A", "value": -31})
        self.assertEqual(len(self.state.steps), 3, "a later write needs no (before) step")

    def test_the_same_value_changes_nothing(self):
        reply = self.write("set_kerning", {"left": "A", "right": "B", "value": -20})
        self.assertFalse(reply["changed"])
        self.assertEqual(self.state.steps, [])

    def test_classes_and_masters(self):
        reply = self.write("set_kerning", {"left": "@V", "right": "@round", "value": -15, "master": "Bold"})
        self.assertEqual((reply["left"], reply["right"], reply["master"]), ("@V", "@round", "Bold"))
        self.assertEqual(self.font.kerning("Bold").get(("V", "round")), -15.0)
        self.assertIsNone(self.font.kerning("Regular").get(("V", "round")))

    def test_refusals(self):
        self.refused("set_kerning", {"left": "V", "right": "A", "value": -5}, "class_name_clash")
        self.refused("set_kerning", {"left": "@round", "right": "A", "value": -5}, "wrong_side")
        self.refused("set_kerning", {"left": "A", "right": "Nope", "value": -5}, "no_glyph")
        self.refused("set_kerning", {"left": "A", "right": "B", "value": -5, "master": "Black"}, "no_master")
        for value in (True, "5", None, float("nan"), 10001, -10001, [1]):
            self.refused("set_kerning", {"left": "A", "right": "B", "value": value}, "bad_args")

    def test_kerning_is_kept_in_whole_units(self):
        """Codex, 2026-11-16: FontLab stored -19.5 as -20, and the write was refused as not kept."""
        self.font.kerning("Regular")._f_round = True
        reply = self.write("set_kerning", {"left": "B", "right": "A", "value": -19.5})
        self.assertEqual(reply["value"], -20.0)
        self.assertIn("whole units", reply["rounded"])

    def test_a_write_fontlab_does_not_keep_is_reported(self):
        self.font.kerning("Regular")._f_drop = True
        self.fails("set_kerning", {"font": 7, "left": "B", "right": "A", "value": -9}, code="not_kept")

    def test_nothing_is_written_when_the_history_cannot_record_the_font_first(self):
        self.state.fail_history = "before"
        error = self.fails("set_kerning", {"font": 7, "left": "B", "right": "A", "value": -9}, code="history_failed")
        self.assertIn("Nothing was changed", error["message"])
        self.assertIsNone(self.font.kerning("Regular").get(("B", "A")))
        self.assertEqual(self.font._f_flags, [])

    def test_a_step_the_history_did_not_record_is_said(self):
        self.write("set_kerning", {"left": "B", "right": "A", "value": -9})  # (the font recorded)
        self.state.fail_history = "step"
        reply = self.write("set_kerning", {"left": "B", "right": "A", "value": -8})
        self.assertFalse(reply["history"]["recorded"])
        self.assertIn("Undo cannot take it back", reply["history"]["warning"])
        self.assertEqual(self.font._f_flags[-1], (2, True), "still marked modified")
        self.state.fail_history = None
        self.write("set_kerning", {"left": "B", "right": "A", "value": -7})
        self.assertTrue(self.state.steps[-2][0].endswith("(before)"), "the font is recorded again after a failed step")

    def test_the_client_giving_up_changes_nothing(self):
        saved = fl._check_deadline

        def late(listener):
            listener.deadline = time.time() - 1
            saved(listener)
        fl._check_deadline = late
        try:
            self.fails("set_kerning", {"font": 7, "left": "B", "right": "A", "value": -9}, code="expired")
        finally:
            fl._check_deadline = saved
        self.assertIsNone(self.font.kerning("Regular").get(("B", "A")))

    def test_remove(self):
        reply = self.write("remove_kerning", {"left": "A", "right": "@round"})
        self.assertEqual((reply["before"], reply["value"]), (-10.0, None))
        self.assertIsNone(self.font.kerning("Regular").get(("A", "round")))
        self.refused("remove_kerning", {"left": "A", "right": "@round"}, "no_pair")


class MetricsWriteTests(FontCase):
    def test_left_sidebearing_moves_the_outline_and_keeps_the_right(self):
        reply = self.write("set_metrics", {"glyph": "A", "left_sidebearing": 70})
        self.assertEqual(reply["before"], {"advance_width": 500.0, "left_sidebearing": 50.0, "right_sidebearing": 50.0})
        self.assertEqual(reply["after"], {"advance_width": 520.0, "left_sidebearing": 70.0, "right_sidebearing": 50.0})
        self.assertEqual(reply["moved_by"], 20.0)
        self.assertEqual(self.layer("A").findAnchor("top").point.x(), 270.0, "the anchor moves with the outline")
        self.assertEqual(self.layer("A", "Bold").advanceWidth, 520.0, "the other master is left alone")
        self.assertIn(self.glyph("A"), self.state.steps[-1][1], "the glyph is part of the step")
        self.assertNotIn("rounded", reply)

    def test_width_alone_keeps_the_outline(self):
        reply = self.write("set_metrics", {"glyph": "A", "advance_width": 600})
        self.assertEqual(reply["after"], {"advance_width": 600.0, "left_sidebearing": 50.0, "right_sidebearing": 150.0})

    def test_right_sidebearing_and_both_sides(self):
        self.assertEqual(self.write("set_metrics", {"glyph": "B", "right_sidebearing": 10})["after"]["advance_width"], 450.0)
        reply = self.write("set_metrics", {"glyph": "B", "left_sidebearing": 0, "right_sidebearing": 0, "layer": "Bold"})
        self.assertEqual(reply["after"], {"advance_width": 400.0, "left_sidebearing": 0.0, "right_sidebearing": 0.0})

    def test_width_with_right_sidebearing_moves_the_outline(self):
        """Codex, 2026-11-16: advance_width with right_sidebearing kept the outline and dropped the sidebearing."""
        reply = self.write("set_metrics", {"glyph": "A", "advance_width": 600, "right_sidebearing": 70})
        self.assertEqual(reply["after"], {"advance_width": 600.0, "left_sidebearing": 130.0, "right_sidebearing": 70.0})
        self.assertEqual(reply["moved_by"], 80.0)
        reply = self.write("set_metrics", {"glyph": "B", "advance_width": 500, "left_sidebearing": 10})
        self.assertEqual(reply["after"], {"advance_width": 500.0, "left_sidebearing": 10.0, "right_sidebearing": 110.0})

    def test_a_component_moves_by_its_offset(self):
        self.write("set_metrics", {"glyph": "Aacute", "left_sidebearing": 40})
        shapes = self.layer("Aacute").shapes
        self.assertEqual([s.transform.dx() for s in shapes], [-10.0, 190.0])

    def test_fontlab_rounding_is_reported(self):
        self.layer("O")._f_round = True
        reply = self.write("set_metrics", {"glyph": "O", "right_sidebearing": 0})
        self.assertEqual(reply["after"]["advance_width"], 460.0)
        self.assertIn("right sidebearing", reply["rounded"])

    def test_refusals(self):
        self.refused("set_metrics", {"glyph": "A"}, "bad_args")
        self.refused("set_metrics", {"glyph": "A", "advance_width": 500, "left_sidebearing": 1, "right_sidebearing": 1}, "bad_args")
        self.refused("set_metrics", {"glyph": "A", "advance_width": -1}, "bad_args")
        self.refused("set_metrics", {"glyph": "A", "right_sidebearing": -600}, "bad_args")
        self.refused("set_metrics", {"glyph": "space", "left_sidebearing": 5}, "no_outline")
        self.refused("set_metrics", {"glyph": "acute", "advance_width": 5, "layer": "Bold"}, "no_layer")
        self.refused("set_metrics", {"glyph": "Nope", "advance_width": 5}, "no_glyph")
        object.__setattr__(self.layer("A"), "metricsLeft", "=H")
        error = self.refused("set_metrics", {"glyph": "A", "left_sidebearing": 60}, "metrics_expression")
        self.assertIn("=H", error["message"])

    def test_width_of_an_empty_glyph(self):
        self.assertEqual(self.write("set_metrics", {"glyph": "space", "advance_width": 300})["after"]["advance_width"], 300.0)

    def test_nothing_to_change(self):
        self.assertFalse(self.write("set_metrics", {"glyph": "A", "left_sidebearing": 50})["changed"])
        self.assertEqual(self.state.steps, [])


class GlyphWriteTests(FontCase):
    def test_rename_follows_components(self):
        reply = self.write("rename_glyph", {"glyph": "acute", "new_name": "acutecomb"})
        self.assertEqual((reply["before"], reply["glyph"]), ("acute", "acutecomb"))
        self.assertEqual(self.layer("Aacute").shapes[1].shapeData.componentGlyph, "acutecomb")
        self.assertGreater(self.font.fgPackage._f_rebuilds, 0)
        steps = [objects for title, objects in self.state.steps]
        self.assertIn(self.glyph("acutecomb"), steps[0], "the glyph is recorded before it is renamed")

    def test_rename_refused_while_kerning_classes_or_features_use_the_name(self):
        for name, where in (("A", "kerning pair"), ("O", "kerning class"), ("B", "class caps"), ("Q", "feature code (ss01)")):
            error = self.refused("rename_glyph", {"glyph": name, "new_name": name + ".new"}, "glyph_in_use")
            self.assertIn(where, error["message"])
        self.assertIsNotNone(self.font.findName("A"))

    def test_a_class_of_the_same_name_in_feature_code_is_no_use_of_the_glyph(self):
        object.__setattr__(self.font.fgPackage.features, "_f_prefix", u"@acute = [Aacute];\n")
        self.assertEqual(self.write("rename_glyph", {"glyph": "acute", "new_name": "acutecomb"})["glyph"], "acutecomb")

    def test_rename_checks_the_new_name(self):
        for bad in ("", "1abc", "-a", "a b", "x" * 64, u"é", None, 5):
            self.refused("rename_glyph", {"glyph": "acute", "new_name": bad}, "bad_args")
        self.refused("rename_glyph", {"glyph": "acute", "new_name": "space"}, "name_taken")

    def test_unicodes(self):
        reply = self.write("set_unicodes", {"glyph": "acute", "unicodes": [0x301, 0xE000]})
        self.assertEqual((reply["before"], reply["unicodes"]), ([0xB4], [0x301, 0xE000]))
        self.assertEqual(self.ok("glyph", {"font": 7, "unicode": 0xE000})["name"], "acute", "the lookup is rebuilt")
        self.assertEqual(self.write("set_unicodes", {"glyph": "acute", "unicodes": []})["unicodes"], [])
        self.assertFalse(self.write("set_unicodes", {"glyph": "acute", "unicodes": []})["changed"])
        self.assertEqual(self.write("set_unicodes", {"unicode": 0x41, "unicodes": [0x41, 0x1F600]})["unicodes"], [0x41, 0x1F600])

    def test_unicodes_refused(self):
        self.refused("set_unicodes", {"glyph": "acute", "unicodes": [0x41]}, "unicode_taken")
        for bad in ([0xD800], [-1], [0x110000], [True], [65, 65], ["65"], 65, None, list(range(65))):
            self.refused("set_unicodes", {"glyph": "acute", "unicodes": bad}, "bad_args")

    def test_a_stale_lookup_is_rebuilt_before_a_code_point_is_called_free(self):
        object.__setattr__(self.glyph("Q").fgGlyph, "_f_unicodes", [0x51, 0xE001])  # (changed without a rebuild)
        self.refused("set_unicodes", {"glyph": "acute", "unicodes": [0xE001]}, "unicode_taken")

    def test_add_component_glyph(self):
        reply = self.write("add_component_glyph", {"name": "Bacute", "components": [{"glyph": "B"}, {"glyph": "acute", "x": 200, "y": 30}],
                                                   "unicodes": [0xE100]})
        self.assertEqual(reply["masters"], [{"master": "Regular", "advance_width": 500.0}])
        self.assertEqual(reply["skipped_masters"], ["Bold"], "acute is not drawn in Bold")
        glyph = self.glyph("Bacute")
        layer = glyph._f_layer("Regular")
        self.assertEqual([(s.shapeData.componentGlyph, s.transform.dx(), s.transform.dy()) for s in layer.shapes],
                         [("B", 0.0, 0.0), ("acute", 200.0, 30.0)])
        self.assertEqual(glyph.fgGlyph.unicodes, [0xE100])
        self.assertEqual(glyph.fgGlyph._f_layers_when_set, 1, "code points set once the layers are made")
        self.assertEqual(self.state.content_updates, [7], "the Font window is told of the new glyph")
        self.assertIn(glyph, self.state.steps[-1][1])

    def test_add_component_glyph_in_every_master_with_a_width(self):
        reply = self.write("add_component_glyph", {"name": "AB", "components": [{"glyph": "A"}, {"glyph": "B", "x": 500}],
                                                   "advance_width": 1000})
        self.assertEqual(reply["masters"], [{"master": "Regular", "advance_width": 1000.0},
                                            {"master": "Bold", "advance_width": 1000.0}])

    def test_add_component_glyph_refusals(self):
        self.refused("add_component_glyph", {"name": "A", "components": [{"glyph": "B"}]}, "name_taken")
        self.refused("add_component_glyph", {"name": "X", "components": [{"glyph": "Nope"}]}, "no_glyph")
        self.refused("add_component_glyph", {"name": "X", "components": [{"glyph": "X"}]}, "bad_args")
        self.refused("add_component_glyph", {"name": "X", "components": [{"glyph": "B", "z": 1}]}, "bad_args")
        self.refused("add_component_glyph", {"name": "X", "components": []}, "bad_args")
        self.refused("add_component_glyph", {"name": "X", "components": [{"glyph": "B", "x": "1"}]}, "bad_args")
        self.refused("add_component_glyph", {"name": "X", "components": [{"glyph": "B"}], "unicodes": [0x41]}, "unicode_taken")
        self.font._f_glyphs.remove(self.glyph("V"))  # (no glyph is drawn in no master: build a case)
        object.__setattr__(self.font, "masters", ["Black"])
        self.refused("add_component_glyph", {"name": "X", "components": [{"glyph": "B"}]}, "no_master")

    def test_a_glyph_fontlab_did_not_build_is_taken_out_again(self):
        object.__setattr__(self.font, "_f_drop_components", True)
        error = self.fails("add_component_glyph", {"font": 7, "name": "X", "components": [{"glyph": "A"}, {"glyph": "B"}]},
                           code="not_kept")
        self.assertIn("put back", error["message"])
        self.assertIsNone(self.font.findName("X"))

    def test_anchors(self):
        reply = self.write("set_anchor", {"glyph": "B", "name": "top", "x": 250, "y": 700})
        self.assertIsNone(reply["before"])
        self.assertEqual(self.layer("B").findAnchor("top").point.y(), 700.0)
        reply = self.write("set_anchor", {"glyph": "B", "name": "top", "x": 260, "y": 710})
        self.assertEqual(reply["before"], {"x": 250.0, "y": 700.0})
        self.assertEqual(len(self.layer("B").anchors), 1, "moved, not added again")
        self.assertFalse(self.write("set_anchor", {"glyph": "B", "name": "top", "x": 260, "y": 710})["changed"])
        self.write("set_anchor", {"glyph": "B", "name": "_top", "x": 1, "y": 2, "layer": "Bold"})
        self.assertEqual(self.write("remove_anchor", {"glyph": "B", "name": "top"})["before"], {"x": 260.0, "y": 710.0})
        self.assertEqual(self.layer("B").anchors, [])
        self.refused("remove_anchor", {"glyph": "B", "name": "top"}, "no_anchor")
        for bad in ({"name": "a b"}, {"name": ""}, {"x": "1"}, {"y": None}, {"x": 40000}):
            args = {"glyph": "B", "name": "top", "x": 1, "y": 1}
            args.update(bad)
            self.refused("set_anchor", args, "bad_args")

    def test_feature_code(self):
        code = u"feature ss02 {\n  sub A by B;\n} ss02;\n"
        reply = self.write("set_feature", {"tag": "ss02", "code": code})
        self.assertTrue(reply["created"])
        self.assertEqual(self.font.fgPackage.features.get_feature("ss02"), code)
        reply = self.write("set_feature", {"tag": "liga", "code": u"# made by hand\nfeature liga {\n  sub A A by V;\n} liga;"})
        self.assertFalse(reply["created"])
        self.assertIn("sub A B by V", reply["before"])
        self.assertFalse(self.write("set_feature", {"tag": "ss02", "code": code})["changed"])
        for bad in ({"tag": "ss2"}, {"tag": "ss02", "code": "sub A by B;"}, {"tag": "ss02", "code": "feature ss03 { } ss03;"},
                    {"tag": "ss02", "code": None}):
            args = {"tag": "ss02", "code": code}
            args.update(bad)
            self.refused("set_feature", args, "bad_args")
        self.font.fgPackage.features._f_refuse = "drop"
        self.fails("set_feature", {"font": 7, "tag": "ss03", "code": u"feature ss03 { sub A by B; } ss03;"}, code="not_kept")

    def test_feature_code_without_a_final_line_break(self):
        """Codex, 2026-11-16: FontLab adds a line break, which made a kept write a not_kept one, left in the
        font outside the History."""
        code = u"feature ss03 { sub A by B; } ss03;"
        reply = self.write("set_feature", {"tag": "ss03", "code": code})
        self.assertEqual(reply["code"], code + u"\n")
        self.assertFalse(self.write("set_feature", {"tag": "ss03", "code": code})["changed"])

    def test_feature_code_fontlab_did_not_keep_is_put_back(self):
        features = self.font.fgPackage.features
        features._f_refuse = "garble"
        error = self.fails("set_feature", {"font": 7, "tag": "ss03", "code": u"feature ss03 { sub A by B; } ss03;\n"},
                           code="not_kept")
        self.assertIn("put back", error["message"])
        self.assertNotIn("ss03", features.keys(), "a new feature is taken out again")
        before = features.get_feature("liga")
        features._f_garble_skip = before
        self.fails("set_feature", {"font": 7, "tag": "liga", "code": u"feature liga { sub B by A; } liga;\n"}, code="not_kept")
        self.assertEqual(features.get_feature("liga"), before, "an existing one gets its code back")

    def test_names_ending_with_a_line_break_are_refused(self):
        """Codex, 2026-11-16: $ matched before a final line break, so "probe\n" passed as an anchor name."""
        self.refused("set_anchor", {"glyph": "B", "name": "top\n", "x": 1, "y": 1}, "bad_args")
        self.refused("rename_glyph", {"glyph": "acute", "new_name": "acute2\n"}, "bad_args")
        self.refused("set_feature", {"tag": "ss02\n", "code": u"feature ss02 { sub A by B; } ss02;"}, "bad_args")

    def test_renaming_to_the_same_name_changes_nothing(self):
        reply = self.write("rename_glyph", {"glyph": "acute", "new_name": "acute"})
        self.assertFalse(reply["changed"])


class UndoTests(FontCase):
    def setUp(self):
        FontCase.setUp(self)
        self.state.current = self.font  # the write font in front

    def test_undo_the_last_write_with_its_before_step(self):
        state = self.write("set_kerning", {"left": "B", "right": "A", "value": -5})["history"]["history_state"]
        self.assertEqual(len(self.state.history), 2, "a (before) step and the write's")
        reply = self.write("undo", {"expected_state": state})
        self.assertEqual(reply["undone"], ["Freelab Mcp: Kerning B A", "Freelab Mcp: Kerning B A (Before)"])
        self.assertEqual(reply["history_state_before"], state)
        self.assertEqual(reply["history_state"], 0)
        self.assertIsNone(reply["next_undo"])
        self.assertEqual(self.state.history, [])

    def test_each_write_is_one_undo(self):
        """Codex, 2026-11-16: a glyph's own (before) step stayed on the History after its write was undone, and
        the next undo took it back while reporting that nothing was undone."""
        first = self.write("set_anchor", {"glyph": "B", "name": "top", "x": 1, "y": 2})["history"]["history_state"]
        second = self.write("set_unicodes", {"glyph": "acute", "unicodes": [0x301]})["history"]["history_state"]
        self.assertTrue(self.state.history[-2][0].endswith("(before)"), "the glyph recorded before the second write")
        reply = self.write("undo", {"expected_state": second})
        self.assertEqual(len(reply["undone"]), 2)
        self.assertEqual(reply["history_state"], first)
        self.assertEqual(reply["next_undo"], "Freelab Mcp: Anchor Top Of B")
        self.assertEqual(self.glyph("acute").fgGlyph.unicodes, [0xB4], "its code point back")
        reply = self.write("undo", {"expected_state": first})
        self.assertEqual(reply["undone"][0], "Freelab Mcp: Anchor Top Of B")

    def test_a_before_step_left_alone_is_undone_alone_and_said(self):
        self.state.fail_history = "step"
        self.write("set_kerning", {"left": "B", "right": "A", "value": -5})  # (its step refused: only (before) is left)
        self.state.fail_history = None
        reply = self.write("undo")
        self.assertEqual(reply["undone"], ["Freelab Mcp: Kerning B A (Before)"])
        self.assertIn("changed nothing", reply["note"])

    def test_another_writes_before_step_stays(self):
        """Live, 2026-11-16: an Undo of a write without a (before) step took back an earlier write's leftover one."""
        self.write("set_kerning", {"left": "B", "right": "A", "value": -5})  # (the font recorded)
        self.state.history.append(("FreeLab MCP: metrics of O (before)", []))  # (a write refused after it)
        reply = self.write("set_kerning", {"left": "B", "right": "A", "value": -6})
        self.assertEqual(self.state.history[-2][0], "FreeLab MCP: metrics of O (before)", "no (before) of its own")
        reply = self.write("undo", {"expected_state": reply["history"]["history_state"]})
        self.assertEqual(reply["undone"], ["Freelab Mcp: Kerning B A"])
        self.assertEqual(reply["next_undo"], "Freelab Mcp: Metrics Of O (Before)")

    def test_undo_refusals(self):
        self.fails("undo", {"font": 7}, code="nothing_to_undo")
        state = self.write("set_kerning", {"left": "B", "right": "A", "value": -5})["history"]["history_state"]
        self.fails("undo", {"font": 7, "expected_state": state + 1}, code="history_moved")
        self.fails("undo", {"font": 7, "expected_state": True}, code="bad_args")
        self.state.current = self.sample
        self.fails("undo", {"font": 7}, code="not_in_front")
        self.state.current = self.font
        self.state.undo_moves = False
        self.fails("undo", {"font": 7}, code="not_kept")
        self.assertEqual(self.state.undone, 1)


class ReviewTests(FontCase):
    """The independent review of 2026-11-16, one test per finding."""

    def setUp(self):
        FontCase.setUp(self)
        self.state.current = self.font  # the write font in front

    def test_1_undo_checks_the_step_by_title_too(self):
        reply = self.write("set_kerning", {"left": "B", "right": "A", "value": -5})
        state, title = reply["history"]["history_state"], reply["history"]["title"]
        self.layer("O")._f_refuse_width = True  # a metrics write FontLab does not keep: put back, its (before) stays
        self.fails("set_metrics", {"font": 7, "glyph": "O", "advance_width": 600}, code="not_kept")
        self.assertTrue(self.state.history[-1][0].endswith("(before)"), "a glyph-only (before) step on top")
        self.assertEqual(self.font.currentHistoryState(), state, "which leaves the font's state where it was")
        error = self.fails("undo", {"font": 7, "expected_state": state, "expected_title": title}, code="history_moved")
        self.assertIn("metrics of o (before)", error["message"].lower())
        self.assertEqual(self.font.kerning("Regular").get(("B", "A")), -5.0, "the kerning write is still there")
        self.state.history.pop()  # (FontLab undoes it on its own here; then the write is next)
        reply = self.write("undo", {"expected_state": state, "expected_title": title})
        self.assertEqual(reply["undone"][0].lower(), title.lower())
        self.assertIsNone(self.font.kerning("Regular").get(("B", "A")), "taken back")
        self.fails("undo", {"font": 7, "expected_state": state, "expected_title": title}, code="history_moved")

    def test_2_a_change_that_cannot_be_put_back_is_recorded_and_marked(self):
        kerning = self.font.kerning("Regular")
        kerning._f_skew = 3.0  # every write lands 3 off, the restore too
        error = self.fails("set_kerning", {"font": 7, "left": "A", "right": "B", "value": -10}, code="not_kept")
        self.assertIn("could not be put back", error["message"])
        self.assertTrue(error["data"]["history"]["recorded"])
        self.assertEqual(self.state.steps[-1][0], "FreeLab MCP: kerning A B", "recorded as the write's step")
        self.assertEqual(self.font._f_flags[-1], (2, True), "and the font marked")

    def test_2_a_change_put_back_says_so_and_is_not_marked(self):
        self.layer("O")._f_refuse_width = True
        error = self.fails("set_metrics", {"font": 7, "glyph": "O", "advance_width": 600}, code="not_kept")
        self.assertIn("put back", error["message"])
        self.assertNotIn("data", error)
        self.assertEqual(self.font._f_flags, [])

    def test_2_an_exception_during_the_change_is_put_back_by_measure(self):
        self.layer("A")._f_raise_transform = True  # the move itself failed: nothing to move back
        error = self.fails("set_metrics", {"font": 7, "glyph": "A", "left_sidebearing": 80}, code="not_kept")
        self.assertIn("applyTransform failed", error["message"])
        self.assertIn("put back", error["message"])
        self.assertEqual((self.layer("A").advanceWidth, self.layer("A").inkBox().x()), (500.0, 50.0))

    def test_2_an_added_glyph_that_cannot_be_taken_out_is_recorded(self):
        object.__setattr__(self.font, "_f_drop_components", True)
        object.__setattr__(self.font, "_f_refuse_delete", True)
        error = self.fails("add_component_glyph", {"font": 7, "name": "X", "components": [{"glyph": "A"}, {"glyph": "B"}]},
                           code="not_kept")
        self.assertIn("could not be put back", error["message"])
        self.assertTrue(error["data"]["history"]["recorded"])
        self.assertIn(self.font.findName("X"), self.state.steps[-1][1], "the glyph left is part of the step")
        self.assertEqual(self.state.content_updates, [7])

    def test_2_anchor_and_rename_read_backs_put_back(self):
        self.layer("B")._f_anchor_drift = 0.5
        error = self.fails("set_anchor", {"font": 7, "glyph": "B", "name": "top", "x": 1, "y": 2}, code="not_kept")
        self.assertIn("put back", error["message"])
        self.assertEqual(self.layer("B").anchors, [])
        object.__setattr__(self.font, "_f_rename_ignored", True)
        error = self.fails("rename_glyph", {"font": 7, "glyph": "acute", "new_name": "acutecomb"}, code="not_kept")
        self.assertIn("put back", error["message"])

    def test_3_a_stale_lookup_is_rebuilt_before_it_is_trusted(self):
        object.__setattr__(self.glyph("acute"), "name", "acutecomb")  # (renamed without FontLab's lookup knowing)
        self.assertIsNone(self.font.findName("acutecomb"))
        self.assertEqual(self.ok("glyph", {"font": 7, "name": "acutecomb"})["name"], "acutecomb")
        object.__setattr__(self.glyph("acutecomb"), "name", "acute2")
        self.fails("glyph", {"font": 7, "name": "acutecomb"}, code="no_glyph")  # (found under a name it no longer has)

    def test_5_feature_code_is_exactly_one_block(self):
        good = u"# note\nfeature ss04 {\n  # a } in a comment\n  lookup x { sub A by B; } x;\n} ss04;\n# after\n"
        self.assertTrue(self.write("set_feature", {"tag": "ss04", "code": good})["changed"])
        for bad in (u"feature ss04 { sub A by B; } ss04;\nfeature liga { sub A by V; } liga;",
                    u"lookup x { sub A by B; } x;\nfeature ss04 { lookup x; } ss04;",
                    u"languagesystem DFLT dflt;\nfeature ss04 { sub A by B; } ss04;",
                    u"#> feature\nfeature ss04 { sub A by B; } ss04;",
                    u"feature ss04 { sub A by B; } ss04;\n#< feature",
                    u"feature ss04 { sub A by B; ss04;",
                    u"feature ss04 { sub A by B; } } ss04;",
                    u"feature ss04 { sub A by B; } ss05;"):
            self.refused("set_feature", {"tag": "ss04", "code": bad}, "bad_args")
        crlf = u"feature ss04 {\r\n  # a } in a comment\r\n  lookup x { sub A by B; } x;\r\n} ss04;"
        self.assertTrue(fl._same_code(u"feature ss04 {\n} ss04;\n", u"feature ss04 {\r\n} ss04;"))
        self.assertTrue(self.write("set_feature", {"tag": "ss04", "code": crlf})["changed"])

    def test_7_a_step_named_like_the_one_redo_names_must_move_the_font(self):
        self.write("set_kerning", {"left": "B", "right": "A", "value": -5})
        self.write("set_kerning", {"left": "B", "right": "A", "value": -6})
        self.write("set_kerning", {"left": "B", "right": "A", "value": -7})
        self.write("undo")  # (Redo now names "kerning B A", as does the step on top)
        self.state.undo_moves = False
        self.fails("undo", {"font": 7}, code="not_kept")

    def test_8_digits_are_an_id_or_a_name_and_font_name_confirms(self):
        object.__setattr__(self.sample, "psn", "2024")
        self.assertEqual(self.ok("font_info", {"font": "2024"})["id"], 1, "digits that are no font's id: a name")
        self.assertEqual(self.ok("font_info", {"font": "7"})["id"], 7)
        self.assertTrue(self.write("set_kerning", {"left": "B", "right": "A", "value": -5, "font_name": "WriteSans-Regular"})["changed"])
        self.refused("set_kerning", {"left": "B", "right": "A", "value": -6, "font_name": "Free Sans Regular"}, "font_mismatch")
        self.refused("set_kerning", {"left": "B", "right": "A", "value": -6, "font_name": "7"}, "font_mismatch")
        self.fails("undo", {"font": 7, "font_name": "fixtures/Test.vfc"}, code="font_mismatch")

    def test_9_class_names_are_compared_as_text(self):
        kerning = self.font.kerning("Regular")
        kerning._f_groups[u"été".encode("utf-8") if fl.PY2 else u"été"] = (["O"], "KernRight")
        self.assertIsNone(self.ok("kerning", {"font": 7, "left": "A", "right": u"@été"})["value"])

    def test_10_whole_units(self):
        self.font.kerning("Regular")._f_round = True
        self.assertFalse(self.write("set_kerning", {"left": "A", "right": "B", "value": -19.6})["changed"],
                         "-19.6 would be stored as -20, the pair's value: nothing to change")
        reply = self.write("set_metrics", {"glyph": "O", "left_sidebearing": 50})  # (ink at 40.4: a 9.6 move)
        self.assertEqual(reply["moved_by"], 10.0, "outlines move by whole units")
        self.assertAlmostEqual(reply["after"]["left_sidebearing"], 50.4)
        self.assertIn("left sidebearing", reply["rounded"])


class CodexRoundTwoTests(FontCase):
    """Codex's review of 2026-11-16 (second round), one test per MCP finding."""

    def setUp(self):
        FontCase.setUp(self)
        self.state.current = self.font

    def unreadable_once_renamed(self, every_time):
        """glyph_named failing as a null check that cannot be read does, from the moment the rename shows (the
        first read-back), once or from then on."""
        real, state = fl.glyph_named, {"armed": False, "raised": 0}

        def flaky(package, name):
            if not state["armed"] and package.findName("acutecomb") is not None:
                state["armed"] = True
            if state["armed"] and (every_time or not state["raised"]):
                state["raised"] += 1
                raise fl.FreeLabError("unreadable", "Could not tell whether the glyph is null (boom).")
            return real(package, name)

        fl.glyph_named = flaky
        return real

    def test_a_read_back_that_cannot_be_read_is_put_back(self):
        """A FreeLabError raised by a read-back after the rename skipped the restore and the History step."""
        real = self.unreadable_once_renamed(False)
        try:
            error = self.fails("rename_glyph", {"font": 7, "glyph": "acute", "new_name": "acutecomb"}, code="not_kept")
        finally:
            fl.glyph_named = real
        self.assertIn("unreadable", error["message"])
        self.assertIn("put back", error["message"])
        self.assertIsNotNone(self.font.findName("acute"), "the old name is back")
        self.assertIsNone(self.font.findName("acutecomb"))

    def test_a_read_back_error_left_in_the_font_is_recorded(self):
        real = self.unreadable_once_renamed(True)
        try:
            error = self.fails("rename_glyph", {"font": 7, "glyph": "acute", "new_name": "acutecomb"}, code="not_kept")
        finally:
            fl.glyph_named = real
        self.assertIn("could not be put back", error["message"])
        self.assertTrue(error["data"]["history"]["recorded"], "a step for what is left, and the font marked")

    def test_dropped_code_points_of_an_added_glyph_are_not_reported_kept(self):
        prop = fakes.fgGlyph.unicodes
        fakes.fgGlyph.unicodes = property(prop.fget, lambda self, values: prop.fset(self, list(values)[:-1]))
        try:
            error = self.fails("add_component_glyph", {"font": 7, "name": "X", "components": [{"glyph": "A"}],
                                                       "unicodes": [0xE100, 0xE101]}, code="not_kept")
        finally:
            fakes.fgGlyph.unicodes = prop
        self.assertIn("Unicode values", error["message"])
        self.assertIn("put back", error["message"])
        self.assertIsNone(self.font.findName("X"), "the glyph is taken out again")

    def test_components_fontlab_swapped_are_not_reported_kept(self):
        real = fl._component_glyph
        fl._component_glyph = lambda shape: u"B"
        try:
            error = self.fails("add_component_glyph", {"font": 7, "name": "X", "components": [{"glyph": "A"}]},
                               code="not_kept")
        finally:
            fl._component_glyph = real
        self.assertIn("not those asked", error["message"])
        self.assertIsNone(self.font.findName("X"))

    def test_an_added_glyph_that_cannot_be_read_is_still_recorded(self):
        """Round 2: with every read of the new glyph failing, collecting the glyphs left let the error out before
        the History step and the modified mark."""
        real = fl.glyph_named

        def unreadable_once_added(package, name):
            if package.findName("X") is not None:
                raise fl.FreeLabError("unreadable", "Could not tell whether the glyph is null (boom).")
            return real(package, name)

        fl.glyph_named = unreadable_once_added
        try:
            error = self.fails("add_component_glyph", {"font": 7, "name": "X", "components": [{"glyph": "A"}]},
                               code="not_kept")
        finally:
            fl.glyph_named = real
        self.assertIn("could not be put back", error["message"])
        self.assertTrue(error["data"]["history"]["recorded"])
        self.assertEqual(self.font._f_flags[-1], (2, True), "the font marked modified")
        self.assertEqual(self.state.content_updates, [7], "the Font window told of the glyph left")

    def test_a_component_offset_fontlab_dropped_is_not_reported_kept(self):
        """Round 2: names, widths, and code points were read back, offsets not."""
        real = fakes.flLayer.setGlyphComponents

        def dropping_offsets(layer, components, width, package, colorize):
            done = real(layer, components, width, package, colorize)
            if layer._f_shapes:
                object.__setattr__(layer._f_shapes[-1], "transform", fakes.QTransform())
            return done

        fakes.flLayer.setGlyphComponents = dropping_offsets
        try:
            error = self.fails("add_component_glyph", {"font": 7, "name": "X",
                                                       "components": [{"glyph": "A"}, {"glyph": "B", "x": 500}]},
                               code="not_kept")
        finally:
            fakes.flLayer.setGlyphComponents = real
        self.assertIn("not at (500, 0)", error["message"])
        self.assertIsNone(self.font.findName("X"), "taken out again")
        reply = self.write("add_component_glyph", {"name": "Y", "components": [{"glyph": "A"}, {"glyph": "B", "x": 500.25}]})
        self.assertEqual(reply["components"][1]["x"], 500.25, "a fraction FontLab keeps is no mismatch")
        self.assertNotIn("rounded", reply)

    def test_offsets_fontlab_rounded_are_reported_as_stored(self):
        """Live, 2026-11-16: Varela Round stored I asked at (500.25, 10.5) at (500, 11); the reply echoed the request."""
        real = fakes.flLayer.setGlyphComponents

        def rounding(layer, components, width, package, colorize):
            done = real(layer, components, width, package, colorize)
            for shape in layer._f_shapes:
                t = shape.transform
                object.__setattr__(shape, "transform", fakes.QTransform(dx=float(round(t.dx())), dy=float(round(t.dy()))))
            return done

        fakes.flLayer.setGlyphComponents = rounding
        try:
            reply = self.write("add_component_glyph", {"name": "Z", "components": [{"glyph": "A"},
                                                                                   {"glyph": "B", "x": 500.25, "y": 10.6}]})
        finally:
            fakes.flLayer.setGlyphComponents = real
        self.assertEqual(reply["components"], [{"glyph": "A", "x": 0.0, "y": 0.0}, {"glyph": "B", "x": 500.0, "y": 11.0}])
        self.assertIn("B", reply["rounded"])

    def test_quoted_braces_do_not_hide_another_feature(self):
        payload = (u'feature ss01 {\n  featureNames { name "{"; };\n} ss01;\nfeature kern { pos A V -200; } kern;\n'
                   u'feature ss01 {\n  featureNames { name "}"; };\n} ss01;\n')
        self.refused("set_feature", {"tag": "ss01", "code": payload}, "bad_args")
        one = u'feature ss01 {\n  featureNames { name "Swash {alt} # one"; };\n  sub A by B;\n} ss01;\n'
        self.assertTrue(self.write("set_feature", {"tag": "ss01", "code": one})["changed"],
                        "a brace or # in a name's text is text")
        self.refused("set_feature", {"tag": "ss01", "code": u'feature ss01 { featureNames { name "open; }; } ss01;'},
                     "bad_args")


class WindowTests(FontCase):
    """Opening, closing, bringing to the front, compiling, the Output panel, and pictures, against fake
    FontLab windows: one window tab per font (Sketchboard first), the panels' own tab widgets beside them."""

    def font_file(self, name="Open.ttf"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(b"font")
        return path

    def ids(self):
        return [font.fgPackage.id for font in self.state.fonts]

    # -- bringing a font to the front
    def test_front_chooses_the_fonts_window_tab(self):
        self.assertEqual(self.ok("front", {"font": 7}), {"font": {"id": 7, "name": "Write Sans Regular"},
                                                         "brought_forward": True})
        self.assertIs(self.state.current, self.font)
        self.assertEqual(self.ok("front", {"font": 7})["brought_forward"], False, "already in front: nothing chosen")

    def test_front_tries_other_tabs_when_none_is_named_after_the_font(self):
        self.state.tabs = [(u"Sketchboard", None), (u"Glyph A", self.sample), (u"Glyph B", self.font)]
        self.assertTrue(self.ok("front", {"font": 7})["brought_forward"])
        self.assertIs(self.state.current, self.font)

    def test_front_without_a_window_of_the_font(self):
        self.state.tabs = [(u"Sketchboard", None), (u" Free Sans Regular ", self.sample)]
        self.assertIn("No window of Write Sans Regular", self.fails("front", {"font": 7}, code="not_found")["message"])
        self.assertIs(self.state.current, self.sample, "each tab tried was put back")

    def test_front_needs_the_font_named(self):
        self.fails("front", {}, code="bad_args")
        self.fails("front", {"font": 7, "font_name": "Free Sans Regular"}, code="font_mismatch")
        self.assertIs(self.state.current, self.sample)

    # -- opening
    def test_open_font_opens_after_the_reply(self):
        path = self.font_file()
        reply = self.ok("open_font", {"path": path})
        self.assertEqual(reply, {"queued": True, "path": path, "fonts_before": [1, 7]})
        self.assertEqual(self.state.loads, [], "not inside the request: FontLab may ask something on the way")
        self.state.fire_timers()
        self.assertEqual(self.state.loads, [path])
        self.assertEqual(self.ids(), [1, 7, 101])
        self.assertEqual(self.listener.opened_paths, [path])

    def test_open_font_names_a_font_already_open(self):
        path = self.font_file()
        object.__setattr__(self.font, "path", path.replace(os.sep, "/"))  # (FontLab mixes separators: live)
        self.assertEqual(self.ok("open_font", {"path": path}), {"already_open": True,
                                                                "font": {"id": 7, "name": "Write Sans Regular"}})
        self.assertEqual((self.state.timers, self.listener.opened_paths), ([], []))

    def test_open_font_refuses_a_path_it_cannot_open(self):
        self.fails("open_font", {"path": "Open.ttf"}, code="bad_args")
        self.fails("open_font", {"path": ""}, code="bad_args")
        self.fails("open_font", {"path": os.path.join(self.tmp, "missing.ttf")}, code="not_found")
        self.assertEqual(self.state.timers, [])

    # -- closing
    def test_close_font_closes_an_unchanged_font_after_the_reply(self):
        reply = self.ok("close_font", {"font": 7})
        self.assertEqual(reply, {"closing": True, "font": {"id": 7, "name": "Write Sans Regular"}, "edited": False,
                                 "discard_changes": False})
        self.assertIs(self.state.current, self.font, "brought to the front: File > Close Font closes the font in front")
        self.assertEqual(self.ids(), [1, 7])
        self.state.fire_timers()
        self.assertEqual(self.ids(), [1])

    def test_close_font_refuses_a_font_with_changes(self):
        self.font._f_states.append(3)  # its History past its first state
        self.assertIn("save it in FontLab first", self.fails("close_font", {"font": 7}, code="unsaved_changes")["message"])
        object.__setattr__(self.sample, "hasChanges", True)
        self.fails("close_font", {"font": 1}, code="unsaved_changes")
        self.assertEqual((self.state.timers, self.ids()), ([], [1, 7]))

    def test_close_font_discards_changes_only_in_a_font_opened_here(self):
        error = self.fails("close_font", {"font": 7, "discard_changes": True}, code="not_opened_here")
        self.assertIn("never throws away changes to a font it did not open", error["message"])
        self.fails("close_font", {"font": 7, "discard_changes": "yes"}, code="bad_args")
        self.assertEqual((self.state.timers, self.ids()), ([], [1, 7]))

    def test_close_font_answers_dont_save_for_a_font_opened_here(self):
        path = self.font_file()
        self.ok("open_font", {"path": path})
        self.state.fire_timers()
        opened = self.state.fonts[-1]
        opened._f_states.append(4)
        self.state.asks_to_save.add(101)
        self.state.current = self.sample
        self.assertIn("close it with discard_changes", self.fails("close_font", {"font": 101}, code="unsaved_changes")["message"])
        self.assertTrue(self.ok("close_font", {"font": 101, "discard_changes": True})["edited"])
        self.state.fire_timers(fl.CLOSE_DELAY_MS)
        self.assertIs(self.state.prompt, opened, "FontLab asked")
        self.state.fire_timers()
        self.assertEqual((self.state.answers, self.ids()), ([u"&Don\u2019t Save"], [1, 7]))

    def test_the_save_question_is_left_without_discard_changes(self):
        self.state.asks_to_save.add(7)  # (FontLab asked about a font no flag said was changed: live, at a quit)
        self.ok("close_font", {"font": 7})
        self.state.fire_timers()
        self.assertIs(self.state.prompt, self.font)
        self.assertEqual(self.state.answers, [])

    def test_dont_save_only_answers_a_question_naming_the_font(self):
        self.state.current = self.state.prompt = self.font
        self.assertFalse(fl._answer_dont_save(7, [u"Write Sans"]), "a name the question's starts with is not it")
        self.assertFalse(fl._answer_dont_save(1, [u"Write Sans Regular"]), "the font in front must be the one named")
        self.assertTrue(fl._answer_dont_save(7, [u"Write Sans Regular"]))
        self.assertEqual(self.ids(), [1])

    def test_close_font_leaves_a_font_another_one_replaced_in_front(self):
        self.ok("close_font", {"font": 7})
        self.state.current = self.sample
        self.state.fire_timers()
        self.assertEqual(self.ids(), [1, 7])

    # -- compiling
    def test_compile_features_reports_what_the_compiler_said(self):
        self.state.output = u"FreeLab listener 1.1.0 on 127.0.0.1:47761\n"
        self.state.compiler = lambda font: u"" if font is self.sample else (
            u'[W] lookup unused\n[F] glyph class not defined (text was "@nope")\n[F] Fatal error in font "Write.vfc"\n')
        reply = self.ok("compile_features", {"font": 7})
        self.assertEqual(self.state.compiled, [self.font], "brought to the front, then compiled")
        self.assertEqual((reply["compiled"], reply["brought_forward"]), (False, True))
        self.assertEqual(reply["errors"], [u'[F] glyph class not defined (text was "@nope")',
                                           u'[F] Fatal error in font "Write.vfc"'])
        self.assertEqual(reply["warnings"], [u"[W] lookup unused"])
        self.assertEqual(reply["output_length"], len(reply["output"]))
        clean = self.ok("compile_features", {"font": 1})
        self.assertEqual((clean["compiled"], clean["errors"], clean["output"]), (True, [], u""))

    def test_compile_features_needs_the_features_panel(self):
        self.state.features_panel = False
        self.assertIn("Window > Panels > Features", self.fails("compile_features", {"font": 7}, code="not_found")["message"])
        self.assertEqual((self.state.compiled, self.state.current), ([], self.sample))

    # -- the Output panel
    def test_output_reads_from_an_offset(self):
        self.state.output = u"abcdef"
        self.assertEqual(self.ok("output"), {"text": u"abcdef", "start": 0, "length": 6, "cleared": False,
                                             "truncated": False, "visible": True})
        self.assertEqual(self.ok("output", {"offset": 4})["text"], u"ef")
        cleared = self.ok("output", {"offset": 9})
        self.assertEqual((cleared["text"], cleared["cleared"]), (u"abcdef", True))
        cut = self.ok("output", {"max_chars": 2})
        self.assertEqual((cut["text"], cut["start"], cut["truncated"]), (u"ef", 4, True))
        self.fails("output", {"max_chars": 0}, code="bad_args")

    # -- pictures
    def test_screenshot_of_the_main_window(self):
        import base64
        reply = self.ok("screenshot")
        self.assertEqual((reply["window"], reply["class"], reply["width"], reply["height"]), (u"FontLab 7", "MainWindow", 2000, 1000))
        self.assertEqual(base64.b64decode(reply["png_base64"]), b"\x89PNG\r\n\x1a\n2000x1000")
        small = self.ok("screenshot", {"max_width": 500})
        self.assertEqual((small["width"], small["height"]), (500, 250))
        self.assertEqual(self.ok("screenshot", {"max_width": 4000})["width"], 2000, "never scaled up")

    def test_screenshot_of_a_window_by_title(self):
        self.state.windows = [u"FreeLab Proof", u"FreeLab Proof Settings", u"Welcome to FontLab 7!"]
        self.assertEqual(self.ok("screenshot", {"window": "FreeLab Proof"})["window"], u"FreeLab Proof", "exact first")
        self.assertEqual(self.ok("screenshot", {"window": "welcome"})["window"], u"Welcome to FontLab 7!")
        self.assertIn("Give the whole title", self.fails("screenshot", {"window": "Proof"}, code="ambiguous_window")["message"])
        error = self.fails("screenshot", {"window": "Metrics"}, code="not_found")
        self.assertIn(u"FontLab 7; FreeLab Proof", error["message"])

    def test_an_error_after_the_reply_is_recorded(self):
        def broken():
            raise RuntimeError("gone")
        self.listener.later(10, broken, "test")
        self.assertEqual(len(self.listener.pending), 1, "held until it runs")
        self.state.fire_timers()
        self.assertEqual((self.listener.pending, self.listener.errors), ([], [u"RuntimeError: gone"]))
        del self.listener.errors[:]


class RetiredTimerTests(unittest.TestCase):
    def test_stop_keeps_its_timer_referenced(self):
        """stop() may run inside the timer's own tick; dropping the QTimer then would let PythonQt delete it
        while it emits."""
        class Timer(object):
            stopped = False

            def stop(self):
                self.stopped = True

        folder = tempfile.mkdtemp(prefix="freelab-retired-")
        listener = fl.Listener(port=0, state_file=os.path.join(folder, "listener.json"))
        timer = listener.timer = Timer()
        saved = getattr(sys, "_freelab_retired_timers", None)
        try:
            listener.stop()
            self.assertTrue(timer.stopped)
            self.assertIsNone(listener.timer)
            self.assertIs(sys._freelab_retired_timers[-1], timer)
        finally:
            if saved is None:
                del sys._freelab_retired_timers
            else:
                sys._freelab_retired_timers = saved
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
