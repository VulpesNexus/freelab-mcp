# Architecture

## Two processes

```
AI app (Claude Code, Claude Desktop, …)
   │ MCP over stdio
server/freelab_mcp        ordinary Python 3, the mcp SDK (FastMCP)
   │ newline-delimited JSON over TCP, 127.0.0.1 only, one request per connection
listener/freelab_listener.py   inside FontLab (Python 2.7 in FontLab 7)
   │ FontLab Python API: fontlab (fl*), fontgate (fg*)
FontLab
```

The split keeps every FontLab call in one small file that can be read in a sitting and tested with fakes, and lets the server use a modern Python and the MCP SDK, which FontLab 7's Python 2.7 cannot run.

## Listener

- **Main thread only.** A `QTimer` polls a non-blocking socket every 25 ms, so every FontLab call runs on FontLab's main thread. A long request keeps FontLab busy until it ends. This follows the Glyphbridge finding that FontLab gives background threads no CPU while it idles ([PRIOR-ART.md](PRIOR-ART.md)).
- **Not re-entrant.** Qt fires timers inside nested event loops, such as a modal dialog opened by `run_python` code. A tick that arrives while a request runs returns at once, so the request in progress is neither dropped nor joined by a second one.
- **A real module.** Run as a script, the file loads itself as the module `freelab_listener` and starts from there, so the running listener's globals are not FontLab's script namespace. FontLab 7 has a preference, `python.clear_context`, that is on by default and presumably empties that namespace between runs (unverified). The listener object itself is kept on the `sys` module. Running the script again stops the old listener and starts a new one.
- **Never raises into FontLab.** The timer callback catches everything, `SystemExit` and `KeyboardInterrupt` included, and prints the first error of each kind in the *Output* panel; `run_python` does the same for the code it runs.
- **Bounded.** At most 16 clients at once, 4 MB per request, and 5 seconds for a client to send its line.
- **Version-neutral source.** The file is valid Python 2.7 and Python 3; the test gate runs it under both. Text from FontLab or the system that is not UTF-8, such as a cp932 path in a Python 2 traceback, is decoded with replacement rather than allowed to break a reply.

## Discovery and authentication

On start the listener writes *%LOCALAPPDATA%\FreeLab\listener.json*: its port, a random 128-bit token, its PID, and its version. The server reads that file for every call and sends the token with every request; the listener compares it in constant time. The token keeps out what cannot read that file: other users' processes, and web pages (the endpoint is not HTTP, and a page cannot read the token). Any program running as the same user can read it, as the server does on every call, so the token is no barrier against those. Outside Windows the folder and the file are created readable by their owner only. The default port is 47761; if it is taken, the listener takes any free port and the state file says which. When the listener stops, it deletes the state file, unless a newer listener has already replaced it. `FREELAB_STATE_DIR` moves the file, which the tests use.

## Protocol 1

Request, one line of JSON:

```json
{"protocol": 1, "id": 7, "token": "…", "deadline": 1793961600.5, "op": "glyph", "args": {"name": "A"}}
```

Reply, one line: `{"id": 7, "ok": true, "result": …}`, or `{"id": 7, "ok": false, "error": {"code": "no_glyph", "message": "…"}}`. The `id` is echoed only once the token is accepted. `deadline` is optional: seconds since the epoch after which the client has given up, so a request FontLab only reaches later is refused (`expired`) instead of run unseen. The server sets it on every call. A protocol change bumps the number in both halves; the server refuses a listener that speaks another one.

Listener error codes: `bad_json`, `bad_request`, `bad_token`, `bad_protocol`, `expired`, `unknown_op`, `bad_args`, `too_large`, `not_in_fontlab`, `no_font`, `no_current_font`, `no_glyph`, `no_layer`, `unexpected_type`, `unreadable`, `unserializable`, `unsaved_changes`, and `exception`, which carries the traceback. The client adds `not_running`, `bad_state`, `timeout`, `connection`, `empty_reply`, `bad_reply`, and `too_large` (a request over the listener's 4 MB, refused before it is sent). The server's tools add `bad_args`, `listener_not_running` (FontLab runs without its listener), `not_found` (no FontLab program), `no_listener` (FontLab started, its listener did not answer), `save_prompt` (FontLab asks whether to save a font, and waits for the user), `dialog_open` (it asks something else), and `still_running` (FontLab asked to close, still open).

| Operation | Does |
|---|---|
| `ping` | versions, Python, whether FontLab's module is present |
| `status` | open font count, and the font in front |
| `font_info` | the font in front, or `no_font` |
| `glyph_names` | glyph names, paged |
| `glyph` | one glyph by name or code point: Unicode values, layers, width, bounding box, counts |
| `outline` | shapes, contours, and nodes on one layer, capped by `max_nodes` |
| `run_python` | runs code in FontLab, returns printed output, `result`, and any traceback |
| `unsaved` | whether the workspace or any open font has changes FontLab reports, and how many fonts were edited since they were opened (`edited`: FontLab 7.2 reports an edit only through its undo history) |
| `dialog` | the window FontLab is waiting on, if any: its class, title, texts, and buttons; it only reads |
| `quit` | refused with `unsaved_changes` when FontLab reports changes (or `unreadable` when it cannot tell); otherwise closes FontLab's main window a moment after replying, as its close button would. FontLab then asks about each edited font itself (*Save changes to … before closing?*); `quit_fontlab` reads that question through `dialog`, leaves it on screen, and returns it as `save_prompt` |

## Error model

- **A failed read is never an empty value.** Each property read is guarded; one that fails is `null` in the reply and listed under `unreadable` with its exception. A reply without `unreadable` read everything it shows.
- **Native containers are not indexed or iterated.** Only real Python lists and tuples that FontLab returns are walked; anything else is refused with `unexpected_type`. A wrong call on a native FontLab object can crash FontLab where `try` cannot catch it (reported for FontLab 8 by Glyphbridge).
- **Qt values are converted by class name** (`QRectF`, `QPointF`, `QTransform`, …); other native objects are returned as their `repr`, never probed for methods.
- **A null check that fails is an error.** If FontLab cannot say whether an object is null, the operation stops with `unreadable` instead of calling into a possibly null object.
- **The server never invents state.** Listener errors become MCP tool errors with the listener's code and message. Its tools are async and make the socket call in a worker thread, so the server keeps answering the AI app while FontLab works.

## Tests

The fakes in *tests/fakes.py* are spies: each fake FontLab object allows only the members the listener is documented to use, and raises `SpyViolation` (a `BaseException`, which the listener's `except Exception` cannot swallow) on any other member, on a subscript, or on iteration. A new FontLab call therefore fails the tests until it is added to the fakes and to [FONTLAB7-API.md](FONTLAB7-API.md). The socket tests run the real listener on a real loopback socket; the server tests run the real MCP tools against it. Every test also fails if the listener caught and recorded an error along the way. The guards added after the first independent review were mutation-tested: each was removed from a throwaway copy in turn, and the suite failed every time.
