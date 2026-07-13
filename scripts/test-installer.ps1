[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '2.7.0',

    [Parameter()]
    [ValidateSet('english', 'polish')]
    [string[]]$Language = @('english', 'polish'),

    [Parameter()]
    [switch]$RequireAuthenticode,

    [Parameter()]
    [string]$ExpectedSignerThumbprint,

    [Parameter()]
    [string]$SignToolPath,

    [Parameter()]
    [switch]$AllowLocalMachineMutation
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$TempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$Installer = Join-Path $Root "release\Mowik-$Version-Setup.exe"

if (-not $env:GITHUB_ACTIONS -and -not $AllowLocalMachineMutation) {
    throw (
        'The installer QA uses the production AppId and may affect an existing ' +
        'Mowik installation. Run it on GitHub Actions or pass ' +
        '-AllowLocalMachineMutation on an isolated Windows account.'
    )
}

if (-not (Test-Path -LiteralPath $Installer)) {
    throw "Brak instalatora: $Installer"
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
elseif ($ExpectedSignerThumbprint -or $SignToolPath) {
    throw 'Signature inputs require -RequireAuthenticode.'
}

foreach ($SelectedLanguage in $Language) {
    $TestDir = Join-Path $TempRoot "Mowik-Installer-QA-$Version-$SelectedLanguage-$PID"
    $InstallLog = Join-Path $TempRoot "Mowik-$Version-$SelectedLanguage-install-$PID.log"
    $AppExe = Join-Path $TestDir 'Mowik.exe'
    $Uninstaller = Join-Path $TestDir 'unins000.exe'
    $MayRunUninstaller = -not $RequireAuthenticode

    if (Test-Path -LiteralPath $TestDir) {
        throw "Katalog testowy już istnieje: $TestDir"
    }

    $SetupArguments = @(
        '/VERYSILENT',
        '/SUPPRESSMSGBOXES',
        '/NORESTART',
        '/NOICONS',
        '/TASKS=',
        "/LANG=$SelectedLanguage",
        "/DIR=$TestDir",
        "/LOG=$InstallLog"
    )

    try {
        $Setup = Start-Process -FilePath $Installer -ArgumentList $SetupArguments -Wait -PassThru
        if ($Setup.ExitCode -ne 0) {
            throw "Instalator ($SelectedLanguage) zakończył się kodem $($Setup.ExitCode). Log: $InstallLog"
        }

        if (-not (Test-Path -LiteralPath $AppExe)) {
            throw "Instalator ($SelectedLanguage) nie utworzył Mowik.exe."
        }
        if (-not (Test-Path -LiteralPath $Uninstaller)) {
            throw "Instalator ($SelectedLanguage) nie utworzył deinstalatora."
        }

        if ($RequireAuthenticode) {
            Assert-AuthenticodeSignature `
                -Path @($AppExe, $Uninstaller) `
                -ExpectedSignerThumbprint $ExpectedSignerThumbprint `
                -SignToolPath $SignToolPath
            $MayRunUninstaller = $true
        }

        $App = Start-Process -FilePath $AppExe -ArgumentList '--version' -Wait -PassThru
        if ($App.ExitCode -ne 0) {
            throw "Zainstalowany Mowik.exe ($SelectedLanguage) zakończył się kodem $($App.ExitCode)."
        }
    }
    finally {
        if ((Test-Path -LiteralPath $Uninstaller) -and $MayRunUninstaller) {
            $Uninstall = Start-Process -FilePath $Uninstaller -ArgumentList @(
                '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
            ) -Wait -PassThru
            if ($Uninstall.ExitCode -ne 0) {
                throw "Deinstalator ($SelectedLanguage) zakończył się kodem $($Uninstall.ExitCode)."
            }
        }
    }

    $Deadline = (Get-Date).AddMinutes(3)
    while ((Test-Path -LiteralPath $AppExe) -and ((Get-Date) -lt $Deadline)) {
        Start-Sleep -Milliseconds 500
    }
    if (Test-Path -LiteralPath $AppExe) {
        throw "Deinstalator ($SelectedLanguage) nie usunął aplikacji z $TestDir."
    }

    Write-Host "Instalacja, uruchomienie i deinstalacja Mówika ${Version} ($SelectedLanguage): OK" -ForegroundColor Green
}
