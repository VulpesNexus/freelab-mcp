"""MCP server tests: the real server and client against the real listener on spy fakes (Python 3)."""

import asyncio
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
                  "open_font", "close_font", "bring_font_to_front", "compile_features", "read_output", "screenshot"}
WRITE_TOOLS = {"set_kerning_pair", "remove_kerning_pair", "set_glyph_metrics", "rename_glyph", "set_glyph_unicodes",
               "add_component_glyph", "set_anchor", "remove_anchor", "set_feature_code", "undo",
               "close_font", "bring_font_to_front", "compile_features"}  # (the tools that name their font)


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
        self.assertEqual(status["server"], "1.1.0")

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
        with self.assertRaisesRegex(RuntimeError, r"\[not_in_front\]"):
            asyncio.run(server.undo(7))
        self.state.current = self.font
        undone = asyncio.run(server.undo(7, expected_state=reply["history"]["history_state"]))
        self.assertTrue(undone["undone"])

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


class WindowToolTests(ServerCase):
    """Opening, closing, bringing to the front, compiling, the Output panel, and pictures, through the real
    server, client, and listener, on the fakes; FontLab's timers fire on the listener's thread."""

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

    def test_open_then_close_a_font(self):
        opened = asyncio.run(server.open_font(self.path))
        self.assertEqual((opened["opened"], opened["font"]["id"], opened["font"]["path"]), (True, 101, self.path))
        again = asyncio.run(server.open_font(self.path))
        self.assertEqual((again["opened"], again["already_open"], again["font"]["id"]), (False, True, 101))
        self.assertEqual(asyncio.run(server.close_font(101)), {"closed": True, "font": {"id": 101, "name": "Free Sans Regular"},
                                                               "changes_discarded": False})
        self.assertEqual(self.ids(), [1, 7])

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
        with self.assertRaisesRegex(RuntimeError, r"\[not_opened_here\]"):
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

    def test_restart_quits_then_launches(self):
        self.fake_call([{"quitting": True}, None, {"listener": "0.1.0"}])
        running = [True, False, False]
        server.launcher.fontlab_running = lambda: running.pop(0) if running else False
        server.launcher.launch = lambda: self.launched.append(1) or 77
        result = asyncio.run(server.restart_fontlab(wait_s=60))
        self.assertEqual((result["started"], result["pid"]), (True, 77))


if __name__ == "__main__":
    unittest.main()
