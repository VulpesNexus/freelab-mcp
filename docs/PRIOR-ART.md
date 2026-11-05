# Prior art

Surveyed on 2026-11-06: GitHub, the LobeHub and Glama MCP directories, and a general web search for FontLab MCP servers. Two exist; neither fits FontLab 7.

## Dithilli/fontlab-mcp-server

[github.com/Dithilli/fontlab-mcp-server](https://github.com/Dithilli/fontlab-mcp-server), MIT, version 0.1.0 "alpha", last push 2025-10-15. The copy reviewed was *fontlab-mcp-server-master.zip*, 48,118 bytes, SHA-256 `0AEED061473F14B7C883480E11C609AE64D4FD423A72A54CAAE73A805810D900`; it was deleted after review, since the repository has the same content.

It cannot work with FontLab 7 on Windows:

- It runs `FontLab -script <file> -output <file>` for every call. FontLab 7 has no such switches (neither string is in *FontLab 7.exe*), a new process would not reach the open font anyway, and the scripts read their output path from `sys.argv[-1]`, which FontLab does not set.
- It looks for FontLab only in macOS locations and stops at startup on Windows.
- Its scripts use f-strings, a syntax error in FontLab 7's Python 2.7.
- Its FontLab calls do not match the FontLab 7 API ([FONTLAB7-API.md](FONTLAB7-API.md)).
- Its `fontlab-mcp-server` entry point names a package that does not exist, and its timeout path uses `signal.SIGKILL`, which Windows lacks.

Nothing of it is reused. Its tool list was a useful checklist of what an agent asks for.

## Glyphbridge-FontLab-MCP

[github.com/TopraksoyEarthmanTsuchimoto/Glyphbridge-FontLab-MCP](https://github.com/TopraksoyEarthmanTsuchimoto/Glyphbridge-FontLab-MCP), active (last push 2026-11-04), **no license**. It targets FontLab 8 with its embedded Python 3.11 and an MCP server on Python 3.14 and `mcp` 2.2.0.

Its design is the right one, and FreeLab follows the same shape: a listener run by hand inside FontLab, a `QTimer` polling a loopback socket on the main thread, and a separate MCP server that never imports `fontlab`. Its documentation records many FontLab 8 API findings, including native calls that crash FontLab; [FONTLAB7-API.md](FONTLAB7-API.md) lists the ones to check on FontLab 7.

With no license, its code is all rights reserved: FreeLab copies none of it, and was written from FontLab's own reference. Porting it would not have saved the work in any case: it is FontLab 8 and Python 3 throughout.
