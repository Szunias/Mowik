[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '2.8.0',

    [Parameter()]
    [string]$InstallerFileName = "Mowik-$Version-Setup.exe",

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
    [switch]$AllowLocalMachineMutation,

    [Parameter()]
    [ValidateRange(5, 600)]
    [int]$SettingsStartupTimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$TempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }

function Get-SettingsSmokeDiagnostics {
    <#
        Runner nie zachowuje katalogu testowego, więc powód porażki musi trafić
        do komunikatu od razu; inaczej zostaje samo „nie utworzył konfiguracji”.
    #>
    param(
        [Parameter(Mandatory)] [string]$SmokeRoot
    )

    $Lines = [Collections.Generic.List[string]]::new()
    try {
        $Files = @(
            Get-ChildItem -LiteralPath $SmokeRoot -Recurse -File -ErrorAction SilentlyContinue |
                Select-Object -First 40
        )
        if ($Files.Count -eq 0) {
            $Lines.Add('Katalog testowy jest pusty.')
        }
        else {
            $Lines.Add('Pliki utworzone przez panel ustawień:')
            foreach ($File in $Files) {
                $Lines.Add("  $($File.FullName.Substring($SmokeRoot.Length)) ($($File.Length) B)")
            }
        }

        $LogPath = Join-Path $SmokeRoot 'Local\Mowik\mowik.log'
        if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
            $Lines.Add('Ostatnie wpisy dziennika:')
            foreach ($Entry in (Get-Content -LiteralPath $LogPath -Tail 25 -ErrorAction SilentlyContinue)) {
                $Lines.Add("  $Entry")
            }
        }
        else {
            $Lines.Add('Panel ustawień nie założył nawet pliku dziennika.')
        }
    }
    catch {
        $Lines.Add("Nie udało się zebrać diagnostyki: $($_.Exception.Message)")
    }
    return [Environment]::NewLine + ($Lines -join [Environment]::NewLine)
}
if ([IO.Path]::GetFileName($InstallerFileName) -ne $InstallerFileName -or
    $InstallerFileName -notmatch '^Mowik-[0-9]+\.[0-9]+\.[0-9]+-Setup(?:-UNSIGNED)?\.exe$') {
    throw "Invalid installer file name: $InstallerFileName"
}
if (-not $InstallerFileName.StartsWith("Mowik-$Version-", [StringComparison]::Ordinal)) {
    throw "Installer file name does not match version ${Version}: $InstallerFileName"
}
$Installer = Join-Path (Join-Path $Root 'release') $InstallerFileName

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
elseif ($InstallerFileName -notlike '*-UNSIGNED.exe') {
    throw 'An unsigned installer QA run requires the explicit -UNSIGNED.exe file name.'
}
else {
    $InstallerSignature = Get-AuthenticodeSignature -LiteralPath $Installer
    if ($InstallerSignature.Status -ne [Management.Automation.SignatureStatus]::NotSigned) {
        throw "Unsigned installer QA expected NotSigned, got $($InstallerSignature.Status)."
    }
}

foreach ($SelectedLanguage in $Language) {
    $TestDir = Join-Path $TempRoot "Mowik-Installer-QA-$Version-$SelectedLanguage-$PID"
    $InstallLog = Join-Path $TempRoot "Mowik-$Version-$SelectedLanguage-install-$PID.log"
    $RepairLog = Join-Path $TempRoot "Mowik-$Version-$SelectedLanguage-repair-$PID.log"
    $SmokeRoot = Join-Path $TempRoot "Mowik-Settings-Smoke-$Version-$SelectedLanguage-$PID"
    $AppExe = Join-Path $TestDir 'Mowik.exe'
    $Uninstaller = Join-Path $TestDir 'unins000.exe'
    $MayRunUninstaller = -not $RequireAuthenticode
    $SettingsProcess = $null

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
        "/DIR=`"$TestDir`"",
        "/LOG=`"$InstallLog`""
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
        else {
            foreach ($UnsignedPath in @($AppExe, $Uninstaller)) {
                $UnsignedSignature = Get-AuthenticodeSignature -LiteralPath $UnsignedPath
                if ($UnsignedSignature.Status -ne
                    [Management.Automation.SignatureStatus]::NotSigned) {
                    throw (
                        "Unsigned installer QA expected NotSigned for $UnsignedPath, " +
                        "got $($UnsignedSignature.Status)."
                    )
                }
            }
        }

        $App = Start-Process -FilePath $AppExe -ArgumentList '--version' -Wait -PassThru
        if ($App.ExitCode -ne 0) {
            throw "Zainstalowany Mowik.exe ($SelectedLanguage) zakończył się kodem $($App.ExitCode)."
        }

        # A second run of the same installer is the supported repair path.
        # Corrupt the isolated installed executable and require repair to
        # restore the exact payload before any further smoke test runs.
        $ExpectedAppHash = (Get-FileHash -LiteralPath $AppExe -Algorithm SHA256).Hash
        [IO.File]::WriteAllBytes($AppExe, [byte[]](0x4D, 0x5A))
        $RepairArguments = @(
            '/VERYSILENT',
            '/SUPPRESSMSGBOXES',
            '/NORESTART',
            '/NOICONS',
            '/TASKS=',
            "/LANG=$SelectedLanguage",
            "/DIR=`"$TestDir`"",
            "/LOG=`"$RepairLog`""
        )
        $Repair = Start-Process `
            -FilePath $Installer `
            -ArgumentList $RepairArguments `
            -Wait `
            -PassThru
        if ($Repair.ExitCode -ne 0) {
            throw "Naprawa instalacji ($SelectedLanguage) zakończyła się kodem $($Repair.ExitCode). Log: $RepairLog"
        }
        $RepairedAppHash = (Get-FileHash -LiteralPath $AppExe -Algorithm SHA256).Hash
        if ($RepairedAppHash -cne $ExpectedAppHash) {
            throw "Naprawa instalacji ($SelectedLanguage) nie odtworzyła Mowik.exe."
        }

        # Full dictation startup intentionally remains outside hosted CI: it
        # needs a speech model and physical audio input.
        #
        # The smoke must not be `--settings`. Hosted Windows runners are
        # elevated, and Mówik blocks elevated startup with a modal message box
        # that nobody can dismiss there, so the process hangs until the job
        # times out. `--runtime-gui-smoke-test` is exempt from that guard by
        # design and still exercises frozen imports and Tk initialization.
        # Writing per-user configuration is covered by the unit suite, which
        # pins the elevation check instead of reading the real token.
        $SmokeAppData = Join-Path $SmokeRoot 'Roaming'
        $SmokeLocalData = Join-Path $SmokeRoot 'Local'
        New-Item -ItemType Directory -Path $SmokeAppData, $SmokeLocalData -ErrorAction Stop | Out-Null
        $PreviousAppData = $env:APPDATA
        $PreviousLocalAppData = $env:LOCALAPPDATA
        try {
            $env:APPDATA = $SmokeAppData
            $env:LOCALAPPDATA = $SmokeLocalData
            $SettingsProcess = Start-Process `
                -FilePath $AppExe `
                -ArgumentList '--runtime-gui-smoke-test' `
                -PassThru
            if (-not $SettingsProcess.WaitForExit($SettingsStartupTimeoutSeconds * 1000)) {
                throw (
                    "Zamrożony start GUI ($SelectedLanguage) nie zakończył się " +
                    "w ciągu $SettingsStartupTimeoutSeconds s." +
                    (Get-SettingsSmokeDiagnostics -SmokeRoot $SmokeRoot)
                )
            }
            if ($SettingsProcess.ExitCode -ne 0) {
                throw (
                    "Zamrożony start GUI ($SelectedLanguage) zakończył się kodem " +
                    "$($SettingsProcess.ExitCode)." +
                    (Get-SettingsSmokeDiagnostics -SmokeRoot $SmokeRoot)
                )
            }
        }
        finally {
            if ($null -ne $SettingsProcess -and -not $SettingsProcess.HasExited) {
                Stop-Process -Id $SettingsProcess.Id -Force -ErrorAction SilentlyContinue
                $SettingsProcess.WaitForExit(10000) | Out-Null
            }
            $env:APPDATA = $PreviousAppData
            $env:LOCALAPPDATA = $PreviousLocalAppData
        }
    }
    finally {
        if ($null -ne $SettingsProcess -and -not $SettingsProcess.HasExited) {
            Stop-Process -Id $SettingsProcess.Id -Force -ErrorAction SilentlyContinue
        }
        if ((Test-Path -LiteralPath $Uninstaller) -and $MayRunUninstaller) {
            $Uninstall = Start-Process -FilePath $Uninstaller -ArgumentList @(
                '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
            ) -Wait -PassThru
            if ($Uninstall.ExitCode -ne 0) {
                throw "Deinstalator ($SelectedLanguage) zakończył się kodem $($Uninstall.ExitCode)."
            }
        }
        if (Test-Path -LiteralPath $SmokeRoot) {
            Remove-Item -LiteralPath $SmokeRoot -Recurse -Force
        }
    }

    $Deadline = (Get-Date).AddMinutes(3)
    while ((Test-Path -LiteralPath $AppExe) -and ((Get-Date) -lt $Deadline)) {
        Start-Sleep -Milliseconds 500
    }
    if (Test-Path -LiteralPath $AppExe) {
        throw "Deinstalator ($SelectedLanguage) nie usunął aplikacji z $TestDir."
    }

    Write-Host "Instalacja, naprawa, start ustawień i deinstalacja Mówika ${Version} ($SelectedLanguage): OK" -ForegroundColor Green
}
