# Roadmap

## Done in 1.0.0

- Every read proven live on test fonts ([FONTLAB7-API.md](FONTLAB7-API.md)), with fonts named by id, path, or name.
- Undoable writes: kerning pairs, metrics, glyph names, code points, component glyphs, anchors, and feature code, and `undo`.

## Done in 1.1.0

What FreeLab Script Suite's live probes did by hand, as tools, each proven live on test fonts: opening and closing fonts (`open_font`, `close_font`), bringing a font to the front, compiling feature code with FontLab's own compiler and reading its errors, reading the *Output* panel, and pictures of FontLab's windows.

## Done in 1.2.0 (tested on the fakes, not yet proven live)

- **Drawing contours** (`set_glyph_outline`) and **deleting glyphs** (`delete_glyph`), each one History step verified by a re-read.
- **Kerning resolved** through classes (`get_kerning` with `resolved`): FontLab's own value (`getPlainPairsDict`) and one computed from the entries, side by side.
- **Outlines with the transforms applied** (`get_glyph_outline` with `apply_transforms`).
- `undo` and `compile_features` bring the font forward and put the font that was in front back; `compile_features` opens the *Features* and *Output* panels when they were never opened, and estimates the feature and line of an error.
- The paths `open_font` opened outlive a listener restart in the same FontLab.
- `screenshot` with `source="screen"`: a picture Windows draws, taken from outside FontLab.
- **Risky mode** ([RISKY.md](RISKY.md)): saving, exporting, answering FontLab's questions, throwing away changes, and rewriting or removing a glyph's references, off unless the user asks.
- The listener's state file closed to other accounts on Windows; peers outside the loopback refused; wrong tokens throttled.

## Next

- **Prove 1.2.0 live**, as listed in [FONTLAB7-API.md](FONTLAB7-API.md#not-yet-proven-live-120).
- **A font with no window tab**: a way to bring it forward or close it (the candidates are listed there).
- **Classes of the Classes panel** in `update_references` and `remove_references`, if `fgFont.groups` proves to change them.
- **Exception pairs of a glyph named like its class**, if FontLab's scripting turns out to have a way to address one (renaming the class for the moment is one, but touches the class).

Tools that run inside FontLab on their own, such as dark mode and the kerning and feature-code helpers, belong to FreeLab Script Suite, the separate `freelab-script-suite` repository, and are on its roadmap.
