# FreeLab MCP

An MCP server that lets an AI app such as Claude Code read the fonts open in FontLab 7, make a small set of undoable edits to them, and start and close FontLab when asked.

**Status: 1.2.0.** Every read and write tool of 1.1.0 was proven live in FontLab 7.2 on test fonts (TrueType and CFF, multiple masters, Japanese, Arabic, and Hebrew). What 1.2.0 adds is tested against the fakes and not yet proven live: [docs/FONTLAB7-API.md](docs/FONTLAB7-API.md) tracks each FontLab call and how far it is proven.

FreeLab Script Suite, the separate `freelab-script-suite` repository, holds tools that run inside FontLab, starting with a dark mode. Neither needs the other; installed together, they share FontLab's *Scripts > FreeLab* menu.

## What is here

- *listener/freelab_listener.py*: a script you run inside FontLab. It answers requests on `127.0.0.1` only, from FontLab's main thread.
- *server/*: the MCP server your AI app starts. It never touches FontLab directly; it passes each tool call to the listener.
- *tests/*: the test gate (*run_tests.py*).
- *tools/*: the installer and the test gate's Python 2.7 fetcher.

## Requirements

- FontLab 7 for Windows (developed against 7.2.0), with its Python working. FontLab 7 runs scripts with a **64-bit** Python 2.7 installed on the system (a 32-bit one does not work); the FontLab 7 installer offers one. If *Window > Panels > Scripting* cannot run a one-line `print(1)`, fix that first: [docs/PYTHON27.md](docs/PYTHON27.md).
- **macOS** is untried. The listener and the server are plain Python and Qt, as FontLab 7 for macOS has them, so the reads and writes should carry over; the Windows-only parts are the installer (PowerShell), `launch_fontlab` and `restart_fontlab` (they look for FontLab's Windows program), `screenshot` with `source="screen"` (Windows' PrintWindow), and the state file's access list. Off Windows the state file is kept in *~/.local/state/FreeLab*.
- Python 3.10 or newer with version 1 of the `mcp` package (`pip install "mcp>=1.2,<2"`), for the server; version 2 renamed the server API.

## Install

1. Start FontLab once, so that it writes its startup script, then quit it and run *tools/install-fontlab.ps1*. It copies the listener into FontLab's user *Scripts* folder (*Documents\FontLab\FontLab 7\Scripts\FreeLab*). With `-ListenerAtLaunch`, it also adds a marked block to FontLab's startup script that starts the listener at every launch; the original startup script is kept as *startupScript.py.freelab-backup*, and each change keeps the file it replaced as *startupScript.py.freelab-previous*. `-Uninstall` takes it all out again, leaving FreeLab's tools alone. Either way it also removes the single block that FreeLab wrote before the MCP and the tools were split, which started dark mode too; run FreeLab's installer again to keep that. The startup script must be UTF-8 text, and its line endings are kept. Scripts run from that folder, not from this repository: Python 2.7 cannot open files whose path the Windows ANSI code page cannot spell.
2. Register the server with your MCP client. For Claude Code:

   ```
   claude mcp add freelab -- python <path to this repository>\server\run_server.py
   ```

## Use

1. In FontLab, choose *Scripts > FreeLab > Start MCP Listener* (or open the file in the *Scripting* panel and run it), unless it starts at launch. The *Output* panel shows the listener's version and port. Run it again to restart it.
2. Ask the AI app, for example, "check the FontLab connection", "describe the glyph A", or "kern A and V by −40 in the font in front".

Tools:

- **Reads.** `fontlab_status`, `list_fonts` (each open font's id), `get_current_font`, `list_glyphs`, `get_glyph` (metrics, ink box, components, anchors), `get_glyph_outline` (shape-local coordinates, or with `apply_transforms` the glyph's), `get_kerning` (one pair, a page of pairs, the classes, or with `resolved` the value a glyph pair gets through its classes), and `get_features` (tags, a feature's code, the prefix). Each takes `font`: a font's id, path, PostScript name, or full name; without it, the font in front.
- **Writes.** `set_kerning_pair`, `remove_kerning_pair`, `set_glyph_metrics` (advance width and sidebearings), `rename_glyph`, `set_glyph_unicodes`, `add_component_glyph`, `set_glyph_outline` (draws a shape's contours), `delete_glyph`, `set_anchor`, `remove_anchor`, and `set_feature_code`. Each needs `font` (and takes `font_name` to confirm it), checks everything before it changes anything, makes one step in FontLab's *History*, marks the font modified, and returns the values before and after; a change FontLab did not keep is put back, or else recorded and said. `undo` takes the last step back, as *Edit > Undo* does, bringing the font to the front for it; given a write's History state and title, it takes back that write only.
- **Fonts and windows.** `open_font` opens a font file; `close_font` closes a font, refusing one with unsaved changes unless `discard_changes` is given for a font `open_font` opened; `bring_font_to_front` makes a font the one in front; `compile_features` compiles a font's feature code as the *Features* panel's *Compile* button does (opening the panel when it was never opened) and returns the compiler's errors and warnings, with an estimate of the feature and line an error names; `read_output` reads the *Output* panel; `screenshot` returns a picture of a FontLab window, as Qt draws it or, with `source="screen"`, as Windows does. When FontLab asks something on the way (*Coordinates Rounding*, *Save changes*), the question is left on screen for you and the tool says so.
- **Risky mode.** Saving (`save_font`), exporting (`export_font`), answering FontLab's questions (`answer_dialog`, `open_font`'s `answer`), throwing away changes (`close_font` for a font `open_font` did not open, `quit_fontlab` and `restart_fontlab` with `discard_changes`), and rewriting or removing a glyph's references (`rename_glyph`'s `update_references`, `delete_glyph`'s `remove_references`) are marked *[risky]* and refused until you ask the AI app for risky mode in your own words; it then calls `set_risky_mode`. `list_risky_actions` lists them; [docs/RISKY.md](docs/RISKY.md) says why each is risky.
- **Probing.** `run_python` runs code inside FontLab.
- **FontLab itself.** `launch_fontlab`, `quit_fontlab`, and `restart_fontlab` start and close FontLab when you ask; FontLab closes as its close button would: when a font has unsaved edits, FontLab asks whether to save it, and the tool leaves that question on screen for you and says so; it answers it only in risky mode, with `discard_changes`.

## What it does not do

- Outside risky mode, no tool saves or exports a font, answers FontLab's questions, or throws away changes to a font it did not open. `run_python` runs whatever code it is given, with FontLab's access to your files and the network, risky mode or not.
- `close_font` cannot see a change FontLab 7 does not report: a change made outside FontLab's *History* and left unmarked (by a script that neither records a step nor marks the font modified) leaves the font looking unchanged, so it is closed without a question and the change is lost. Every FreeLab MCP write records a step and marks the font.
- `bring_font_to_front`, `undo`, `compile_features`, and `save_font` need a window tab of the font, since they bring it to the front by choosing that tab; a font with no window open is refused (`not_found`): open one in FontLab.
- `rename_glyph` refuses a glyph that kerning, a class, or feature code uses, since FontLab's rename updates only the components that use it, unless `update_references` is given in risky mode; a class of FontLab's *Classes* panel is refused even then. `delete_glyph` refuses a glyph used as a component or named in feature code. `set_kerning_pair` cannot write a pair of a glyph whose name is also a kerning class's (FontLab names imported classes after their key glyph); it writes the class pair when given `@name`. `set_feature_code` stores code without compiling it: `compile_features` compiles it.
- `compile_features` clicks the *Features* panel's *Compile* button, since FontLab 7 has no scripting call to compile. FontLab clears its *Output* panel when it compiles. The feature and line it gives for an error are an estimate (`certain: false`).
- It is no security boundary: any program running as you can read the listener's token in *%LOCALAPPDATA%\FreeLab\listener.json*, and so run code in FontLab ([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#discovery-and-authentication)).
- FontLab 8 is untested. The listener is written to run there too.

## Tests

```
python tests/run_tests.py
```

It runs the listener and server tests under Python 3, then the listener tests under Python 2.7, which *tools/get-python27.ps1* fetches (a hash-checked copy of the official package, kept outside the repository). The gate fails if the Python 2.7 run is missing.

## More

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): processes, protocol, discovery, and error model.
- [docs/FONTLAB7-API.md](docs/FONTLAB7-API.md): the FontLab calls in use and how far each is proven.
- [docs/RISKY.md](docs/RISKY.md): risky mode and the actions it allows.
- [docs/PRIOR-ART.md](docs/PRIOR-ART.md): the existing FontLab MCP servers, and why FreeLab MCP does not build on them.
- [docs/ROADMAP.md](docs/ROADMAP.md): what comes next.

## License

**GPL-3.0-or-later** ([LICENSE](LICENSE)). Copyright © 2026 Vixen420. It comes with no warranty.
