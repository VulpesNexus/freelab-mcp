# -*- coding: utf-8 -*-
"""The window tools against spy fakes of Qt widgets (Python 2.7 and Python 3): windows, controls and their
addresses, actions run from a timer after the reply and checked again then, and the safety rules."""

from __future__ import absolute_import, division, print_function

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from listener_tests import FontCase, fl  # noqa: E402
import widgetfakes as W  # noqa: E402

TITLE = u"FreeLab Auto Kerning (Free Sans Regular)"


class ToolWindow(object):
    """A script's tool window as FreeLab Script Suite builds one: a top line, a tools button with a menu, a busy
    row (a step and a bar, hidden), a tab bar over two pages, a log with links, and Close."""

    def __init__(self, applied=None):
        self.applied = []
        self.top = W.Widget("QLabel", text=u"<b>Free Sans Regular</b>: 3 pairs", textFormat=1)
        self.tools = W.Widget("QToolButton", toolTip=u"FreeLab tools\nOpens another tool on this font.", popupMode=2)
        self.font_check = W.Action(u"Font &Check")
        self.export = W.Action(u"Export Proof…", name=u"exportProof")
        self.tools._f_menu = W.menu(self.font_check, W.Action(u"", separator=True), self.export,
                                    W.Action(u"Hidden", visible=False))
        self.step = W.Widget("QLabel", name=u"freelabBusyStep", text=u"")
        self.bar = W.Widget("QProgressBar", minimum=0, maximum=1000, value=0)
        self.busy_row = W.Widget("QWidget", [self.step, self.bar], name=u"freelabBusy")
        self.busy_row._f_shown = False
        self.master_label = W.Widget("QLabel", text=u"Master:")
        self.master = W.Widget("QComboBox", items=[u"Regular", u"Bold"], currentIndex=0)
        self.tolerance_label = W.Widget("QLabel", text=u"&Tolerance:")
        self.tolerance = W.Widget("QSpinBox", value=5, minimum=0, maximum=50)
        self.tolerance_label._f_buddy = self.tolerance
        self.only = W.Widget("QCheckBox", text=u"Only &kerned pairs")
        self.rows = [W.Row(u"A V", u"-50", check=2, children=[W.Row(u"A Vtilde", u"-45", check=0)], expanded=True),
                     W.Row(u"T o", u"-30", check=0), W.Row(u"L T", u"-60")]
        self.pairs = W.view("QTreeWidget", self.rows, headers=(u"Pair", u"Value"))
        self.apply = W.Widget("QPushButton", text=u"&Apply", on={"clicked": lambda: self.applied.append(1)})
        self.save = W.Widget("QPushButton", text=u"Save Pairs…")
        self.page1 = W.Widget("QWidget", [self.master_label, self.master, self.tolerance_label, self.tolerance,
                                          self.only, self.pairs, self.apply, self.save])
        self.page1._f_layout = W.Layout(W.Layout(self.master_label, self.master), W.Layout(self.tolerance_label, self.tolerance),
                                        self.only, self.pairs, W.Layout(self.apply, self.save))
        self.apply2 = W.Widget("QPushButton", text=u"Apply")
        self.filter = W.Widget("QLineEdit", name=u"classFilter", text=u"")
        self.page2 = W.Widget("QWidget", [self.apply2, self.filter])
        self.page2._f_shown = False
        self.tabs = W.Widget("QTabBar", tabs=[u"Pairs", u"Classes"], currentIndex=0, pages=[self.page1, self.page2])
        self.log = W.Widget("QTextBrowser", readOnly=True, plain=u"Applied 2 pairs: A V help",
                            html=u'<p>Applied 2 pairs: <a href="glyph:A"><span style="x">A</span></a> '
                                 u'<a href="glyph:V">V</a> <a href="https://example.org/help">help</a></p>')
        self.close = W.Widget("QPushButton", text=u"Close")
        self.window = W.window("QDialog", TITLE, [self.top, self.tools, self.busy_row, self.tabs, self.page1, self.page2,
                                                  self.log, self.close])


class WindowCase(FontCase):
    """FontLab with a script's tool window shown beside the main window."""

    def setUp(self):
        FontCase.setUp(self)
        self.tool = ToolWindow()
        self.state.qt_windows = [self.tool.window]
        fl._WINDOW_NUMBERS[0] = 0  # (windows of one name are numbered from 1 in each test's FontLab)

    def queued(self, op, args):
        reply = self.ok(op, args)
        self.assertTrue(reply["queued"])
        return reply

    def outcome(self, reply):
        return self.ok("action_result", {"action_id": reply["action_id"]})["action"]

    def act(self, op, args):
        """An action, asked and then run as its timer fires; what became of it."""
        reply = self.queued(op, args)
        self.state.fire_timers()
        return self.outcome(reply)

    def controls(self, **args):
        args.setdefault("window", TITLE)
        return dict((c["address"], c) for c in self.ok("window_controls", args)["controls"])

    def addresses(self, **args):
        args.setdefault("window", TITLE)
        return [c["address"] for c in self.ok("window_controls", args)["controls"]]

    def box(self, title=u"Replace?", parent=None, own=False):
        """A script's question (a QMessageBox the tool window parents), shown modal; own: it says it is the
        script's (mcpScriptWindow)."""
        self.replaced = []
        box = W.window("QMessageBox", title, [W.Widget("QLabel", text=u"Replace the kerning?"),
                                              W.Widget("QPushButton", text=u"&Replace", on={"clicked": lambda: self.replaced.append(1)}),
                                              W.Widget("QPushButton", text=u"Cancel")], modal=True)
        box._f_parent = parent if parent is not None else self.tool.window
        if own:
            box._f_props["mcpScriptWindow"] = True
        self.state.qt_windows.append(box)
        self.state.qt_modal = box
        return box


class ListWindowsTests(WindowCase):
    def test_windows_with_keys_and_busy(self):
        self.state.windows = [u"Glyph Window", u"Glyph Window"]
        windows = self.ok("windows")["windows"]
        self.assertEqual([w["window"] for w in windows], [u"main", u"Glyph Window#1", u"Glyph Window#2", TITLE])
        tool = windows[-1]
        self.assertEqual((tool["class"], tool["busy"], tool["waiting"], tool["question"], tool["script_window"]),
                         ("QDialog", False, False, False, False))
        self.tool.busy_row._f_shown = True  # (its work began: the bar shown)
        self.tool.bar._f_set(value=250)
        busy = self.ok("windows")["windows"][-1]
        self.assertEqual((busy["busy"], busy["busy_by"], busy["progress"]), (True, "progress_bar", 0.25))
        self.tool.window._f_props["mcpBusy"] = u"Kerning A V"  # (the convention outranks the bar)
        busy = self.ok("windows")["windows"][-1]
        self.assertEqual((busy["busy"], busy["busy_by"], busy["step"]), (True, "property", u"Kerning A V"))
        self.tool.window._f_props["mcpBusy"] = False
        self.assertFalse(self.ok("windows")["windows"][-1]["busy"], "the window says it is not busy, bar or not")

    def test_a_question_is_waited_on(self):
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK", u"Cancel"])
        windows = self.ok("windows")
        question = [w for w in windows["windows"] if w["window"] == u"Coordinates Rounding"][0]
        self.assertEqual((windows["dialog_waiting"], question["waiting"], question["question"], question["modal"]),
                         (True, True, True, True))

    def test_finding_a_window(self):
        for wanted in (TITLE, u"auto kerning", u"QDialog"):
            self.assertEqual(self.ok("window_controls", {"window": wanted})["window"], TITLE)
        self.assertEqual(self.ok("window_controls", {"window": "main"})["window"], u"main")
        self.state.windows = [u"Auto Kerning notes"]
        self.assertIn(u"Give one's key", self.fails("window_controls", {"window": "auto kerning"}, code="ambiguous_window")["message"])
        self.assertIn(TITLE, self.fails("window_controls", {"window": "Nope"}, code="not_found")["message"])
        self.fails("window_controls", {"window": "dialog"}, code="not_found")
        self.fails("window_controls", {"window": ""}, code="bad_args")


class ControlTests(WindowCase):
    def test_addresses(self):
        self.assertEqual(self.addresses(), [
            u"label#1", u"FreeLab tools > button", u"tabs", u"label#2", u"Master > combo", u"label#3",
            u"Tolerance > spin", u"Only kerned pairs > check", u"tree", u"Apply > button#1", u"Save Pairs… > button",
            u"log", u"Close > button"])
        everything = self.controls(include_hidden=True)
        for address in (u"freelabBusyStep", u"progress", u"Apply > button#2", u"classFilter"):
            self.assertIn(address, everything)
        self.assertFalse(everything[u"Apply > button#2"]["visible"])

    def test_what_each_control_holds(self):
        controls = self.controls()
        self.assertEqual(controls[u"label#1"]["text"], u"Free Sans Regular: 3 pairs")
        tools = controls[u"FreeLab tools > button"]
        self.assertEqual((tools["menu"], tools["click_needs_entry"]), ([u"Font Check", u"Export Proof…"], True))
        self.assertEqual(controls[u"tabs"]["tabs"], [u"Pairs", u"Classes"])
        master = controls[u"Master > combo"]
        self.assertEqual((master["choices"], master["current"], master["caption"]), ([u"Regular", u"Bold"], u"Regular", u"Master"))
        self.assertEqual((controls[u"Tolerance > spin"]["value"], controls[u"Tolerance > spin"]["maximum"]), (5, 50))
        self.assertEqual(controls[u"Only kerned pairs > check"]["checked"], False)
        self.assertEqual((controls[u"tree"]["columns"], controls[u"tree"]["rows"]), ([u"Pair", u"Value"], 3))
        self.assertEqual((controls[u"log"]["length"], controls[u"log"]["read_only"]), (25, True))
        self.assertTrue(controls[u"Save Pairs… > button"]["risky"], "named like a save")
        self.assertNotIn("risky", controls[u"Apply > button#1"])
        self.tool.window._f_props["mcpScriptWindow"] = True  # (the script says the window is its own)
        self.assertNotIn("risky", self.controls()[u"Save Pairs… > button"])
        listed = self.ok("window_controls", {"window": TITLE, "kinds": ["button"], "limit": 2})
        self.assertEqual((listed["count"], len(listed["controls"]), listed["more"]), (4, 2, True))
        self.fails("window_controls", {"window": TITLE, "kinds": ["knob"]}, code="bad_args")

    def test_object_names_are_addresses_when_unique(self):
        self.tool.apply._f_set(objectName=u"applyPairs")
        self.tool.apply2._f_set(objectName=u"applyPairs")  # (shared: not an address)
        self.tool.master._f_set(objectName=u"masterChoice")
        controls = self.controls(include_hidden=True)
        self.assertIn(u"masterChoice", controls)
        self.assertIn(u"Apply > button#2", controls)


class ClickTests(WindowCase):
    def test_a_click_runs_after_the_reply(self):
        reply = self.queued("click", {"window": TITLE, "control": "apply"})  # (its caption: the one shown of two)
        self.assertEqual((reply["control"], self.tool.applied), (u"Apply > button#1", []))
        self.assertEqual(self.outcome(reply)["state"], "queued")
        self.state.fire_timers()
        outcome = self.outcome(reply)
        self.assertEqual((outcome["state"], outcome["result"], self.tool.applied), ("done", {"clicked": u"Apply > button#1"}, [1]))
        self.assertEqual(self.tool.apply._f_emitted, [("clicked",)])

    def test_refusals(self):
        self.fails("click", {"window": TITLE, "control": u"Apply > button#2"}, code="unavailable")
        self.tool.apply._f_set(enabled=False)
        self.fails("click", {"window": TITLE, "control": u"Apply > button#1"}, code="disabled")
        self.fails("click", {"window": TITLE, "control": u"Master > combo"}, code="wrong_kind")
        self.assertIn(u"Close > button", self.fails("click", {"window": TITLE, "control": "Knob"}, code="not_found")["message"])
        self.tool.page2._f_shown = True
        self.tool.apply._f_set(enabled=True)
        self.fails("click", {"window": TITLE, "control": "Apply"}, code="ambiguous_control")
        self.fails("click", {"window": TITLE, "control": u"tree"}, code="bad_args")  # (a row is needed: row)
        self.assertEqual(self.state.timers, [])

    def test_a_click_named_like_a_save_is_risky(self):
        error = self.fails("click", {"window": TITLE, "control": u"Save Pairs…"}, code="risky_mode_off")
        self.assertEqual(error["data"]["action"], "risky_command")
        self.assertEqual(self.act("click", {"window": TITLE, "control": u"Save Pairs…", "risky": True})["state"], "done")
        self.tool.window._f_props["mcpScriptWindow"] = True
        self.assertEqual(self.act("click", {"window": TITLE, "control": u"Save Pairs…"})["state"], "done")
        self.tool.page1._f_props["mcpRisky"] = True  # (the script marks a part of its window risky)
        self.fails("click", {"window": TITLE, "control": u"Apply > button#1"}, code="risky_mode_off")
        self.assertEqual(self.tool.save._f_emitted, [("clicked",), ("clicked",)])

    def test_a_tools_menu_entry(self):
        self.assertIn(u"Font Check", self.fails("click", {"window": TITLE, "control": u"FreeLab tools"}, code="wrong_kind")["message"])
        outcome = self.act("click", {"window": TITLE, "control": u"FreeLab tools", "entry": u"font check"})
        self.assertEqual((outcome["state"], outcome["result"], self.tool.font_check._f_triggered), ("done", {"entry": u"Font Check"}, 1))
        self.fails("click", {"window": TITLE, "control": u"FreeLab tools", "entry": u"Hidden"}, code="not_found")
        self.fails("click", {"window": TITLE, "control": u"FreeLab tools", "entry": u"Export Proof"}, code="risky_mode_off")
        self.tool.window._f_props["mcpScriptWindow"] = True
        self.assertEqual(self.act("click", {"window": TITLE, "control": u"FreeLab tools", "entry": u"Export Proof"})["state"], "done")
        self.tool.export._f_props["mcpRisky"] = True
        self.fails("click", {"window": TITLE, "control": u"FreeLab tools", "entry": u"Export Proof"}, code="risky_mode_off")
        self.fails("click", {"window": TITLE, "control": u"Apply > button#1", "entry": u"x"}, code="wrong_kind")
        self.tool.tools._f_set(popupMode=1)  # (a menu from its arrow: the button itself clicks)
        self.assertEqual(self.act("click", {"window": TITLE, "control": u"FreeLab tools"})["state"], "done")

    def test_a_check_box_click_says_its_state(self):
        outcome = self.act("click", {"window": TITLE, "control": u"Only kerned pairs > check"})
        self.assertEqual(outcome["result"], {"clicked": u"Only kerned pairs > check", "checked": True})

    def test_checked_again_when_its_turn_comes(self):
        reply = self.queued("click", {"window": TITLE, "control": u"Apply > button#1"})
        self.tool.apply._f_set(enabled=False)  # (disabled by the time of the click)
        self.state.fire_timers()
        outcome = self.outcome(reply)
        self.assertEqual((outcome["state"], outcome["code"], self.tool.applied), ("skipped", "disabled", []))
        self.tool.apply._f_set(enabled=True)
        reply = self.queued("click", {"window": TITLE, "control": u"Apply > button#1"})
        self.tool.window._f_set(windowTitle=u"FreeLab Auto Kerning (Other Font)")  # (another font's window by then)
        self.state.fire_timers()
        self.assertEqual((self.outcome(reply)["code"], self.tool.applied), ("not_found", []))
        self.tool.window._f_set(windowTitle=TITLE)
        reply = self.queued("click", {"window": TITLE, "control": u"Apply > button#1"})
        self.tool.apply._f_set(text=u"Apply all")  # (the control the address named is another by then)
        self.tool.save._f_set(text=u"Apply")
        self.state.fire_timers()
        self.assertEqual(self.outcome(reply)["state"], "skipped")
        self.assertEqual(self.tool.applied, [])

    def test_a_stopped_listener_does_nothing(self):
        reply = self.queued("click", {"window": TITLE, "control": u"Apply > button#1"})
        self.listener.stopped = True
        self.state.fire_timers()
        self.assertEqual((self.outcome(reply)["code"], self.tool.applied), ("listener_stopped", []))


class RowTests(WindowCase):
    def test_read_list(self):
        reply = self.ok("read_list", {"window": TITLE, "control": "tree"})
        self.assertEqual((reply["columns"], reply["total"], reply["more"]), ([u"Pair", u"Value"], 4, False))
        self.assertEqual(reply["rows"][0], {"row": 0, "texts": [u"A V", u"-50"], "checked": True, "selected": False,
                                            "depth": 0, "children": 1, "expanded": True})
        self.assertEqual(reply["rows"][1], {"row": 1, "texts": [u"A Vtilde", u"-45"], "checked": False, "selected": False,
                                            "depth": 1})
        self.assertNotIn("checked", reply["rows"][3])
        page = self.ok("read_list", {"window": TITLE, "control": "tree", "offset": 2, "limit": 1, "columns": [1]})
        self.assertEqual((page["rows"], page["more"], page["columns"]),
                         ([{"row": 2, "texts": [u"-30"], "checked": False, "selected": False, "depth": 0}], True, [u"Value"]))
        for bad in ([2], [], [True], "0"):
            self.fails("read_list", {"window": TITLE, "control": "tree", "columns": bad}, code="bad_args")
        self.fails("read_list", {"window": TITLE, "control": u"Master > combo"}, code="wrong_kind")

    def test_a_flat_list_is_read_without_walking_it(self):
        model = W.Model([W.Row(u"row %d" % n) for n in range(5000)])
        listing = W.Widget("QListWidget", model=model)
        self.tool.page1._f_add(listing)
        reply = self.ok("read_list", {"window": TITLE, "control": "list", "offset": 4998})
        self.assertEqual(([r["texts"][0] for r in reply["rows"]], reply["total"]), ([u"row 4998", u"row 4999"], 5000))
        self.assertLess(model._f_calls[0], 10)

    def test_click_and_tick_a_row(self):
        outcome = self.act("click", {"window": TITLE, "control": "tree", "row": 2})
        self.assertEqual(outcome["result"], {"row": 2, "texts": [u"T o", u"-30"], "double": False})
        self.assertEqual(self.tool.pairs._f_emitted, [("currentChanged", u"T o"), ("clicked", u"T o")])
        self.assertTrue(self.ok("read_list", {"window": TITLE, "control": "tree"})["rows"][2]["selected"])
        self.act("click", {"window": TITLE, "control": "tree", "row": 1, "double": True})
        self.assertEqual(self.tool.pairs._f_emitted[-2:], [("doubleClicked", u"A Vtilde"), ("activated", u"A Vtilde")])
        outcome = self.act("set_value", {"window": TITLE, "control": "tree", "row": 1, "value": True})
        self.assertEqual((outcome["result"]["after"], self.tool.rows[0].children[0].check), (2, 2))
        self.fails("set_value", {"window": TITLE, "control": "tree", "row": 3, "value": True}, code="wrong_kind")
        self.fails("click", {"window": TITLE, "control": "tree", "row": 9}, code="not_found")

    def test_a_row_changed_meanwhile_is_left_alone(self):
        reply = self.queued("set_value", {"window": TITLE, "control": "tree", "row": 2, "value": True})
        rows = self.tool.pairs._f_values["model"]._f_rows
        rows.insert(0, W.Row(u"new", u"0", check=0))  # (the list filled again before the timer)
        self.state.fire_timers()
        self.assertEqual(self.outcome(reply)["code"], "changed")
        self.assertEqual([row.check for row in rows] + [rows[1].children[0].check], [0, 2, 0, None, 0])


class SetValueTests(WindowCase):
    def test_each_kind(self):
        self.assertEqual(self.act("set_value", {"window": TITLE, "control": u"Only kerned pairs", "value": "on"})["result"],
                         {"before": False, "after": True})
        outcome = self.act("set_value", {"window": TITLE, "control": u"Master > combo", "value": u"bold"})
        self.assertEqual(outcome["result"], {"before": u"Regular", "after": u"Bold"})
        self.assertEqual(self.tool.master._f_emitted, [("currentIndexChanged", 1), ("activated", 1)])
        self.act("set_value", {"window": TITLE, "control": u"Master > combo", "value": 0})
        self.assertEqual(self.tool.master._f_values["currentIndex"], 0)
        self.assertEqual(self.act("set_value", {"window": TITLE, "control": u"Tolerance", "value": 12})["result"],
                         {"before": 5, "after": 12})
        self.assertEqual(self.tool.tolerance._f_emitted, [("valueChanged", 12), ("editingFinished",)])
        self.act("set_value", {"window": TITLE, "control": "tabs", "value": u"Classes"})
        self.assertEqual((self.tool.page2._f_shown, self.tool.page1._f_shown), (True, False))
        self.act("set_value", {"window": TITLE, "control": "classFilter", "value": u"round"})
        self.assertEqual(self.tool.filter._f_emitted, [("textChanged", u"round"), ("textEdited", u"round"), ("editingFinished",)])

    def test_refusals(self):
        for value in (60, 1.5, "x", True):
            self.fails("set_value", {"window": TITLE, "control": u"Tolerance", "value": value}, code="bad_args")
        self.assertIn(u"Regular, Bold", self.fails("set_value", {"window": TITLE, "control": u"Master", "value": u"Black"},
                                                   code="not_found")["message"])
        for value in (2, True):
            self.fails("set_value", {"window": TITLE, "control": u"Master", "value": value}, code="bad_args")
        self.fails("set_value", {"window": TITLE, "control": u"Only kerned pairs", "value": "maybe"}, code="bad_args")
        self.fails("set_value", {"window": TITLE, "control": u"Only kerned pairs"}, code="bad_args")
        self.fails("set_value", {"window": TITLE, "control": u"log", "value": u"x"}, code="wrong_kind")
        self.fails("set_value", {"window": TITLE, "control": "classFilter", "value": u"x"}, code="unavailable")
        self.tool.tabs._f_set(disabled_tabs=[1])
        self.fails("set_value", {"window": TITLE, "control": "tabs", "value": 1}, code="disabled")
        self.tool.tolerance._f_set(readOnly=True)
        self.fails("set_value", {"window": TITLE, "control": u"Tolerance", "value": 3}, code="unavailable")
        self.assertEqual(self.state.timers, [])

    def test_a_radio_button_and_a_three_state_box(self):
        one, two = W.Widget("QRadioButton", text=u"Left", checked=True), W.Widget("QRadioButton", text=u"Right")
        box = W.Widget("QCheckBox", text=u"Mixed", tristate=True, _state=1, checked=True)
        group = W.Widget("QGroupBox", [one, two, box], title=u"Side")
        self.tool.page1._f_add(group)
        self.assertIn(u"Side > Right > radio", self.controls())
        self.assertEqual(self.act("set_value", {"window": TITLE, "control": u"Side > Right > radio", "value": True})["result"],
                         {"before": False, "after": True})
        self.assertFalse(one._f_values["checked"])
        self.fails("set_value", {"window": TITLE, "control": u"Right", "value": False}, code="bad_args")
        self.assertEqual(self.act("set_value", {"window": TITLE, "control": u"Mixed", "value": False})["result"],
                         {"before": u"partial", "after": False})
        self.assertEqual(box._f_emitted, [("clicked",), ("clicked",)])


class TextAndLinkTests(WindowCase):
    def test_read_text(self):
        reply = self.ok("read_text", {"window": TITLE, "control": "log"})
        self.assertEqual(reply["text"], u"Applied 2 pairs: A V help")
        self.assertEqual(reply["links"], [{"href": u"glyph:A", "text": u"A"}, {"href": u"glyph:V", "text": u"V"},
                                          {"href": u"https://example.org/help", "text": u"help"}])
        part = self.ok("read_text", {"window": TITLE, "control": "log", "offset": 8, "max_chars": 7})
        self.assertEqual((part["text"], part["truncated"], part["length"]), (u"2 pairs", True, 25))
        label = self.ok("read_text", {"window": TITLE, "control": "label#1"})
        self.assertEqual((label["text"], label["links"]), (u"Free Sans Regular: 3 pairs", []))
        self.assertEqual(self.ok("read_text", {"window": TITLE, "control": u"Master"})["text"], u"Regular")
        self.fails("read_text", {"window": TITLE, "control": "tree"}, code="wrong_kind")

    def test_click_a_link(self):
        outcome = self.act("click_link", {"window": TITLE, "control": "log", "text": u"v"})
        self.assertEqual((outcome["result"], self.tool.log._f_emitted), ({"link": u"glyph:V"}, [("anchorClicked", u"glyph:V")]))
        self.act("click_link", {"window": TITLE, "control": "log", "href": u"glyph:A"})
        self.fails("click_link", {"window": TITLE, "control": "log", "text": u"help"}, code="unavailable")
        self.assertIn(u"glyph:A", self.fails("click_link", {"window": TITLE, "control": "log", "text": u"B"}, code="not_found")["message"])
        self.fails("click_link", {"window": TITLE, "control": "log"}, code="bad_args")
        self.tool.top._f_set(text=u'Kerned: <a href="glyph:A">A</a> and <a href="glyph:A.alt">A</a>')
        self.fails("click_link", {"window": TITLE, "control": "label#1", "text": u"A"}, code="ambiguous_control")
        self.act("click_link", {"window": TITLE, "control": "label#1", "href": u"glyph:A.alt"})
        self.assertEqual(self.tool.top._f_emitted, [("linkActivated", u"glyph:A.alt")])
        self.tool.top._f_set(openExternalLinks=True)
        self.fails("click_link", {"window": TITLE, "control": "label#1", "href": u"glyph:A"}, code="unavailable")


class DialogTests(WindowCase):
    def test_only_the_dialog_waiting_is_driven(self):
        box = self.box()
        error = self.fails("click", {"window": TITLE, "control": u"Apply > button#1"}, code="dialog_open")
        self.assertEqual(error["data"]["dialog"]["buttons"], [u"Replace", u"Cancel"])
        self.fails("run_menu_command", {"path": u"Scripts > FreeLab > Font Check"}, code="dialog_open")
        # A script's own question, unmarked, is taken for FontLab's: risky.
        self.assertEqual(self.fails("click", {"window": "dialog", "control": u"Replace"}, code="risky_mode_off")["data"]["action"],
                         "question_control")
        self.tool.window._f_props["mcpScriptWindow"] = True  # (its window marked: FontLab may parent its own there)
        self.fails("click", {"window": "dialog", "control": u"Replace"}, code="risky_mode_off")
        box._f_props["mcpScriptWindow"] = True  # (the question says it is the script's own)
        windows = self.ok("windows")["windows"]
        self.assertEqual([(w["waiting"], w["question"]) for w in windows if w["window"] == u"Replace?"], [(True, False)])
        self.assertEqual(self.act("click", {"window": "dialog", "control": u"Replace"})["state"], "done")
        self.assertEqual(self.replaced, [1])

    def test_a_fontlab_question_needs_risky_mode_and_must_be_the_same(self):
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK", u"Cancel"], text=u"Round the coordinates?")
        self.fails("click", {"window": "dialog", "control": "OK"}, code="risky_mode_off")
        self.assertTrue(all(c["risky"] for c in self.ok("window_controls", {"window": "dialog"})["controls"]))
        reply = self.queued("click", {"window": "dialog", "control": "OK", "risky": True})
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK", u"Cancel"], text=u"Round the other font?")
        self.state.fire_timers()
        self.assertEqual((self.outcome(reply)["code"], self.state.answers), ("changed", []))
        self.assertEqual(self.act("click", {"window": "dialog", "control": "OK", "risky": True})["state"], "done")
        self.assertEqual((self.state.answers, self.state.question), ([u"OK"], None))

    def test_the_welcome_window_closes_outside_risky_mode(self):
        """The review of 2026-11-20: closing FontLab's Welcome window loses nothing. Its other controls stay a
        question's (its recent fonts open a file without open_font's check), and a Close elsewhere is risky."""
        closed = []
        welcome = W.window("DlgInitialize", u"Welcome to FontLab 7!", [
            W.Widget("QPushButton", name=u"buttonNewFont", text=u"Create New Font"),
            W.Widget("QPushButton", name=u"buttonClose", text=u"Close", on={"clicked": lambda: closed.append(1)})],
            base="QDialog", modal=True)
        self.state.qt_windows.append(welcome)
        self.state.qt_modal = welcome
        risky = dict((c["address"], c.get("risky", False)) for c in self.ok("window_controls", {"window": "dialog"})["controls"])
        self.assertEqual(risky, {u"buttonNewFont": True, u"buttonClose": False})
        self.fails("click", {"window": "dialog", "control": u"buttonNewFont"}, code="risky_mode_off")
        self.assertEqual(self.act("click", {"window": "dialog", "control": u"buttonClose"})["state"], "done")
        self.assertEqual(closed, [1])
        other = W.window("QMessageBox", u"Close?", [W.Widget("QPushButton", name=u"buttonClose", text=u"Close")], modal=True)
        self.state.qt_windows.append(other)
        self.state.qt_modal = other
        self.fails("click", {"window": "dialog", "control": u"buttonClose"}, code="risky_mode_off")

    def test_a_running_action_lets_its_own_dialog_be_driven(self):
        """A click whose work asks a question: the click runs until the question is answered, and only that
        question can be driven meanwhile (the listener answers inside the dialog's loop, as live)."""
        seen = {}

        def apply_asks():
            box = self.box(own=True)
            seen["main"] = self.fails("click", {"window": "main", "control": "x"}, code="dialog_open")
            seen["windows"] = [w["window"] for w in self.ok("windows")["windows"] if w.get("action_running")]
            seen["inner"] = self.queued("click", {"window": "dialog", "control": u"Replace"})
            self.state.fire_timers()  # (the dialog's loop runs the timers)
            self.state.qt_modal = None
            self.state.qt_windows.remove(box)
        self.tool.window._f_props["mcpScriptWindow"] = True
        self.tool.apply._f_on["clicked"] = apply_asks
        reply = self.queued("click", {"window": TITLE, "control": u"Apply > button#1"})
        self.fails("click", {"window": TITLE, "control": u"Close"}, code="busy")  # (the first is still queued)
        self.state.fire_timers()
        self.assertEqual((self.outcome(reply)["state"], self.outcome(seen["inner"])["state"], self.replaced), ("done", "done", [1]))
        self.assertEqual(seen["windows"], [TITLE])

    def test_a_busy_window_holds_every_action(self):
        self.tool.busy_row._f_shown = True
        error = self.fails("click", {"window": "main", "control": "x"}, code="busy")
        self.assertEqual(error["data"]["window"], TITLE)
        self.fails("set_value", {"window": TITLE, "control": u"Tolerance", "value": 3}, code="busy")
        self.tool.busy_row._f_shown = False
        self.assertEqual(self.act("set_value", {"window": TITLE, "control": u"Tolerance", "value": 3})["state"], "done")


class MenuTests(WindowCase):
    def setUp(self):
        WindowCase.setUp(self)
        self.font_check = W.Action(u"Font Check")
        self.features = W.Action(u"&Features", name=u"actionFeaturesPanel", checkable=True)
        self.save = W.Action(u"&Save Font...", name=u"actionSaveFont")
        self.close_all = W.Action(u"Close All")
        self.state.menu_bar = W.Widget("QMenuBar", actions=[
            W.Action(u"&File", menu=W.menu(W.Action(u"Open Fonts…"), self.save)),
            W.Action(u"&Window", menu=W.menu(W.Action(u"Panels", menu=W.menu(self.features)), self.close_all,
                                             W.Action(u"Tile", enabled=False))),
            W.Action(u"Scripts", menu=W.menu(W.Action(u"FreeLab", menu=W.menu(self.font_check)), W.Action(u"Empty", menu=W.menu())))])

    def test_list_menu(self):
        self.assertEqual([e["text"] for e in self.ok("menu")["entries"]], [u"File", u"Window", u"Scripts"])
        self.assertEqual(self.ok("menu", {"path": u"scripts > freelab"}),
                         {"path": u"Scripts > FreeLab", "entries": [{"text": u"Font Check", "kind": "command", "enabled": True}]})
        self.assertTrue(all(e["risky"] for e in self.ok("menu", {"path": u"File"})["entries"]))
        window = self.ok("menu", {"path": u"Window"})["entries"]
        self.assertEqual([(e["text"], e["kind"], e["enabled"], e.get("risky")) for e in window],
                         [(u"Panels", "menu", True, None), (u"Close All", "command", True, True), (u"Tile", "command", False, None)])
        self.assertEqual(self.ok("menu", {"path": u"Window > Panels"})["entries"][0]["checked"], False)
        self.assertIn("note", self.ok("menu", {"path": u"Scripts > Empty"}))
        self.fails("menu", {"path": u"Scripts > FreeLab > Font Check"}, code="wrong_kind")
        self.assertIn(u"File, Window, Scripts", self.fails("menu", {"path": u"Help"}, code="not_found")["message"])

    def test_run_a_command(self):
        outcome = self.act("run_menu_command", {"path": u"Scripts > FreeLab > Font Check"})
        self.assertEqual((outcome["state"], outcome["result"], self.font_check._f_triggered), ("done", {"command": u"Scripts > FreeLab > Font Check"}, 1))
        self.assertEqual(self.act("run_menu_command", {"path": u"Window > Panels > Features"})["result"],
                         {"command": u"Window > Panels > Features", "checked": True})
        for path, code in ((u"File > Open Fonts", "risky_mode_off"), (u"Window > Close All", "risky_mode_off"),
                           (u"Window > Tile", "disabled"), (u"Window > Panels", "wrong_kind"), (u"Window >", "bad_args"),
                           (u"Window > Nope", "not_found"), (u"", "bad_args")):
            self.fails("run_menu_command", {"path": path}, code=code)
        self.assertEqual(self.act("run_menu_command", {"path": u"File > Save Font", "risky": True})["state"], "done")
        self.assertEqual((self.save._f_triggered, self.close_all._f_triggered), (1, 0))


class StateTests(WindowCase):
    def test_window_state(self):
        state = self.ok("window_state", {"window": TITLE})
        self.assertEqual((state["dialog"], state["busy_windows"], state["actions_pending"], state["window"]["busy"]),
                         (None, [], [], False))
        reply = self.queued("click", {"window": TITLE, "control": u"Apply > button#1"})
        state = self.ok("window_state", {"window": TITLE})
        self.assertEqual((state["window"]["busy"], state["window"]["busy_by"], state["actions_pending"][0]["id"]),
                         (True, "action", reply["action_id"]))
        self.state.fire_timers()
        state = self.ok("window_state")
        self.assertEqual((state["last_action"]["state"], state["actions_pending"]), ("done", []))
        self.box()
        state = self.ok("window_state", {"window": TITLE})
        self.assertEqual((state["dialog"]["title"], state["dialog_window"], state["question"]), (u"Replace?", u"Replace?", True))
        self.state.qt_windows = []
        self.state.qt_modal = None
        self.assertIsNone(self.ok("window_state", {"window": TITLE})["window"])
        self.fails("action_result", {"action_id": 999}, code="not_found")

    def test_a_lost_action_stops_holding_the_others(self):
        reply = self.queued("click", {"window": TITLE, "control": u"Apply > button#1"})
        self.listener.actions[reply["action_id"]]["asked"] -= fl.ACTION_LOST_S + 1
        self.state.timers = []  # (its timer lost)
        self.assertEqual(self.outcome(reply)["code"], "lost")
        self.assertEqual(self.act("click", {"window": TITLE, "control": u"Apply > button#1"})["state"], "done")


class ReviewRoundTwoTests(WindowCase):
    """Codex's review of 1.3.0 (2026-11-19): each finding's case, which 1.3.0 failed."""

    def test_1_a_mnemonic_never_hides_a_risky_name(self):
        for text in (u"Sa&ve", u"E&xport", u"C&lose", u"&Discard changes"):
            button = W.Widget("QPushButton", name=u"operation1", text=text, on={"clicked": lambda: self.tool.applied.append(1)})
            self.tool.page1._f_add(button)
            self.assertEqual(self.fails("click", {"window": TITLE, "control": "operation1"}, code="risky_mode_off")["data"]["action"],
                             "risky_command", text)
            self.tool.page1._f_children.remove(button)
        tip = W.Widget("QPushButton", name=u"operation2", text=u"Go", toolTip=u"<b>Save</b>&nbsp;the font")
        self.tool.page1._f_add(tip)
        self.fails("click", {"window": TITLE, "control": "operation2"}, code="risky_mode_off")
        self.assertEqual((self.tool.applied, self.state.timers), ([], []))

    def test_2_a_command_is_judged_by_its_action_and_the_menus_it_is_in(self):
        action = W.Action(u"Perform", props={"mcpRisky": True})
        button = W.Widget("QToolButton", name=u"perform", text=u"Perform")
        button._f_default_action = action
        self.tool.page1._f_add(button)
        self.fails("click", {"window": TITLE, "control": "perform"}, code="risky_mode_off")
        self.tool.window._f_props["mcpScriptWindow"] = True  # (a mark counts in a script's window too)
        self.fails("click", {"window": TITLE, "control": "perform"}, code="risky_mode_off")
        del self.tool.window._f_props["mcpScriptWindow"]
        named = W.Widget("QToolButton", name=u"other", text=u"Other")
        named._f_default_action = W.Action(u"Other", name=u"actionExportFont")
        self.tool.page1._f_add(named)
        self.fails("click", {"window": TITLE, "control": "other"}, code="risky_mode_off")
        # A menu entry: its tooltip, and the menus it is in, on a button's menu and in the main window's
        self.state.menu_bar = W.Widget("QMenuBar", actions=[W.Action(u"Tools", menu=W.menu(
            W.Action(u"Apply", toolTip=u"Save font to disk"), W.Action(u"Export", menu=W.menu(W.Action(u"OpenType"))),
            W.Action(u"Kern")))])
        self.fails("run_menu_command", {"path": "Tools > Apply"}, code="risky_mode_off")
        self.fails("run_menu_command", {"path": "Tools > Export > OpenType"}, code="risky_mode_off")
        entries = dict((e["text"], e.get("risky")) for e in self.ok("menu", {"path": u"Tools > Export"})["entries"])
        self.assertEqual(entries, {u"OpenType": True})
        self.assertEqual(self.act("run_menu_command", {"path": "Tools > Kern"})["state"], "done")
        self.tool.tools._f_menu = W.menu(W.Action(u"Export", menu=W.menu(W.Action(u"OpenType"))))
        self.fails("click", {"window": TITLE, "control": u"FreeLab tools", "entry": "Export > OpenType"}, code="risky_mode_off")

    def test_3_rows_that_look_alike_never_stand_in_for_each_other(self):
        first, second = W.Row(u"same", check=0), W.Row(u"same", check=0)
        view = W.view("QListWidget", [first, second], name=u"sameRows")
        self.tool.page1._f_add(view)
        reply = self.queued("set_value", {"window": TITLE, "control": "sameRows", "row": 0, "value": True})
        view._f_values["model"]._f_rows[:] = [second, first]  # (swapped before the timer)
        self.state.fire_timers()
        self.assertEqual((self.outcome(reply)["code"], first.check, second.check), ("changed", 0, 0))
        reply = self.queued("click", {"window": TITLE, "control": "sameRows", "row": 1, "double": True})
        view._f_set(model=W.Model([W.Row(u"same", check=0), W.Row(u"same", check=0)]))  # (another model, alike)
        self.state.fire_timers()
        self.assertEqual((self.outcome(reply)["code"], view._f_emitted), ("changed", []))
        self.assertEqual(self.act("set_value", {"window": TITLE, "control": "sameRows", "row": 1, "value": True})["state"], "done")

    def test_4_an_action_reported_lost_never_runs_later(self):
        reply = self.queued("click", {"window": TITLE, "control": u"Apply > button#1"})
        self.listener.actions[reply["action_id"]]["asked"] -= fl.ACTION_LOST_S + 1
        self.assertEqual(self.outcome(reply)["code"], "lost")
        self.state.fire_timers()  # (its timer kept: late is not canceled)
        self.assertEqual((self.tool.applied, self.outcome(reply)["state"]), ([], "skipped"))

    def test_5_a_window_of_a_shared_name_keeps_its_key(self):
        clicked = []
        a = W.window("QDialog", u"Same title", [W.Widget("QPushButton", text=u"Apply", on={"clicked": lambda: clicked.append("a")})])
        b = W.window("QDialog", u"Same title", [W.Widget("QPushButton", text=u"Apply", on={"clicked": lambda: clicked.append("b")})])
        self.state.qt_windows = [a, b]
        keys = [w["window"] for w in self.ok("windows")["windows"]]
        self.assertEqual(keys, [u"main", u"Same title#1", u"Same title#2"])
        self.state.qt_windows[:] = [b, a]  # (Qt's order changed)
        self.assertEqual(self.act("click", {"window": u"Same title#1", "control": "Apply"})["state"], "done")
        self.assertEqual(clicked, ["a"])
        self.state.qt_windows[:] = [b]  # (a closed: its key names no other window)
        self.assertEqual([w["window"] for w in self.ok("windows")["windows"]], [u"main", u"Same title"])
        self.state.qt_windows.append(W.window("QDialog", u"Same title"))
        self.assertEqual([w["window"] for w in self.ok("windows")["windows"]], [u"main", u"Same title#2", u"Same title#3"])
        self.fails("window_controls", {"window": u"Same title#1"}, code="not_found")
        impostor = W.window("QDialog", u"main", [W.Widget("QPushButton", text=u"Apply")])
        self.state.qt_windows = [impostor]
        self.assertEqual([w["window"] for w in self.ok("windows")["windows"]], [u"main", u"main#4"])
        self.assertEqual(self.ok("window_controls", {"window": "main"})["class"], "MainWindow")

    def test_6_an_address_is_never_chosen_by_what_is_shown(self):
        hidden = W.Widget("QPushButton", name=u"Apply > button#1", text=u"Another operation")
        hidden._f_shown = False
        self.tool.page1._f_add(hidden)
        self.fails("click", {"window": TITLE, "control": u"Apply > button#1"}, code="ambiguous_control")
        self.assertEqual(self.tool.applied, [])

    def test_7_a_button_that_means_another_command_by_then_is_not_clicked(self):
        effects = []
        button = W.Widget("QToolButton", name=u"invoke", text=u"Perform")
        old, new = W.Action(u"Perform"), W.Action(u"Perform")
        button._f_default_action = old
        button._f_on["clicked"] = lambda: effects.append(button._f_default_action is new)
        self.tool.page1._f_add(button)
        reply = self.queued("click", {"window": TITLE, "control": "invoke"})
        button._f_default_action = new
        self.state.fire_timers()
        self.assertEqual((self.outcome(reply)["code"], effects), ("changed", []))
        start = W.Widget("QPushButton", name=u"runStop", text=u"Start", on={"clicked": lambda: effects.append(1)})
        self.tool.page1._f_add(start)
        reply = self.queued("click", {"window": TITLE, "control": "runStop"})
        start._f_set(text=u"Stop")
        self.state.fire_timers()
        self.assertEqual((self.outcome(reply)["code"], effects), ("changed", []))

    def test_8_a_disabled_row_is_not_driven_and_a_refused_tick_fails(self):
        model = self.tool.pairs._f_values["model"]
        model._f_flags = 1 | 16  # (selectable, checkable, not enabled)
        self.fails("click", {"window": TITLE, "control": "tree", "row": 0}, code="disabled")
        self.fails("set_value", {"window": TITLE, "control": "tree", "row": 0, "value": False}, code="disabled")
        model._f_flags = None
        model._f_refuse = True
        outcome = self.act("set_value", {"window": TITLE, "control": "tree", "row": 2, "value": True})
        self.assertEqual(outcome["state"], "failed")
        self.assertIn("refused", outcome["error"])
        self.assertEqual(self.tool.pairs._f_emitted, [])

    def test_9_a_slots_system_exit_ends_the_action(self):
        def stop():
            raise SystemExit("test interruption")
        self.tool.apply._f_on["clicked"] = stop
        reply = self.queued("click", {"window": TITLE, "control": u"Apply > button#1"})
        self.state.fire_timers()
        self.assertEqual(len(self.listener.errors), 1)  # (the timer's own guard recorded it, as before)
        self.listener.errors[:] = []
        self.assertEqual(self.outcome(reply)["state"], "failed")
        self.assertEqual(self.ok("window_state")["actions_pending"], [])

    def test_12_rows_are_counted_lazily_under_python_2(self):
        with open(fl.__file__.replace(".pyc", ".py"), "rb") as handle:
            source = handle.read().decode("utf-8")
        self.assertIn(u"for row in _lazy_range(int(model.rowCount(parent))):", source)
        self.assertIs(fl._lazy_range, xrange if sys.version_info[0] == 2 else range)  # noqa: F821

    def test_13_a_listing_reads_each_layout_once(self):
        reads = []
        for pairs in (50, 200):
            widgets = []
            for n in range(pairs):
                widgets += [W.Widget("QLabel", text=u"Field %d:" % n), W.Widget("QLineEdit", text=u"")]
            page = W.Widget("QWidget", widgets)
            layout = W.Layout(*widgets)
            page._f_layout = layout
            window = W.window("QDialog", u"Many fields %d" % pairs, [page])
            self.state.qt_windows = [window]
            controls = self.ok("window_controls", {"window": u"Many fields %d" % pairs, "kinds": ["field"], "limit": 5000})
            self.assertEqual(controls["controls"][7]["address"], u"Field 7 > field")
            reads.append(layout._f_reads[0])
        self.assertLessEqual(reads[1], 4 * reads[0] + 10, "linear: %r" % reads)


class LiveRunTests(WindowCase):
    """Found driving FreeLab Script Suite live (2026-11-19)."""

    def test_a_rich_labels_lines_stay_lines(self):
        self.tool.top._f_set(text=u"<b>1,002 languages</b> covered.<br>3 languages count marks.<p>No script</p>is whole.")
        self.assertEqual(self.ok("read_text", {"window": TITLE, "control": "label#1"})["text"],
                         u"1,002 languages covered.\n3 languages count marks.\nNo script\nis whole.")

    def test_a_caption_that_is_a_sentence_is_found_without_its_period(self):
        box = W.Widget("QCheckBox", name=u"lacking", text=u"Leave out the glyphs the font has drawn.")
        self.tool.page1._f_add(box)
        self.assertEqual(self.act("set_value", {"window": TITLE, "control": u"Leave out the glyphs the font has drawn",
                                                "value": True})["result"], {"before": False, "after": True})

    def test_the_controls_in_a_lists_cells(self):
        combos = [W.Widget("QComboBox", items=[u"Standard", u"Discretionary", u"Off"], currentIndex=0) for _ in range(2)]
        viewport = W.Widget("QWidget", combos, name=u"qt_scrollarea_viewport")
        tree = W.view("QTreeWidget", [W.Row(u"fi"), W.Row(u"fl")], name=u"ligature_tree")
        tree._f_add(viewport)
        self.tool.page1._f_add(tree)
        controls = self.controls()
        self.assertIn(u"ligature_tree > combo#2", controls)
        self.assertEqual(self.act("set_value", {"window": TITLE, "control": u"ligature_tree > combo#2", "value": u"Off"})["result"],
                         {"before": u"Standard", "after": u"Off"})
        self.assertEqual(combos[1]._f_emitted, [("currentIndexChanged", 2), ("activated", 2)])


if __name__ == "__main__":
    unittest.main()
