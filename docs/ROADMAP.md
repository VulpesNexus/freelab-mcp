# Roadmap

Nothing is outstanding, and nothing else is planned: everything below is done, and the [known limits](#known-limits) say what FreeLab MCP leaves as it is, and why.

## Done in 1.0.0

- Every read proven live on test fonts ([FONTLAB7-API.md](FONTLAB7-API.md)), with fonts named by id, path, or name.
- Undoable writes: kerning pairs, metrics, glyph names, code points, component glyphs, anchors, and feature code, and `undo`.

## Done in 1.1.0

What FreeLab Script Suite's live probes did by hand, as tools, each proven live on test fonts: opening and closing fonts (`open_font`, `close_font`), bringing a font to the front, compiling feature code with FontLab's own compiler and reading its errors, reading the *Output* panel, and pictures of FontLab's windows.

## Done in 1.2.0 (proven live on 2026-11-19)

Proven live on test fonts, with what FontLab did, in [FONTLAB7-API.md](FONTLAB7-API.md#120-found-live-2026-11-19). The live run fixed four things (a step of a font behind another got no title, so writes now record with their font in front; the compiler's line counts the prefix without the *Classes* panel's lines; smoothness is set after the node types; a quit closes FontLab's *Welcome* window) and Codex's review of 1.2.0 nine more (a close's or an open's answers outliving it, an answer reaching another question, an exemption bound to a path rather than a font, a write reported done though its History step failed, the compile output read again, feature code rewritten as words rather than glyph names, a new shape read from itself rather than the layer, an unbounded flattening of kerning, the token file made before it was private, and a GDI bitmap leaked on a failed capture).

- **Drawing contours** (`set_glyph_outline`) and **deleting glyphs** (`delete_glyph`), each one History step verified by a re-read.
- **Kerning resolved** through classes (`get_kerning` with `resolved`): FontLab's own value (`getPlainPairsDict`) and one computed from the entries, side by side.
- **Outlines with the transforms applied** (`get_glyph_outline` with `apply_transforms`).
- `undo` and `compile_features` bring the font forward and put the font that was in front back; `compile_features` opens the *Features* and *Output* panels when they were never opened, and estimates the feature and line of an error.
- The paths `open_font` opened outlive a listener restart in the same FontLab.
- `screenshot` with `source="screen"`: a picture Windows draws, taken from outside FontLab.
- **Risky mode** ([RISKY.md](RISKY.md)): saving, exporting, answering FontLab's questions, throwing away changes, and rewriting or removing a glyph's references, off unless the user asks.
- The listener's state file closed to other accounts on Windows; peers outside the loopback refused; wrong tokens throttled.

## Done in 1.3.0 (live since 2026-11-19)

- **Window tools** ([WINDOW-TOOLS.md](WINDOW-TOOLS.md)): any FontLab window driven as a user would, FreeLab Script Suite's tool windows included, without `run_python`: windows and their controls listed with stable addresses, clicks, values, list rows, texts with their links, menu commands, and `wait_idle` for the work an action starts. Actions run from a timer after the reply and are checked again then; questions of FontLab's and commands that save, export, close, or revert need risky mode; optional conventions (`objectName`, `mcpBusy`, `mcpScriptWindow`, `mcpRisky`) let a script be driven more surely.

- **Vertical metrics** (`get_vertical_metrics`, live 2026-11-27): each master's typo, hhea, and win values read from FontLab's model, as its export writes them, beside the font info copy the export overwrites ([FONTLAB7-API.md](FONTLAB7-API.md#font-info-and-vertical-metrics-as-exported-found-live-2026-11-27)).
- **Whether the user is at FontLab** (`fontlab_status`'s `user`, live 2026-11-27): seconds since the last input, and whether FontLab is in front, without reading another program's window title.

## Known limits

Nothing is outstanding, and nothing else is planned. What 1.3.0 leaves as it is, and why:

- **A floating panel of FontLab's** is not proven to be listed and walked by the window tools: FontLab 7 floats a panel by a drag only, which the tools do not do ([FONTLAB7-API.md](FONTLAB7-API.md#the-window-tools-130-still-to-prove-live)). A font tab undocked by *Window > Docking* is proven.
- **Proven on fakes, not reproducible live** ([FONTLAB7-API.md](FONTLAB7-API.md#not-yet-proven-live)): `answer_dialog` refusing a question swapped for another within its 50 ms, a new shape the layer does not take, `set_prefix` inside a rename, and the state file held by a second account. The ordinary case of each is proven live.
- **A font with no window tab** is refused (`not_found`) by the tools that bring a font to the front: open a tab of it in FontLab. No call found reaches such a font ([FONTLAB7-API.md](FONTLAB7-API.md#a-font-with-no-window-tab-candidates)).
- **A class of the *Classes* panel** is refused by `update_references` and `remove_references`: no call to change one is proven ([FONTLAB7-API.md](FONTLAB7-API.md#classes-of-the-classes-panel-candidate)).
- **An exception pair of a glyph named like its class** cannot be written: FontLab takes such a name as the class. `set_kerning_pair` writes the class pair given `@name`.
- **Pictures in glyphs** have no tool: what FontLab gives of them is recorded ([FONTLAB7-API.md](FONTLAB7-API.md#the-canvas-and-pictures-found-live-2026-11-25)), and `run_python` reaches them.

Tools that run inside FontLab on their own, such as dark mode and the kerning and feature-code helpers, belong to FreeLab Script Suite, the separate `freelab-script-suite` repository.
