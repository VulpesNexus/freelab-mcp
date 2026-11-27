# -*- coding: utf-8 -*-
"""Listener tests against spy fakes; they run under Python 2.7 and Python 3."""

from __future__ import absolute_import, division, print_function

import errno
import json
import os
import shutil
import socket
import struct
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
import fontbytes  # noqa: E402
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

            def isVisible(self):
                return True

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

    def test_the_welcome_window_is_closed_first(self):
        """Live, 2026-11-19: FontLab's Welcome window, modal since a launch with no font, kept FontLab running
        once its main window had closed."""
        self.App.windows = [self.Window("MainWindow"), self.Window("DlgInitialize")]
        self.listener._close_main_window()
        self.assertEqual(self.closed, ["DlgInitialize", "MainWindow"])

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
        self.assertEqual(self.ok("quit"), {"quitting": True, "edited": 1, "discard_changes": False})
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
        self.assertEqual(self.ok("quit"), {"quitting": True, "edited": 0, "discard_changes": False})
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

    def test_vertical_metrics_one_master(self):
        """Live 2026-11-27: the model (flPackage.info) holds the master's typo and win values; hhea is exported as
        the typo values while Custom hhea linespacing is off; the export copy agrees here."""
        self.open_sample()
        found = self.ok("vertical_metrics")
        self.assertEqual(found["current_master"], "Regular")
        self.assertIs(found["custom_hhea"], False)
        self.assertIs(found["use_typo_metrics"], False)
        [master] = found["masters"]
        self.assertEqual(master["model"]["typo_ascender"], 800)
        self.assertIsNone(master["model"]["hhea_ascender"])
        self.assertEqual(master["exported"]["hhea_ascender"], 800, "hhea exported as typo")
        self.assertEqual(master["exported"]["win_descent"], 250)
        self.assertEqual(master["x_height"], 500.0)
        self.assertEqual(found["export_copy_stale"], [])
        self.assertEqual(self.state.current._f_switches, [], "one master: never switched")
        self.assertNotIn("unreadable", found)

    def test_vertical_metrics_every_master_and_a_stale_copy(self):
        font = fakes.write_font()
        self.state.fonts = [font]
        self.state.current = font
        font._f_models["Regular"] = fakes.Info(typo=(965, -260, 0), hhea=(1000, -300, 10), win=(1010, 290))
        font._f_models["Bold"] = fakes.Info(typo=(980, -270, 0), hhea=(1020, -310, 0), win=(1100, 300))
        font.fgPackage.info.dict.flags._f_flags["hhea_use_custom"] = True
        font.fgPackage.info.openTypeOS2Selection.append(7)
        found = self.ok("vertical_metrics", {"font": 7})
        self.assertIs(found["custom_hhea"], True)
        self.assertIs(found["use_typo_metrics"], True)
        bold = [m for m in found["masters"] if m["master"] == "Bold"][0]
        self.assertEqual(bold["exported"]["hhea_ascender"], 1020, "Custom hhea on: each master's own")
        self.assertEqual(bold["exported"]["win_ascent"], 1100)
        self.assertEqual(font.master, "Regular", "the current master current again")
        self.assertEqual(font._f_switches, [("Regular", False), ("Bold", False), ("Regular", False)], "quietly")
        self.assertIn("typo_ascender", found["export_copy_stale"], "the copy's 800 is not the model's 965")
        only = self.ok("vertical_metrics", {"font": 7, "all_masters": False})
        self.assertEqual([m["master"] for m in only["masters"]], ["Regular"])

    def test_vertical_metrics_a_master_that_will_not_switch(self):
        font = fakes.write_font()
        self.state.fonts = [font]
        self.state.current = font
        switch = font.setMaster

        def refusing(name, notify=True):
            if name == "Bold":
                raise RuntimeError("no switch")
            switch(name, notify)
        object.__setattr__(font, "setMaster", refusing)
        found = self.ok("vertical_metrics", {"font": 7})
        self.assertEqual([m["master"] for m in found["masters"]], ["Regular"])
        self.assertIn("Bold: switch", found["unreadable"])
        self.assertEqual(font.master, "Regular")

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

    def test_no_workspace_yet_is_refused_not_raised(self):
        # (seen live: only FontLab's activation window up, flWorkspace.instance() was None)
        self.open_sample()
        self.state.no_workspace = True
        for op in ("font_info", "fonts"):
            error = self.fails(op, code="not_ready")
            self.assertIn("no workspace yet", error["message"])

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

    def test_a_write_whose_step_the_history_refuses_is_put_back(self):
        """Codex, 2026-11-19: a write whose History step failed was reported as a success, Undo unable to take
        it back."""
        self.write("set_kerning", {"left": "B", "right": "A", "value": -9})  # (the font recorded)
        self.state.fail_history = "step"
        error = self.fails("set_kerning", {"font": 7, "left": "B", "right": "A", "value": -8}, code="history_failed")
        self.assertIn("it was put back", error["message"])
        self.assertIn("Nothing was changed", error["message"])
        self.assertEqual(self.font.kerning("Regular").get(("B", "A")), -9)
        self.state.fail_history = None
        self.write("set_kerning", {"left": "B", "right": "A", "value": -7})
        self.assertTrue(self.state.steps[-2][0].endswith("(before)"), "the font is recorded again after a failed step")

    def test_a_change_that_cannot_be_put_back_after_a_refused_step_is_an_error(self):
        self.state.fail_history = "step"
        error = self.fails("delete_glyph", {"font": 7, "glyph": "space"}, code="history_failed")
        self.assertIn("could not be put back", error["message"])
        self.assertFalse(error["data"]["history"]["recorded"])
        self.assertIsNone(self.font.findName("space"), "a deleted glyph comes back only through Undo")
        self.assertEqual(self.font._f_flags[-1], (2, True), "marked modified")

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

    def test_a_write_to_a_font_behind_is_recorded_in_front(self):
        """Live, 2026-11-19: FontLab gives a step no title when its font is behind another (blank in the History
        panel and in Edit > Undo), so undo could not match a write made behind by its title."""
        self.state.current = self.sample
        reply = self.write("set_kerning", {"left": "B", "right": "A", "value": -5})
        self.assertEqual([title for title, _o in self.state.steps], [u"FreeLab MCP: kerning B A (before)",
                                                                    u"FreeLab MCP: kerning B A"])
        self.assertIs(self.state.current, self.sample, "the font that was in front is put back")
        undone = self.write("undo", {"expected_state": reply["history"]["history_state"],
                                     "expected_title": reply["history"]["title"]})
        self.assertEqual(undone["undone"][0], u"Freelab Mcp: Kerning B A")
        self.assertIs(self.state.current, self.sample)

    def test_a_font_without_a_window_tab_writes_an_untitled_step_and_says_so(self):
        self.state.current = self.sample
        self.state.tabs = [(u"Sketchboard", None), (u" Free Sans Regular ", self.sample)]
        reply = self.write("set_kerning", {"left": "B", "right": "A", "value": -5})
        self.assertIn("no title", reply["history"]["warning"])
        self.assertEqual(self.state.steps[-1][0], u"")

    def test_a_before_step_left_alone_is_undone_alone_and_said(self):
        self.state.fail_history = "step"
        self.fails("set_kerning", {"font": 7, "left": "B", "right": "A", "value": -5},
                   code="history_failed")  # (its step refused, the pair put back: only (before) is left)
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
        self.fails("undo", {"font": 7, "keep_in_front": "yes"}, code="bad_args")
        self.state.current = self.font
        self.state.undo_moves = False
        self.fails("undo", {"font": 7}, code="not_kept")
        self.assertEqual(self.state.undone, 1)
        self.state.tabs = [(u"Sketchboard", None), (u" Free Sans Regular ", self.sample)]
        self.state.current = self.sample
        self.assertIn("No window of Write Sans", self.fails("undo", {"font": 7}, code="not_found")["message"])
        self.assertEqual(self.state.undone, 1, "nothing undone in the font in front")

    def test_undo_brings_the_font_forward_and_puts_the_other_back(self):
        """1.1.0 refused a font not in front (not_in_front); 1.2.0 brings it forward, as compile_features does."""
        state = self.write("set_kerning", {"left": "B", "right": "A", "value": -5})["history"]["history_state"]
        self.state.current = self.sample
        reply = self.write("undo", {"expected_state": state})
        self.assertEqual((reply["brought_forward"], reply["front_restored"]),
                         (True, {"font": {"id": 1, "name": "Free Sans Regular"}}))
        self.assertIs(self.state.current, self.sample, "the font that was in front is in front again")
        self.assertIsNone(self.font.kerning("Regular").get(("B", "A")), "the write font's step was undone")
        self.write("set_kerning", {"left": "B", "right": "A", "value": -6})
        self.state.current = self.sample
        reply = self.write("undo", {"keep_in_front": True})
        self.assertNotIn("front_restored", reply)
        self.assertIs(self.state.current, self.font, "kept in front")

    def test_a_refused_undo_still_puts_the_front_font_back(self):
        self.write("set_kerning", {"left": "B", "right": "A", "value": -5})
        self.state.current = self.sample
        self.fails("undo", {"font": 7, "expected_title": "FreeLab MCP: other"}, code="history_moved")
        self.assertIs(self.state.current, self.sample)


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
        self.assertEqual(self.listener.opened, [{"id": 101, "path": path}], "recorded once it opened, by its id")

    def test_open_font_names_a_font_already_open(self):
        path = self.font_file()
        object.__setattr__(self.font, "path", path.replace(os.sep, "/"))  # (FontLab mixes separators: live)
        self.assertEqual(self.ok("open_font", {"path": path}), {"already_open": True,
                                                                "font": {"id": 7, "name": "Write Sans Regular"}})
        self.assertEqual((self.state.timers, self.listener.opened), ([], []))

    def test_open_font_refuses_a_path_it_cannot_open(self):
        self.fails("open_font", {"path": "Open.ttf"}, code="bad_args")
        self.fails("open_font", {"path": ""}, code="bad_args")
        self.fails("open_font", {"path": os.path.join(self.tmp, "missing.ttf")}, code="not_found")
        self.assertEqual(self.state.timers, [])

    # -- a font FontLab 7.2 crashes on
    def crasher_file(self, name="NotoSansJP[wght].ttf", data=None):
        path = os.path.join(self.tmp, name)
        if data is None:
            gpos = fontbytes.gpos([fontbytes.lookup(1, [fontbytes.single(2, 0, 0)]),
                                   fontbytes.lookup(9, [fontbytes.extension(fontbytes.single(2, 0, 0))])])
            data = fontbytes.sfnt({b"head": fontbytes.head(), b"GPOS": gpos})
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_open_font_refuses_a_font_fontlab_crashes_on(self):
        path = self.crasher_file()
        error = self.fails("open_font", {"path": path}, code="known_crasher")
        self.assertIn(u"FontLab 7.2 crashes on opening NotoSansJP[wght].ttf", error["message"])
        self.assertIn(u"loses the unsaved work in every open font", error["message"])
        self.assertIn(u"2 single adjustment subtables with no values (lookups 0 and 1)", error["message"])
        self.assertIn(u"safe_copy=True", error["message"])
        # (live, 2026-11-19: it read "such subtables positions nothing")
        self.assertIn(u"such subtables position nothing, so each is made", error["message"])
        self.assertEqual((error["data"]["count"], [s["lookup"] for s in error["data"]["fonts"][0]["subtables"]]),
                         (2, [0, 1]))
        one = self.crasher_file("One.ttf", fontbytes.sfnt({b"head": fontbytes.head(), b"GPOS": fontbytes.gpos(
            [fontbytes.lookup(1, [fontbytes.single(2, 0, 0)])])}))
        self.assertIn(u"such a subtable positions nothing, so it is made",
                      self.fails("open_font", {"path": one}, code="known_crasher")["message"])
        self.assertNotIn("at", error["data"]["fonts"][0]["subtables"][0], "no file offsets")
        self.assertEqual((self.state.timers, self.state.loads), ([], []), "nothing opened")

    def test_a_crasher_found_before_a_damaged_part_is_refused(self):
        # (Codex, 2026-11-19: a damaged part after the crasher made the check "not made", and the file opened)
        broken = bytearray(fontbytes.lookup(1, [fontbytes.single(1, 0)]))
        struct.pack_into(">H", broken, 6, 0xFFF0)
        gpos = fontbytes.gpos([fontbytes.lookup(1, [fontbytes.single(2, 0, 0)]), bytes(broken)])
        path = self.crasher_file("Half.ttf", fontbytes.sfnt({b"head": fontbytes.head(), b"GPOS": gpos}))
        error = self.fails("open_font", {"path": path}, code="known_crasher")
        self.assertIn("past the end", error["data"]["unchecked"])
        # (Codex, 2026-11-27: the copy of a file checked in part was opened, the caveat only in the reply)
        self.assertIn(u"Part of it could not be checked", error["message"])
        self.assertIn(u"writes one to FreeLab's own folder", error["message"])
        self.assertIn(u"and does not open it", error["message"])
        self.assertNotIn(u"which FontLab opens", error["message"])
        error = self.fails("open_font", {"path": path, "safe_copy": True}, code="known_crasher")
        copy = error["data"]["copy"]
        self.assertIn(u"A copy with it fixed was written to %s, and not opened" % copy, error["message"])
        self.assertIn(u"FontLab may crash on the copy too", error["message"])
        self.assertIn(u"once the work in their open fonts is saved", error["message"])
        self.assertEqual((error["data"]["changed"], fl.scan_font_file(copy)["count"]), (1, 0))
        self.state.fire_timers()
        self.assertEqual((self.state.timers, self.state.loads), ([], []), "neither the copy nor the file opened")

    def test_a_crasher_past_the_budget_is_never_opened_by_its_copy(self):
        # (Codex, 2026-11-27: one found, then 240,000 subtables, then another: the copy kept the second one)
        empty = fontbytes.lookup(1, [fontbytes.single(2, 0, 0)])
        count, repeats = 4000, 60
        shared = struct.pack(">HHH", 1, 0, count) + struct.pack(">H", 6 + 2 * count) * count + fontbytes.single(1, 0)
        listing = 2 + 2 * (repeats + 2)
        offsets = [listing] + [listing + len(empty)] * repeats + [listing + len(empty) + len(shared)]
        gpos = (struct.pack(">HHHHH", 1, 0, 10, 12, 14) + struct.pack(">HH", 0, 0) + struct.pack(">H", repeats + 2) +
                b"".join(struct.pack(">H", at) for at in offsets) + empty + shared + empty)
        path = self.crasher_file("Loop.ttf", fontbytes.sfnt({b"head": fontbytes.head(), b"GPOS": gpos}))
        error = self.fails("open_font", {"path": path, "safe_copy": True}, code="known_crasher")
        self.assertIn(u"over and over", error["data"]["unchecked"])
        self.assertIn(u"and not opened", error["message"])
        self.state.fire_timers()
        self.assertEqual(self.state.loads, [], "nothing opened")

    def test_open_font_opens_a_fixed_copy_with_safe_copy(self):
        path = self.crasher_file()
        with open(path, "rb") as handle:
            original = handle.read()
        reply = self.ok("open_font", {"path": path, "safe_copy": True})
        copy = reply["safe_copy"]
        folder = os.path.join(self.tmp, fl.SAFE_COPIES)
        self.assertEqual((copy["original"], copy["changed"], os.path.dirname(os.path.dirname(copy["path"]))),
                         (path, 2, folder), "beside the state file")
        self.assertEqual((reply["path"], os.path.basename(copy["path"])), (copy["path"], "NotoSansJP[wght].ttf"))
        self.assertIn(u"Opened a fixed copy of NotoSansJP[wght].ttf, written to %s" % copy["path"], copy["note"])
        self.assertIn(u"The file itself is not changed.", copy["note"])
        self.state.fire_timers()
        self.assertEqual(self.state.loads, [copy["path"]], "the copy opened, not the file")
        self.assertEqual(fl.scan_font_file(copy["path"])["count"], 0)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), original, "the file itself is not changed")
        self.assertEqual(self.listener.opened, [{"id": 101, "path": copy["path"]}])
        again = self.ok("open_font", {"path": path, "safe_copy": True})
        self.assertEqual((again["already_open"], again["font"]["id"], again["safe_copy"]["path"]),
                         (True, 101, copy["path"]), "the copy open already: named, not written again")
        self.assertIn(u"open already", again["safe_copy"]["note"])

    def test_safe_copy_of_a_font_that_needs_none_opens_the_file(self):
        path = self.crasher_file("Clean.ttf", fontbytes.sfnt({b"head": fontbytes.head(), b"GPOS": fontbytes.gpos([])}))
        reply = self.ok("open_font", {"path": path, "safe_copy": True})
        self.assertEqual((reply["path"], reply["safe_copy"]["path"]), (path, None))
        self.assertIn(u"No copy was needed", reply["safe_copy"]["note"])
        self.assertNotIn("crash_check", reply)
        self.assertEqual(self.ok("open_font", {"path": self.font_file("Plain.ttf")})["path"],
                         os.path.join(self.tmp, "Plain.ttf"))
        self.fails("open_font", {"path": path, "safe_copy": "yes"}, code="bad_args")

    def test_open_font_says_a_check_it_could_not_make(self):
        real = fl._brotli_decompress
        fl._brotli_decompress = lambda: None
        try:
            path = self.crasher_file("Web.woff2", b"wOF2" + b"\0" * 60)
            reply = self.ok("open_font", {"path": path})
        finally:
            fl._brotli_decompress = real
        self.assertIn(u"Not made: WOFF2 needs brotli", reply["crash_check"])
        self.state.fire_timers()
        self.assertEqual(self.state.loads, [path], "opened all the same, as the user asked")

    def test_a_copy_that_cannot_be_made_opens_nothing(self):
        real = fl._brotli_decompress
        fl._brotli_decompress = lambda: (lambda data, limit: data)  # (fontbytes' stream is not compressed)
        try:
            path = self.crasher_file("Web.woff2", fontbytes.woff2({b"head": fontbytes.head(), b"GPOS": fontbytes.gpos(
                [fontbytes.lookup(1, [fontbytes.single(2, 0, 0)])])}))
            self.fails("open_font", {"path": path}, code="known_crasher")
            error = self.fails("open_font", {"path": path, "safe_copy": True}, code="cannot_copy")
        finally:
            fl._brotli_decompress = real
        self.assertIn(u"convert it to TTF or OTF first", error["message"])
        self.assertEqual((self.state.timers, self.state.loads), ([], []))

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
        error = self.fails("close_font", {"font": 7, "discard_changes": True}, code="risky_mode_off")
        self.assertIn("was not opened with open_font", error["message"])
        self.assertEqual(error["data"], {"action": "close_font.discard_changes"})
        self.fails("close_font", {"font": 7, "discard_changes": True, "risky": "true"}, code="risky_mode_off")
        self.fails("close_font", {"font": 7, "discard_changes": "yes"}, code="bad_args")
        self.assertEqual((self.state.timers, self.ids()), ([], [1, 7]))

    def test_risky_mode_discards_changes_in_any_font(self):
        self.font._f_states.append(3)
        self.state.asks_to_save.add(7)
        self.assertTrue(self.ok("close_font", {"font": 7, "discard_changes": True, "risky": True})["edited"])
        for _ in range(3):
            self.state.fire_timers()
        self.assertEqual((self.state.answers, self.ids()), ([u"&Don’t Save"], [1]))

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
        asked = []
        self.state.on_modal = lambda: asked.append(self.state.prompt)
        self.state.fire_timers(fl.CLOSE_DELAY_MS)  # (the question is answered inside File > Close Font: live)
        self.assertEqual(asked, [opened], "FontLab asked")
        self.assertEqual((self.state.answers, self.ids()), ([u"&Don\u2019t Save"], [1, 7]))

    def test_a_canceled_close_leaves_no_answer_for_a_later_close(self):
        """Codex, 2026-11-19: the Don't Save answers of a close the user canceled stayed armed, and answered
        the user's own close of the same font a moment later."""
        self.font._f_states.append(3)
        self.state.asks_to_save.add(7)
        self.ok("close_font", {"font": 7, "discard_changes": True, "risky": True})
        self.state.on_modal = lambda: fl._click_modal_button("Cancel")  # (the user cancels the question)
        self.state.fire_timers(fl.CLOSE_DELAY_MS)
        self.assertEqual((self.state.answers, self.ids()), (["Cancel"], [1, 7]))
        self.state.close_current()  # (then closes the font in FontLab themselves)
        self.state.fire_timers()
        self.assertIs(self.state.prompt, self.font, "the user's own question is left to them")
        self.assertEqual(self.state.answers, ["Cancel"])

    def test_a_close_skipped_leaves_no_answer(self):
        self.font._f_states.append(3)
        self.state.asks_to_save.add(7)
        self.ok("close_font", {"font": 7, "discard_changes": True, "risky": True})
        self.state.current = self.sample  # (another font came to the front: the close is skipped)
        self.state.fire_timers(fl.CLOSE_DELAY_MS)
        self.state.current = self.font
        self.state.close_current()
        self.state.fire_timers()
        self.assertEqual((self.state.answers, self.state.prompt), ([], self.font))

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

    def test_a_question_naming_the_family_is_answered_too(self):
        """Live, 2026-11-19: for Rubik[wght].ttf (full name Rubik Light), FontLab asked Save changes to  "Rubik"
        before closing?, the family name, and the question stayed unanswered."""
        path = self.font_file()
        self.ok("open_font", {"path": path})
        self.state.fire_timers()
        opened = self.state.fonts[-1]
        opened._f_states.append(4)
        self.state.asks_to_save.add(101)
        self.state.prompt_key = "tfn"
        self.ok("close_font", {"font": 101, "discard_changes": True})
        self.state.fire_timers()
        self.assertEqual((self.state.answers, self.ids()), ([u"&Don’t Save"], [1, 7]))
        self.state.current = self.state.prompt = self.font
        object.__setattr__(self.font, "tfn", u"Write Sans Mono")
        self.assertFalse(fl._answer_dont_save(7, [u"Write Sans"]), "a family whose name starts the same is not it")

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

    def test_compile_features_opens_the_panels_it_needs(self):
        self.state.features_panel, self.state.output_panel = False, False
        reply = self.ok("compile_features", {"font": 7})
        self.assertEqual(reply["opened_panels"], [u"Features", u"Output"])
        self.assertEqual(self.state.panels_chosen, ["actionFeaturesPanel", "actionOutputPanel"])
        self.assertEqual(self.state.compiled, [self.font])
        self.assertIs(self.state.current, self.sample, "the font that was in front is put back")
        self.assertEqual(reply["front_restored"], {"font": {"id": 1, "name": "Free Sans Regular"}})
        self.assertEqual(self.ok("compile_features", {"font": 7})["opened_panels"], [], "shown: never chosen again")
        self.ok("compile_features", {"font": 7, "keep_in_front": True})
        self.assertIs(self.state.current, self.font)

    def test_compile_features_needs_the_features_panel(self):
        self.state.features_panel = False
        saved = fl._open_panel
        fl._open_panel = lambda name: name == fl.FEATURES_PANEL_ACTION  # (chosen, and still no panel)
        try:
            error = self.fails("compile_features", {"font": 7}, code="not_found")
        finally:
            fl._open_panel = saved
        self.assertIn("though Window > Panels > Features was chosen", error["message"])
        self.assertEqual((self.state.compiled, self.state.current), ([], self.sample))

    def test_compile_errors_are_located_in_the_fonts_code(self):
        """Live, 2026-11-19: FontLab names a line of its combined "features" file: the prefix without the
        Classes panel's lines get_prefix() starts with, then each feature (Varela Round, Bellefair, Tinos: the
        line was the rebuilt one less exactly their 16, 5, and 10 panel classes). Here: the prefix (2 lines,
        after caps's line), liga (3 lines), ss01 (3 lines)."""
        self.state.compiler = lambda font: u'[F] glyph class not defined\n[F] Fatal error in font "W" in file "features" at line 8\n'
        reply = self.ok("compile_features", {"font": 7})
        located = reply["located"]
        self.assertEqual((located["line"], located["tag"], located["line_in_feature"], located["certain"]),
                         (8, u"ss01", 3, False))
        self.assertNotIn("without_classes", located)
        self.assertEqual(len(reply["located_all"]), 1)
        far = self.ok("locate_feature_line", {"font": 7, "line": 7703})["located"]
        self.assertEqual((far["tag"], far["lines_rebuilt"]), (None, 8))
        self.assertEqual(self.ok("locate_feature_line", {"font": 7, "line": 1})["located"]["tag"], u"prefix")
        self.fails("locate_feature_line", {"font": 7}, code="bad_args")

    def test_the_prefix_keeps_the_classes_panels_lines_out_of_the_count(self):
        """A prefix that does not start with the panel's lines (FontLab's own, or a font of none) counts whole."""
        self.assertEqual(fl._own_prefix(self.font), u"languagesystem DFLT dflt;\n@round = [O Q];\n"[:0] +
                         self.font.fgPackage.features._f_prefix)
        object.__setattr__(self.font, "_f_classes", [])
        self.assertEqual(fl._own_prefix(self.font), self.font.fgPackage.features.get_prefix())

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


class RiskyTests(FontCase):
    """Risky actions (RISKY_ACTIONS): each refused without risky: true, with nothing changed or queued."""

    def font_file(self, name="Open.ttf"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(b"font")
        return path

    def test_the_list(self):
        keys = [key for key, _tools, _reason, _enforced in fl.RISKY_ACTIONS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual([key for key, _t, _r, enforced in fl.RISKY_ACTIONS if not enforced], ["run_python"])
        listed = self.ok("risky_actions")["actions"]
        self.assertEqual([entry["action"] for entry in listed], keys)
        self.assertTrue(all(entry["reason"].endswith(".") for entry in listed))

    def test_every_risky_action_is_refused_without_risky_mode(self):
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK"])
        self.font.kerning("Bold")._f_pairs.append((("Q", "glyphMode"), ("A", "glyphMode"), -7.0))
        cases = [
            ("answer_dialog", {"button": "OK"}),
            ("open_font", {"path": self.font_file(), "answer": "OK"}),
            ("quit", {"discard_changes": True}),
            ("rename_glyph", {"font": 7, "glyph": "Q", "new_name": "Qalt", "update_references": True}),
            ("delete_glyph", {"font": 7, "glyph": "Q", "remove_references": True}),
            ("save_font", {"font": 7}),
            ("export_font", {"font": 7, "folder": self.tmp}),
            ("close_font", {"font": 7, "discard_changes": True}),
            ("click", {"window": "dialog", "control": "OK"}),  # (a control of FontLab's question)
            ("run_menu_command", {"path": "File > Save Font"}),  # (once the question is gone: below)
        ]
        refused = set()
        for op, args in cases:
            if op == "run_menu_command":
                import widgetfakes
                self.state.question = None
                self.state.menu_bar = widgetfakes.Widget("QMenuBar", actions=[widgetfakes.Action(
                    u"&File", menu=widgetfakes.menu(widgetfakes.Action(u"&Save Font...", name=u"actionSaveFont")))])
            for risky in (None, False, 1, "true"):
                payload = dict(args)
                if risky is not None:
                    payload["risky"] = risky
                error = self.fails(op, payload, code="risky_mode_off")
                self.assertIn("set_risky_mode", error["message"])
                refused.add(error["data"]["action"])
        self.assertEqual(refused, set(key for key, _t, _r, enforced in fl.RISKY_ACTIONS if enforced),
                         "every enforced action is refused, and tested here")
        self.assertEqual((self.state.timers, self.state.steps, self.state.answers, self.state.loads), ([], [], [], []))
        self.assertIsNotNone(self.font.findName("Q"))


class AnswerDialogTests(FontCase):
    def test_a_button_is_clicked_after_the_reply(self):
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK", u"Cancel"], text=u"Round the coordinates?")
        reply = self.ok("answer_dialog", {"button": "ok", "expected_title": "coordinates rounding", "risky": True})
        self.assertEqual(reply["dialog"], {"class": u"QDialog", "title": u"Coordinates Rounding",
                                           "text": [u"Round the coordinates?"], "buttons": [u"OK", u"Cancel"]})
        self.assertEqual(self.state.answers, [], "not inside the request")
        self.state.fire_timers()
        self.assertEqual((self.state.answers, self.state.question), ([u"OK"], None))

    def test_a_save_question_is_answered_as_shown(self):
        self.state.current = self.state.prompt = self.font
        self.ok("answer_dialog", {"button": "Don't Save", "risky": True})  # (FontLab writes a curly apostrophe)
        self.state.fire_timers()
        self.assertEqual((self.state.answers, self.ids()), ([u"&Don’t Save"], [1]))

    def ids(self):
        return [font.fgPackage.id for font in self.state.fonts]

    def test_refusals(self):
        self.fails("answer_dialog", {"button": "OK", "risky": True}, code="not_found")
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK"])
        self.assertEqual(self.fails("answer_dialog", {"button": "OK", "expected_title": "Font Info", "risky": True},
                                    code="dialog_changed")["data"]["dialog"]["title"], u"Coordinates Rounding")
        self.assertIn("its buttons: OK", self.fails("answer_dialog", {"button": "Yes", "risky": True}, code="no_button")["message"])
        for bad in ("", None, 5):
            self.fails("answer_dialog", {"button": bad, "risky": True}, code="bad_args")
        self.assertEqual((self.state.timers, self.state.answers), ([], []))

    def test_a_dialog_replaced_meanwhile_is_not_answered(self):
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK"])
        reply = self.ok("answer_dialog", {"button": "OK", "risky": True})
        self.assertIsNone(self.ok("answer_result", {"answer_id": reply["answer_id"]})["outcome"], "still to come")
        self.state.ask(u"Font Exported", u"DlgSavePackage", [u"OK"])
        self.state.fire_timers()
        self.assertEqual((self.state.answers, self.state.question), ([], u"Font Exported"))
        self.assertEqual(self.ok("answer_result", {"answer_id": reply["answer_id"]})["outcome"], u"dialog_changed")

    def test_another_fonts_save_question_is_not_answered(self):
        """Codex, 2026-11-19: two Save questions share class and title; the one about font 7 was described,
        and font 1's, shown by the time of the click, was answered Don't Save."""
        self.state.current = self.state.prompt = self.font
        reply = self.ok("answer_dialog", {"button": "Don't Save", "risky": True})
        self.state.current = self.state.prompt = self.sample
        self.state.fire_timers()
        self.assertEqual((self.state.answers, self.ids()), ([], [1, 7]))
        self.assertEqual(self.ok("answer_result", {"answer_id": reply["answer_id"]})["outcome"], u"dialog_changed")
        self.fails("answer_result", {"answer_id": 999}, code="not_found")

    def test_a_clicked_answer_is_said(self):
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK"])
        reply = self.ok("answer_dialog", {"button": "OK", "risky": True})
        self.state.fire_timers()
        self.assertEqual(self.ok("answer_result", {"answer_id": reply["answer_id"]})["outcome"], u"clicked")

    def test_an_open_canceled_stops_its_answer(self):
        """Codex, 2026-11-19: once the open's question was canceled, its answer kept looking for 120 s and
        clicked a later question of the user's own."""
        path = os.path.join(self.tmp, "Round.ttf")
        with open(path, "wb") as handle:
            handle.write(b"font")
        self.state.hold_open = True
        self.ok("open_font", {"path": path, "answer": "OK", "risky": True})
        self.state.on_modal = lambda: fl._click_modal_button("Cancel")  # (the user cancels before the answer)
        self.state.fire_timers(fl.OPEN_DELAY_MS)
        self.assertEqual((self.state.answers, self.state.loads), (["Cancel"], [path]))
        manual = []
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK"], then=manual.append)  # (the user's own, later)
        for _ in range(3):
            self.state.fire_timers()
        self.assertEqual((manual, self.state.question), ([], u"Coordinates Rounding"))

    def test_an_open_answers_only_while_its_font_loads(self):
        path = os.path.join(self.tmp, "Round.ttf")
        with open(path, "wb") as handle:
            handle.write(b"font")
        self.ok("open_font", {"path": path, "answer": "OK", "risky": True})
        self.state.ask(u"Font Info", buttons=[u"OK"])  # (a question already up, before the open runs)
        self.state.fire_timers(fl.ANSWER_POLL_MS - 1)  # (the open itself, at once: no question of its own)
        for _ in range(3):
            self.state.fire_timers()
        self.assertEqual((self.state.answers, self.state.question), ([], u"Font Info"))

    def test_open_font_answers_fontlabs_question(self):
        path = os.path.join(self.tmp, "Round.ttf")
        with open(path, "wb") as handle:
            handle.write(b"font")
        self.state.hold_open = True
        reply = self.ok("open_font", {"path": path, "answer": "OK", "risky": True})
        self.assertEqual(reply["answer"], u"OK")
        self.state.fire_timers()
        self.assertEqual((self.state.answers, self.state.question), ([u"OK"], None))
        self.assertEqual(self.ids(), [1, 7, 101], "the font opened once the question was answered")
        self.fails("open_font", {"path": path, "answer": "", "risky": True}, code="bad_args")

    def test_quit_with_discard_changes_answers_each_save_question(self):
        quits = []
        self.listener.request_quit = lambda delay_ms=0: quits.append(delay_ms)
        self.state.unsaved = [True, False]
        self.fails("quit", code="unsaved_changes")
        reply = self.ok("quit", {"discard_changes": True, "risky": True})
        self.assertEqual((reply["discard_changes"], len(quits)), (True, 1))
        self.listener.closing = True  # (inside the main window's close(), which waits on FontLab's questions)
        try:
            for font in (self.font, self.sample):  # (FontLab asks about each edited font in turn)
                self.state.current = self.state.prompt = font
                self.state.fire_timers()
            self.assertEqual((self.state.answers, self.ids()), ([u"&Don’t Save"] * 2, []))
            self.state.ask(u"Workspace", buttons=[u"Don't Save"])  # (only FontLab's Save questions about fonts)
            self.state.fire_timers()
            self.assertEqual(self.state.question, u"Workspace")
        finally:
            self.listener.closing = False

    def test_a_quit_canceled_on_screen_answers_nothing_after(self):
        """A Save question the user meets later, closing a font by hand, is never answered for them."""
        self.listener.request_quit = lambda delay_ms=0: None
        self.ok("quit", {"discard_changes": True, "risky": True})  # (then Cancel: the quit is over)
        self.state.current = self.state.prompt = self.font
        for _ in range(3):
            self.state.fire_timers()
        self.assertEqual((self.state.answers, self.state.prompt), ([], self.font))
        self.assertEqual(self.state.timers, [], "the answering stopped")

    def test_open_font_answers_nothing_once_the_font_is_open(self):
        path = os.path.join(self.tmp, "Plain.ttf")
        with open(path, "wb") as handle:
            handle.write(b"font")
        self.ok("open_font", {"path": path, "answer": "OK", "risky": True})
        self.state.fire_timers(fl.OPEN_DELAY_MS)  # (opened without a question)
        self.state.ask(u"Later", buttons=[u"OK"])
        for _ in range(3):
            self.state.fire_timers()
        self.assertEqual((self.state.answers, self.state.question, self.state.timers), ([], u"Later", []))


class ReferenceTests(FontCase):
    """rename_glyph's update_references and delete_glyph's remove_references (risky), undone in one step."""

    def setUp(self):
        FontCase.setUp(self)
        self.state.current = self.font
        self.font.kerning("Bold")._f_pairs.append((("Q", "glyphMode"), ("A", "glyphMode"), -7.0))
        self.features = self.font.fgPackage.features

    def classes(self, master="Regular"):
        return self.font.kerning(master).groups.asDict()

    def add_glyph(self, name):
        self.font._f_glyphs.append(fakes.flGlyph(name, []))
        self.font.fgPackage.rebuildMaps()

    def test_a_feature_named_like_the_glyph_is_left_alone(self):
        """Codex, 2026-11-19: renaming a glyph named liga rewrote feature liga { } liga; as liga.new."""
        self.add_glyph("liga")
        before = self.features.get_feature("liga")
        self.write("rename_glyph", {"glyph": "liga", "new_name": "liga.new", "update_references": True, "risky": True})
        self.assertEqual(self.features.get_feature("liga"), before)
        self.assertIsNone(fl._feature_block("liga", self.features.get_feature("liga")))

    def test_only_glyph_names_are_rewritten(self):
        self.add_glyph("sub")
        code = (u"feature ss02 {\n  # sub stays a word here\n  featureNames { name \"sub\"; };\n"
                u"  lookup sub {\n    sub \\sub by B;\n  } sub;\n} ss02;\n")
        self.features.set_feature("ss02", code)
        reply = self.write("rename_glyph", {"glyph": "sub", "new_name": "sub.alt", "update_references": True,
                                            "risky": True})
        self.assertEqual(reply["references"]["features"], [u"ss02"])
        self.assertEqual(self.features.get_feature("ss02"), code.replace(u"\\sub by", u"\\sub.alt by"))

    def test_a_glyph_range_that_may_hold_the_glyph_is_refused(self):
        """Codex, 2026-11-19: [A-B] was not seen as naming A or B, so B could be deleted under it."""
        for name in ("x1", "x2", "x3"):
            self.add_glyph(name)
        for prefix, glyph in ((u"@xs = [x1-x2];\n", "x2"), (u"@xs = [x1 - x2];\n", "x1"), (u"@xs = [x1-x3];\n", "x2")):
            self.features.set_prefix(prefix)
            error = self.refused("delete_glyph", {"glyph": glyph}, "glyph_in_use")
            self.assertIn("glyph range", error["message"], prefix)
            error = self.refused("delete_glyph", {"glyph": glyph, "remove_references": True, "risky": True},
                                 "glyph_in_use")
            self.assertIn("glyph range", error["message"], prefix)
            error = self.refused("rename_glyph", {"glyph": glyph, "new_name": glyph + ".x", "update_references": True,
                                                  "risky": True}, "glyph_in_use")
            self.assertIn("range", error["message"], prefix)
        self.sample.fgPackage.features.set_prefix(u"@letters = [A-B];\n")  # (Codex's case, as written)
        self.assertIn(u"a glyph range that may hold it in feature code (prefix)", fl._references(self.sample, "B"))

    def test_a_hyphenated_glyph_is_a_glyph_not_a_range(self):
        code, glyphs = u"@pair = [A-B];\n", set([u"A-B", u"A", u"B"])
        self.assertEqual(fl._fea_uses(code, u"A-B", glyphs), (1, False))
        self.assertEqual(fl._fea_uses(code, u"B", glyphs), (0, False))
        self.assertEqual(fl._fea_uses(code, u"B", set([u"A", u"B"])), (0, True), "without that glyph: a range")

    def test_rename_rewrites_kerning_classes_and_feature_code_and_undoes_in_one_step(self):
        self.features.set_prefix(u"languagesystem DFLT dflt;\n@round = [O Q];\n# Q.alt and Qx stay\n")
        error = self.refused("rename_glyph", {"glyph": "Q", "new_name": "Qalt"}, "glyph_in_use")
        self.assertIn("update_references (a risky action)", error["message"])
        reply = self.write("rename_glyph", {"glyph": "Q", "new_name": "Qalt", "update_references": True, "risky": True})
        self.assertEqual(reply["references"], {"kerning_pairs": 1, "kerning_classes": [u"round (Regular)", u"round (Bold)"],
                                               "features": [u"ss01"], "prefix": True})
        self.assertEqual(self.font.kerning("Bold").get(("Qalt", "A")), -7.0)
        self.assertNotIn((("Q", "glyphMode"), ("A", "glyphMode"), -7.0), self.font.kerning("Bold")._f_pairs)
        self.assertEqual((self.classes()["round"][0], self.classes("Bold")["round"][0]), (["O", "Qalt"], ["O", "Qalt"]))
        self.assertIn(u"sub Qalt by O", self.features.get_feature("ss01"))
        self.assertEqual(self.features.get_prefix(),  # (the Classes panel's class first, as FontLab gives it: live)
                         u"@caps = [A B];\nlanguagesystem DFLT dflt;\n@round = [O Qalt];\n# Q.alt and Qx stay\n")
        self.assertIs(self.state.steps[0][1][0], self.font, "the font recorded before the rename, whatever its state")
        self.assertEqual(len(self.state.steps), 2)
        undone = self.write("undo", {"expected_state": reply["history"]["history_state"]})
        self.assertEqual(len(undone["undone"]), 2, "one undo")
        self.font.fgPackage.rebuildMaps()  # (FontLab's lookups lag behind an Undo; the listener rebuilds them)
        self.assertIsNotNone(self.font.findName("Q"))
        self.assertEqual(self.font.kerning("Bold").get(("Q", "A")), -7.0)
        self.assertEqual(self.classes()["round"][0], ["O", "Q"])
        self.assertIn(u"sub Q by O", self.features.get_feature("ss01"))
        self.assertIn(u"@round = [O Q];", self.features.get_prefix())

    def test_what_scripting_cannot_rewrite_is_refused(self):
        risky = {"update_references": True, "risky": True}
        args = dict(risky, glyph="A", new_name="A.alt")
        self.assertIn("Classes panel", self.refused("rename_glyph", args, "glyph_in_use")["message"])
        args = dict(risky, glyph="Q", new_name="round")
        self.assertIn("'round' is the name of a kerning class", self.refused("rename_glyph", args, "class_name_clash")["message"])
        self.font.kerning("Regular")._f_pairs.append((("V", "glyphMode"), ("Q", "glyphMode"), -3.0))
        args = dict(risky, glyph="Q", new_name="Qalt")
        self.assertIn("named like a kerning class", self.refused("rename_glyph", args, "class_name_clash")["message"])
        self.assertIsNotNone(self.font.findName("Q"))

    def test_a_rewrite_fontlab_does_not_keep_is_put_back(self):
        self.font.kerning("Bold").groups._f_refuse = True
        error = self.fails("rename_glyph", {"font": 7, "glyph": "Q", "new_name": "Qalt", "update_references": True,
                                            "risky": True}, code="not_kept")
        self.assertIn("kerning class round in master Bold", error["message"])
        self.assertIn("put back", error["message"])
        self.assertIsNotNone(self.font.findName("Q"))
        self.assertEqual(self.font.kerning("Bold").get(("Q", "A")), -7.0)
        self.assertIsNone(self.font.kerning("Bold").get(("Qalt", "A")))
        self.assertEqual(self.classes()["round"][0], ["O", "Q"])
        self.assertIn(u"sub Q by O", self.features.get_feature("ss01"))

    def test_delete_a_glyph_and_undo_it(self):
        reply = self.write("delete_glyph", {"glyph": "space"})
        self.assertEqual((reply["deleted"], reply["glyph"]), (True, u"space"))
        self.assertIsNone(self.font.findName("space"))
        self.assertEqual(self.state.content_updates, [7], "the Font window is told")
        self.assertEqual(self.state.steps[0][1][:2], [self.font, self.state.steps[0][1][1]])
        self.assertEqual(self.state.steps[-1][1], [self.font], "the deleted glyph is not recorded again")
        self.write("undo", {"expected_state": reply["history"]["history_state"]})
        self.font.fgPackage.rebuildMaps()
        self.assertIsNotNone(self.font.findName("space"))
        self.assertEqual(self.font.findName("space")._f_layer("Bold").advanceWidth, 260.0)

    def test_delete_refusals(self):
        self.assertIn("a component of Aacute", self.refused("delete_glyph", {"glyph": "acute"}, "glyph_in_use")["message"])
        self.assertIn("remove_references (a risky action)",
                      self.refused("delete_glyph", {"glyph": "Q"}, "glyph_in_use")["message"])
        error = self.refused("delete_glyph", {"glyph": "Q", "remove_references": True, "risky": True}, "glyph_in_use")
        self.assertIn("feature code", error["message"])
        self.refused("delete_glyph", {"glyph": "Nope"}, "no_glyph")
        self.refused("delete_glyph", {"glyph": "space", "remove_references": "yes"}, "bad_args")
        self.font._f_refuse_delete = True
        self.assertIn("did not delete", self.fails("delete_glyph", {"font": 7, "glyph": "space"}, code="not_kept")["message"])

    def test_delete_removes_kerning_and_class_memberships(self):
        w = fakes.flGlyph("W", [fakes.flLayer("Regular", [fakes.flShape([fakes.square(0, 0, 500)])], width=520.0)], [0x57])
        self.font._f_glyphs.append(w)
        self.font.fgPackage.rebuildMaps()
        self.font.kerning("Regular")._f_pairs.append((("W", "glyphMode"), ("A", "glyphMode"), -12.0))
        for master in ("Regular", "Bold"):
            self.font.kerning(master)._f_groups["V"] = (["V", "W"], "KernLeft")
        reply = self.write("delete_glyph", {"glyph": "W", "remove_references": True, "risky": True})
        self.assertEqual(reply["references"], {"kerning_pairs": 1, "kerning_classes": [u"V (Regular)", u"V (Bold)"],
                                               "features": [], "prefix": False})
        self.assertIsNone(self.font.findName("W"))
        self.assertEqual((self.classes()["V"][0], self.font.kerning("Regular").get(("W", "A"))), (["V"], None))
        self.write("undo", {"expected_state": reply["history"]["history_state"]})
        self.font.fgPackage.rebuildMaps()
        self.assertIsNotNone(self.font.findName("W"))
        self.assertEqual((self.classes("Bold")["V"][0], self.font.kerning("Regular").get(("W", "A"))), (["V", "W"], -12.0))


class OutlineWriteTests(FontCase):
    def setUp(self):
        FontCase.setUp(self)
        self.state.current = self.font

    TRIANGLE = [{"nodes": [{"x": 0, "y": 0}, {"x": 300, "y": 0}, {"x": 150, "y": 400}]}]
    # (live, 2026-11-19: "curve" is a cubic handle, "off" a TrueType off-curve point, "on" every node on the outline;
    # smooth holds on an on node beside a handle, and only when set after every node's type)
    CURVE = [{"closed": True, "nodes": [{"x": 0, "y": 0, "smooth": True}, {"x": 0, "y": 100, "type": "curve"},
                                        {"x": 50, "y": 150, "type": "curve"}, {"x": 100, "y": 150, "smooth": True},
                                        {"x": 100, "y": 0}]}]
    QUADRATIC = [{"closed": True, "nodes": [{"x": 0, "y": 0}, {"x": 0, "y": 100, "type": "off"},
                                            {"x": 100, "y": 100, "smooth": True}, {"x": 200, "y": 100, "type": "off"},
                                            {"x": 200, "y": 0}]}]

    def nodes(self, glyph, layer="Regular", shape=0):
        out = self.ok("outline", {"font": 7, "name": glyph, "layer": layer})
        return [[(n["x"], n["y"], n["type"], n["smooth"]) for n in c["nodes"]] for c in out["shapes"][shape]["contours"]]

    def test_contours_replace_the_shapes_and_undo(self):
        before = self.nodes("B")
        reply = self.write("set_outline", {"glyph": "B", "contours": self.TRIANGLE})
        self.assertEqual((reply["contours_before"], reply["contours"], reply["nodes"], reply["new_shape"]), (1, 1, 3, False))
        self.assertEqual(self.nodes("B"), [[(0.0, 0.0, "on", False), (300.0, 0.0, "on", False), (150.0, 400.0, "on", False)]])
        self.assertIn(self.glyph("B"), self.state.steps[-1][1])
        self.assertEqual(self.ok("glyph", {"font": 7, "name": "B"})["ink_box"], {"x": 0.0, "y": 0.0, "width": 300.0, "height": 400.0})
        self.write("undo", {"expected_state": reply["history"]["history_state"]})
        self.assertEqual(self.nodes("B"), before)
        self.assertEqual(self.nodes("B", "Bold"), [[(50, 0, "on", False), (450, 0, "on", False), (450, 400, "on", False),
                                                     (50, 400, "on", False)]], "the other master is left alone")

    def test_node_types_and_smoothness(self):
        """Live, 2026-11-19: smooth set in the same pass as the types was lost on a node whose next neighbor
        was not yet a handle (the first node here)."""
        self.write("set_outline", {"glyph": "B", "contours": self.CURVE, "layer": "Bold"})
        self.assertEqual(self.nodes("B", "Bold"), [[(0.0, 0.0, "on", True), (0.0, 100.0, "curve", False),
                                                     (50.0, 150.0, "curve", False), (100.0, 150.0, "on", True),
                                                     (100.0, 0.0, "on", False)]])
        self.assertFalse(self.write("set_outline", {"glyph": "B", "contours": self.CURVE, "layer": "Bold"})["changed"])
        self.write("set_outline", {"glyph": "B", "contours": self.QUADRATIC})
        self.assertEqual([n[2:] for n in self.nodes("B")[0]], [("on", False), ("off", False), ("on", True),
                                                                ("off", False), ("on", False)])

    def test_smoothness_fontlab_refuses_is_named(self):
        corner = [{"nodes": [{"x": 0, "y": 0}, {"x": 100, "y": 0, "smooth": True}, {"x": 100, "y": 100}]}]
        error = self.fails("set_outline", {"font": 7, "glyph": "B", "contours": corner}, code="not_kept")
        self.assertIn("contour 0 node 1 reads 100 0 on, not 100 0 on smooth", error["message"])
        self.assertEqual(len(self.nodes("B")[0]), 4, "put back")

    def test_a_new_shape_the_layer_did_not_take_is_not_kept(self):
        """Codex, 2026-11-19: the new shape was read back from the shape made, not from the layer."""
        saved = fakes.flLayer.addShape
        fakes.flLayer.addShape = lambda layer, shape: None  # (FontLab keeps nothing, and says nothing)
        try:
            error = self.fails("set_outline", {"font": 7, "glyph": "Aacute", "contours": self.TRIANGLE}, code="not_kept")
        finally:
            fakes.flLayer.addShape = saved
        self.assertIn("0 shapes of its own", error["message"])
        self.assertEqual(len(self.layer("Aacute").shapes), 2)

    def test_a_changed_copy_the_layer_took_is_not_kept(self):
        saved = fakes.flLayer.addShape

        def altered(layer, shape):
            saved(layer, fakes.flShape([fakes.square(0, 0, 10)]))  # (a shape, but not the one given)
        fakes.flLayer.addShape = altered
        try:
            error = self.fails("set_outline", {"font": 7, "glyph": "Aacute", "contours": self.TRIANGLE}, code="not_kept")
        finally:
            fakes.flLayer.addShape = saved
        self.assertIn("did not keep the contours", error["message"])

    def test_a_layer_without_a_shape_of_its_own_gets_one(self):
        reply = self.write("set_outline", {"glyph": "Aacute", "contours": self.TRIANGLE})
        self.assertEqual((reply["new_shape"], reply["shape"]), (True, 2))
        out = self.ok("outline", {"font": 7, "name": "Aacute"})
        self.assertEqual([s.get("component") for s in out["shapes"]], ["A", "acute", None], "the components are kept")
        self.assertFalse(self.write("set_outline", {"glyph": "space", "contours": []})["changed"])

    def test_which_shape(self):
        layer = self.layer("B")
        layer._f_shapes.append(fakes.flShape([fakes.square(0, 600, 50)]))
        self.assertIn("give shape", self.refused("set_outline", {"glyph": "B", "contours": self.TRIANGLE}, "bad_args")["message"])
        self.write("set_outline", {"glyph": "B", "contours": self.TRIANGLE, "shape": 1})
        self.assertEqual(len(self.nodes("B", shape=1)[0]), 3)
        self.assertEqual(len(self.nodes("B", shape=0)[0]), 4, "the other shape is left alone")
        self.refused("set_outline", {"glyph": "Aacute", "contours": self.TRIANGLE, "shape": 0}, "bad_args")
        self.refused("set_outline", {"glyph": "B", "contours": self.TRIANGLE, "shape": True}, "bad_args")

    def test_refusals(self):
        for bad in (None, {}, [{"nodes": [{"x": 0, "y": 0}]}], [{"nodes": [{"x": 0, "y": "1"}, {"x": 1, "y": 1}]}],
                    [{"nodes": [{"x": 0, "y": 0}, {"x": 1, "y": 1, "type": "qcurve"}]}],
                    [{"nodes": [{"x": 0, "y": 0}, {"x": 1, "y": 1}], "closed": "no"}],
                    [{"nodes": [{"x": 0, "y": 0, "type": "off"}, {"x": 1, "y": 1}], "closed": False}],
                    [{"nodes": [{"x": 0, "y": 0}, {"x": 1, "y": 1}], "z": 1}],
                    [{"nodes": [{"x": 0, "y": 0}, {"x": 40000, "y": 1}]}]):
            self.refused("set_outline", {"glyph": "B", "contours": bad}, "bad_args")
        self.refused("set_outline", {"glyph": "acute", "contours": self.TRIANGLE, "layer": "Bold"}, "no_layer")

    def test_rounded_coordinates_are_said(self):
        self.layer("B").shapes[0]._f_round = True
        reply = self.write("set_outline", {"glyph": "B", "contours": [{"nodes": [{"x": 0.3, "y": 0}, {"x": 300, "y": 0.2},
                                                                                 {"x": 150, "y": 400}]}]})
        self.assertIn("rounded", reply["rounded"])
        self.assertEqual(self.nodes("B")[0][0], (0.0, 0.0, "on", False))

    def test_a_new_shape_fontlab_did_not_keep_is_taken_out(self):
        fontlab = sys.modules["fontlab"]
        saved = fontlab.flShape

        def dropping():
            shape = fakes.flShape([])
            object.__setattr__(shape, "_f_drop", True)
            return shape
        fontlab.flShape = dropping
        try:
            error = self.fails("set_outline", {"font": 7, "glyph": "Aacute", "contours": self.TRIANGLE}, code="not_kept")
        finally:
            fontlab.flShape = saved
        self.assertIn("put back", error["message"])
        self.assertEqual(len(self.layer("Aacute").shapes), 2)

    def test_outline_with_the_transforms_applied(self):
        out = self.ok("outline", {"font": 1, "name": "o", "apply_transforms": True})
        nodes = out["shapes"][0]["contours"][0]["nodes"]
        self.assertEqual((nodes[1]["x"], nodes[1]["y"]), (1 * 10 + 0.2 * 50 + 10, 50.0), "x' = m11 x + m21 y + dx")
        self.assertIn("is applied", out["coordinates"])
        out = self.ok("outline", {"font": 7, "name": "Aacute", "apply_transforms": True})
        self.assertEqual(out["shapes"][1]["contours"][0]["nodes"][0], {"x": 200.0, "y": 500.0, "type": "on", "smooth": False})
        self.fails("outline", {"font": 1, "name": "o", "apply_transforms": 1}, code="bad_args")


class ResolvedKerningTests(FontCase):
    def resolved(self, left, right, **extra):
        args = {"font": 7, "left": left, "right": right, "resolved": True}
        args.update(extra)
        return self.ok("kerning", args)

    def test_pairs_through_classes(self):
        for left, right, value, through in (("V", "A", -50.0, u"@V A"), ("A", "Q", -10.0, u"A @round"),
                                            ("A", "B", -20.0, u"A B"), ("B", "A", None, None)):
            reply = self.resolved(left, right)
            self.assertEqual((reply["value"], reply["computed"], reply["fontlab"], reply["through"], reply["resolved_by"],
                              reply["agree"]), (value, value, value, through, "fontlab", True), (left, right))
        self.assertEqual(self.resolved("V", "A", master="Bold")["value"], None)
        self.assertEqual(self.resolved("A", "O")["right_classes"], [u"round"])

    def test_a_glyph_named_like_its_class_is_read_by_mode(self):
        self.font.kerning("Regular")._f_pairs.append((("V", "glyphMode"), ("A", "glyphMode"), -70.0))
        reply = self.resolved("V", "A")
        self.assertEqual((reply["computed"], reply["through"], reply["fontlab"]), (-70.0, u"V A", -70.0))

    def test_fontlab_first_then_computed(self):
        self.font.kerning("Regular")._f_plain = 5.0
        reply = self.resolved("V", "A")
        self.assertEqual((reply["value"], reply["computed"], reply["agree"]), (-45.0, -50.0, False))
        self.font.kerning("Regular")._f_plain = "raise"
        reply = self.resolved("V", "A")
        self.assertEqual((reply["value"], reply["resolved_by"], reply["fontlab"]), (-50.0, "computed", None))
        self.assertIn("getPlainPairsDict failed", reply["fontlab_error"])

    def test_a_large_flattening_is_not_asked_for(self):
        """Codex, 2026-11-19: one pair asked FontLab to flatten the whole master's class kerning, on its main
        thread, however large."""
        kerning = self.font.kerning("Regular")
        object.__setattr__(kerning, "_f_plain", "raise")  # (asked for, it would say so)
        saved = fl.MAX_PLAIN_PAIRS
        fl.MAX_PLAIN_PAIRS = 2
        try:
            reply = self.resolved("V", "A")
        finally:
            fl.MAX_PLAIN_PAIRS = saved
        self.assertEqual((reply["value"], reply["resolved_by"], reply["fontlab"]), (-50.0, "computed", None))
        self.assertIn("was not asked for", reply["fontlab_skipped"])
        self.assertNotIn("fontlab_error", reply)

    def test_refusals(self):
        self.fails("kerning", {"font": 7, "left": "@V", "right": "A", "resolved": True}, code="bad_args")
        self.fails("kerning", {"font": 7, "resolved": True}, code="bad_args")
        self.fails("kerning", {"font": 7, "left": "Nope", "right": "A", "resolved": True}, code="no_glyph")
        self.fails("kerning", {"font": 7, "left": "A", "right": "B", "resolved": "yes"}, code="bad_args")


class SaveExportTests(FontCase):
    def test_save_to_the_fonts_own_file(self):
        reply = self.ok("save_font", {"font": 7, "risky": True})
        self.assertEqual((reply["path"], reply["how"], reply["brought_forward"]),
                         ("fixtures/Write.vfc", u"File > Save (actionSaveFont)", True))  # (its name, live 2026-11-19)
        self.assertEqual(self.state.saved, [], "after the reply")
        self.state.fire_timers()
        self.assertEqual(self.state.saved, [self.font])
        self.assertIs(self.state.current, self.sample, "the font that was in front is put back")
        self.ok("save_font", {"font": 7, "risky": True, "keep_in_front": True})
        self.state.fire_timers()
        self.assertIs(self.state.current, self.font)

    def test_save_to_a_path(self):
        path = os.path.join(self.tmp, "Copy.ufo")
        reply = self.ok("save_font", {"font": 7, "path": path, "risky": True})
        self.assertEqual(reply["how"], "fgFont.save")
        self.state.fire_timers()
        self.assertEqual(self.font.fgPackage._f_saves, [(path, False)])
        self.assertTrue(os.path.isdir(path))
        self.assertIn("overwrite", self.fails("save_font", {"font": 7, "path": path, "risky": True}, code="file_exists")["message"])
        self.ok("save_font", {"font": 7, "path": path, "overwrite": True, "risky": True})
        vfb = os.path.join(self.tmp, "Copy.vfb")
        self.ok("save_font", {"font": 7, "path": vfb, "risky": True})
        self.state.fire_timers()
        self.assertEqual(self.font.fgPackage._f_saves[-1], (vfb, True))
        for bad in (os.path.join(self.tmp, "Copy.vfc"), "Copy.ufo", 5):
            self.fails("save_font", {"font": 7, "path": bad, "risky": True}, code="bad_args")
        self.fails("save_font", {"font": 7, "path": os.path.join(self.tmp, "no", "Copy.ufo"), "risky": True}, code="not_found")

    def test_export_into_a_folder(self):
        reply = self.ok("export_font", {"font": 7, "folder": self.tmp, "format": "OpenType PS", "risky": True})
        self.assertEqual((reply["exporting"], reply["format"]), (True, u"OpenType PS"))
        self.assertEqual(self.state.exports, [])
        self.state.fire_timers()
        package, values = self.state.exports[0]
        self.assertIs(package, self.font)
        self.assertEqual(values, {"profileName": u"OpenType PS", "destinationMode": 3,
                                  "destinationFolder": self.tmp.replace(os.sep, "/").rstrip("/") + "/"})
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "WriteSans-Regular.ttf")))
        self.assertEqual((self.state.answers, self.state.question), ([u"OK"], None), "its Font Exported message closed")
        self.ok("export_font", {"font": 7, "folder": self.tmp, "risky": True})
        self.state.fire_timers()
        self.assertEqual(self.state.exports[1][1]["profileName"], u"OpenType TT", "the user's last profile")

    def test_export_refusals(self):
        self.fails("export_font", {"font": 7, "folder": "out", "risky": True}, code="bad_args")
        self.fails("export_font", {"font": 7, "folder": os.path.join(self.tmp, "none"), "risky": True}, code="not_found")
        self.fails("export_font", {"font": 7, "folder": self.tmp, "format": 5, "risky": True}, code="bad_args")
        self.assertEqual(self.state.timers, [])


class OpenedPathsTests(FontCase):
    def test_paths_open_font_opened_outlive_a_listener_restart(self):
        path = os.path.join(self.tmp, "Open.ttf")
        with open(path, "wb") as handle:
            handle.write(b"font")
        self.ok("open_font", {"path": path})
        self.state.fire_timers()
        with open(self.listener.opened_file()) as handle:
            self.assertEqual(json.load(handle), {"pid": os.getpid(), "fonts": [{"id": 101, "path": path}]})
        again = fl.Listener(port=0, state_file=self.listener.state_file)
        self.assertEqual(again.opened, [{"id": 101, "path": path}], "the same FontLab process")
        self.state.fonts[-1]._f_states.append(4)
        self.listener = again
        self.ok("close_font", {"font": 101, "discard_changes": True})
        self.state.fire_timers()
        self.assertEqual(again.opened, [])
        with open(again.opened_file()) as handle:
            self.assertEqual(json.load(handle)["fonts"], [])

    def test_another_processs_paths_are_ignored(self):
        with open(self.listener.opened_file(), "w") as handle:
            json.dump({"pid": os.getpid() + 1, "fonts": [{"id": 7, "path": "elsewhere.ttf"}]}, handle)
        self.assertEqual(fl.Listener(port=0, state_file=self.listener.state_file).opened, [])
        with open(self.listener.opened_file(), "w") as handle:
            handle.write("{broken")
        self.assertEqual(fl.Listener(port=0, state_file=self.listener.state_file).opened, [])
        with open(self.listener.opened_file(), "w") as handle:  # (1.2.0's paths alone grant nothing)
            json.dump({"pid": os.getpid(), "paths": ["elsewhere.ttf"]}, handle)
        self.assertEqual(fl.Listener(port=0, state_file=self.listener.state_file).opened, [])

    def font_file(self):
        path = os.path.join(self.tmp, "Open.ttf")
        with open(path, "wb") as handle:
            handle.write(b"font")
        return path

    def test_the_same_file_opened_again_by_hand_is_not_exempt(self):
        """Codex, 2026-11-19: the path alone stood for the font, so a font the user closed and opened again
        by hand could have its changes thrown away without risky mode."""
        path = self.font_file()
        self.ok("open_font", {"path": path})
        self.state.fire_timers()
        self.state.drop(self.state.fonts[-1])  # (the user closes it)
        manual = fakes.sample_font(path=path, font_id=202)  # (and opens the file again)
        manual._f_states.append(5)
        self.state.fonts.append(manual)
        self.state.current = manual
        self.state.asks_to_save.add(202)
        self.fails("close_font", {"font": 202, "discard_changes": True}, code="risky_mode_off")
        self.state.fire_timers()
        self.assertIn(manual, self.state.fonts)
        self.assertEqual(self.listener.opened, [], "the closed font forgotten")

    def test_an_open_that_did_not_happen_grants_nothing(self):
        self.state.hold_open = True
        path = self.font_file()
        self.state.on_modal = lambda: fl._click_modal_button("Cancel")  # (the user cancels Coordinates Rounding)
        self.ok("open_font", {"path": path})
        self.state.fire_timers()
        self.assertEqual((self.state.loads, self.listener.opened), ([path], []))
        self.state.hold_open = False
        manual = fakes.sample_font(path=path, font_id=303)  # (then opens the file by hand)
        manual._f_states.append(5)
        self.state.fonts.append(manual)
        self.state.current = manual
        self.fails("close_font", {"font": 303, "discard_changes": True}, code="risky_mode_off")


def _file_sddl(path):
    """A file's access list as SDDL text (Windows), read back through the Win32 security API."""
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32")
    kernel = ctypes.WinDLL("kernel32")
    advapi.GetFileSecurityW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    needed = wintypes.DWORD()
    advapi.GetFileSecurityW(fl._text(path), 4, None, 0, ctypes.byref(needed))
    buffer = ctypes.create_string_buffer(needed.value)
    assert advapi.GetFileSecurityW(fl._text(path), 4, buffer, needed, ctypes.byref(needed))
    text = ctypes.c_void_p()
    assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer, 1, 4, ctypes.byref(text), None)
    try:
        return ctypes.wstring_at(text.value)
    finally:
        kernel.LocalFree(text)


class HardeningTests(ListenerCase):
    def test_the_state_file_is_closed_to_other_accounts(self):
        self.listener.bind()
        if os.name != "nt":
            self.assertTrue(self.listener.state_private)
            return
        self.assertIs(self.listener.state_private, True)
        sddl = _file_sddl(self.listener.state_file)
        self.assertTrue(sddl.startswith(u"D:P(A;;FA;;;S-1-"), sddl)
        self.assertEqual(sddl.count(u"(A;"), 1, "one entry: this user")
        self.assertIs(self.ok("ping")["state_file_private"], True)
        with open(self.listener.state_file) as handle:
            self.assertEqual(json.load(handle)["token"], self.listener.token, "still readable by its own user")

    def test_a_failure_to_close_it_is_reported_not_fatal(self):
        if os.name != "nt":
            return
        saved = (fl._Win32Security.descriptor, fl._owner_only)
        fl._Win32Security.descriptor = lambda win: u"ConvertSidToStringSidW failed (error 5)"
        fl._owner_only = lambda path: u"SetFileSecurityW failed (error 5)"
        try:
            self.listener.bind()
        finally:
            fl._Win32Security.descriptor, fl._owner_only = saved
        private = self.ok("ping")["state_file_private"]
        self.assertIn(u"ConvertSidToStringSidW failed (error 5)", private)
        self.assertIn(u"SetFileSecurityW failed (error 5)", private)
        self.assertTrue(os.path.isfile(self.listener.state_file))

    def test_the_file_is_born_closed_to_other_accounts(self):
        """Codex, 2026-11-19: the file was made with inherited access, and closed only afterward."""
        if os.name != "nt":
            return
        path = os.path.join(self.tmp, "born.json")
        handle, private = fl._create_private(path)
        try:
            self.assertIs(private, True)
            sddl = _file_sddl(path)  # (read while nothing is written yet, the file still held open)
            self.assertTrue(sddl.startswith(u"D:P(A;;FA;;;S-1-"), sddl)
            self.assertEqual(sddl.count(u"(A;"), 1)
            handle.write("{}")
        finally:
            handle.close()
        with open(path) as reading:
            self.assertEqual(reading.read(), "{}")
        with self.assertRaises(OSError):
            fl._create_private(path)

    class Peer(object):
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        def setblocking(self, flag):
            pass

        def fileno(self):
            return -1

        def recv(self, size):
            raise socket.error(errno.EWOULDBLOCK, "would block")

    class Server(object):
        def __init__(self, peers):
            self.peers = list(peers)

        def accept(self):
            if not self.peers:
                raise socket.error(errno.EWOULDBLOCK, "would block")
            return self.peers.pop(0)

        def close(self):
            pass

    def test_peers_outside_the_loopback_are_refused(self):
        outside, inside = self.Peer(), self.Peer()
        self.listener.server = self.Server([(outside, ("192.168.1.20", 50000)), (inside, ("127.0.0.1", 50001))])
        self.listener.poll()
        self.assertTrue(outside.closed)
        self.assertEqual(([c.sock for c in self.listener.clients], self.listener.refused_peers), ([inside], 1))
        self.listener.clients = []

    def test_wrong_tokens_close_connections_for_a_moment(self):
        for _ in range(fl.BAD_TOKEN_LIMIT - 1):
            self.request("ping", token="0" * 32)
        self.assertEqual(self.listener.throttled_until, 0.0)
        self.request("ping", token="0" * 32)
        self.assertGreater(self.listener.throttled_until, time.time())
        peer = self.Peer()
        self.listener.server = self.Server([(peer, ("127.0.0.1", 50002))])
        self.listener.poll()
        self.assertTrue(peer.closed, "closed unanswered")
        self.assertEqual(self.listener.clients, [])
        self.listener.throttled_until = 0.0


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
