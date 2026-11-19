# Window tools

New in 1.3.0: tools that drive any of FontLab's windows the way a user does, without `run_python`. They know nothing of any one script: they work on Qt's widgets, so FontLab's own windows and panels, and the tool windows of a script such as FreeLab Script Suite, are driven alike. A script can follow a few optional conventions (below) to be driven more surely. They drove a whole FreeLab Script Suite workflow live on 2026-11-19; [FONTLAB7-API.md](FONTLAB7-API.md#the-window-tools-found-live-2026-11-19) says what was proven, what still needs a live proof, and the Qt calls they make.

## The tools

| Tool | Does |
|---|---|
| `list_windows` | The visible top-level windows: each one's key, title, class, whether it is modal, whether FontLab waits on it (and whether it is then a question of FontLab's), whether a script marks it as its own, and whether it is busy |
| `list_window_controls` | A window's controls, each with its address, its kind, whether it is shown and enabled, whether a click on it needs risky mode, and what it holds |
| `click` | A button, check box, or radio button, as a click does; a button's menu entry (`entry`); a row of a list, tree, or table (`row`, `double`) |
| `set_value` | Ticks or unticks a check box (or a row's), chooses a radio button, a dropdown's entry by its text, or a tab, sets a spin box, slider, field, or text box |
| `read_list` | The rows of a list, tree, or table, a page at a time: text per column, check state, selection, and, in a tree, depth, child count, and whether it is expanded |
| `read_text` | The whole text of a label, field, text box, or log, with the links it holds |
| `click_link` | Follows a link of a log or a label, as a click on it does |
| `list_menu` | The main window's menus, or the entries of one by its path |
| `run_menu_command` | A command of the main window's menus by its path: *Scripts > FreeLab > Font Check*, *Window > Panels > Features* |
| `wait_idle` | Waits until the work an action started is over, and says what it left |

## Windows

A window is named by its key, as `list_windows` gives it: its title, else its objectName or class when it has no title. Windows that share a name are told apart by a number each one keeps for the FontLab session (*Glyph Window#3*, *Glyph Window#4*), never by their order, which Qt may change between two calls: a key names one window for good, and is refused once that window is gone. `main` is FontLab's main window (its docked panels are in it), and `dialog` the dialog FontLab waits on; another window titled so is numbered (*main#5*), so it never takes their place. A part of a title names the only window whose title holds it (`auto kerning`). Popup menus and tooltips are not windows here.

## Controls and their addresses

The kinds: `button`, `check`, `radio`, `combo`, `spin`, `field` (a line edit), `text` (a text box), `log` (a text browser, whose links can be clicked), `tabs` (a tab widget or a tab bar), `list`, `tree`, `table`, `label`, `progress`, and `slider`. Containers (frames, splitters, scroll areas, pages) are walked through, never listed; windows a window holds (a dialog it parents) and the inside of a control (a spin box's own field) are not walked, but for the controls a list, tree, or table holds in its cells (a dropdown in each row), which take the list's objectName (or kind) before their own address, numbered in the order they were made: *ligature_tree > combo#2*.

A control's address is:

- its objectName, when no other control of the window has it;
- else the tabs (of a tab widget), group boxes, and docked panels around it, its caption, and its kind, joined by " > ", with *#1*, *#2* when several controls share that: `Pairs > Apply > button`, `Master > combo`, `tree#2`.

A caption is a button's (or check box's) text, else its accessible name, its default action's text, or the first line of its tooltip; for another control, its accessible name, the label whose buddy it is, or the label just before it in its layout (`Master:` before a dropdown). Labels have no caption: their text is what they show, and they are numbered (`label#3`). A control is found by its address, its objectName, or its caption (case and a closing ellipsis or period ignored); when a caption names several controls and only one of them is shown, that one. An address or objectName that names two controls (an objectName spelled like another control's address) is refused (`ambiguous_control`), never settled by which one is shown.

An address made of labels holds while the window is laid out as it was; text that changes (a button that reads *Start*, then *Stop*) changes it. An objectName does not change: see the conventions.

## Actions run after the reply

Reads (`list_windows`, `list_window_controls`, `read_list`, `read_text`, `list_menu`) answer at once and change nothing. An action (`click`, `set_value`, `click_link`, `run_menu_command`) is checked, then run from a timer a moment after the reply, as `open_font` is: a click may open a modal dialog, or start work that runs until it ends, and a request must not wait on either. When its timer fires it is checked again, and done only if what it acts on is as it was: the same window, the very same control (with the same text, and for a tool button the same action) or menu command, the very same row (its model's, followed where it moved, not another row whose text looks the same), and, for a question of FontLab's, the very question described (its title, texts, and buttons, as `answer_dialog` checks). Otherwise nothing is done, and the tool says why (`changed`, `disabled`, `dialog_open`, `busy`, …). An action reported lost (its timer never fired within 30 seconds) is never done later, and one whose slot raises ends `failed`, whatever it raised. A row whose model says it is disabled is refused, as a click on it would be; a tick the model refuses ends `failed`.

The tool waits up to `wait_s` (2 seconds by default) for the action to be over, and returns what it changed (`result`), or says that it still runs. `wait_idle` then waits until no action of these tools is queued or running and no window is busy, held for `settle_s` (a moment FontLab does not answer, or refuses the connection while it still runs, starts that again); with `window`, only that window's work and actions count, and its closing ends the wait (`window_closed`). It returns at once when a dialog shows (other than the window waited on): a script's dialog is driven next with `window="dialog"`; a question of FontLab's is answered by the user, or in risky mode. It reports the last action's outcome, and `[still_busy]` after `timeout_s`. FontLab answers while a script's work lets its events through, as FreeLab Script Suite's does; work that does not leaves FontLab silent until it ends, and `wait_idle` keeps asking.

A window is busy while:

- its `mcpBusy` property is set and true (a convention, below), or, for a window with no such property but FontLab's main window and its panels, while it shows a progress bar that is not full;
- an action of these tools on it is queued or running.

## Safety

- **One thing at a time.** While a dialog waits, only that dialog can be driven, as Qt itself lets no input reach another window then; while an earlier action is queued or running, or any window is busy, every other action is refused (`busy`), except on the dialog that work waits on. A script's long work reads the fonts as they are, and FreeLab Script Suite's blocks every input meanwhile, FontLab's included.
- **Risky mode** ([RISKY.md](RISKY.md)) is needed for any control of a question FontLab asks (`question_control`: a *Save changes* question, *Coordinates Rounding*), checked as `answer_dialog` checks it, so the window tools are no way around `answer_dialog`; and for any command of FontLab's *File* menu, a button or menu command named Save, Export, Close, Quit, Exit, Revert, or Discard (in its text, tooltip, accessible name, or objectName, its mnemonic left out: *Sa&ve* is *Save*; for a tool button, its action's too; for a menu command, the menus it is in too) outside a script's own window, and a control, a tool button's action, or a command (or a menu it is in) a script marks risky (`risky_command`). Writes a script records in FontLab's *History* are ordinary actions: *Edit > Undo* takes them back.
- **A modal dialog is a question of FontLab's** unless it says itself that it is a script's (`mcpScriptWindow`): FontLab may parent a question of its own to a script's window, so a parent's mark is not enough.
- Hidden and disabled controls, read-only fields, and disabled tabs and menu commands are refused. A link out of FontLab (a web page, a mail, a file) is never followed, nor a label's that opens its links in a browser.
- `run_python` stays what it was: it runs any code, risky mode or not.

## Conventions a script can follow

All optional: without them, the tools work as described above. Each is a Qt property set with `setProperty` (a dynamic property), or Qt's own objectName, accessible name, or buddy.

| Convention | Set on | What it does |
|---|---|---|
| `setObjectName("…")` | a control | Its address, stable whatever its text, tab, or place, when no other control of the window has it. The surest way to be driven |
| `setAccessibleName("…")` | a control with no text | Its caption (an icon button, a field with no label) |
| `QLabel.setBuddy(control)` | a label | The label's text becomes the control's caption |
| `mcpBusy` | a window | Busy while true; a text is shown as the step (`Kerning A V`). Set it to `False` (or an empty text) when the work is over: once a window has the property, its progress bars are no longer looked at |
| `mcpScriptWindow` | a window | The window is the script's own: the name rule (Save, Close, …) does not apply to its buttons and menus, nor to the windows it holds. Set it also on each modal dialog the script shows (a `QMessageBox` asking to replace something): only then is it the script's question rather than FontLab's |
| `mcpRisky` | a control, a menu command (`QAction`), or a widget holding controls | Its use needs risky mode: a button that writes files outside FontLab's *History* or throws work away |

A tab bar over pages the script shows and hides itself (rather than a `QTabWidget`) gives its pages no tab in their controls' addresses: the objectNames of the pages' controls keep those apart.

FreeLab Script Suite follows every one of these, as an example of a script made to be driven: its windows and dialogs are marked its own, a window's `mcpBusy` is the step its top row shows, each control takes the name of the attribute its tool holds it in, its icon buttons have names, and its buttons that write files are marked risky. Its *docs/WINDOWS.md*, section *Driving FreeLab with FreeLab MCP*, lists the names. Nothing in FreeLab MCP depends on it.

## What it does not do

- Context menus (right click), typing keys and shortcuts, drag and drop, scrolling, and selecting several rows at once.
- Closing a window by its title bar's close button.
- Windows' own file dialogs (*Open*, *Save As*): they are not Qt's widgets, so there is nothing to drive.
- Menus FontLab fills only as they open may list nothing until they have been opened once on screen.
- FontLab's *Welcome to FontLab 7!* window, shown modal at a launch with no font while *Open this dialog at startup* is on, is a question like any other: until it is closed (by the user, or its *Close* in risky mode), only it can be driven.
- A `QTextBrowser` that follows its links itself (`openLinks`) is not made to: `click_link` emits `anchorClicked`, as a click does, and leaves the rest to the script.
