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
# FreeLab, a separate repository of FontLab tools (dark mode), installs its scripts
# beside this one with a startup block of its own: either can be installed or
# removed without the other.
#
# The first change to the startup script keeps the original as
# startupScript.py.freelab-backup. Quit FontLab first or restart it afterwards:
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

function Remove-Block([string]$text) {
    $re = New-Object System.Text.RegularExpressions.Regex ('(?s)\n?' + [regex]::Escape($begin) + '.*?' + [regex]::Escape($end) + '[^\n]*\n?')
    # a block at the very top leaves no blank first line; elsewhere one line break stays between its neighbors
    $evaluator = [System.Text.RegularExpressions.MatchEvaluator] { param($m) if ($m.Index -eq 0) { '' } else { "`n" } }
    return $re.Replace($text, $evaluator).TrimEnd("`n") + "`n"
}

# The block put where it already is (the other repository's block keeps its place), or appended.
function Set-Block([string]$text, [string]$newBlock) {
    $re = New-Object System.Text.RegularExpressions.Regex ([regex]::Escape($begin) + '(?s:.*?)' + [regex]::Escape($end) + '[^\n]*')
    if ($re.IsMatch($text)) {
        # the first copy replaced, any later one (a duplicated block) taken out
        $seen = @{ first = $true }
        $evaluator = [System.Text.RegularExpressions.MatchEvaluator] {
            param($m)
            if ($seen.first) { $seen.first = $false; return $newBlock }
            return ''
        }.GetNewClosure()
        return $re.Replace($text, $evaluator)
    }
    $text = $text.TrimEnd("`n")
    if ($text) { $text += "`n" }
    return $text + $newBlock + "`n"
}

# The one block FreeLab wrote before the MCP and the tools became separate repositories
# (dark mode and the listener together). Either installer takes it out and says so.
$legacyPattern = '(?s)\n?# >>> FreeLab \(managed.*?# <<< FreeLab[ \t]*(\n|$)'

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
# Each begin marker must be followed by its end marker before the next begin: counting them was not
# enough (an end marker above its begin marker let a later install delete the code between them).
function Test-Paired([string]$text, [string]$open, [string]$close) {
    $inside = $false
    foreach ($line in $text -split "`n") {
        if ($line -match ('^' + $open)) {
            if ($inside) { return $false }
            $inside = $true
        }
        elseif ($line -match ('^' + $close + '[ \t]*$')) {
            if (-not $inside) { return $false }
            $inside = $false
        }
    }
    return -not $inside
}
if (-not (Test-Paired $current ([regex]::Escape($begin)) ([regex]::Escape($end))) -or
    -not (Test-Paired $current '# >>> FreeLab \(managed' '# <<< FreeLab')) {
    Write-Output "$startup has a FreeLab begin marker without its end marker, or the reverse, so it is left as it is. Remove the broken block by hand and run this again."
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
    [System.IO.File]::WriteAllText($startup, $text, (New-Object System.Text.UTF8Encoding $bom))
}
$legacy = [regex]::IsMatch($current, $legacyPattern)
$hadBlock = $current.Contains($begin)
if ($ListenerAtLaunch -and -not $Uninstall -and -not (Test-Path $startup)) {
    # FontLab writes its own startup script (its default imports) when it first starts; one made here
    # holding only this block could keep FontLab from writing it.
    Write-Output "FontLab has not made its startup script yet ($startup): start FontLab once, quit it, and run this again."
    exit 1
}
if ($legacy) { $current = [regex]::Replace($current, $legacyPattern, "`n").TrimEnd("`n") + "`n" }
function Write-LegacyNote {
    if ($legacy) {
        Write-Output 'Removed the combined FreeLab block of earlier versions, which also applied dark mode at launch. To keep that, run tools\install-fontlab.ps1 from FreeLab.'
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
