[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ApplicationDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ApplicationDirectory = [IO.Path]::GetFullPath($ApplicationDirectory)
if (-not (Test-Path -LiteralPath $ApplicationDirectory -PathType Container)) {
    throw "Application directory does not exist: $ApplicationDirectory"
}

foreach ($RelativePath in (
    '_internal\_tcl_data\init.tcl',
    '_internal\_tk_data\tk.tcl'
)) {
    $PayloadFile = Join-Path $ApplicationDirectory $RelativePath
    if (-not (Test-Path -LiteralPath $PayloadFile -PathType Leaf)) {
        throw "Frozen Tcl/Tk payload is missing: $PayloadFile"
    }
    if ((Get-Item -LiteralPath $PayloadFile -Force).Length -le 0) {
        throw "Frozen Tcl/Tk payload file is empty: $PayloadFile"
    }
}

$TclModuleDirectory = Join-Path $ApplicationDirectory '_internal\tcl8'
if (-not (Test-Path -LiteralPath $TclModuleDirectory -PathType Container)) {
    throw "Frozen Tcl module directory is missing: $TclModuleDirectory"
}
$TclModuleFile = Get-ChildItem -LiteralPath $TclModuleDirectory -Recurse -File -Force |
    Where-Object { $_.Length -gt 0 } |
    Select-Object -First 1
if ($null -eq $TclModuleFile) {
    throw "Frozen Tcl module directory contains no non-empty files: $TclModuleDirectory"
}

Write-Host "Frozen Tcl/Tk payload verified: $ApplicationDirectory"
