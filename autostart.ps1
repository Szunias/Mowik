[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("enable", "disable")]
    [string]$Mode
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Startup = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $Startup "Mowik.lnk"

try {
    if ($Mode -eq "disable") {
        if (Test-Path -LiteralPath $ShortcutPath) {
            Remove-Item -LiteralPath $ShortcutPath -Force
        }
        Write-Host "Autostart Mowika zostal wylaczony."
        exit 0
    }

    $PythonW = Join-Path $Root ".venv\Scripts\pythonw.exe"
    $Script = Join-Path $Root "mowik.py"
    if (-not (Test-Path -LiteralPath $PythonW)) {
        throw "Najpierw uruchom ZAINSTALUJ.cmd."
    }
    if (-not (Test-Path -LiteralPath $Script)) {
        throw "Brakuje pliku mowik.py."
    }

    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $PythonW
    $Shortcut.Arguments = '"' + $Script + '"'
    $Shortcut.WorkingDirectory = $Root
    $Shortcut.Description = "Lokalne dyktowanie Mowik"
    $Shortcut.Save()

    Write-Host "Autostart Mowika zostal wlaczony."
    Write-Host "Skrot: $ShortcutPath"
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
