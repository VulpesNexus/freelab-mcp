# FreeLab MCP

An MCP server that lets an AI app such as Claude Code read the fonts open in FontLab 7, make a small set of undoable edits to them, drive FontLab's windows as a user would, and start and close FontLab when asked.

**Status: 1.3.0.** Every tool of 1.2.0 was proven live in FontLab 7.2 on test fonts (TrueType and CFF, multiple masters, Japanese, Arabic, and Hebrew); the window tools, new in 1.3.0, drove a whole FreeLab Script Suite workflow live on 2026-11-19; only a floating panel of FontLab's is not yet proven. [docs/FONTLAB7-API.md](docs/FONTLAB7-API.md) tracks each FontLab call, how far it is proven, and what is not yet.

[FreeLab Script Suite](https://github.com/VulpesNexus/freelab-script-suite), a separate repository, holds tools that run inside FontLab: a dark mode, kerning and spacing, glyph building, feature code, and font checks. Neither needs the other; installed together, they share FontLab's *Scripts > FreeLab* menu, and the window tools drive the suite's tool windows as a user would.

## What is here

- *listener/freelab_listener.py*: a script you run inside FontLab. It answers requests on `127.0.0.1` only, from FontLab's main thread.
- *server/*: the MCP server your AI app starts. It never touches FontLab directly; it passes each tool call to the listener.
- *tests/*: the test gate (*run_tests.py*).
- *docs/*: the design, the FontLab calls in use, and the safety rules, listed under [More](#more).
- *tools/*: the installer and the test gate's Python 2.7 fetcher.

## Requirements

- FontLab 7 for Windows (developed against 7.2.0), with its Python working. FontLab 7 runs scripts with a **64-bit** Python 2.7 installed on the system (a 32-bit one does not work); the FontLab 7 installer offers one. If *Window > Panels > Scripting* cannot run a one-line `print(1)`, fix that first: [docs/PYTHON27.md](docs/PYTHON27.md).
- **macOS** is untried. The listener and the server are plain Python and Qt, as FontLab 7 for macOS has them, so the reads and writes should carry over; the Windows-only parts are the installer (PowerShell), `launch_fontlab`, `restart_fontlab`, and `quit_fontlab` (they find FontLab's Windows program and process), `fontlab_status`'s `user`, `screenshot` with `source="screen"` (Windows' PrintWindow), and the state file's access list. Off Windows the state file is kept in *~/.local/state/FreeLab*.
- Python 3.10 or newer with version 1 of the `mcp` package (`pip install "mcp>=1.2,<2"`), for the server; version 2 renamed the server API.

## Install

1. Download a [release](https://github.com/VulpesNexus/freelab-mcp/releases)'s source code and unpack it, or clone this repository.
2. Install the server's one dependency: `pip install "mcp>=1.2,<2"`.
3. Quit FontLab, and copy *listener/freelab_listener.py* into a *FreeLab* folder in FontLab's *Scripts* folder (*Documents\FontLab\FontLab 7\Scripts\FreeLab*; make the folder if FreeLab Script Suite has not). That is all the listener needs.
4. Optional, in place of step 3: run *tools/install-fontlab.ps1* in PowerShell. It copies the same file, and with `-ListenerAtLaunch` (which needs FontLab to have started once and written its startup script) it also makes FontLab start the listener at every launch. `-Uninstall` takes it all out again, leaving FreeLab Script Suite's tools alone.
5. Register the server with your MCP client. For Claude Code:

   ```
   claude mcp add freelab -- python <path to this repository>\server\run_server.py
   ```

What the installer does to FontLab's startup script:

- With `-ListenerAtLaunch`, it adds a marked block to it. The original startup script is kept as *startupScript.py.freelab-backup*, and each change keeps the file it replaced as *startupScript.py.freelab-previous*. The startup script must be UTF-8 text, and its line endings are kept.
- Without `-ListenerAtLaunch`, it takes out a FreeLab MCP block an earlier run added, so FontLab no longer starts the listener at launch: give `-ListenerAtLaunch` on every run to keep it.
- Either way, it removes the single block that FreeLab wrote before the MCP and the tools were split, which started dark mode too (run FreeLab Script Suite's installer again to keep that), and an old copy of the listener left directly in the *Scripts* folder.

The listener runs from FontLab's *Scripts* folder, not from this repository: Python 2.7 cannot open files whose path the Windows ANSI code page cannot spell.

## Use

1. In FontLab, choose *Scripts > FreeLab > Start MCP Listener* (or open the file in the *Scripting* panel and run it), unless it starts at launch. The *Output* panel shows the listener's version and port. Run it again to restart it.
2. Ask the AI app, for example, "check the FontLab connection", "describe the glyph A", or "kern A and V by −40 in the font in front".

Tools:

- **Reads.** `fontlab_status` (with, on Windows, how long since the user last typed or moved the mouse, and whether FontLab is in front, so an AI app can hold off while the user works in FontLab), `list_fonts` (each open font's id), `get_current_font`, `get_vertical_metrics` (each master's typo, hhea, and win values as FontLab's export writes them, and Use Typo Metrics), `list_glyphs`, `get_glyph` (metrics, ink box, components, anchors), `get_glyph_outline` (shape-local coordinates, or with `apply_transforms` the glyph's), `get_kerning` (one pair, a page of pairs, the classes, or with `resolved` the value a glyph pair gets through its classes), and `get_features` (tags, a feature's code, the prefix). Each takes `font`: a font's id, path, PostScript name, or full name; without it, the font in front.
- **Writes.** `set_kerning_pair`, `remove_kerning_pair`, `set_glyph_metrics` (advance width and sidebearings), `rename_glyph`, `set_glyph_unicodes`, `add_component_glyph`, `set_glyph_outline` (draws a shape's contours), `delete_glyph`, `set_anchor`, `remove_anchor`, and `set_feature_code`. Each needs `font` (and takes `font_name` to confirm it), checks everything before it changes anything, makes one step in FontLab's *History* (bringing the font to the front for that moment, since FontLab titles a step only for the font in front), marks the font modified, and returns the values before and after; a change FontLab did not keep, or whose step the History refused, is put back, or else recorded and said. `undo` takes the last step back, as *Edit > Undo* does, bringing the font to the front for it; given a write's History state and title, it takes back that write only.
- **Fonts and windows.** `open_font` opens a font file; it refuses one FontLab 7.2 is known to crash on (a `GPOS` single adjustment with no values, as in Noto Sans JP's variable font), and with `safe_copy` opens a fixed copy of it from FreeLab's own folder instead, leaving your file as it is ([docs/FONTLAB7-API.md](docs/FONTLAB7-API.md#what-open_font-does-about-it)); `close_font` closes a font, refusing one with unsaved changes unless `discard_changes` is given for a font `open_font` opened; `bring_font_to_front` makes a font the one in front; `compile_features` compiles a font's feature code as the *Features* panel's *Compile* button does (opening the panel when it was never opened) and returns the compiler's errors and warnings, with an estimate of the feature and line an error names; `read_output` reads the *Output* panel; `screenshot` returns a picture of a FontLab window, as Qt draws it or, with `source="screen"`, as Windows does. When FontLab asks something on the way (*Coordinates Rounding*, *Save changes*), the question is left on screen for you and the tool says so.
- **Windows and their controls.** `list_windows` and `list_window_controls` list FontLab's windows (its own and scripts', FreeLab Script Suite's tool windows among them) and their controls, each with a stable address; `click`, `set_value`, `click_link`, and `run_menu_command` act on them as a user would (a button, a check box, a dropdown's entry, a field, a tab, a list's row, a link in a log, *Scripts > FreeLab > Font Check*), a moment after the call; `read_list`, `read_text`, and `list_menu` read them; `wait_idle` waits until the work an action started is over and says what it left. While a dialog waits only it can be driven, and nothing runs while other work does. [docs/WINDOW-TOOLS.md](docs/WINDOW-TOOLS.md) describes the addresses, the safety rules, and the conventions a script can follow to be driven more surely.
- **Risky mode.** Saving (`save_font`), exporting (`export_font`), answering FontLab's questions (`answer_dialog`, `open_font`'s `answer`), throwing away changes (`close_font` for a font `open_font` did not open, `quit_fontlab` and `restart_fontlab` with `discard_changes`), rewriting or removing a glyph's references (`rename_glyph`'s `update_references`, `delete_glyph`'s `remove_references`), and, through the window tools, a control of FontLab's questions, a command of its *File* menu, or a command named Save, Export, Close, Quit, Exit, Revert, or Discard are marked *[risky]* and refused until you ask the AI app for risky mode in your own words; it then calls `set_risky_mode`. `list_risky_actions` lists them; [docs/RISKY.md](docs/RISKY.md) says why each is risky.
- **Probing.** `run_python` runs code inside FontLab.
- **FontLab itself.** `launch_fontlab`, `quit_fontlab`, and `restart_fontlab` start and close FontLab when you ask. `launch_fontlab` starts *C:\Program Files\Fontlab\FontLab 7\FontLab 7.exe*, or the program the `FREELAB_FONTLAB_EXE` environment variable names. `quit_fontlab` is refused while FontLab reports unsaved changes, as it does after any FreeLab MCP write (`unsaved_changes`). A font edited only in a way FontLab does not report makes FontLab ask whether to save it as it closes; the tool leaves that question on screen for you and says so. With `discard_changes`, in risky mode only, nothing is refused and it answers *Don't Save*.

## What it does not do

- Outside risky mode, no tool saves or exports a font, answers FontLab's questions, or throws away changes to a font it did not open. `run_python` runs whatever code it is given, with FontLab's access to your files and the network, risky mode or not.
- `close_font` cannot see a change FontLab 7 does not report: a change made outside FontLab's *History* and left unmarked (by a script that neither records a step nor marks the font modified) leaves the font looking unchanged, so it is closed without a question and the change is lost. Every FreeLab MCP write records a step and marks the font.
- `bring_font_to_front`, `undo`, `compile_features`, `close_font`, and `save_font` without a path need a window tab of the font, since they bring it to the front by choosing that tab (unless it is in front already); a font with no window tab is refused (`not_found`): open one in FontLab.
- `rename_glyph` refuses a glyph that kerning, a class, or feature code uses, since FontLab's rename updates only the components that use it, unless `update_references` is given in risky mode; a class of FontLab's *Classes* panel, and a glyph range in feature code that may hold the glyph (`[A-C]`), are refused even then. `delete_glyph` refuses a glyph used as a component or named in feature code. `set_kerning_pair` cannot write a pair of a glyph whose name is also a kerning class's (FontLab names imported classes after their key glyph); it writes the class pair when given `@name`. `set_feature_code` stores code without compiling it: `compile_features` compiles it.
- The window tools do not open context menus, type keys, drag, scroll, select several rows at once, close a window by its title bar, or drive Windows' own file dialogs ([docs/WINDOW-TOOLS.md](docs/WINDOW-TOOLS.md#what-it-does-not-do)).
- `open_font`'s crash check looks for one known FontLab 7.2 crash, not for every font FontLab cannot open. It reads TrueType, OpenType, collections, and WOFF; a WOFF2 only where the listener's Python has `brotli` 1.2 or later (FontLab's has none), and a WOFF2 is never copied: convert it to TTF or OTF first. When part of a file could not be read and a crasher was found elsewhere in it, `safe_copy` writes the fixed copy but does not open it (FontLab may still crash in the part not read, losing the unsaved work in every open font): the error names the copy, to open once your work is saved. *File > Open Fonts* in FontLab itself is not checked.
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
- [docs/WINDOW-TOOLS.md](docs/WINDOW-TOOLS.md): the window tools, their safety rules, and the conventions a script can follow.
- [docs/PRIOR-ART.md](docs/PRIOR-ART.md): the existing FontLab MCP servers, and why FreeLab MCP does not build on them.
- [docs/ROADMAP.md](docs/ROADMAP.md): what comes next.

## License

**GPL-3.0-or-later** ([LICENSE](LICENSE)). Copyright © 2026 Vixen420. It comes with no warranty.
