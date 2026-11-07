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
$utf8 = New-Object System.Text.UTF8Encoding $false

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
    $pattern = '(?s)\n?' + [regex]::Escape($begin) + '.*?' + [regex]::Escape($end) + '[^\n]*\n?'
    return [regex]::Replace($text, $pattern, "`n").TrimEnd("`n") + "`n"
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

$current = ''
if (Test-Path $startup) { $current = [System.IO.File]::ReadAllText($startup) -replace "`r`n", "`n" }
$legacy = [regex]::IsMatch($current, $legacyPattern)
$hadBlock = $current.Contains($begin)
if ($legacy) { $current = [regex]::Replace($current, $legacyPattern, "`n").TrimEnd("`n") + "`n" }
function Write-LegacyNote {
    if ($legacy) {
        Write-Output 'Removed the combined FreeLab block of earlier versions, which also applied dark mode at launch. To keep that, run tools\install-fontlab.ps1 from FreeLab.'
    }
}

function Save-Startup([string]$text) {
    if (-not (Test-Path "$startup.freelab-backup")) {
        [System.IO.File]::Copy($startup, "$startup.freelab-backup")
        Write-Output "Kept the original startup script as $startup.freelab-backup"
    }
    [System.IO.File]::WriteAllText($startup, $text, $utf8)
}

if ($Uninstall) {
    if (Test-Path $startup) {
        if ($hadBlock -or $legacy) {
            Save-Startup (Remove-Block $current)
            Write-Output "Removed the FreeLab MCP block from $startup"
            Write-LegacyNote
        }
    }
    foreach ($dir in @($scripts, $rootScripts)) {  # the Scripts folder itself held it before FreeLab 0.3
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
$old = Join-Path $rootScripts $listenerName  # before FreeLab 0.3 it sat in the Scripts folder itself
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
    name = "freelab_listener"
    os.environ["FREELAB_NO_AUTOSTART"] = "1"  # load only; main() below starts it
    try:
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
    finally:
        del os.environ["FREELAB_NO_AUTOSTART"]
    sys.modules[name] = module
    module.main()
try:
    _freelab_mcp_startup()
except Exception as exc:
    print("FreeLab listener at startup: %s" % (exc,))
$end
"@ -replace "`r`n", "`n"
    $folder = Split-Path -Parent $startup
    if (-not (Test-Path $folder)) { New-Item -ItemType Directory -Path $folder | Out-Null }
    if (Test-Path $startup) { Save-Startup ($text + $block + "`n") }
    else { [System.IO.File]::WriteAllText($startup, $text + $block + "`n", $utf8) }
    Write-Output "Added the FreeLab MCP block to ${startup}: FontLab starts the listener at every launch"
    Write-LegacyNote
}
elseif ($hadBlock -or $legacy) {
    Save-Startup $text
    Write-Output "Removed the FreeLab MCP block from ${startup}: start the listener with Scripts > FreeLab > Start Listener"
    Write-LegacyNote
}
