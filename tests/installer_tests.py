"""tools/install-fontlab.ps1 run on fixture files: the startup script's blocks, encoding, and line endings (Python 3, Windows).

The installer runs in Windows PowerShell with every location redirected to a temporary folder;
nothing of FontLab's is touched. Skipped where Windows PowerShell is missing.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "tools" / "install-fontlab.ps1"
POWERSHELL = shutil.which("powershell.exe") if sys.platform == "win32" else None
NO_WINDOW = 0x08000000

LEGACY = b"# >>> FreeLab (managed by FreeLab tools/install-fontlab.ps1; -Uninstall removes it)\ndef _freelab_startup():\n    pass\n# <<< FreeLab\n"
TOOLS = b"# >>> FreeLab tools (managed by FreeLab tools/install-fontlab.ps1; -Uninstall removes it)\ntools = 1\n# <<< FreeLab tools\n"
MINE = b"import fontlab\nprint('mine')\n"


@unittest.skipIf(POWERSHELL is None, "Windows PowerShell is not available")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="freelab-installer-"))
        self.scripts = self.tmp / "Scripts"
        self.startup = self.tmp / "startupScript.py"
        (self.scripts / "FreeLab").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_installer(self, *flags):
        proc = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
             "-ScriptsDir", str(self.scripts), "-StartupScript", str(self.startup), *flags],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW,
            timeout=120,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def write(self, data):
        self.startup.write_bytes(data)

    def read(self):
        return self.startup.read_bytes()

    def test_flagged_install_replaces_the_legacy_block_and_keeps_the_rest(self):
        self.write(b"\xef\xbb\xbf" + (MINE + LEGACY + TOOLS).replace(b"\n", b"\r\n"))
        code, out = self.run_installer("-ListenerAtLaunch")
        self.assertEqual(code, 0, out)
        data = self.read()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"), "the BOM is kept")
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""), "CRLF line endings are kept")
        text = data[3:].replace(b"\r\n", b"\n")
        self.assertTrue(text.startswith(MINE), "the user's code is kept")
        self.assertNotIn(b"# >>> FreeLab (managed", text)
        self.assertIn(TOOLS, text, "the other repository's block is kept")
        self.assertEqual(text.count(b"# >>> FreeLab MCP"), 1)
        self.assertIn("combined FreeLab block", out)
        self.assertTrue((self.scripts / "FreeLab" / "freelab_listener.py").is_file())
        before = self.read()
        code, out = self.run_installer("-ListenerAtLaunch")
        self.assertEqual((code, self.read()), (0, before), "a second install changes nothing")

    def test_block_is_updated_where_it_is(self):
        self.write(MINE + b"# >>> FreeLab MCP (managed)\nold = 1\n# <<< FreeLab MCP\n" + TOOLS)
        code, out = self.run_installer("-ListenerAtLaunch")
        self.assertEqual(code, 0, out)
        text = self.read()
        self.assertNotIn(b"old = 1", text)
        self.assertTrue(text.startswith(MINE) and text.endswith(TOOLS), "both blocks keep their order")

    def test_plain_install_after_a_flagged_one_removes_only_its_block(self):
        self.write(MINE + TOOLS)
        self.assertEqual(self.run_installer("-ListenerAtLaunch")[0], 0)
        code, out = self.run_installer()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(), MINE + TOOLS)

    def test_uninstall_without_a_block_leaves_the_file_alone(self):
        self.write(MINE + TOOLS)
        (self.scripts / "FreeLab" / "freelab_dark_mode.py").write_text("other")
        code, out = self.run_installer("-Uninstall")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(), MINE + TOOLS)
        self.assertNotIn("Removed the FreeLab MCP block", out)
        self.assertTrue((self.scripts / "FreeLab" / "freelab_dark_mode.py").is_file(), "the other repository's files stay")

    def test_block_replaced_in_place_is_whole_even_with_a_dollar_in_the_path(self):
        self.scripts = self.tmp / "Scripts $& $1"
        (self.scripts / "FreeLab").mkdir(parents=True)
        self.write(MINE + b"# >>> FreeLab MCP (managed)\nold = 1\n# <<< FreeLab MCP\n" + TOOLS)
        code, out = self.run_installer("-ListenerAtLaunch")
        self.assertEqual(code, 0, out)
        text = self.read()
        self.assertEqual(text.count(b"# >>> FreeLab MCP"), 1)
        self.assertIn(b"_freelab_mcp_startup()", text)
        self.assertIn(b"Scripts $& $1", text, "the path is written as it is")

    def test_a_duplicated_block_becomes_one(self):
        block = b"# >>> FreeLab MCP (managed)\nold = 1\n# <<< FreeLab MCP\n"
        self.write(MINE + block + TOOLS + block)
        code, out = self.run_installer("-ListenerAtLaunch")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read().count(b"# >>> FreeLab MCP"), 1)

    def test_markers_in_the_wrong_order_are_refused(self):
        data = MINE + b"# <<< FreeLab MCP\nmine = 2\n# >>> FreeLab MCP (managed)\nmine = 3\n"
        self.write(data)
        code, out = self.run_installer("-ListenerAtLaunch")
        self.assertEqual((code, self.read()), (1, data), out)

    def test_removing_a_block_at_the_top_leaves_no_blank_line(self):
        self.write(b"# >>> FreeLab MCP (managed)\nold = 1\n# <<< FreeLab MCP\n" + MINE)
        code, out = self.run_installer("-Uninstall")
        self.assertEqual((code, self.read()), (0, MINE), out)

    def test_an_unchanged_file_is_not_rewritten(self):
        self.write(MINE + TOOLS)
        self.assertEqual(self.run_installer("-ListenerAtLaunch")[0], 0)
        before = self.startup.stat().st_mtime_ns
        os.utime(self.startup, ns=(before - 10 ** 9, before - 10 ** 9))
        stamp = self.startup.stat().st_mtime_ns
        self.assertEqual(self.run_installer("-ListenerAtLaunch")[0], 0)
        self.assertEqual(self.startup.stat().st_mtime_ns, stamp)

    def test_non_utf8_file_without_markers_is_left_alone_unless_a_block_is_needed(self):
        data = b"# -*- coding: cp1252 -*-\n# caf\xe9\n"
        self.write(data)
        code, out = self.run_installer()
        self.assertEqual((code, self.read()), (0, data), out)
        self.assertTrue((self.scripts / "FreeLab" / "freelab_listener.py").is_file())
        code, out = self.run_installer("-ListenerAtLaunch")
        self.assertEqual((code, self.read()), (1, data), out)
        code, out = self.run_installer("-Uninstall")
        self.assertEqual((code, self.read()), (0, data), out)

    def test_unpaired_marker_is_refused(self):
        data = MINE + b"# >>> FreeLab MCP (managed)\nuser = 1\n"
        self.write(data)
        code, out = self.run_installer("-ListenerAtLaunch")
        self.assertEqual((code, self.read()), (1, data), out)

    def test_non_utf8_file_is_refused(self):
        data = b"# caf\xe9\n"
        self.write(data)
        code, out = self.run_installer("-ListenerAtLaunch")
        self.assertEqual((code, self.read()), (1, data), out)

    def test_missing_startup_script_is_not_created(self):
        code, out = self.run_installer("-ListenerAtLaunch")
        self.assertEqual(code, 1, out)
        self.assertFalse(self.startup.exists(), "FontLab writes its own first")


if __name__ == "__main__":
    unittest.main()
