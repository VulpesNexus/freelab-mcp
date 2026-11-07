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

EXPECTED_TOOLS = {"fontlab_status", "get_current_font", "list_glyphs", "get_glyph", "get_glyph_outline", "run_python",
                  "launch_fontlab", "quit_fontlab", "restart_fontlab"}


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
        self.assertIn("Start Listener", str(ctx.exception))

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
        self.assertEqual(status["server"], "0.1.0")

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
