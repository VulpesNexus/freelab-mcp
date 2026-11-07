# FreeLab MCP

An MCP server that lets an AI app such as Claude Code read the font open in FontLab 7, and start and close FontLab when asked.

**Status: 0.1.0.** The listener runs in FontLab 7.2, and `fontlab_status` and `run_python` drove the whole development of FreeLab's dark mode, but the font reads are not proven live yet: [docs/FONTLAB7-API.md](docs/FONTLAB7-API.md) tracks each call.

FreeLab, the separate `freelab` repository, holds tools that run inside FontLab, starting with a dark mode. Neither needs the other; installed together, they share FontLab's *Scripts > FreeLab* menu.

## What is here

- *listener/freelab_listener.py*: a script you run inside FontLab. It answers requests on `127.0.0.1` only, from FontLab's main thread.
- *server/*: the MCP server your AI app starts. It never touches FontLab directly; it passes each tool call to the listener.
- *tests/*: the test gate (*run_tests.py*).
- *tools/*: the installer and the test gate's Python 2.7 fetcher.

## Requirements

- FontLab 7 for Windows (developed against 7.2.0), with its Python working. FontLab 7 runs scripts with a **64-bit** Python 2.7 installed on the system (a 32-bit one does not work); the FontLab 7 installer offers one. If *Window > Panels > Scripting* cannot run a one-line `print(1)`, fix that first: [docs/PYTHON27.md](docs/PYTHON27.md).
- Python 3.10 or newer with version 1 of the `mcp` package (`pip install "mcp>=1.2,<2"`), for the server; version 2 renamed the server API.

## Install

1. Start FontLab once, so that it writes its startup script, then quit it and run *tools/install-fontlab.ps1*. It copies the listener into FontLab's user *Scripts* folder (*Documents\FontLab\FontLab 7\Scripts\FreeLab*). With `-ListenerAtLaunch`, it also adds a marked block to FontLab's startup script that starts the listener at every launch; the original startup script is kept as *startupScript.py.freelab-backup*. `-Uninstall` takes it all out again, leaving FreeLab's tools alone. Either way it also removes the single block that FreeLab wrote before the MCP and the tools were split, which started dark mode too; run FreeLab's installer again to keep that. The startup script must be UTF-8 text, and its line endings are kept. Scripts run from that folder, not from this repository: Python 2.7 cannot open files whose path the Windows ANSI code page cannot spell.
2. Register the server with your MCP client. For Claude Code:

   ```
   claude mcp add freelab -- python <path to this repository>\server\run_server.py
   ```

## Use

1. In FontLab, choose *Scripts > FreeLab > Start Listener* (or open the file in the *Scripting* panel and run it), unless it starts at launch. The *Output* panel shows the listener's version and port. Run it again to restart it.
2. Ask the AI app, for example, "check the FontLab connection" or "describe the glyph A".

Tools: `fontlab_status`, `get_current_font`, `list_glyphs`, `get_glyph`, `get_glyph_outline`, and `run_python`, which runs code inside FontLab for probing. `launch_fontlab`, `quit_fontlab`, and `restart_fontlab` start and close FontLab when you ask; quitting is refused while the workspace has changes or any open font has been edited since it was opened (FontLab 7.2 does not report unsaved edits, so an edited font counts even once saved): close FontLab yourself then.

## What it does not do

- It does not write to fonts through dedicated tools yet; only `run_python` can, and only when asked to.
- It never saves, exports, or closes a font.
- It reads the font in front only, and outline coordinates are shape-local: each shape's transform is reported, not applied.
- FontLab 8 is untested. The listener is written to run there too.

## Tests

```
python tests/run_tests.py
```

It runs the listener and server tests under Python 3, then the listener tests under Python 2.7, which *tools/get-python27.ps1* fetches (a hash-checked copy of the official package, kept outside the repository). The gate fails if the Python 2.7 run is missing.

## More

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): processes, protocol, discovery, and error model.
- [docs/FONTLAB7-API.md](docs/FONTLAB7-API.md): the FontLab calls in use and how far each is proven.
- [docs/PRIOR-ART.md](docs/PRIOR-ART.md): the existing FontLab MCP servers, and why FreeLab MCP does not build on them.
- [docs/ROADMAP.md](docs/ROADMAP.md): what comes next.
