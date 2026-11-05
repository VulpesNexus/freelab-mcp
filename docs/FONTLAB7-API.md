# FontLab 7 API in use

Every FontLab member the listener touches, where it comes from, and how far it is proven. The source is FontLab's own FontLab 7 reference, archived at [fontlabcom/fontlab-python-docs](https://github.com/fontlabcom/fontlab-python-docs) under *docs/archive/fontlab7/* (*fontlab.html*, *fontgate.html*), and the module-level names FontLab 7 imports in its own *startupScript.py*.

Proof levels: **documented** (in the FontLab 7 reference), **stub-tested** (the listener uses it and the spy fakes accept only it), **live** (run in FontLab 7.2 and checked against what FontLab shows). Nothing is live yet.

| Object | Member | Kind | Used by | Level |
|---|---|---|---|---|
| `fontlab` module | `CountFonts()` | function | `status`, every font op | documented (startup script), stub-tested |
| `fontlab` module | `ApplicationPath()` | function | `ping` | documented (startup script), stub-tested |
| `fontlab.flWorkspace` | `instance()` | static method | every font op | documented, stub-tested |
| `flWorkspace` | `currentPackage` | property, `flPackage` | every font op | documented, stub-tested |
| `flPackage` | `tfn`, `tsn`, `psn`, `ffn`, `path`, `upm`, `glyphsCount`, `master`, `mastersCount`, `versionMajor`, `versionMinor`, `hasChanges`, `isNull` | properties | `font_info`, `status` | documented, stub-tested |
| `flPackage` | `findName(name)` | method, `flGlyph` | `glyph`, `outline` | documented, stub-tested |
| `flPackage` | `findUnicode(codepoint)` | method, `flGlyph` | `glyph`, `outline` | documented, stub-tested |
| `flPackage` | `fgPackage` | property, `fgFont` | `glyph_names` | documented, stub-tested |
| `fgFont` | `keys()` | method, list of names | `glyph_names` | documented, stub-tested |
| `flGlyph` | `name`, `layers` (list), `activeLayer`, `fgGlyph`, `mark`, `tags`, `note`, `isNull` | properties | `glyph`, `outline` | documented, stub-tested |
| `fgGlyph` | `unicodes` | property, list of int | `glyph` | documented, stub-tested |
| `flLayer` | `name`, `shapes` (list), `advanceWidth`, `boundingBox` (`QRectF`), `shapesCount`, `isNull` | properties | `glyph`, `outline` | documented, stub-tested |
| `flLayer` | `nodesCount()` | method, int | `glyph` | documented, stub-tested |
| `flShape` | `name`, `isComponent`, `transform` (`QTransform`), `contours` (list) | properties | `outline` | documented, stub-tested |
| `flContour` | `closed` | property | `outline` | documented, stub-tested |
| `flContour` | `nodes()` | method, list of `flNode` | `outline` | documented, stub-tested |
| `flNode` | `x`, `y`, `type`, `smooth` | properties | `outline` | documented, stub-tested |
| `PythonQt.QtCore` | `QTimer`: `setInterval`, `connect("timeout()", …)` (falling back to `timeout.connect(…)` if that raises or returns `False`), `start`, `stop` | Qt | the listener loop | not stub-tested; first live run |

## Corrections to the earlier FontLab MCP

The calls in Dithilli's server ([PRIOR-ART.md](PRIOR-ART.md)) do not match FontLab 7's documented API: `flWorkspace.instance().currentFont()` does not exist (the current font is the `currentPackage` property), `flPackage` has no `glyphs` list or `info.familyName` (names are `tfn`, `tsn`, …), and `flGlyph` has no `unicode` (Unicode values live on the legacy `fgGlyph`).

## FontLab 8 findings to check on FontLab 7

Glyphbridge-FontLab-MCP records these for FontLab 8 in its *mental-model/FONTLAB-API-RULES.md*. FontLab 8 descends from FontLab 7, so treat each as likely until a live run on FontLab 7 confirms or refutes it.

- Indexing a native object (`layer[0]`, `font.shapes.get(…)`) crashed FontLab natively, past `try`.
- `flPackage()` with no argument creates a new font.
- The legacy margin setters (`glyph.leftMargin = …`) crashed FontLab on a glyph with API-built shapes; the legacy width write lost work.
- After a write through the modern API, `fontlab.Update(<flGlyph>)` syncs the legacy view; the legacy `font.update()` can throw unsynced work away.
- `flLayer.boundingBox` is a property, not a method.
- GUI-made kerning lives in `flPackage.kerning(<master>)`; the legacy `font.kerning` can be empty while FontLab applies kerning.

## Proving a call live

One call per probe, through `run_python`, on a disposable test font: read the object's type and the call's docstring first, then make the call once, check the result's type, and compare it with what FontLab shows. Record the FontLab build and the result here, then raise the row to **live**.
