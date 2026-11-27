# Install FreeLab MCP's listener into FontLab 7 for the current user, or remove it.
#
#   install-fontlab.ps1                   copy the listener into FontLab 7's
#                                         Scripts\FreeLab folder (a FreeLab submenu
#                                         of the Scripts menu)
#   install-fontlab.ps1 -ListenerAtLaunch also add the FreeLab MCP block to FontLab's
#                                         startupScript.py, which starts the listener
#                                         at every launch, so an AI app can reach
#                                         FontLab (and its launch_fontlab tool can
#                                         start it) without a click in FontLab
#   install-fontlab.ps1 -Uninstall        remove that block and the listener
#
# FreeLab Script Suite, a separate repository of FontLab tools (dark mode), installs its scripts
# beside this one with a startup block of its own: either can be installed or
# removed without the other.
#
# The first change to the startup script keeps the original as
# startupScript.py.freelab-backup, and each change keeps the file it replaced as
# startupScript.py.freelab-previous. Quit FontLab first or restart it afterwards:
# FontLab reads its Scripts folder and startup script when it starts.
#
# ASCII only: Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page.

# -ScriptsDir and -StartupScript override the locations, for testing.
param([switch]$Uninstall, [switch]$ListenerAtLaunch, [string]$ScriptsDir, [string]$StartupScript)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

if ($ScriptsDir) { $scripts = $ScriptsDir }
else {
    # FontLab 7's own data folder setting, else its default under Documents.
    $dataPath = $null
    try { $dataPath = (Get-ItemProperty 'HKCU:\Software\Fontlab\fontlab7' -Name 'general.data_path' -ErrorAction Stop).'general.data_path' } catch { }
    if (-not $dataPath) { $dataPath = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'FontLab\FontLab 7' }
    $scripts = Join-Path ($dataPath -replace '/', '\') 'Scripts'
}
$rootScripts = $scripts
$scripts = Join-Path $rootScripts 'FreeLab'  # its own submenu of the Scripts menu
if ($StartupScript) { $startup = $StartupScript }
else { $startup = Join-Path $env:LOCALAPPDATA 'FontLab\FontLab 7\startupScript.py' }
$listenerName = 'freelab_listener.py'
$begin = '# >>> FreeLab MCP'
$end = '# <<< FreeLab MCP'

# FreeLab's markers are whole lines, from the start of the line. A line starting "# >>> FreeLab" opens a
# block and one starting "# <<< FreeLab" closes it, in turn; a block of this installer's opens with
# $begin alone or followed by a space and a note, and closes with $end alone. Marker text anywhere else
# (a string in the user's code, an indented comment) is the user's, never a marker. This one recognizer
# serves the check and every change: the check once took markers at the start of a line while the
# changes took marker text anywhere, and deleted the user's code between two quoted markers.
$ownOpen = '^' + [regex]::Escape($begin) + '(?:[ \t].*)?$'
$ownClose = '^' + [regex]::Escape($end) + '[ \t]*$'
# The one block FreeLab wrote before the MCP and the tools became separate repositories
# (dark mode and the listener together). Either installer takes it out and says so.
$legacyOpen = '^# >>> FreeLab \(managed.*$'
$legacyClose = '^# <<< FreeLab[ \t]*$'

# The blocks a pair of markers delimits, as (first line, last line) index pairs; $null when the file's
# FreeLab markers do not pair: a block opened inside another, closed without being opened, never
# closed, or opened by one kind of marker and closed by another (another FreeLab block's markers count).
function Find-Blocks([string[]]$lines, [string]$open, [string]$close) {
    $found = New-Object System.Collections.ArrayList
    $start = -1
    $mine = $false
    for ($i = 0; $i -lt $lines.Length; $i++) {
        $line = $lines[$i]
        if ($line.StartsWith('# >>> FreeLab', [System.StringComparison]::Ordinal)) {
            if ($start -ge 0) { return $null }
            $start = $i
            $mine = $line -cmatch $open
        }
        elseif ($line.StartsWith('# <<< FreeLab', [System.StringComparison]::Ordinal)) {
            if ($start -lt 0) { return $null }
            if (($line -cmatch $close) -ne $mine) { return $null }
            if ($mine) { [void]$found.Add(@($start, $i)) }
            $start = -1
        }
    }
    if ($start -ge 0) { return $null }
    return ,$found
}

function Split-Lines([string]$text) { return ,$text.Split([char]10) }

# The text without the blocks of that kind (the lines between their markers, markers included).
function Remove-Blocks([string]$text, [string]$open, [string]$close) {
    $lines = Split-Lines $text
    $blocks = Find-Blocks $lines $open $close
    if ($null -eq $blocks) { throw "unpaired FreeLab markers" }  # (checked before: never reached)
    $drop = @{}
    foreach ($block in $blocks) { for ($i = $block[0]; $i -le $block[1]; $i++) { $drop[$i] = $true } }
    $kept = New-Object System.Collections.Generic.List[string]
    for ($i = 0; $i -lt $lines.Length; $i++) { if (-not $drop.ContainsKey($i)) { $kept.Add($lines[$i]) } }
    return ([string]::Join("`n", $kept)).TrimEnd("`n") + "`n"
}

function Remove-Block([string]$text) { return Remove-Blocks $text $ownOpen $ownClose }

# The block put where it already is (the other repository's block keeps its place), or appended.
function Set-Block([string]$text, [string]$newBlock) {
    $lines = Split-Lines $text
    $blocks = Find-Blocks $lines $ownOpen $ownClose
    if ($null -eq $blocks) { throw "unpaired FreeLab markers" }  # (checked before: never reached)
    if ($blocks.Count -gt 0) {
        # the first copy replaced, any later one (a duplicated block) taken out
        $drop = @{}
        foreach ($block in $blocks) { for ($i = $block[0]; $i -le $block[1]; $i++) { $drop[$i] = $true } }
        $kept = New-Object System.Collections.Generic.List[string]
        for ($i = 0; $i -lt $lines.Length; $i++) {
            if ($i -eq $blocks[0][0]) { $kept.Add($newBlock) }
            elseif (-not $drop.ContainsKey($i)) { $kept.Add($lines[$i]) }
        }
        return [string]::Join("`n", $kept)
    }
    $text = $text.TrimEnd("`n")
    if ($text) { $text += "`n" }
    return $text + $newBlock + "`n"
}

# A Python string literal that is pure ASCII: backslashes doubled, everything
# outside ASCII as \uXXXX, so Python 2 reads it whatever the code page.
function ConvertTo-PyLiteral([string]$s) {
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('u"')
    foreach ($ch in $s.ToCharArray()) {
        $code = [int]$ch
        if ($ch -eq '\') { [void]$sb.Append('\\') }
        elseif ($ch -eq '"') { [void]$sb.Append('\"') }
        elseif ($code -lt 32 -or $code -gt 126) { [void]$sb.Append(('\u{0:x4}' -f $code)) }
        else { [void]$sb.Append($ch) }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

# FontLab's startup script is read as strict UTF-8 (a BOM is kept) and written back with its own
# line endings, and only when it changes. A file that is not UTF-8, or whose FreeLab markers are
# not paired, is left alone: rewriting it would replace characters or delete the user's code.
$current = ''
$normalized = ''
$notUtf8 = $false
$bom = $false
$crlf = $false
if (Test-Path $startup) {
    $bytes = [System.IO.File]::ReadAllBytes($startup)
    $bom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    $skip = 0
    if ($bom) { $skip = 3 }
    $strict = New-Object System.Text.UTF8Encoding $false, $true
    $original = $null
    try { $original = $strict.GetString($bytes, $skip, $bytes.Length - $skip) }
    catch {
        # Not UTF-8 (an ANSI file with a coding line, say): read as ASCII only to look for FreeLab's markers.
        # With none, there is nothing of FreeLab's in it, and it is changed only if this run must add a block.
        if ([System.Text.Encoding]::ASCII.GetString($bytes).Contains('# >>> FreeLab') -or
            [System.Text.Encoding]::ASCII.GetString($bytes).Contains('# <<< FreeLab')) {
            Write-Output "$startup is not UTF-8 text and holds a FreeLab block, so it is left as it is. Save it as UTF-8 and run this again."
            exit 1
        }
        $notUtf8 = $true
    }
    if ($null -ne $original) {
        $crlf = $original.Contains("`r`n")
        $normalized = $original -replace "`r`n", "`n"
        $current = $normalized
    }
}
# Each begin marker must be followed by its own end marker before the next begin: counting them was not
# enough (an end marker above its begin marker let a later install delete the code between them).
$currentLines = Split-Lines $current
if ($null -eq (Find-Blocks $currentLines $ownOpen $ownClose) -or $null -eq (Find-Blocks $currentLines $legacyOpen $legacyClose)) {
    Write-Output "$startup has a FreeLab begin marker without its end marker, or the reverse, or two blocks' markers mixed, so it is left as it is. Remove the broken block by hand and run this again."
    exit 1
}
function Test-Changed([string]$text) {
    return $text.TrimEnd("`n") -ne $normalized.TrimEnd("`n")
}
function Save-Startup([string]$text) {
    if ($notUtf8) {
        Write-Output "$startup is not UTF-8 text, so the FreeLab block was not added. Save it as UTF-8 and run this again."
        exit 1
    }
    if ((Test-Path $startup) -and -not (Test-Path "$startup.freelab-backup")) {
        [System.IO.File]::Copy($startup, "$startup.freelab-backup")
        Write-Host "Kept the original startup script as $startup.freelab-backup"
    }
    $folder = Split-Path -Parent $startup
    if (-not (Test-Path $folder)) { New-Item -ItemType Directory -Path $folder | Out-Null }
    if ($crlf) { $text = $text -replace "`n", "`r`n" }
    # Written whole beside it, then put in its place, the file it replaces kept as .freelab-previous
    # (each update: the first install's copy is the user's file before FreeLab, not their later changes)
    $temp = "$startup.freelab-new"
    try {
        [System.IO.File]::WriteAllText($temp, $text, (New-Object System.Text.UTF8Encoding $bom))
        if ([System.IO.File]::Exists($startup)) { [System.IO.File]::Replace($temp, $startup, "$startup.freelab-previous", $true) }
        else { [System.IO.File]::Move($temp, $startup) }
    }
    catch {
        if ([System.IO.File]::Exists($temp)) { [System.IO.File]::Delete($temp) }
        Write-Output "Could not write ${startup}: $($_.Exception.Message) It is left as it was."
        exit 1
    }
}
$legacy = (Find-Blocks $currentLines $legacyOpen $legacyClose).Count -gt 0
$hadBlock = (Find-Blocks $currentLines $ownOpen $ownClose).Count -gt 0
if ($ListenerAtLaunch -and -not $Uninstall -and -not (Test-Path $startup)) {
    # FontLab writes its own startup script (its default imports) when it first starts; one made here
    # holding only this block could keep FontLab from writing it.
    Write-Output "FontLab has not made its startup script yet ($startup): start FontLab once, quit it, and run this again."
    exit 1
}
if ($legacy) { $current = Remove-Blocks $current $legacyOpen $legacyClose }
function Write-LegacyNote {
    if ($legacy) {
        Write-Output 'Removed the combined FreeLab block of earlier versions, which also applied dark mode at launch. To keep that, run tools\install-fontlab.ps1 from FreeLab Script Suite.'
    }
}

if ($Uninstall) {
    if (Test-Path $startup) {
        $text = Remove-Block $current
        if (Test-Changed $text) {
            Save-Startup $text
            if ($hadBlock) { Write-Output "Removed the FreeLab MCP block from $startup" }
            Write-LegacyNote
        }
    }
    foreach ($dir in @($scripts, $rootScripts)) {  # the Scripts folder itself held it before the repositories split
        $target = Join-Path $dir $listenerName
        if (Test-Path $target) { [System.IO.File]::Delete($target); Write-Output "Removed $target" }
    }
    if ((Test-Path $scripts) -and -not (Get-ChildItem $scripts -Force)) {
        [System.IO.Directory]::Delete($scripts); Write-Output "Removed $scripts"
    }
    exit 0
}

if (-not (Test-Path $scripts)) { New-Item -ItemType Directory -Path $scripts | Out-Null }
$target = Join-Path $scripts $listenerName
[System.IO.File]::Copy((Join-Path $repo "listener\$listenerName"), $target, $true)
Write-Output "Copied $listenerName to $scripts"
$old = Join-Path $rootScripts $listenerName  # before the repositories split it sat in the Scripts folder itself
if (Test-Path $old) { [System.IO.File]::Delete($old); Write-Output "Removed the old $old" }

$text = (Remove-Block $current).TrimEnd("`n")
if ($text) { $text += "`n" }
if ($ListenerAtLaunch) {
    $listenerPath = ConvertTo-PyLiteral $target
    $block = @"
$begin (managed by FreeLab MCP tools/install-fontlab.ps1; -Uninstall removes it)
def _freelab_mcp_startup():
    import os, sys
    if getattr(sys, "_freelab_listener", None) is not None:
        return  # FontLab may run this script again in a session: keep the running listener and its requests
    path = $listenerPath
    if not os.path.isfile(path):
        return
    name = "freelab_listener"  # loaded under this name, the listener does not start itself; main() below does
    if sys.version_info[0] == 2:
        # open() takes a unicode path through the wide Windows API; imp.load_source does not.
        import imp
        module = imp.new_module(name)
        module.__file__ = path
        sys.modules[name] = module
        with open(path, "rb") as handle:
            source = handle.read()
        exec(compile(source, name + ".py", "exec", 0, True), module.__dict__)
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    sys.modules[name] = module
    module.main()
try:
    _freelab_mcp_startup()
except Exception as exc:
    print("FreeLab listener at startup: %s" % (exc,))
$end
"@ -replace "`r`n", "`n"
    $text = Set-Block $current $block
    if (Test-Changed $text) {
        Save-Startup $text
        Write-Output "Added the FreeLab MCP block to ${startup}: FontLab starts the listener at every launch"
        Write-LegacyNote
    }
    else { Write-Output "The FreeLab MCP block in $startup is up to date" }
}
elseif ((Test-Path $startup) -and (Test-Changed $text)) {
    Save-Startup $text
    if ($hadBlock) { Write-Output "Removed the FreeLab MCP block from ${startup}: start the listener with Scripts > FreeLab > Start MCP Listener" }
    Write-LegacyNote
}
