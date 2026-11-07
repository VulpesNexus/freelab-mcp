# Roadmap

## Next

1. On a disposable test font, prove each read in [FONTLAB7-API.md](FONTLAB7-API.md) and raise its row to **live**.

## Then

- **Writes**, each one verified by a re-read before it is reported as done: width and sidebearings, Unicode values, creating and deleting glyphs, drawing contours. Check the FontLab 8 traps listed in [FONTLAB7-API.md](FONTLAB7-API.md) first.
- **Kerning reads** of explicit pairs, with class resolution done by FontLab.

Tools that run inside FontLab on their own, such as dark mode and the planned kerning and feature-code helpers, belong to FreeLab, the separate `freelab` repository, and are on its roadmap.
