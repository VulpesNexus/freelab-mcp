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
        self.assertEqual(first, {"total": 4, "offset": 0, "names": ["A", "o"], "more": True})
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
        object.__setattr__(self.state.current.findName("A"), "layers", fakes.fgFont([]))
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


if __name__ == "__main__":
    unittest.main()
