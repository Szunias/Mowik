[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '2.5.0',

    [Parameter()]
    [switch]$SkipTests,

    [Parameter()]
    [switch]$SkipToolInstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$BuildDir = Join-Path $Root 'build'
$DistDir = Join-Path $Root 'dist'
$ReleaseDir = Join-Path $Root 'release'

function Invoke-Checked {
    param(
        [Parameter(Mandatory)] [string]$FilePath,
        [Parameter()] [string[]]$ArgumentList = @()
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Polecenie zakończyło się kodem $LASTEXITCODE`: $FilePath $($ArgumentList -join ' ')"
    }
}

function Remove-ProjectDirectory {
    param([Parameter(Mandatory)] [string]$Path)

    $ResolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $ResolvedPath = [IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
    if (-not $ResolvedPath.StartsWith($ResolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Odmowa usunięcia katalogu poza projektem: $ResolvedPath"
    }
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Find-InnoCompiler {
    $Command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($null -ne $Command) {
        return $Command.Source
    }

    $Candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate) {
            return $Candidate
        }
    }
    return $null
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Instalator Mówika można zbudować wyłącznie w Windows.'
}

Set-Location $Root
& (Join-Path $PSScriptRoot 'test-release-version.ps1') -Version $Version

if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Brak .venv. Najpierw uruchom ZAINSTALUJ.cmd.'
}

Write-Host "[1/6] Przygotowuję zależności wydania..." -ForegroundColor Cyan
Invoke-Checked $Python @('-m', 'pip', 'install', '--disable-pip-version-check', 'PyInstaller==6.21.0')
Invoke-Checked $Python @('-m', 'pip', 'install', '--disable-pip-version-check', '--prefer-binary', '-r', 'requirements.txt', '-r', 'requirements-gpu.txt')

Write-Host "[2/6] Generuję ikonę i uruchamiam testy..." -ForegroundColor Cyan
Invoke-Checked $Python @('scripts\generate-icon.py')
if (-not $SkipTests) {
    Invoke-Checked $Python @('-m', 'unittest', 'discover', '-s', 'tests', '-v')
}

Write-Host "[3/6] Buduję aplikację Windows..." -ForegroundColor Cyan
Remove-ProjectDirectory $BuildDir
Remove-ProjectDirectory $DistDir
Remove-ProjectDirectory $ReleaseDir
Invoke-Checked $Python @('-m', 'PyInstaller', '--noconfirm', '--clean', 'packaging\Mowik.spec')
$AppExe = Join-Path $DistDir 'Mowik\Mowik.exe'
if (-not (Test-Path -LiteralPath $AppExe)) {
    throw "PyInstaller nie utworzył pliku: $AppExe"
}
$BuiltVersion = (Get-Item -LiteralPath $AppExe).VersionInfo.ProductVersion
if ($BuiltVersion -notlike "$Version*") {
    throw "Metadane Mowik.exe mają wersję '$BuiltVersion', oczekiwano '$Version'."
}
$DpiManifestProbe = @'
import sys
from PyInstaller.utils.win32.winmanifest import read_manifest_from_executable

manifest = read_manifest_from_executable(sys.argv[1]).decode("utf-8")
required = (
    ">true</dpiAware>",
    ">System</dpiAwareness>",
    ">true</longPathAware>",
)
missing = [value for value in required if value not in manifest]
if missing:
    raise SystemExit(f"Mowik.exe manifest is missing: {', '.join(missing)}")
print("Mowik.exe DPI manifest: SystemAware")
'@
Invoke-Checked $Python @('-c', $DpiManifestProbe, $AppExe)
$SmokeProcess = Start-Process -FilePath $AppExe -ArgumentList '--version' -Wait -PassThru
if ($SmokeProcess.ExitCode -ne 0) {
    throw "Test Mowik.exe --version zakończył się kodem $($SmokeProcess.ExitCode)."
}

Write-Host "[4/6] Przygotowuję Inno Setup..." -ForegroundColor Cyan
$Iscc = Find-InnoCompiler
if (($null -eq $Iscc) -and (-not $SkipToolInstall)) {
    $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($null -eq $Winget) {
        throw 'Brak Inno Setup i winget. Zainstaluj Inno Setup 6 lub uruchom bez -SkipToolInstall.'
    }
    Invoke-Checked $Winget.Source @(
        'install', '--id', 'JRSoftware.InnoSetup', '--exact', '--scope', 'user',
        '--silent', '--accept-package-agreements', '--accept-source-agreements',
        '--disable-interactivity'
    )
    $Iscc = Find-InnoCompiler
}
if ($null -eq $Iscc) {
    throw 'Nie znaleziono ISCC.exe (Inno Setup 6).'
}

Write-Host "[5/6] Buduję właściwy instalator..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
Invoke-Checked $Iscc @("/DMyAppVersion=$Version", 'packaging\Mowik.iss')
$Installer = Join-Path $ReleaseDir "Mowik-$Version-Setup.exe"
if (-not (Test-Path -LiteralPath $Installer)) {
    throw "Inno Setup nie utworzył pliku: $Installer"
}

Write-Host "[6/6] Zapisuję sumę kontrolną..." -ForegroundColor Cyan
$Hash = Get-FileHash -LiteralPath $Installer -Algorithm SHA256
$HashLine = "$($Hash.Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($Installer))"
Set-Content -LiteralPath (Join-Path $ReleaseDir 'SHA256SUMS.txt') -Value $HashLine -Encoding ASCII

$SizeMiB = [Math]::Round((Get-Item -LiteralPath $Installer).Length / 1MB, 1)
Write-Host "Gotowe: $Installer ($SizeMiB MiB)" -ForegroundColor Green
Write-Host "SHA-256: $($Hash.Hash.ToLowerInvariant())"
