"""MCP server tests: the real server and client against the real listener on spy fakes (Python 3)."""

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "listener"))
sys.path.insert(0, str(ROOT / "server"))
os.environ["FREELAB_NO_AUTOSTART"] = "1"

import fakes  # noqa: E402
import freelab_listener as fl  # noqa: E402
from freelab_mcp import client, server  # noqa: E402

EXPECTED_TOOLS = {"fontlab_status", "list_fonts", "get_current_font", "list_glyphs", "get_glyph", "get_glyph_outline",
                  "get_kerning", "get_features", "set_kerning_pair", "remove_kerning_pair", "set_glyph_metrics",
                  "rename_glyph", "set_glyph_unicodes", "add_component_glyph", "set_anchor", "remove_anchor",
                  "set_feature_code", "undo", "run_python", "launch_fontlab", "quit_fontlab", "restart_fontlab",
                  "open_font", "close_font", "bring_font_to_front", "compile_features", "read_output", "screenshot",
                  "set_risky_mode", "list_risky_actions", "answer_dialog", "save_font", "export_font", "delete_glyph",
                  "set_glyph_outline", "list_windows", "list_window_controls", "click", "set_value", "read_list",
                  "read_text", "click_link", "list_menu", "run_menu_command", "wait_idle"}
WRITE_TOOLS = {"set_kerning_pair", "remove_kerning_pair", "set_glyph_metrics", "rename_glyph", "set_glyph_unicodes",
               "add_component_glyph", "set_anchor", "remove_anchor", "set_feature_code", "undo",
               "close_font", "bring_font_to_front", "compile_features", "delete_glyph", "set_glyph_outline",
               "save_font", "export_font"}  # (the tools that name their font)
# Where each enforced risky action shows in the tools: (tool, argument), the argument None for a whole tool.
RISKY_PLACES = {"close_font.discard_changes": ("close_font", "discard_changes"), "answer_dialog": ("answer_dialog", None),
                "open_font.answer": ("open_font", "answer"), "quit_fontlab.discard_changes": ("quit_fontlab", "discard_changes"),
                "rename_glyph.update_references": ("rename_glyph", "update_references"),
                "delete_glyph.remove_references": ("delete_glyph", "remove_references"),
                "save_font": ("save_font", None), "export_font": ("export_font", None), "run_python": ("run_python", None),
                # (risky only for some controls or commands, which the listener tells apart: said in the description)
                "question_control": ("click", "*"), "risky_command": ("run_menu_command", "*")}


def entry_tools(action):
    """The tools RISKY_ACTIONS names for an action."""
    return [name.strip() for key, tools, _reason, _enforced in fl.RISKY_ACTIONS if key == action for name in tools.split(",")]


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="freelab-server-")
        self._old_state = os.environ.get("FREELAB_STATE_DIR")
        os.environ["FREELAB_STATE_DIR"] = self.tmp
        self.state = fakes.FontLabState()
        fakes.install(self.state)
        self.listener = None
        self.stop_event = threading.Event()
        self.thread = None

    def start_listener(self):
        self.listener = fl.Listener(port=0, state_file=str(client.state_path()))
        self.listener.bind()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            self.listener.tick()
            self.state.fire_timers()  # (FontLab's QTimer.singleShot callbacks, on the listener's thread)
            time.sleep(0.005)

    def tearDown(self):
        server.risky.Mode.enabled = False
        self.stop_event.set()
        if self.thread:
            self.thread.join(5)
        errors = list(self.listener.errors) if self.listener else []
        if self.listener:
            self.listener.stop()
        fakes.uninstall()
        if self._old_state is None:
            os.environ.pop("FREELAB_STATE_DIR", None)
        else:
            os.environ["FREELAB_STATE_DIR"] = self._old_state
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(errors, [], "the listener swallowed errors: %r" % errors)

    def open_sample(self):
        font = fakes.sample_font()
        self.state.fonts = [font]
        self.state.current = font


class NotRunningTests(ServerCase):
    def test_no_state_file(self):
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(server.fontlab_status())
        self.assertIn("[not_running]", str(ctx.exception))
        self.assertIn("Start MCP Listener", str(ctx.exception))

    def test_stale_state_file(self):
        self.start_listener()
        port = self.listener.port
        self.stop_event.set()
        self.thread.join(5)
        state = json.loads(client.state_path().read_text())
        self.listener.stop()
        client.state_path().write_text(json.dumps(state))  # FontLab died without cleaning up
        self.listener = None
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(server.fontlab_status())
        self.assertIn("[not_running]", str(ctx.exception), "port %d" % port)

    def test_protocol_mismatch(self):
        client.state_path().write_text(json.dumps({"port": 1, "token": "x", "protocol": 99}))
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(server.get_current_font())
        self.assertIn("[bad_protocol]", str(ctx.exception))

    def test_wrong_token(self):
        self.start_listener()
        state = json.loads(client.state_path().read_text())
        state["token"] = "f" * 32
        client.state_path().write_text(json.dumps(state))
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(server.fontlab_status())
        self.assertIn("[bad_token]", str(ctx.exception))


class ToolTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.start_listener()

    def test_tool_list(self):
        tools = asyncio.run(server.mcp.list_tools())
        self.assertEqual({t.name for t in tools}, EXPECTED_TOOLS)
        for tool in tools:
            self.assertTrue(tool.description and len(tool.description) > 20, tool.name)

    def test_status_without_font(self):
        status = asyncio.run(server.fontlab_status())
        self.assertEqual(status["fonts_open"], 0)
        self.assertEqual(status["listener"]["listener"], fl.VERSION)
        self.assertEqual(status["server"], "1.3.0")
        self.assertEqual(fl.VERSION, "1.3.0")

    def test_font_and_glyph_tools(self):
        self.open_sample()
        self.assertEqual(asyncio.run(server.get_current_font())["family"], "Free Sans")
        self.assertEqual(asyncio.run(server.list_glyphs(limit=2))["names"], ["A", "o"])
        self.assertEqual(asyncio.run(server.get_glyph(unicode=65))["name"], "A")
        outline = asyncio.run(server.get_glyph_outline(name="o"))
        self.assertEqual(outline["node_count"], 5)

    def test_glyph_tools_need_exactly_one_selector(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(server.get_glyph())
        with self.assertRaises(RuntimeError):
            asyncio.run(server.get_glyph(name="A", unicode=65))

    def test_listener_errors_are_tool_errors(self):
        self.open_sample()
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(server.get_glyph(name="nope"))
        self.assertIn("[no_glyph]", str(ctx.exception))

    def test_call_tool_through_mcp(self):
        self.open_sample()
        result = asyncio.run(server.mcp.call_tool("get_glyph", {"name": "o"}))
        text = json.dumps([getattr(block, "text", "") for block in (result[0] if isinstance(result, tuple) else result)])
        self.assertIn('\\"name\\": \\"o\\"', text)

    def test_run_python(self):
        result = asyncio.run(server.run_python("print(6 * 7)\nresult = [1, 2]"))
        self.assertEqual(result["output"].strip(), "42")
        self.assertEqual(result["result"], [1, 2])

    def test_run_python_timeout_bounds(self):
        for bad in (0, -1, 601):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(server.run_python("result = 1", timeout_s=bad))
            self.assertIn("[bad_args]", str(ctx.exception))

    def test_requests_carry_a_deadline_the_listener_honors(self):
        sys._freelab_ran = 0
        try:
            self.stop_event.set()  # FontLab is busy: nobody polls
            self.thread.join(5)
            with self.assertRaises(client.ListenerError) as ctx:
                client.call("run_python", {"code": "import sys; sys._freelab_ran += 1"}, timeout=0.3)
            self.assertEqual(ctx.exception.code, "timeout")
            for _ in range(20):  # FontLab comes back and drains the queued request
                self.listener.tick()
                time.sleep(0.01)
            self.assertEqual(sys._freelab_ran, 0, "an abandoned request ran")
        finally:
            del sys._freelab_ran

    def test_bad_state_files(self):
        self.stop_event.set()
        self.thread.join(5)
        for bad in ([1, 2], {"port": True, "token": "x", "protocol": 1}, {"port": 0, "token": "x", "protocol": 1}):
            client.state_path().write_text(json.dumps(bad))
            with self.assertRaises(client.ListenerError) as ctx:
                client.call("ping")
            self.assertEqual(ctx.exception.code, "bad_state")

    def test_timeout(self):
        self.stop_event.set()  # the listener stops polling: FontLab is "busy"
        self.thread.join(5)
        start = time.time()
        with self.assertRaises(client.ListenerError) as ctx:
            client.call("ping", timeout=0.5)
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertLess(time.time() - start, 3)


class WriteToolTests(ServerCase):
    """The write tools through the real server, client, and listener, on the fakes."""

    def setUp(self):
        super().setUp()
        self.start_listener()
        self.font = fakes.write_font()
        self.state.fonts = [fakes.sample_font(), self.font]
        self.state.current = self.state.fonts[0]
        self.qt = fakes.install_qt(self.state)

    def tearDown(self):
        fakes.uninstall_qt(self.qt)
        super().tearDown()

    def test_every_write_tool_requires_its_font(self):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        for name in WRITE_TOOLS:
            self.assertIn("font", tools[name].inputSchema.get("required", []), name)
        for name in EXPECTED_TOOLS - WRITE_TOOLS:
            self.assertNotIn("font", tools[name].inputSchema.get("required", []), name)

    def test_writes_wait_longer_than_reads(self):
        seen = []
        saved = server.call

        def spy(op, args=None, timeout=None):
            seen.append((op, timeout))
            return saved(op, args, timeout=timeout)
        server.call = spy
        try:
            asyncio.run(server.set_kerning_pair(7, "B", "A", -12))
            asyncio.run(server.get_kerning(7, left="B", right="A"))
        finally:
            server.call = saved
        self.assertEqual(seen, [("set_kerning", server.WRITE_TIMEOUT_S), ("kerning", client.DEFAULT_TIMEOUT_S)])

    def test_a_write_and_its_undo(self):
        reply = asyncio.run(server.set_kerning_pair("WriteSans-Regular", "B", "A", -12))
        self.assertEqual((reply["font"]["id"], reply["value"], reply["history"]["recorded"]), (7, -12.0, True))
        self.assertEqual(asyncio.run(server.get_kerning(7, left="B", right="A"))["value"], -12.0)
        undone = asyncio.run(server.undo(7, expected_state=reply["history"]["history_state"]))
        self.assertTrue(undone["undone"])
        self.assertEqual((undone["brought_forward"], undone["front_restored"]["font"]["id"]), (True, 1))
        self.assertIs(self.state.current, self.state.fonts[0], "the font that was in front is back")

    def test_left_out_arguments_are_not_sent(self):
        reply = asyncio.run(server.set_glyph_metrics(7, glyph="A", advance_width=550))
        self.assertEqual(reply["layer"], "Regular")
        with self.assertRaisesRegex(RuntimeError, r"\[bad_args\] Give exactly one of glyph or unicode"):
            asyncio.run(server.set_glyph_metrics(7, advance_width=550))

    def test_the_other_writes_through_the_server(self):
        self.assertTrue(asyncio.run(server.set_anchor(7, "B", "top", 250, 700))["changed"])
        self.assertTrue(asyncio.run(server.remove_anchor(7, "B", "top"))["changed"])
        self.assertTrue(asyncio.run(server.set_glyph_unicodes(7, [0xE000], glyph="acute"))["changed"])
        self.assertTrue(asyncio.run(server.rename_glyph(7, "acute", "acutecomb"))["changed"])
        added = asyncio.run(server.add_component_glyph(7, "Bdot", [{"glyph": "B"}, {"glyph": "acutecomb", "x": 100}]))
        self.assertEqual(added["skipped_masters"], ["Bold"])
        self.assertTrue(asyncio.run(server.set_feature_code(7, "ss05", "feature ss05 { sub A by B; } ss05;"))["created"])
        self.assertTrue(asyncio.run(server.remove_kerning_pair(7, "A", "B", master="Bold"))["changed"])
        self.assertEqual(asyncio.run(server.get_features(7, tag="ss05"))["tag"], "ss05")
        self.assertEqual(len(asyncio.run(server.list_fonts())["fonts"]), 2)

    def call_tool(self, name, args):
        result = asyncio.run(server.mcp.call_tool(name, args))
        blocks = result[0] if isinstance(result, tuple) else result
        return " ".join(getattr(block, "text", "") for block in blocks)

    def test_a_glyph_named_like_json_reaches_the_listener_as_text(self):
        """Codex, 2026-11-16: FastMCP parsed "null" given for a str | None argument as JSON, so a glyph
        named null was looked up as no glyph at all."""
        glyphs = self.font._f_glyphs
        glyphs.append(fakes.flGlyph("null", [fakes.flLayer("Regular")]))
        glyphs.append(fakes.flGlyph("true", [fakes.flLayer("Regular")]))
        self.assertIn('"name": "null"', self.call_tool("get_glyph", {"font": 7, "name": "null"}))
        self.assertIn('"name": "true"', self.call_tool("get_glyph_outline", {"font": 7, "name": "true"}))
        self.assertIn('"glyph": "null"', self.call_tool("set_glyph_metrics", {"font": 7, "glyph": "null", "advance_width": 10}))

    def test_numbers_are_strict(self):
        """Codex, 2026-11-16: true given as advance_width became a width of 1."""
        for name, args in (("set_glyph_metrics", {"font": 7, "glyph": "A", "advance_width": True}),
                           ("set_kerning_pair", {"font": 7, "left": "B", "right": "A", "value": True}),
                           ("set_anchor", {"font": 7, "glyph": "A", "name": "top", "x": "5", "y": 1}),
                           ("set_glyph_unicodes", {"font": 7, "glyph": "acute", "unicodes": [True]})):
            with self.assertRaises(Exception, msg=name):
                self.call_tool(name, args)
        self.assertEqual(self.state.steps, [], "nothing written")
        self.assertIn('"advance_width": 520.0', self.call_tool("set_glyph_metrics", {"font": 7, "glyph": "A", "advance_width": 520}))

    def test_a_font_id_is_strict_and_a_name_confirms_it(self):
        """Review of 2026-11-16: font true became the id 1; a font id given alone may name a font opened since."""
        with self.assertRaises(Exception):
            self.call_tool("set_kerning_pair", {"font": True, "left": "B", "right": "A", "value": -5})
        with self.assertRaisesRegex(Exception, r"font_mismatch"):
            self.call_tool("set_kerning_pair", {"font": 7, "font_name": "Free Sans Regular", "left": "B", "right": "A",
                                                "value": -5})
        self.assertEqual(self.state.steps, [])
        self.assertIn('"changed": true', self.call_tool("set_kerning_pair", {"font": 7, "font_name": "WriteSans-Regular",
                                                                             "left": "B", "right": "A", "value": -5}))

    def test_undo_takes_the_writes_title(self):
        reply = asyncio.run(server.set_kerning_pair(7, "B", "A", -12))
        self.state.current = self.font
        with self.assertRaisesRegex(RuntimeError, r"\[history_moved\]"):
            asyncio.run(server.undo(7, expected_state=reply["history"]["history_state"], expected_title="FreeLab MCP: other"))
        undone = asyncio.run(server.undo(7, expected_state=reply["history"]["history_state"],
                                         expected_title=reply["history"]["title"]))
        self.assertEqual(undone["undone"][0].lower(), reply["history"]["title"].lower())

    def test_a_write_that_could_not_be_put_back_carries_its_step(self):
        self.font.kerning("Regular")._f_skew = 3.0
        with self.assertRaisesRegex(RuntimeError, r'\[not_kept\].*"recorded": true'):
            asyncio.run(server.set_kerning_pair(7, "A", "B", -9))

    def test_tool_errors_carry_the_listeners_code(self):
        with self.assertRaisesRegex(RuntimeError, r"\[class_name_clash\]"):
            asyncio.run(server.set_kerning_pair(7, "V", "A", -5))
        with self.assertRaisesRegex(RuntimeError, r"\[no_such_font\]"):
            asyncio.run(server.set_kerning_pair("Nope", "B", "A", -5))


class WindowCase(ServerCase):
    """Two fonts and FontLab's windows, faked; FontLab's timers fire on the listener's thread."""

    def setUp(self):
        super().setUp()
        self.font = fakes.write_font()
        self.state.fonts = [fakes.sample_font(), self.font]
        self.state.current = self.state.fonts[0]
        self.qt = fakes.install_qt(self.state)
        self._poll = server.POLL_S
        server.POLL_S = 0.01
        self.start_listener()
        self.path = os.path.join(self.tmp, "Open.ttf")
        Path(self.path).write_bytes(b"font")

    def tearDown(self):
        server.POLL_S = self._poll
        super().tearDown()
        fakes.uninstall_qt(self.qt)

    def ids(self):
        return [entry["id"] for entry in asyncio.run(server.list_fonts())["fonts"]]


class WindowToolTests(WindowCase):
    """Opening, closing, bringing to the front, compiling, the Output panel, and pictures, through the real
    server, client, and listener, on the fakes."""

    def test_open_then_close_a_font(self):
        opened = asyncio.run(server.open_font(self.path))
        self.assertEqual((opened["opened"], opened["font"]["id"], opened["font"]["path"]), (True, 101, self.path))
        again = asyncio.run(server.open_font(self.path))
        self.assertEqual((again["opened"], again["already_open"], again["font"]["id"]), (False, True, 101))
        self.assertEqual(asyncio.run(server.close_font(101)), {"closed": True, "font": {"id": 101, "name": "Free Sans Regular"},
                                                               "changes_discarded": False})
        self.assertEqual(self.ids(), [1, 7])

    def test_a_font_fontlab_crashes_on_is_refused_or_opened_as_a_fixed_copy(self):
        import fontbytes
        gpos = fontbytes.gpos([fontbytes.lookup(1, [fontbytes.single(2, 0, 0)])])
        path = os.path.join(self.tmp, "Crash.ttf")
        Path(path).write_bytes(fontbytes.sfnt({b"head": fontbytes.head(), b"GPOS": gpos}))
        with self.assertRaisesRegex(RuntimeError, r"\[known_crasher\] FontLab 7\.2 crashes on opening Crash\.ttf .*1 single "
                                                  r"adjustment subtable with no values \(lookup 0\).*safe_copy=True"):
            asyncio.run(server.open_font(path))
        self.assertEqual(self.ids(), [1, 7])
        opened = asyncio.run(server.open_font(path, safe_copy=True))
        copy = opened["safe_copy"]
        self.assertEqual((opened["opened"], opened["font"]["id"], opened["font"]["path"]), (True, 101, copy["path"]))
        self.assertEqual((copy["original"], copy["changed"]), (path, 1))
        self.assertTrue(copy["path"].startswith(os.path.join(self.tmp, "safe-copies")))
        self.assertIn("The file itself is not changed.", copy["note"])
        again = asyncio.run(server.open_font(path, safe_copy=True))
        self.assertEqual((again["already_open"], again["font"]["id"], again["safe_copy"]["path"]), (True, 101, copy["path"]))

    def test_open_stops_at_fontlabs_question(self):
        self.state.hold_open = True
        with self.assertRaisesRegex(RuntimeError, r"\[dialog_open\] FontLab is asking: Round the coordinates"):
            asyncio.run(server.open_font(self.path, wait_s=5))
        with self.assertRaisesRegex(RuntimeError, r"\[bad_args\] wait_s"):
            asyncio.run(server.open_font(self.path, wait_s=0))

    def test_close_leaves_fontlabs_save_question_for_the_user(self):
        self.state.asks_to_save.add(7)
        with self.assertRaisesRegex(RuntimeError, r'\[save_prompt\] FontLab is asking: Save changes to  "Write Sans Regular"'):
            asyncio.run(server.close_font(7, wait_s=5))
        self.assertEqual(self.state.answers, [])

    def test_close_discards_changes_in_a_font_it_opened(self):
        asyncio.run(server.open_font(self.path))
        self.state.fonts[-1]._f_states.append(4)
        self.state.asks_to_save.add(101)
        with self.assertRaisesRegex(RuntimeError, r"\[unsaved_changes\]"):
            asyncio.run(server.close_font(101))
        with self.assertRaisesRegex(RuntimeError, r"\[risky_mode_off\] .*was not opened with open_font"):
            asyncio.run(server.close_font(7, discard_changes=True))
        closed = asyncio.run(server.close_font(101, discard_changes=True, font_name="FreeSans-Regular"))
        self.assertEqual((closed["closed"], closed["changes_discarded"]), (True, True))
        self.assertEqual(self.ids(), [1, 7])

    def test_front_and_compile(self):
        self.assertTrue(asyncio.run(server.bring_font_to_front(7))["brought_forward"])
        self.state.current = self.state.fonts[0]
        self.state.compiler = lambda font: u"[F] glyph class not defined (text was \"@nope\")\n" if font is self.font else u""
        reply = asyncio.run(server.compile_features(7))
        self.assertEqual((reply["compiled"], reply["errors"], reply["brought_forward"]),
                         (False, ['[F] glyph class not defined (text was "@nope")'], True))
        self.assertTrue(asyncio.run(server.compile_features(1))["compiled"])
        self.assertEqual(asyncio.run(server.read_output())["length"], 0, "FontLab cleared the panel at the compile")

    def test_screenshot_is_an_image(self):
        picture, about = asyncio.run(server.screenshot())
        self.assertEqual(picture.data, b"\x89PNG\r\n\x1a\n1600x800")
        self.assertEqual((about["window"], about["width"]), ("FontLab 7", 1600))
        result = asyncio.run(server.mcp.call_tool("screenshot", {"max_width": 0}))
        blocks = result[0] if isinstance(result, tuple) else result
        self.assertEqual([block.type for block in blocks], ["image", "text"])
        self.assertEqual(blocks[0].mimeType, "image/png")
        self.assertIn('"width": 2000', blocks[1].text)


class RiskyToolTests(WindowCase):
    """Risky mode through the real server: off by default, the list read from the listener's file."""

    def tools(self):
        return {t.name: t for t in asyncio.run(server.mcp.list_tools())}

    def test_one_list_and_every_risky_place_is_marked(self):
        self.assertEqual(server.risky.ACTIONS, [{"action": a, "tools": t, "reason": r, "enforced": e}
                                                for a, t, r, e in fl.RISKY_ACTIONS])
        self.assertEqual(set(RISKY_PLACES), set(entry["action"] for entry in server.risky.ACTIONS))
        tools = self.tools()
        for action, (tool, argument) in RISKY_PLACES.items():
            if argument == "*":
                for name in entry_tools(action):
                    self.assertIn("[risky]", tools[name].description, (action, name))
            elif argument is None:
                self.assertTrue(tools[tool].description.startswith("[risky]"), tool)
            else:
                self.assertIn("[risky]", tools[tool].inputSchema["properties"][argument]["description"], (tool, argument))
                self.assertIn("[risky]", tools[tool].description, tool)
        self.assertIn("own words", tools["set_risky_mode"].description)
        self.assertIn("never on your own initiative", server.mcp.instructions)
        listed = asyncio.run(server.list_risky_actions())
        self.assertEqual((listed["risky_mode"], listed["source"], len(listed["actions"])), (False, "listener file", 11))

    def test_risky_tools_are_refused_until_risky_mode_is_on(self):
        folder = os.path.join(self.tmp, "out")
        os.mkdir(folder)
        calls = [lambda: server.answer_dialog("OK"), lambda: server.save_font(7), lambda: server.export_font(7, folder),
                 lambda: server.rename_glyph(7, "Q", "Qalt", update_references=True),
                 lambda: server.delete_glyph(7, "Q", remove_references=True),
                 lambda: server.open_font(self.path, answer="OK"), lambda: server.quit_fontlab(discard_changes=True),
                 lambda: server.restart_fontlab(discard_changes=True)]
        for call in calls:
            with self.assertRaisesRegex(RuntimeError, r"\[risky_mode_off\] .*own words"):
                asyncio.run(call())
        self.assertEqual((self.state.steps, self.state.loads, self.state.exports), ([], [], []))
        self.assertEqual(asyncio.run(server.set_risky_mode(True))["risky_mode"], True)
        self.assertTrue(asyncio.run(server.list_risky_actions())["risky_mode"])
        self.assertFalse(asyncio.run(server.set_risky_mode(False))["risky_mode"])

    def test_answer_a_question(self):
        asyncio.run(server.set_risky_mode(True))
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK", u"Cancel"], text=u"Round the coordinates?")
        reply = asyncio.run(server.answer_dialog("OK", expected_title="Coordinates Rounding", wait_s=5))
        self.assertEqual((reply["answered"], reply["button"], reply["next_dialog"]), (True, "OK", None))
        self.assertEqual(self.state.answers, [u"OK"])

    def test_open_a_font_answering_its_question(self):
        asyncio.run(server.set_risky_mode(True))
        self.state.hold_open = True
        opened = asyncio.run(server.open_font(self.path, answer="OK", wait_s=10))
        self.assertEqual((opened["opened"], opened["font"]["id"], self.state.answers), (True, 101, [u"OK"]))

    def test_save_and_export(self):
        asyncio.run(server.set_risky_mode(True))
        path = os.path.join(self.tmp, "Copy.ufo")
        saved = asyncio.run(server.save_font(7, path=path, wait_s=10))
        self.assertEqual((saved["saved"], saved["path"], saved["how"]), (True, path, "fgFont.save"))
        with self.assertRaisesRegex(RuntimeError, r"\[file_exists\]"):
            asyncio.run(server.save_font(7, path=path, wait_s=10))
        folder = os.path.join(self.tmp, "out")
        os.mkdir(folder)
        exported = asyncio.run(server.export_font(7, folder, format="OpenType PS", wait_s=10))
        self.assertEqual((exported["files"], exported["format"]), ([os.path.join(folder, "WriteSans-Regular.ttf")],
                                                                     "OpenType PS"))
        self.assertEqual(self.state.answers, [u"OK"], "its Font Exported message closed")

    def test_references_and_outlines_through_the_server(self):
        asyncio.run(server.set_risky_mode(True))
        reply = asyncio.run(server.rename_glyph(7, "Q", "Qalt", update_references=True))
        self.assertEqual(reply["references"]["features"], ["ss01"])
        drawn = asyncio.run(server.set_glyph_outline(7, [{"nodes": [{"x": 0, "y": 0}, {"x": 100, "y": 0},
                                                                   {"x": 50, "y": 80}]}], glyph="B"))
        self.assertEqual(drawn["nodes"], 3)
        self.assertEqual(asyncio.run(server.get_glyph_outline("B", font=7, apply_transforms=True))["node_count"], 3)
        self.assertEqual(asyncio.run(server.get_kerning(7, left="V", right="A", resolved=True))["value"], -50.0)
        self.assertTrue(asyncio.run(server.delete_glyph(7, "space"))["deleted"])

    def test_compile_errors_are_located(self):
        self.state.compiler = lambda font: u'[F] Fatal error in font "W" in file "features" at line 8\n'
        reply = asyncio.run(server.compile_features(7))
        self.assertEqual((reply["located"]["tag"], reply["located"]["line_in_feature"]), ("ss01", 3))

    def test_compile_keeps_what_the_listener_read(self):
        """Codex, 2026-11-19: the server read the shared Output panel again after the compile, and could report
        another compile's output (the user's, of the font put back in front) as this font's."""
        self.state.compiler = lambda font: u"[F] original compile failed\n" if font is self.font else u""
        ops = []
        saved = server._call

        async def watching(op, args=None, timeout=server.DEFAULT_TIMEOUT_S):
            ops.append(op)
            reply = await saved(op, args, timeout)
            if op == "compile_features":
                self.state.output = u""  # (the user compiles the restored font meanwhile)
            return reply
        server._call = watching
        try:
            reply = asyncio.run(server.compile_features(7))
        finally:
            server._call = saved
        self.assertEqual((reply["compiled"], reply["errors"], reply["font"]["id"]),
                         (False, ["[F] original compile failed"], 7))
        self.assertEqual(ops, ["compile_features"], "the Output panel is not read again")

    def test_an_answer_to_a_question_gone_meanwhile_clicks_nothing(self):
        asyncio.run(server.set_risky_mode(True))
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK", u"Cancel"], text=u"Round the coordinates?")
        saved, seen = fl._modal_dialog, []

        def first_only():
            seen.append(1)
            return saved() if len(seen) == 1 else None  # (gone by the time of the click)
        fl._modal_dialog = first_only
        try:
            with self.assertRaisesRegex(RuntimeError, r"\[dialog_changed\] Nothing was clicked.*dialog gone"):
                asyncio.run(server.answer_dialog("OK", wait_s=5))
        finally:
            fl._modal_dialog = saved
        self.assertEqual(self.state.answers, [])

    def test_screenshot_from_the_screen(self):
        seen = []

        def capture(pid, title, max_width):
            seen.append((pid, title, max_width))
            return b"\x89PNGscreen", {"window": "FontLab 7", "width": 800, "height": 600, "source": "screen"}
        saved = server.screen.capture
        server.screen.capture = capture
        try:
            picture, about = asyncio.run(server.screenshot(max_width=800, source="screen"))
        finally:
            server.screen.capture = saved
        self.assertEqual((picture.data, about["source"], seen), (b"\x89PNGscreen", "screen", [(os.getpid(), "", 800)]))
        with self.assertRaisesRegex(RuntimeError, r"\[bad_args\] source"):
            asyncio.run(server.screenshot(source="desktop"))
        self.assertEqual(asyncio.run(server.screenshot())[1]["source"], "qt")


class WindowDrivingTests(WindowCase):
    """The window tools through the real server, client, and listener: a script's tool window driven as a user
    would, actions run on the listener's thread from its timers, and wait_idle waiting on their work."""

    def setUp(self):
        super().setUp()
        import widgetfakes as W
        from window_tests import TITLE, ToolWindow
        self.W, self.title = W, TITLE
        self.tool = ToolWindow()
        self.tool.window._f_props["mcpScriptWindow"] = True
        self.state.qt_windows = [self.tool.window]

    def work(self, seconds):
        """The Apply button's work: the window busy (mcpBusy) for a while, as a script's long work."""
        window = self.tool.window
        until = time.time() + seconds

        def check():
            if time.time() < until:
                self.state.timers.append((10, check))
            else:
                window._f_props["mcpBusy"] = False

        def start():
            window._f_props["mcpBusy"] = u"Kerning"
            self.state.timers.append((10, check))
        self.tool.apply._f_on["clicked"] = start

    def test_list_and_read(self):
        keys = [w["window"] for w in asyncio.run(server.list_windows())["windows"]]
        self.assertEqual(keys, ["main", self.title])
        listed = asyncio.run(server.list_window_controls("auto kerning", kinds=["combo", "spin"]))
        self.assertEqual([c["address"] for c in listed["controls"]], ["Master > combo", "Tolerance > spin"])
        rows = asyncio.run(server.read_list(self.title, "tree", limit=2, columns=[0]))
        self.assertEqual([r["texts"] for r in rows["rows"]], [["A V"], ["A Vtilde"]])
        text = asyncio.run(server.read_text(self.title, "log"))
        self.assertEqual([link["href"] for link in text["links"]], ["glyph:A", "glyph:V", "https://example.org/help"])

    def test_a_click_then_wait_idle(self):
        self.work(0.3)
        reply = asyncio.run(server.click(self.title, "Apply", wait_s=2))
        self.assertEqual((reply["state"], reply["result"]), ("done", {"clicked": "Apply > button#1"}))
        with self.assertRaisesRegex(RuntimeError, r"\[busy\] The window 'FreeLab Auto Kerning .*' is busy \(Kerning\)"):
            asyncio.run(server.set_value(self.title, "Tolerance", 3))
        started = time.monotonic()
        idle = asyncio.run(server.wait_idle(self.title, timeout_s=10, settle_s=0.05))
        self.assertGreater(time.monotonic() - started, 0.2)
        self.assertEqual((idle["idle"], idle["last_action"]["state"], idle["window"]["busy"]), (True, "done", False))
        reply = asyncio.run(server.set_value(self.title, "Tolerance", 3))
        self.assertEqual(reply["result"], {"before": 5, "after": 3})

    def test_wait_idle_times_out_and_stops_at_a_dialog(self):
        self.work(30)
        asyncio.run(server.click(self.title, "Apply"))
        with self.assertRaisesRegex(RuntimeError, r"\[still_busy\] Not idle after 0.3 s"):
            asyncio.run(server.wait_idle(timeout_s=0.3))
        self.tool.window._f_props["mcpBusy"] = False
        self.state.ask(u"Coordinates Rounding", buttons=[u"OK"])
        waited = asyncio.run(server.wait_idle(self.title, timeout_s=5))
        self.assertEqual((waited["idle"], waited["question"], waited["dialog"]["title"]), (False, True, "Coordinates Rounding"))
        self.assertIn("risky mode", waited["note"])
        with self.assertRaisesRegex(RuntimeError, r"\[dialog_open\]"):
            asyncio.run(server.click(self.title, "Apply"))
        with self.assertRaisesRegex(RuntimeError, r"\[risky_mode_off\] .*own words"):
            asyncio.run(server.click("dialog", "OK"))
        asyncio.run(server.set_risky_mode(True))
        self.assertEqual(asyncio.run(server.click("dialog", "OK"))["state"], "done")
        self.assertEqual(self.state.answers, [u"OK"])

    def test_a_closed_window_ends_the_wait(self):
        self.tool.close._f_on["clicked"] = lambda: self.state.qt_windows.remove(self.tool.window)
        self.assertEqual(asyncio.run(server.click(self.title, "Close"))["state"], "done")
        self.assertTrue(asyncio.run(server.wait_idle(self.title, timeout_s=5))["window_closed"])

    def test_an_action_skipped_at_its_turn_is_an_error(self):
        original = fl._start_action

        def racing(listener, args, tool, plan):
            reply = original(listener, args, tool, plan)
            self.tool.apply._f_set(enabled=False)  # (disabled between the reply and the timer)
            return reply
        fl._start_action = racing
        try:
            with self.assertRaisesRegex(RuntimeError, r"\[disabled\] Nothing was done \(checked again"):
                asyncio.run(server.click(self.title, "Apply"))
        finally:
            fl._start_action = original
        self.assertEqual(self.tool.applied, [])

    def test_menus_and_links(self):
        W = self.W
        check = W.Action(u"Font Check")
        self.state.menu_bar = W.Widget("QMenuBar", actions=[
            W.Action(u"&File", menu=W.menu(W.Action(u"&Save Font...", name=u"actionSaveFont"))),
            W.Action(u"Scripts", menu=W.menu(W.Action(u"FreeLab", menu=W.menu(check))))])
        self.assertEqual([e["text"] for e in asyncio.run(server.list_menu("Scripts > FreeLab"))["entries"]], ["Font Check"])
        self.assertEqual(asyncio.run(server.run_menu_command("Scripts > FreeLab > Font Check"))["state"], "done")
        self.assertEqual(check._f_triggered, 1)
        with self.assertRaisesRegex(RuntimeError, r"\[risky_mode_off\] File > Save Font"):
            asyncio.run(server.run_menu_command("File > Save Font"))
        reply = asyncio.run(server.click_link(self.title, "log", text="V"))
        self.assertEqual((reply["state"], self.tool.log._f_emitted), ("done", [("anchorClicked", "glyph:V")]))
        reply = asyncio.run(server.click(self.title, "FreeLab tools", entry="Font Check"))
        self.assertEqual((reply["result"], self.tool.font_check._f_triggered), ({"entry": "Font Check"}, 1))
        with self.assertRaisesRegex(RuntimeError, r"\[bad_args\] wait_s"):
            asyncio.run(server.click(self.title, "Apply", wait_s=-1))

    def test_values_through_mcp(self):
        """FastMCP's own argument handling: a value true stays a bool, a number a number, a text a text."""
        for value, address, after in ((True, "Only kerned pairs > check", True), (12, "Tolerance > spin", 12),
                                      ("Bold", "Master > combo", "Bold")):
            result = asyncio.run(server.mcp.call_tool("set_value", {"window": self.title, "control": address, "value": value}))
            blocks = result[0] if isinstance(result, tuple) else result
            self.assertEqual(json.loads(blocks[0].text)["result"]["after"], after)
        self.tool.page1._f_add(self.W.Widget("QLineEdit", name="note", text="x"))
        self.assertEqual(asyncio.run(server.set_value(self.title, "note", ""))["result"]["after"], "", "a field emptied")


class ScreenTests(unittest.TestCase):
    def test_png_round_trip(self):
        import struct
        import zlib
        bgra = bytes([1, 2, 3, 0, 10, 20, 30, 0, 100, 110, 120, 0, 7, 8, 9, 0])  # 2 x 2 pixels
        data = server.screen.png(2, 2, server.screen.bgra_to_rgb(bgra))
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        width, height, depth, kind = struct.unpack(">IIBB", data[16:26])
        self.assertEqual((width, height, depth, kind), (2, 2, 8, 2))
        length = struct.unpack(">I", data[33:37])[0]
        self.assertEqual(data[37:41], b"IDAT")
        rows = zlib.decompress(data[41:41 + length])
        self.assertEqual(rows, b"\x00" + bytes([3, 2, 1, 30, 20, 10]) + b"\x00" + bytes([120, 110, 100, 9, 8, 7]))
        self.assertTrue(data.endswith(b"IEND\xaeB`\x82"))
        with self.assertRaises(ValueError):
            server.screen.png(2, 2, b"\x00")

    def test_which_window(self):
        choose = server.screen.choose_window
        windows = [{"hwnd": 1, "title": "FontLab 7", "owned": False, "width": 1600, "height": 900},
                   {"hwnd": 2, "title": "FreeLab Proof", "owned": True, "width": 1800, "height": 1000},
                   {"hwnd": 3, "title": "FreeLab Proof Settings", "owned": True, "width": 400, "height": 300},
                   {"hwnd": 4, "title": "", "owned": False, "width": 0, "height": 0}]
        self.assertEqual(choose(windows)["hwnd"], 1, "the largest window no other owns")
        self.assertEqual(choose(windows, "FreeLab Proof")["hwnd"], 2, "exact first")
        self.assertEqual(choose(windows, "settings")["hwnd"], 3)
        for title, code in (("Proof", "ambiguous_window"), ("Metrics", "not_found")):
            with self.assertRaises(server.screen.CaptureError) as ctx:
                choose(windows, title)
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(server.screen.CaptureError):
            choose([])

    def test_capture_scales_and_restores_dpi(self):
        calls = []

        class Api(object):
            def dpi_aware(self):
                calls.append("aware")
                return 17

            def restore_dpi(self, previous):
                calls.append(("restore", previous))

            def windows(self, pid):
                return [{"hwnd": 5, "title": "FontLab 7", "owned": False, "width": 400, "height": 200}]

            def minimized(self, hwnd):
                return False

            def print_window(self, hwnd, width, height, out_width, out_height):
                calls.append((width, height, out_width, out_height))
                return b"\x00\x00\xff\x00" * (out_width * out_height)

        data, about = server.screen.capture(42, "", 100, _api=Api)
        self.assertEqual((about["width"], about["height"]), (100, 50))
        self.assertEqual(calls, ["aware", (400, 200, 100, 50), ("restore", 17)])
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")

    def gdi(self, fail):
        """_Win32.print_window over a fake user32 and gdi32 that log each call; fail names the call that fails."""
        import ctypes
        log = []
        handles = iter(range(100, 200))

        class Dll(object):
            def __getattr__(self, name):
                def call(*args):
                    log.append((name,) + tuple(a for a in args if isinstance(a, int)))
                    if name == fail:
                        return 0
                    if name in ("GetDC", "CreateCompatibleDC", "CreateCompatibleBitmap", "SelectObject"):
                        return next(handles)
                    if name == "CreateDIBSection":
                        args[3]._obj.value = 1
                        return next(handles)
                    return 1
                return call
        api = object.__new__(server.screen._Win32)
        api.ctypes, api.user32, api.gdi32 = ctypes, Dll(), Dll()
        saved = ctypes.string_at
        ctypes.string_at = lambda address, size: b"\x00" * size
        try:
            with self.assertRaises(server.screen.CaptureError) if fail else contextlib.nullcontext():
                api.print_window(5, 4, 4, 2, 2)
        finally:
            ctypes.string_at = saved
        return log

    def test_every_gdi_object_is_released_after_its_bitmap_is_selected_out(self):
        """Codex, 2026-11-19: a failed PrintWindow left the full-size bitmap selected in its DC when it was
        deleted, so it leaked."""
        for fail in ("PrintWindow", "CreateDIBSection", "StretchBlt", "CreateCompatibleDC", None):
            log = self.gdi(fail)
            names = [entry[0] for entry in log]
            made = [entry for entry in log if entry[0] in ("CreateCompatibleDC", "CreateCompatibleBitmap",
                                                           "CreateDIBSection")]
            deleted = [entry for entry in log if entry[0] in ("DeleteObject", "DeleteDC")]
            self.assertEqual(len(deleted), len([m for m in made if m[0] != fail]), (fail, log))
            if "SelectObject" in names:
                selects = [i for i, n in enumerate(names) if n == "SelectObject"]
                self.assertLess(max(selects), names.index("DeleteObject"), (fail, "selected out before deleted"))
                self.assertEqual(len(selects) % 2, 0, (fail, "each selection undone"))
            self.assertEqual(names[-1], "ReleaseDC", fail)


class ClientLimitTests(unittest.TestCase):
    def test_oversized_request_is_refused_before_sending(self):
        saved = client.read_state
        client.read_state = lambda: {"port": 9, "token": "x", "protocol": client.PROTOCOL}
        try:
            with self.assertRaises(client.ListenerError) as ctx:
                client.call("run_python", {"code": "#" * (client.MAX_REQUEST_BYTES + 1)})
        finally:
            client.read_state = saved
        self.assertEqual(ctx.exception.code, "too_large")


class LaunchTests(unittest.TestCase):
    """launch_fontlab with the process side faked: no FontLab is started by these tests."""

    def setUp(self):
        self._saved = (server.launcher.fontlab_running, server.launcher.launch, server._call, server.anyio.sleep)
        self.launched = []
        self.pings = []

        async def no_sleep(_seconds):
            return None

        server.anyio.sleep = no_sleep

    def tearDown(self):
        (server.launcher.fontlab_running, server.launcher.launch, server._call, server.anyio.sleep) = self._saved

    dialog = None  # what the fake FontLab is asking, if anything

    def fake_call(self, answers):
        async def call(op, args=None, timeout=None):
            if op == "dialog":
                return {"dialog": self.dialog}
            self.pings.append(op)
            answer = answers.pop(0) if answers else None
            if answer is None:
                raise RuntimeError("[not_running] no listener")
            return answer
        server._call = call

    def test_already_running_with_listener(self):
        self.fake_call([{"listener": "0.1.0"}])
        server.launcher.launch = lambda: self.launched.append(1) or 1
        result = asyncio.run(server.launch_fontlab())
        self.assertEqual(result["started"], False)
        self.assertEqual(self.launched, [])

    def test_running_without_listener_is_not_relaunched(self):
        self.fake_call([None])
        server.launcher.fontlab_running = lambda: True
        server.launcher.launch = lambda: self.launched.append(1) or 1
        with self.assertRaisesRegex(RuntimeError, "listener_not_running"):
            asyncio.run(server.launch_fontlab())
        self.assertEqual(self.launched, [])

    def test_starts_and_waits_for_the_listener(self):
        self.fake_call([None, None, {"listener": "0.1.0"}])
        server.launcher.fontlab_running = lambda: False
        server.launcher.launch = lambda: self.launched.append(1) or 4242
        result = asyncio.run(server.launch_fontlab(wait_s=60))
        self.assertEqual((result["started"], result["pid"]), (True, 4242))
        self.assertEqual(self.launched, [1])

    def test_missing_program(self):
        self.fake_call([None])
        server.launcher.fontlab_running = lambda: False

        def missing():
            raise FileNotFoundError("missing/FontLab 7.exe")

        server.launcher.launch = missing
        with self.assertRaisesRegex(RuntimeError, "not_found"):
            asyncio.run(server.launch_fontlab())

    def test_wait_bounds(self):
        with self.assertRaisesRegex(RuntimeError, "bad_args"):
            asyncio.run(server.launch_fontlab(wait_s=0))
        with self.assertRaisesRegex(RuntimeError, "bad_args"):
            asyncio.run(server.quit_fontlab(wait_s=0))

    def test_quit_refusal_is_passed_on_and_nothing_waits(self):
        async def call(op, args=None, timeout=None):
            raise RuntimeError("[unsaved_changes] FontLab has unsaved changes")
        server._call = call
        checks = []
        server.launcher.fontlab_running = lambda: checks.append(1) or True
        with self.assertRaisesRegex(RuntimeError, "unsaved_changes"):
            asyncio.run(server.quit_fontlab())
        self.assertEqual(checks, [])

    def test_quit_waits_for_the_process_to_end(self):
        self.fake_call([{"quitting": True}])
        states = [True, True, False]
        server.launcher.fontlab_running = lambda: states.pop(0)
        self.assertEqual(asyncio.run(server.quit_fontlab()), {"quit": True})

    def test_quit_reports_a_fontlab_that_stays_open(self):
        self.fake_call([{"quitting": True}])
        server.launcher.fontlab_running = lambda: True
        with self.assertRaisesRegex(RuntimeError, "still_running"):
            asyncio.run(server.quit_fontlab(wait_s=0.01))

    def test_quit_names_a_window_that_keeps_fontlab_running(self):
        self.fake_call([{"quitting": True}])
        server.launcher.fontlab_running = lambda: True
        saved = server._windows_left

        async def left():
            return {"dialog": None, "main_window": False, "windows": ["FreeLab Auto Kerning"]}

        server._windows_left = left
        try:
            with self.assertRaisesRegex(RuntimeError, "main window closed.*FreeLab Auto Kerning"):
                asyncio.run(server.quit_fontlab(wait_s=0.01))
        finally:
            server._windows_left = saved

    def test_restart_without_the_program_closes_nothing(self):
        self.fake_call([{"quitting": True}])
        saved = server.launcher.program_found
        server.launcher.program_found = lambda: False
        try:
            with self.assertRaisesRegex(RuntimeError, "not_found"):
                asyncio.run(server.restart_fontlab())
        finally:
            server.launcher.program_found = saved
        self.assertEqual(self.pings, [], "FontLab left open")

    def test_restart_waits_for_closing_whatever_its_wait(self):
        self.fake_call([{"quitting": True}, None, {"listener": "0.1.0"}])
        saved = server.launcher.program_found
        server.launcher.program_found = lambda: True
        running = [True] * 20 + [False]  # closing takes longer than wait_s would allow
        server.launcher.fontlab_running = lambda: running.pop(0) if running else False
        server.launcher.launch = lambda: self.launched.append(1) or 5
        try:
            result = asyncio.run(server.restart_fontlab(wait_s=0.01))
        finally:
            server.launcher.program_found = saved
        self.assertEqual((self.launched, result["started"]), ([1], True))

    def test_a_launched_fontlab_still_starting_is_waited_for(self):
        answers = [RuntimeError("[not_running] none"), RuntimeError("[timeout] busy"), RuntimeError("[bad_state] half"),
                   {"listener": "0.1.0"}]

        async def call(op, args=None, timeout=None):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        server._call = call
        server.launcher.fontlab_running = lambda: False
        server.launcher.launch = lambda: self.launched.append(1) or 9
        result = asyncio.run(server.launch_fontlab(wait_s=60))
        self.assertEqual((result["started"], result["pid"]), (True, 9))

    def test_restart_checks_its_bounds_before_closing(self):
        self.fake_call([{"quitting": True}])
        with self.assertRaisesRegex(RuntimeError, "bad_args"):
            asyncio.run(server.restart_fontlab(wait_s=601))
        self.assertEqual(self.pings, [], "nothing sent: FontLab stays open")

    def test_a_listener_that_answers_badly_is_not_relaunched(self):
        async def call(op, args=None, timeout=None):
            raise RuntimeError("[bad_protocol] the listener speaks protocol 2")
        server._call = call
        server.launcher.launch = lambda: self.launched.append(1) or 1
        with self.assertRaisesRegex(RuntimeError, "bad_protocol"):
            asyncio.run(server.launch_fontlab())
        self.assertEqual(self.launched, [])

    def test_image_name_follows_the_program(self):
        saved = os.environ.get("FREELAB_FONTLAB_EXE")
        os.environ["FREELAB_FONTLAB_EXE"] = os.path.join("C:" + os.sep, "Apps", "FontLab 7 Beta.exe")
        try:
            self.assertEqual(server.launcher.image_name(), "FontLab 7 Beta.exe")
        finally:
            if saved is None:
                os.environ.pop("FREELAB_FONTLAB_EXE", None)
            else:
                os.environ["FREELAB_FONTLAB_EXE"] = saved

    def test_a_save_prompt_is_reported_at_once(self):
        self.fake_call([{"quitting": True, "edited": 1}])
        self.dialog = {"class": "DlgClosePackage", "title": "FontLab Warning",
                       "text": ['Save changes to "Test" before closing?'], "buttons": ["Don't Save", "Cancel", "OK"]}
        server.launcher.fontlab_running = lambda: True
        with self.assertRaisesRegex(RuntimeError, r"\[save_prompt\].*Save changes to \"Test\"") as ctx:
            asyncio.run(server.quit_fontlab(wait_s=60))
        self.assertIn("left on screen", str(ctx.exception))

    def test_quit_with_discard_changes_waits_through_save_questions(self):
        sent = []

        async def call(op, args=None, timeout=None):
            if op == "dialog":
                return {"dialog": self.dialog}
            sent.append((op, args))
            return {"quitting": True, "edited": 1, "discard_changes": True}
        server._call = call
        self.dialog = {"class": "DlgClosePackage", "title": "FontLab Warning", "text": ["Save changes?"], "buttons": []}
        states = [True, True, False]
        server.launcher.fontlab_running = lambda: states.pop(0)
        server.risky.Mode.enabled = True
        try:
            self.assertEqual(asyncio.run(server.quit_fontlab(discard_changes=True)), {"quit": True, "changes_discarded": True})
        finally:
            server.risky.Mode.enabled = False
        self.assertEqual(sent, [("quit", {"discard_changes": True, "risky": True})])

    def test_restart_quits_then_launches(self):
        self.fake_call([{"quitting": True}, None, {"listener": "0.1.0"}])
        running = [True, False, False]
        server.launcher.fontlab_running = lambda: running.pop(0) if running else False
        server.launcher.launch = lambda: self.launched.append(1) or 77
        result = asyncio.run(server.restart_fontlab(wait_s=60))
        self.assertEqual((result["started"], result["pid"]), (True, 77))


class WaitIdleReviewTests(unittest.TestCase):
    """Codex's review of 1.3.0 (2026-11-19), findings 10 and 11: wait_idle with a window counts that window's work
    only, and idle seen before FontLab fell silent does not count toward settle_s."""

    def run_wait(self, answer, **kwargs):
        from types import SimpleNamespace
        from unittest import mock
        clock = SimpleNamespace(now=0.0, calls=0)

        async def call(op, args=None, timeout=None):
            clock.calls += 1
            return answer(clock, args)

        async def sleep(seconds):
            clock.now += seconds

        async def run_sync(func):
            return func()
        with mock.patch.object(server, "_call", call), \
                mock.patch.object(server, "time", SimpleNamespace(monotonic=lambda: clock.now)), \
                mock.patch.object(server, "anyio", SimpleNamespace(sleep=sleep, to_thread=SimpleNamespace(run_sync=run_sync))):
            return asyncio.run(server.wait_idle(**kwargs)), clock

    @staticmethod
    def idle(window="A", pending=()):
        return {"window": {"window": window, "busy": False}, "dialog": None, "busy_windows": [],
                "actions_pending": list(pending)}

    def test_another_windows_action_does_not_hold_the_wait(self):
        other = [{"id": 1, "tool": "click", "state": "running", "window": "B"}]
        result, _clock = self.run_wait(lambda clock, args: self.idle(pending=other), window="a", timeout_s=2, settle_s=0)
        self.assertTrue(result["idle"])
        own = [{"id": 2, "tool": "click", "state": "running", "window": "A"}]
        with self.assertRaises(RuntimeError):  # (its own action holds it)
            self.run_wait(lambda clock, args: self.idle(pending=own), window="a", timeout_s=2, settle_s=0)
        closed, _clock = self.run_wait(lambda clock, args: dict(self.idle(pending=other), window=None),
                                       window="a", timeout_s=2, settle_s=0)
        self.assertTrue(closed["window_closed"])

    def test_silence_starts_the_settling_again(self):
        def answer(clock, args):
            if clock.calls == 2:
                clock.now += 5
                raise RuntimeError("[timeout] FontLab did not answer")
            if clock.calls >= 4:
                clock.now += 0.5
            return self.idle()
        result, clock = self.run_wait(answer, window="A", timeout_s=20, settle_s=0.5)
        self.assertTrue(result["idle"])
        self.assertGreaterEqual(clock.calls, 4, "the idle seen before the silence did not count")

    def test_a_refused_connection_while_fontlab_runs_is_silence(self):
        # (live, 2026-11-19: FontLab held by a locked clipboard; each poll that timed out stayed in the listener's
        # queue until it refused connections, and wait_idle gave up although FontLab came back)
        from unittest import mock

        def answer(clock, args):
            if clock.calls <= 3:
                clock.now += 5
                raise RuntimeError("[not_running] The FreeLab listener refused the connection: ...")
            return self.idle()
        with mock.patch.object(server, "_fontlab_still_runs", lambda: True):
            result, clock = self.run_wait(answer, window="A", timeout_s=60, settle_s=0)
        self.assertTrue(result["idle"])
        with mock.patch.object(server, "_fontlab_still_runs", lambda: False), self.assertRaises(RuntimeError):
            self.run_wait(answer, window="A", timeout_s=60, settle_s=0)


class SilenceTests(unittest.TestCase):
    """A tool waiting for what it asked keeps waiting while FontLab runs without answering (live, 2026-11-19: a
    clipboard another program held froze FontLab 20 to 60 s at a time; close_font gave up after its 30 s and
    reported the font still open, and it closed), up to SILENCE_MAX_S past wait_s, and says so when it gives up."""

    FONT = {"id": 7, "name": "Write Sans Regular"}
    PATH = os.path.join("C:" + os.sep, "Fonts", "Open.ttf")

    def run_tool(self, tool, answer, runs=True):
        from types import SimpleNamespace
        from unittest import mock
        clock = SimpleNamespace(now=0.0, calls=[])

        async def call(op, args=None, timeout=None):
            clock.calls.append(op)
            return answer(clock, op, args)

        async def sleep(seconds):
            clock.now += seconds

        async def run_sync(func):
            return func()
        with mock.patch.object(server, "_call", call), \
                mock.patch.object(server, "_fontlab_still_runs", lambda: runs), \
                mock.patch.object(server, "time", SimpleNamespace(monotonic=lambda: clock.now)), \
                mock.patch.object(server, "anyio", SimpleNamespace(sleep=sleep, to_thread=SimpleNamespace(run_sync=run_sync))):
            try:
                return asyncio.run(tool()), clock
            except RuntimeError as exc:
                exc.clock = clock
                raise

    def fontlab(self, silent_until, fonts):
        """FontLab that does not answer until silent_until (each poll costing 10 s: timed out, or refused once
        the listener's queue is full), then lists fonts."""
        def answer(clock, op, args):
            if op == "close_font":
                return {"font": self.FONT, "edited": False}
            if op == "open_font":
                return {"path": self.PATH, "fonts_before": [1, 7]}
            if clock.now < silent_until:
                clock.now += 10
                raise RuntimeError("[timeout] FontLab did not answer" if len(clock.calls) % 2 else
                                   "[not_running] The FreeLab listener refused the connection: ...")
            return {"dialog": None} if op == "dialog" else {"fonts": fonts}
        return answer

    def test_close_waits_through_silence_past_its_wait(self):
        result, clock = self.run_tool(lambda: server.close_font(7, wait_s=30), self.fontlab(90, [{"id": 1}]))
        self.assertEqual((result["closed"], result["font"]), (True, self.FONT))
        self.assertGreater(clock.now, 90)

    def test_open_waits_through_silence_past_its_wait(self):
        fonts = [{"id": 1}, {"id": 7}, {"id": 101, "path": self.PATH}]
        result, clock = self.run_tool(lambda: server.open_font(self.PATH, wait_s=20), self.fontlab(150, fonts))
        self.assertEqual((result["opened"], result["font"]["id"]), (True, 101))

    def test_a_silence_that_does_not_end_is_given_up_and_named(self):
        with self.assertRaisesRegex(RuntimeError, r"\[still_open\] Write Sans Regular was not seen closed: .*stopped "
                                    r"answering\. FontLab still runs but has not answered for \d+ s .*clipboard") as ctx:
            self.run_tool(lambda: server.close_font(7, wait_s=30), self.fontlab(10_000, []))
        clock = ctx.exception.clock
        self.assertGreaterEqual(clock.now, 30 + server.SILENCE_MAX_S)
        self.assertLess(clock.now, 30 + server.SILENCE_MAX_S + 30, "the ceiling holds")
        with self.assertRaisesRegex(RuntimeError, r"\[not_opened\] .* was not seen open: .*has not answered for"):
            self.run_tool(lambda: server.open_font(self.PATH, wait_s=10), self.fontlab(10_000, []))

    def test_fontlab_back_with_the_font_still_open_is_still_open(self):
        with self.assertRaisesRegex(RuntimeError, r"\[still_open\] Write Sans Regular is still open after 30 s: .*"
                                    r"FontLab did not answer for \d+ s of the \d+ s waited\.") as ctx:
            self.run_tool(lambda: server.close_font(7, wait_s=30), self.fontlab(60, [{"id": 1}, self.FONT]))
        clock = ctx.exception.clock
        self.assertLessEqual(clock.now, 60 + 10 + server.SILENCE_GRACE_S + 1, "a short look once FontLab answers")

    def test_a_silence_that_starts_on_the_dialog_poll_is_waited_through(self):
        # (Codex, 2026-11-19: the dialog polls of open_font and close_font did not tell the wait FontLab was silent)
        def answer(clock, op, args):
            if op == "close_font":
                return {"font": self.FONT, "edited": False}
            if op == "dialog":
                if 0.4 < clock.now < 60:
                    clock.now += 5
                    raise RuntimeError("[timeout] FontLab did not answer")
                return {"dialog": None}
            return {"fonts": [{"id": 1}, self.FONT] if clock.now < 60 else [{"id": 1}]}
        result, clock = self.run_tool(lambda: server.close_font(7, wait_s=1), answer)
        self.assertTrue(result["closed"])
        self.assertGreater(clock.now, 60)

    def test_files_that_came_while_fontlab_was_silent_are_no_export(self):
        # (Codex, 2026-11-19: they were returned as exported, though FontLab may still have been writing them)
        import tempfile
        folder = tempfile.mkdtemp(prefix="freelab-export-")
        self.addCleanup(__import__("shutil").rmtree, folder, True)

        def answer(clock, op, args):
            if op == "export_font":
                return {"font": self.FONT, "format": "OpenType PS"}
            if clock.now > 1:
                if not os.path.exists(os.path.join(folder, "Write.otf")):
                    with open(os.path.join(folder, "Write.otf"), "wb") as handle:
                        handle.write(b"half")
                clock.now += 10
                raise RuntimeError("[timeout] FontLab did not answer")
            return {"dialog": None}
        from unittest import mock
        with mock.patch.object(server.risky.Mode, "enabled", True):
            result, _clock = self.run_tool(lambda: server.export_font(7, folder, wait_s=5), answer)
        self.assertEqual((result["exported"], result["outcome"], result["files_seen"]),
                         (None, "unknown", [os.path.join(folder, "Write.otf")]))
        self.assertNotIn("files", result)
        self.assertIn("not known to be over", result["note"])

    def test_a_silence_within_the_wait_adds_nothing(self):
        result, clock = self.run_tool(lambda: server.close_font(7, wait_s=30), self.fontlab(20, [{"id": 1}]))
        self.assertTrue(result["closed"])
        self.assertLess(clock.now, 30)

    def test_a_refused_connection_once_fontlab_ended_is_no_silence(self):
        with self.assertRaisesRegex(RuntimeError, r"^\[not_running\]") as ctx:
            self.run_tool(lambda: server.close_font(7, wait_s=30), self.fontlab(10_000, []), runs=False)
        self.assertLess(ctx.exception.clock.now, 30, "no wait for a FontLab that is gone")

    def test_an_action_refused_while_fontlab_runs_still_runs(self):
        def answer(clock, op, args):
            if op == "click":
                return {"action_id": 3, "queued": True, "window": "A"}
            clock.now += 5
            raise RuntimeError("[not_running] The FreeLab listener refused the connection: ...")
        result, _clock = self.run_tool(lambda: server.click("A", "button", wait_s=2), answer)
        self.assertEqual(result["state"], "running")
        self.assertIn("wait_idle", result["note"])
        with self.assertRaisesRegex(RuntimeError, r"^\[not_running\]"):
            self.run_tool(lambda: server.click("A", "button", wait_s=2), answer, runs=False)

    def test_an_answer_waits_through_silence(self):
        question = {"class": "DlgRound", "title": "Coordinates Rounding", "text": ["Round?"], "buttons": ["OK"]}

        def answer(clock, op, args):
            if op == "answer_dialog":
                return {"dialog": question, "answering": "OK", "answer_id": 4}
            if clock.now < 40:
                clock.now += 10
                raise RuntimeError("[timeout] FontLab did not answer")
            return {"outcome": "clicked"} if op == "answer_result" else {"dialog": None}
        from unittest import mock
        with mock.patch.object(server.risky.Mode, "enabled", True):
            result, clock = self.run_tool(lambda: server.answer_dialog("OK", wait_s=10), answer)
        self.assertEqual((result["answered"], result["next_dialog"]), (True, None))
        self.assertGreater(clock.now, 40)

    def test_launch_waits_through_a_listener_held_but_not_a_missing_one(self):
        from unittest import mock

        def held(clock, op, args):
            if len(clock.calls) == 1:  # (before the launch: no FontLab)
                raise RuntimeError("[not_running] The FreeLab listener is not running (no state file).")
            if clock.now < 200:
                clock.now += 5
                raise RuntimeError("[timeout] FontLab did not answer")
            return {"listener": "1.3.0"}

        def missing(clock, op, args):
            raise RuntimeError("[not_running] The FreeLab listener refused the connection: ...")
        with mock.patch.object(server.launcher, "launch", lambda: 9), \
                mock.patch.object(server.launcher, "fontlab_running", lambda: False):
            result, _clock = self.run_tool(lambda: server.launch_fontlab(wait_s=120), held, runs=False)
            self.assertEqual((result["started"], result["pid"]), (True, 9))
            with self.assertRaisesRegex(RuntimeError, r"\[no_listener\]") as ctx:
                self.run_tool(lambda: server.launch_fontlab(wait_s=120), missing, runs=False)
        self.assertLess(ctx.exception.clock.now, 130, "a refusal does not extend the wait")


if __name__ == "__main__":
    unittest.main()
