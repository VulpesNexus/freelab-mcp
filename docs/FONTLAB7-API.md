# FontLab 7 API in use

Every FontLab member the listener touches, where it comes from, and how far it is proven. The source is FontLab's own FontLab 7 reference, archived at [fontlabcom/fontlab-python-docs](https://github.com/fontlabcom/fontlab-python-docs) under *docs/archive/fontlab7/* (*fontlab.html*, *fontgate.html*), and the module-level names FontLab 7 imports in its own *startupScript.py*.

Proof levels: **documented** (in the FontLab 7 reference), **stub-tested** (the listener uses it and the spy fakes accept only it), **live** (run in FontLab 7.2 and checked against what FontLab shows). Only the listener loop is live so far; the font reads still need a test font.

| Object | Member | Kind | Used by | Level |
|---|---|---|---|---|
| `fontlab` module | `CountFonts()` | function | `status`, every font op | documented (startup script), stub-tested |
| `fontlab` module | `ApplicationPath()` | function | `ping` | documented (startup script), stub-tested |
| `fontlab.flWorkspace` | `instance()` | static method | every font op | documented, stub-tested |
| `flWorkspace` | `currentPackage` | property, `flPackage` | every font op | documented, stub-tested |
| `flWorkspace` | `hasChanges` | property, bool | `unsaved`, `quit` | documented, stub-tested, **live** (2026-11-07): a bool |
| `flWorkspace` | `packages()` | method, list of `flObject`, one per open font | `unsaved`, `quit` | documented, stub-tested, **live** (2026-11-07): a real Python list of `flObject` |
| `flObject` (a package) | `hasChanges` | property, bool | `unsaved`, `quit` | stub-tested, **live** (2026-11-07): stayed `False` after an edit to the font, as did `isChanged`, the workspace's `hasChanges`, and every window's `windowModified` |
| `flObject` (a package) | `hasHistoryStates` | property, bool | `unsaved`, `quit` | documented, stub-tested, **live** (2026-11-07): `True` after that edit, and FontLab asked whether to save the font on closing: `quit` goes ahead and leaves that question on screen for the user (it may stay `True` after saving, and the question then simply does not appear) |
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
| `PythonQt.QtCore` | `QTimer`: `setInterval`, `setSingleShot`, `connect("timeout()", …)` (falling back to `timeout.connect(…)` if that raises or returns `False`), `start`, `stop` | Qt | the listener loop, `quit` | **live** (2026-11-06): `connect("timeout()", ...)` works |
| `PythonQt.QtGui` | `QApplication.instance()`, `topLevelWidgets()`, `metaObject().className()`, `close()` | Qt | `quit` | stub-tested; **live** (2026-11-07): the main window's class is `MainWindow` |
| `PythonQt.QtGui` | `QApplication.activeModalWidget()`, its labels' and buttons' `text` | Qt | `dialog` | **live** (2026-11-07): closing with an edited font shows `DlgClosePackage`, *FontLab Warning*, *Save changes to "…" before closing?*, with *Don't Save*, *Cancel*, and *OK* |

## Found live in FontLab 7.2 (2026-11-06)

- *%LOCALAPPDATA%\FontLab\FontLab 7\startupScript.py* runs at launch, and its `print` output reaches the *Output* panel.
- A script picked from the *Scripts* menu runs with `__file__` set to its path; modules it loads stay in `sys.modules` between runs.
- PythonQt exposes Qt properties as values, not methods (`widget.styleSheet`, `widget.visible`), and `findChildren(QWidget)` returns an empty list.
- `QMainWindow.statusBar()` creates a status bar when the window has none, and FontLab's main window has none: a probe must not call it.

## Kerning and outlines, found live (2026-11-08)

Probes for FreeLab's kerner (read-only, except the writes noted, made in memory and undone):

- `flPackage.kerning(master)` returns an `fgKerning`: `keys()`, `items()` (pairs as `fgKerningObjectPair` with values), `asDict()`, `get(('left', 'right'))`, item assignment, `remove(pair)`, `getPlainPairs`, `getPlainPairsDict`, `setPlainPairs`, `groups` (`fgKerningGroups`), `addGroup`, `renameGroup`, `hasGroupKerning()`, `scale`.
- **Reading** (2026-11-08): `get(('left', 'right'))` reads that pair's own entry only and misses kerning classes: a font kerned by classes gives `None` for pairs FontLab kerns. `getPlainPairsDict()` resolves the classes: every kerned glyph pair, flattened, with the value FontLab applies (47,224 pairs in 0.02 s on a font of 2,361 entries). `get` and assignment refuse keys of `(name, is class)` tuples and `fgKerningObjectPair`s in practice.
- **Writing** (2026-11-08, in memory, then undone): `kerning[(a, b)] = value` takes a name that is also a class's name as the class, even when a glyph has that name (writing `('A', 'V')` with a class *A* kerns the whole class). `remove((a, b))` removes an entry the same way; `del kerning[pair]` removes it but raises. `renameGroup(old, new)` keeps the class's pairs, so a glyph named like a class can be written with the class renamed for the moment. `groups[name] = (members, flag)` makes a class, or replaces one and keeps its pairs; the flag `KernLeft` means first in a pair, `KernRight` second, `KernBothSide` either.
- **Unsaved state**: a script's kerning write does not mark the font modified (`isChanged` and `hasChanges` stay `False`), so FontLab may close it without asking. A script that writes should call `package.setFlag(2)` (`ofChanged`, from `flObjectFlags`). `package.changed` is a method that marks the font modified: never call it to read it.
- `fontlab.flPackage()` registers a new, empty font in the workspace and makes it the current one: a probe must not make one in the user's session. Closing such fonts while one of them was current crashed FontLab on its next repaint.
- `flPackage.getAllGroups()` lists `(kernGroup, name, id, mark, glyph names)`; feature classes come back with `kernGroup` False.
- `flPackage.autoKern(left_glyph, right_glyph, master_name)` is FontLab's own pair autokern: it returns the pair's value (as text or a number) without writing it (FreeLab's Auto Kerning calls it, 2026-11-08).
- `flGlyph` has no `layer(name)`: use `getLayerByName(master)` or `activeLayer`. `flLayer.shapes` holds `flShape`s with a `transform` (`QTransform`) and `contours`; `flContour.path()` returns a `QPainterPath`, and `draw(pen)` and `drawPoints(point_pen)` follow the pen protocols.

## History, notifications, and layers, found live (2026-11-14)

Proof level **live**: FontLab 7.2.0, test fonts from the corpus (Varela Round, Bellefair, Cardo, IBM Plex Sans JP, Noto Sans Hebrew), each unsaved; FreeLab's tools rely on these facts.

- **History is per object.** `currentHistoryState()`, `initialHistoryState()`, and `historyStates(True)` are readable on a package, a glyph, and a layer. A font opened afresh has one state (`[0]`) on the package and none on its glyphs or layers. A step of `flItems.notifyChangesApplied(title, objects, True)` records each object as it is after the change, and Undo puts back each object's state before that step: an object with none stays as written while the History moves back (Bellefair: `O`'s width 818 → 828 stayed 828 on Undo). The font's own state (its kerning, its feature code) is recorded at opening, so a first kerning or feature write does go on Undo (Bellefair: the pair and `ss07` went, and came back on Redo).
- **Recording the font before its first write makes that write undoable.** `notifyChangesApplied(u"… (before)", package, True)`, then the change, then the step: Undo put the width back (Varela Round `H`: 755 → 765 → 755, Redo 765, Undo 755).
- **The package recorded recursively holds its layers, not its glyphs.** After it, every layer has a state; a glyph's own properties (its flag, `glyph.mark`) still have none, and Undo left a flag on. `notifyChangesApplied` takes a list mixing the package and glyphs (`[package, glyph]`): recorded so, a flag went on Undo and came back on Redo.
- **Its cost is a locked clipboard's, not the font's.** Every `notifyChangesApplied` took 17.3 s, whether it recorded one glyph or the whole package, on 151, 824, 3,845, and 9,505 glyphs (FontLab's CPU time over the call: 0.16 s). Another program held the clipboard open (`OpenClipboard` refused with error 5 in every process; Chrome was its last owner), and one `QClipboard.mimeData().hasImage()` took 4.3 s: the step checks the clipboard four times. Recording the whole package instead of one glyph cost 0.18 s more on 3,845 glyphs and 0.26 s on 9,505. `notifyPackageContentUpdated(fgPackage.id)` took 8.6 s for the font shown (two checks) and 0.0 s for another; Undo and Redo through `actionUndo` and `actionRedo` took 8.8 s each; `notifyHistoryChanged()` and `notifyPackageUpdated(id)` took 0.01 s.
- **The Font window redraws changed glyphs after the step alone.** A flag set and recorded showed in the glyph's cell without `notifyPackageContentUpdated`, which is needed only to show glyphs added or removed.
- **A layer whose shapes were replaced keeps its old box until `update()`.** After `removeAllShapes()` and `addShape(new)`, the new shape's `boundingBox` was right but the layer's stayed the old one (IBM Plex Sans JP `zero.sups`: 48.9 to 266.1, at every Strokes value) until `layer.update()` (35.0 to 294.0). A component pointing to that glyph kept its old outline even then, through `shape.update()`, `resetInkBox()`, the glyph's `update()`, and `notifyPackageUpdated`: only a component made again (`setGlyphComponents`) took the new one.
- **`flContour.clone()` copies the nodes.** Moving a clone's nodes left the source contour's nodes and box as they were (two fonts, five builds).
- **`requestLoadingFont(path)`** may stop on a modal *Coordinates Rounding* question (Cardo): the call returns only once it is answered.

## Vertical metrics, found live (2026-11-14)

Proof level **live**: FontLab 7.2.0, IBM Plex Sans JP from the corpus (a TrueType font whose file has `vhea` and `vmtx`: ascent 500, descent -500, every kana and kanji 1000 high, あ's top sidebearing 68), unsaved; read-only probes except the writes noted, which were undone.

- **FontLab drops the vertical tables when it opens a font file.** Every layer read `advanceHeight` 0.0 and `hasAdvanceHeight` `False`; the legacy `fgGlyph.height` and `fgGlyph.vsb` read 0.0; the font info's `openTypeVheaVertTypoAscender`, `…Descender`, `…LineGap`, and `openTypeVheaCaret*` read `None`. `fgPackage.tables` (an `fgMapBytes`) kept only `BASE`, `GPOS`, and `GSUB`. Its *Open* preferences list `vmtx` among the tables not decompiled into the *Tables* panel.
- **`flLayer.advanceHeight` is the one per-glyph vertical metric scripts can set.** A double; `hasAdvanceHeight` is a bool property (calling it raises), `True` once any value is set, 0 included. Set on a layer and recorded, Undo put it back (1000 → 0, `hasAdvanceHeight` back to `False`). `fgGlyph.height` did not follow it (stayed 0.0).
- **No vertical origin is exposed.** `flLayer`, `flGlyph`, and `flPackage` have no member for one (no `vsb`, origin, or VORG); only the legacy `fgGlyph.vsb`, which reads 0.0 and was not written. `FontLab 7.exe` holds the AFDKO feature compiler's `vmtx` keywords (`VertOriginY`, `VertAdvanceY`, `VertTypoAscender`) and its messages (*VertOriginY redefined for glyph*), so its compiler may take a `table vmtx { … } vmtx;` block; unproven, as is what *File > Export* writes for `vhea`, `vmtx`, and `VORG` (no export was run).
- **The vhea values are settable, and `None` unsets one.** `info.openTypeVheaVertTypoAscender = 500.0` read back 500.0, and `= None` read back `None` (the font info is outside FontLab's History, as the OS/2 values are).
- **A turned component is kept as given.** `fontlab.GlyphComponent("uni300C")` with `transform = QTransform(0, -1, 1, 0, 120, 880)` (a quarter turn clockwise about the em's center, 500 by 380), set by `setGlyphComponents` on a glyph never added to the font: one component, its transform read back unchanged, the layer's box 230 to 955 across and -64 to 226 up (the source's 654 to 944 and 110 to 835, turned).
- **In the font too, and undone in one step** (FreeLab CJK Vertical's write, run from a hidden window): 53 glyphs added (`addGlyph`, then `setGlyphComponents` with a moved or turned component each, and `advanceHeight` 1000), `vert`, `vrt2`, and `vpal` set, and `advanceHeight` set on all 9,507 layers, between a recorded "(before)" step and one `notifyChangesApplied(title, [package] + glyphs, True)`; one *Edit > Undo* (through `actionUndo`, 2.4 s) took the 53 glyphs, the three features, and every advance height back (`hasAdvanceHeight` `False` again). `notifyPackageContentUpdated` after such a step left the advance heights as set. That font had been read glyph by glyph before; on fonts opened afresh (Zen Old Mincho, IBM Plex Sans JP and KR), advance heights set on every layer, layer by layer, were gone once the write was over in all but a few glyphs (Zen Old Mincho: 256 of 7,866), though each read back right after it was set, and setting or reading single glyphs by hand kept them. Calling each changed glyph's `update()` after setting its layers kept every one (Zen Kaku Gothic New: 7,792 of 7,792). The font's whole feature code with those blocks, saved from `get_prefix()` and `get_feature()`, built with fontTools' feaLib against the font's glyph names; FontLab's own *Compile* was not run.
- **A flipped component is kept too**: `QTransform(0, -1, -1, 0, 880, 880)` (a quarter turn clockwise, then mirrored) on a component of `uni301C` drew exactly the box of the font's own `uniFF5E.vert` (400 to 600 across, 24 to 736 up).
- **FontLab 7 exports no vertical metrics** (2026-11-14, IBM Plex Sans KR, `flWorkspace.exportFont(package, ExportControl)` with `profileName` *OpenType TT* or *OpenType PS*, `destinationMode` 3, into a temp folder; each export ends on a *Font Exported* message that waits for its button): no vhea, vmtx, or VORG table, with every layer's `advanceHeight` and the vhea values in the font info set; with a `table vhea { VertTypoAscender 500; … } vhea;` block in the feature prefix, the features compiled and vhea was still left out; with a `table vmtx { VertOriginY uniAC00 880; VertAdvanceY uniAC00 1000; … } vmtx;` block, the compiler stopped (*[F] VertOriginY redefined for glyph uniAC00*) and the font was exported without GSUB and GPOS. `fgOpt_OTExport` lists no option for vertical tables.
- **FontLab 7's Python ships fontTools 3.43.2** (*Resources\python\2.7\site-packages* of the install), importable from a script: it read the font FontLab exported (IBM Plex Sans JP as *OpenType PS*, 9,507 glyphs), added vhea, vmtx, and VORG, saved a copy, and read it back, in 13 seconds inside FontLab (2026-11-14, FreeLab's CJK Vertical).
- **`QRectF` of `flLayer.boundingBox` is upward**: `top()` is the ink's bottom and `bottom()` its top (`uni300C`: `top()` 110, `bottom()` 835); `y()` and `height()` read as the bottom and the height upward.

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
