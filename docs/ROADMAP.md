# Roadmap

## Done in 1.0.0

- Every read proven live on test fonts ([FONTLAB7-API.md](FONTLAB7-API.md)), with fonts named by id, path, or name.
- Undoable writes: kerning pairs, metrics, glyph names, code points, component glyphs, anchors, and feature code, and `undo`.

## Next

- **Drawing contours** and **deleting glyphs**, each one a single History step like the writes in 1.0.0, verified by a re-read before it is reported as done. Check the FontLab 8 traps listed in [FONTLAB7-API.md](FONTLAB7-API.md) first.
- **Kerning resolved by FontLab** (`getPlainPairsDict`): the value a glyph pair actually gets through its classes.
- **Exception pairs of a glyph named like its class**, if FontLab's scripting turns out to have a way to address one (renaming the class for the moment is one, but touches the class).

Tools that run inside FontLab on their own, such as dark mode and the kerning and feature-code helpers, belong to FreeLab Script Suite, the separate `freelab-script-suite` repository, and are on its roadmap.
