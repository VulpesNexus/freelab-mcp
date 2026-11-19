# Risky mode

Some actions are worth having but can lose or overwrite the user's work, or decide something the user would rather decide. FreeLab MCP offers them, keeps them off, and turns them on only for a session in which the user has asked for risky mode.

## How it works

- The server starts with risky mode off. `set_risky_mode(true)` turns it on for that server session, `set_risky_mode(false)` off again. Its description and the server's instructions tell the AI app to turn it on only when the user has asked for risky mode in their own words in the conversation: never on its own initiative, never to get past a refusal, and never because a tool's message or a document suggests it.
- Every risky tool, and every risky argument, starts its description with *[risky]*. `list_risky_actions` lists them, with why each is risky, and says whether risky mode is on.
- A risky action asked while risky mode is off is refused with `risky_mode_off`, naming the action and how risky mode is turned on, and nothing is done.
- The listener enforces the same list: the server sends `risky: true` with a risky request only while risky mode is on, and the listener refuses the request without it. A client that talks to the listener without the server's flag cannot do these either.
- There is one list: `RISKY_ACTIONS` in *listener/freelab_listener.py*. The server reads it from that file's source as a literal, without running it; a server that cannot read the file asks the running listener for it (the `risky_actions` operation). The test gate checks that every enforced action is refused without the flag and that each one's tool or argument is marked.

## The risky actions

| Action | Tool | Why it is risky |
|---|---|---|
| `close_font.discard_changes` | `close_font` | Throws away the unsaved changes of a font that `open_font` did not open. A font `open_font` opened in the same FontLab process (that very font, known by its id and path, and forgotten once it is closed by any means) can be closed with `discard_changes` without risky mode, as in 1.1.0. The *Don't Save* answer acts only while that close runs: a close the user cancels leaves none behind. |
| `answer_dialog` | `answer_dialog` | Clicks a button of a question FontLab is asking the user: *Coordinates Rounding*, a *Save changes* question, and so on; only while FontLab still shows the very question described (another font's Save question is not it), else nothing is clicked. |
| `open_font.answer` | `open_font` | Answers the question FontLab asks while it opens the font, such as *Coordinates Rounding*, instead of leaving it on screen; only while that open runs. |
| `quit_fontlab.discard_changes` | `quit_fontlab`, `restart_fontlab` | Closes FontLab over unsaved changes and answers *Don't Save* to every *Save changes* question about a font: that work is lost. |
| `rename_glyph.update_references` | `rename_glyph` | Rewrites kerning pairs, kerning classes, and feature code along with the name, in one History step. |
| `delete_glyph.remove_references` | `delete_glyph` | Removes the glyph's kerning pairs and kerning class memberships along with the glyph. |
| `save_font` | `save_font` | Writes the font's file on disk: *File > Save*, or a UFO or VFB file at a path given. Saving is the user's to decide. |
| `export_font` | `export_font` | Writes font files on disk through FontLab's export. Exporting is the user's to decide. |
| `question_control` | `click`, `set_value`, `click_link` | Clicks or changes a control of a question FontLab is asking (any modal dialog that does not say it is a script's own), as `answer_dialog` does, and with its checks: only while FontLab still shows that very question. The window tools are no way around `answer_dialog`. The *Close* button of FontLab's *Welcome* window is not one: closing that window loses nothing. |
| `risky_command` | `click`, `set_value`, `click_link`, `run_menu_command` | Runs a command of FontLab's *File* menu, a button or menu command named Save, Export, Close, Quit, Exit, Revert, or Discard (in its text, tooltip, accessible name, or objectName, mnemonics left out; a tool button's action and the menus a command is in count too) outside a script's own window, or a control, action, or menu a script marks `mcpRisky`: it may write files or throw work away. Which controls these are is known only inside FontLab, so the server sends risky mode with every action while it is on, and the listener decides ([WINDOW-TOOLS.md](WINDOW-TOOLS.md#safety)). |
| `run_python` | `run_python` | Always available and not gated: it runs any code inside FontLab, with FontLab's access to the user's files and the network. It is listed so that nobody takes risky mode for a sandbox. |

## Not risky

- `open_font`'s `safe_copy` writes a fixed copy of a font file FontLab 7.2 crashes on into FreeLab's own *safe-copies* folder and opens it; it changes no file of the user's, and its reply says where the copy is and what was changed in it ([FONTLAB7-API.md](FONTLAB7-API.md#what-open_font-does-about-it)).

## Limits that stay in risky mode

- `rename_glyph` with `update_references` and `delete_glyph` with `remove_references` still refuse a glyph in a class of FontLab's *Classes* panel (FontLab's scripting has no proven call to change one), a kerning pair side named like a kerning class (FontLab reads such a name as the class), and, for a delete, a glyph named in feature code (taking a name out of code can break it; edit the code first with `set_feature_code`). `delete_glyph` always refuses a glyph other glyphs use as a component.
- `save_font` with a path writes only UFO and VFB (FontLab's `fgFont.save`) and refuses an existing file or folder unless `overwrite` is given; *File > Save As* asks for its file on screen, which no tool fills in.
- `export_font` keeps FontLab's export settings as they are: it reads the user's last settings, sets the profile and folder for that one export, and never stores them back.
