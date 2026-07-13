[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '2.3.0'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$TempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$TestDir = Join-Path $TempRoot "Mowik-Installer-QA-$Version-$PID"
$Installer = Join-Path $Root "release\Mowik-$Version-Setup.exe"
$InstallLog = Join-Path $TempRoot "Mowik-$Version-install-$PID.log"

if (-not (Test-Path -LiteralPath $Installer)) {
    throw "Brak instalatora: $Installer"
}
if (Test-Path -LiteralPath $TestDir) {
    throw "Katalog testowy już istnieje: $TestDir"
}

$SetupArguments = @(
    '/VERYSILENT',
    '/SUPPRESSMSGBOXES',
    '/NORESTART',
    '/NOICONS',
    '/TASKS=',
    "/DIR=$TestDir",
    "/LOG=$InstallLog"
)
$Setup = Start-Process -FilePath $Installer -ArgumentList $SetupArguments -Wait -PassThru
if ($Setup.ExitCode -ne 0) {
    throw "Instalator zakończył się kodem $($Setup.ExitCode). Log: $InstallLog"
}

$AppExe = Join-Path $TestDir 'Mowik.exe'
$Uninstaller = Join-Path $TestDir 'unins000.exe'
if (-not (Test-Path -LiteralPath $AppExe)) {
    throw 'Instalator nie utworzył Mowik.exe.'
}
if (-not (Test-Path -LiteralPath $Uninstaller)) {
    throw 'Instalator nie utworzył deinstalatora.'
}

$App = Start-Process -FilePath $AppExe -ArgumentList '--version' -Wait -PassThru
if ($App.ExitCode -ne 0) {
    throw "Zainstalowany Mowik.exe zakończył się kodem $($App.ExitCode)."
}

$Uninstall = Start-Process -FilePath $Uninstaller -ArgumentList @(
    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
) -Wait -PassThru
if ($Uninstall.ExitCode -ne 0) {
    throw "Deinstalator zakończył się kodem $($Uninstall.ExitCode)."
}

$Deadline = (Get-Date).AddMinutes(3)
while ((Test-Path -LiteralPath $AppExe) -and ((Get-Date) -lt $Deadline)) {
    Start-Sleep -Milliseconds 500
}
if (Test-Path -LiteralPath $AppExe) {
    throw "Deinstalator nie usunął aplikacji z $TestDir."
}

Write-Host "Instalacja, uruchomienie i deinstalacja Mówika ${Version}: OK" -ForegroundColor Green
