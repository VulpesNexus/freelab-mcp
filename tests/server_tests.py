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

EXPECTED_TOOLS = {"fontlab_status", "get_current_font", "list_glyphs", "get_glyph", "get_glyph_outline", "run_python"}


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
        if self.listener:
            self.listener.stop()
        fakes.uninstall()
        if self._old_state is None:
            os.environ.pop("FREELAB_STATE_DIR", None)
        else:
            os.environ["FREELAB_STATE_DIR"] = self._old_state
        shutil.rmtree(self.tmp, ignore_errors=True)

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
        self.assertIn("[not_running]", str(ctx.exception), port)

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


if __name__ == "__main__":
    unittest.main()
