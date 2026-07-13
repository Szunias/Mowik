[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '2.7.3',

    [Parameter()]
    [string]$InstallerFileName = "Mowik-$Version-Setup.exe",

    [Parameter()]
    [switch]$RequireAuthenticode,

    [Parameter()]
    [string]$ExpectedSignerThumbprint,

    [Parameter()]
    [string]$SignToolPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$ReleaseDir = Join-Path $Root 'release'
$HashFileName = 'SHA256SUMS.txt'

if ([IO.Path]::GetFileName($InstallerFileName) -ne $InstallerFileName -or
    $InstallerFileName -notmatch '^Mowik-[0-9]+\.[0-9]+\.[0-9]+-Setup(?:-UNSIGNED)?\.exe$') {
    throw "Invalid installer file name: $InstallerFileName"
}
if (-not $InstallerFileName.StartsWith("Mowik-$Version-", [StringComparison]::Ordinal)) {
    throw "Installer file name does not match version ${Version}: $InstallerFileName"
}
if (-not (Test-Path -LiteralPath $ReleaseDir -PathType Container)) {
    throw "Release directory not found: $ReleaseDir"
}

$ReleaseItem = Get-Item -LiteralPath $ReleaseDir -Force
if (($ReleaseItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "Release directory must not be a reparse point: $ReleaseDir"
}

$ExpectedNames = @($InstallerFileName, $HashFileName) | Sort-Object
$ActualItems = @(Get-ChildItem -LiteralPath $ReleaseDir -Force)
$ActualNames = @($ActualItems | ForEach-Object { $_.Name } | Sort-Object)
if (($ActualNames.Count -ne $ExpectedNames.Count) -or
    (($ActualNames -join "`n") -cne ($ExpectedNames -join "`n"))) {
    throw (
        'Release directory contains an unexpected payload. Expected only: ' +
        ($ExpectedNames -join ', ') + '; found: ' + ($ActualNames -join ', ')
    )
}
if (@($ActualItems | Where-Object { $_.PSIsContainer }).Count -gt 0) {
    throw 'Release payload must contain files only.'
}

$Installer = Join-Path $ReleaseDir $InstallerFileName
$HashPath = Join-Path $ReleaseDir $HashFileName
if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
    throw "Installer not found: $Installer"
}
if (-not (Test-Path -LiteralPath $HashPath -PathType Leaf)) {
    throw "Checksum file not found: $HashPath"
}

$Hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
$ExpectedHashLine = "$Hash  $InstallerFileName"
$RawHashFile = [IO.File]::ReadAllText($HashPath, [Text.Encoding]::ASCII)
if (($RawHashFile -cne ($ExpectedHashLine + "`n")) -and
    ($RawHashFile -cne ($ExpectedHashLine + "`r`n"))) {
    throw 'SHA256SUMS.txt is non-canonical or does not match the installer.'
}

if ($RequireAuthenticode) {
    if ([string]::IsNullOrWhiteSpace($ExpectedSignerThumbprint)) {
        throw '-RequireAuthenticode requires -ExpectedSignerThumbprint.'
    }
    Import-Module (Join-Path $PSScriptRoot 'WindowsReleaseTools.psm1') -Force -DisableNameChecking
    Assert-AuthenticodeSignature `
        -Path $Installer `
        -ExpectedSignerThumbprint $ExpectedSignerThumbprint `
        -SignToolPath $SignToolPath
}
elseif ($InstallerFileName -notlike '*-UNSIGNED.exe') {
    throw 'An unsigned artifact must use the explicit -UNSIGNED.exe file name.'
}

Write-Host "Release payload verified and immutable by name/hash: $InstallerFileName" -ForegroundColor Green
