[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '2.7.5',

    [Parameter()]
    [string]$InstallerFileName = "Mowik-$Version-Setup.exe",

    [Parameter(Mandatory)]
    [ValidateSet('UnsignedLocal', 'UnsignedRelease', 'SignedRelease')]
    [string]$ExpectedBuildMode,

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
$BuildInfoFileName = 'BUILD-INFO.txt'
$HashFileName = 'SHA256SUMS.txt'
Import-Module (Join-Path $PSScriptRoot 'WindowsReleaseTools.psm1') -Force -DisableNameChecking

if ([IO.Path]::GetFileName($InstallerFileName) -ne $InstallerFileName -or
    $InstallerFileName -notmatch
        '^Mowik-[0-9]+\.[0-9]+\.[0-9]+-Setup(?:-LOCAL-UNSIGNED|-UNSIGNED)?\.exe$') {
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

$ExpectedNames = @($InstallerFileName, $BuildInfoFileName, $HashFileName) | Sort-Object
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
$ExpectedInstallerName = switch ($ExpectedBuildMode) {
    'SignedRelease' { "Mowik-$Version-Setup.exe" }
    'UnsignedRelease' { "Mowik-$Version-Setup-UNSIGNED.exe" }
    'UnsignedLocal' { "Mowik-$Version-Setup-LOCAL-UNSIGNED.exe" }
}
if ($InstallerFileName -cne $ExpectedInstallerName) {
    throw (
        "Installer name '$InstallerFileName' does not match build mode " +
        "'$ExpectedBuildMode'."
    )
}
if (($ExpectedBuildMode -eq 'SignedRelease') -ne [bool]$RequireAuthenticode) {
    throw 'Only SignedRelease can require Authenticode, and it must require it.'
}
if (@($ActualItems | Where-Object {
    ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
}).Count -gt 0) {
    throw 'Release payload must not contain reparse points.'
}

$Installer = Join-Path $ReleaseDir $InstallerFileName
$BuildInfoPath = Join-Path $ReleaseDir $BuildInfoFileName
$HashPath = Join-Path $ReleaseDir $HashFileName
if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
    throw "Installer not found: $Installer"
}
if (-not (Test-Path -LiteralPath $HashPath -PathType Leaf)) {
    throw "Checksum file not found: $HashPath"
}
if (-not (Test-Path -LiteralPath $BuildInfoPath -PathType Leaf)) {
    throw "Build information file not found: $BuildInfoPath"
}

$Hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
$SourceIdentity = Get-ReleaseSourceIdentity -ProjectRoot $Root
$ExpectedBuildInfo = @(
    'MOWIK-RELEASE-BUILD-INFO-V2'
    "version`t$Version"
    "build-mode`t$ExpectedBuildMode"
    "installer`t$InstallerFileName"
    "installer-sha256`t$Hash"
    "source-sha256`t$SourceIdentity"
) -join "`n"
$RawBuildInfo = [IO.File]::ReadAllText($BuildInfoPath, [Text.Encoding]::ASCII)
if ($RawBuildInfo -cne ($ExpectedBuildInfo + "`n")) {
    throw 'BUILD-INFO.txt does not match the current release source and installer.'
}
$BuildInfoHash = (
    Get-FileHash -LiteralPath $BuildInfoPath -Algorithm SHA256
).Hash.ToLowerInvariant()
$ExpectedHashFile = @(
    "$Hash  $InstallerFileName"
    "$BuildInfoHash  $BuildInfoFileName"
) -join "`n"
$RawHashFile = [IO.File]::ReadAllText($HashPath, [Text.Encoding]::ASCII)
if (($RawHashFile -cne ($ExpectedHashFile + "`n")) -and
    ($RawHashFile -cne ($ExpectedHashFile.Replace("`n", "`r`n") + "`r`n"))) {
    throw 'SHA256SUMS.txt is non-canonical or does not match the installer.'
}

if ($RequireAuthenticode) {
    if ([string]::IsNullOrWhiteSpace($ExpectedSignerThumbprint)) {
        throw '-RequireAuthenticode requires -ExpectedSignerThumbprint.'
    }
    Assert-AuthenticodeSignature `
        -Path $Installer `
        -ExpectedSignerThumbprint $ExpectedSignerThumbprint `
        -SignToolPath $SignToolPath
}
elseif ($ExpectedBuildMode -eq 'SignedRelease') {
    throw 'An unsigned artifact must use the explicit -UNSIGNED.exe file name.'
}

Write-Host "Release payload verified and immutable by name/hash: $InstallerFileName" -ForegroundColor Green
