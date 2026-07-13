[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '2.7.3',

    [Parameter()]
    [ValidateSet('UnsignedLocal', 'UnsignedRelease', 'SignedRelease')]
    [string]$BuildMode = 'UnsignedLocal',

    [Parameter()]
    [string]$SigningCertificateThumbprint,

    [Parameter()]
    [ValidateSet('CurrentUser', 'LocalMachine')]
    [string]$SigningCertificateStore = 'CurrentUser',

    [Parameter()]
    [string]$TimestampServer,

    [Parameter()]
    [string]$SignToolPath,

    [Parameter()]
    [string]$InnoCompilerPath,

    [Parameter()]
    [switch]$SkipTests,

    [Parameter()]
    [switch]$SkipToolInstall,

    [Parameter()]
    [switch]$PrepareApplicationOnly,

    [Parameter()]
    [switch]$UsePreparedApplication,

    [Parameter()]
    [string]$PreparedAppManifestPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$BuildDir = Join-Path $Root 'build'
$DistDir = Join-Path $Root 'dist'
$ReleaseDir = Join-Path $Root 'release'
$ReleaseTools = Join-Path $PSScriptRoot 'WindowsReleaseTools.psm1'

Import-Module $ReleaseTools -Force -DisableNameChecking

function Invoke-Checked {
    param(
        [Parameter(Mandatory)] [string]$FilePath,
        [Parameter()] [string[]]$ArgumentList = @()
    )

    $global:LASTEXITCODE = 0
    & $FilePath @ArgumentList
    if ((-not $?) -or ($LASTEXITCODE -ne 0)) {
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
        $Item = Get-Item -LiteralPath $Path -Force
        if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Odmowa usunięcia dowiązania/reparse point: $ResolvedPath"
        }
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Write-NewAsciiFile {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$Value
    )

    $Encoding = [Text.ASCIIEncoding]::new()
    $Stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $Bytes = $Encoding.GetBytes($Value)
        $Stream.Write($Bytes, 0, $Bytes.Length)
        $Stream.Flush($true)
    }
    finally {
        $Stream.Dispose()
    }
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Instalator Mówika można zbudować wyłącznie w Windows.'
}

$IsSignedRelease = $BuildMode -eq 'SignedRelease'
$IsUnsignedRelease = $BuildMode -eq 'UnsignedRelease'
$IsReleaseBuild = $IsSignedRelease -or $IsUnsignedRelease
$ResolvedSignTool = $null
$ResolvedInnoCompiler = $null
$SigningCertificate = $null

if ($PrepareApplicationOnly -and $UsePreparedApplication) {
    throw '-PrepareApplicationOnly and -UsePreparedApplication are mutually exclusive.'
}
if ($PrepareApplicationOnly -and $IsReleaseBuild) {
    throw '-PrepareApplicationOnly must use the UnsignedLocal build mode.'
}
if ($UsePreparedApplication -and (-not $IsSignedRelease)) {
    throw '-UsePreparedApplication is allowed only for SignedRelease.'
}
if (($PrepareApplicationOnly -or $UsePreparedApplication) -and
    [string]::IsNullOrWhiteSpace($PreparedAppManifestPath)) {
    throw '-PrepareApplicationOnly and -UsePreparedApplication require -PreparedAppManifestPath.'
}
if ((-not $PrepareApplicationOnly) -and (-not $UsePreparedApplication) -and
    -not [string]::IsNullOrWhiteSpace($PreparedAppManifestPath)) {
    throw '-PreparedAppManifestPath is valid only for a prepared-application build.'
}

if ($IsSignedRelease) {
    if (-not $UsePreparedApplication) {
        throw 'SignedRelease requires -UsePreparedApplication and its verified directory manifest.'
    }
    if ($SkipTests) {
        throw 'SignedRelease cannot be built with -SkipTests.'
    }
    if ([string]::IsNullOrWhiteSpace($SigningCertificateThumbprint)) {
        throw 'SignedRelease requires -SigningCertificateThumbprint.'
    }
    if ([string]::IsNullOrWhiteSpace($TimestampServer)) {
        throw 'SignedRelease requires an explicit RFC 3161 -TimestampServer.'
    }
    if ([string]::IsNullOrWhiteSpace($InnoCompilerPath)) {
        throw 'SignedRelease requires a preinstalled, verified -InnoCompilerPath.'
    }
    if (-not $SkipToolInstall) {
        throw 'SignedRelease requires -SkipToolInstall so no installer is downloaded after key import.'
    }
    if (Test-Path -LiteralPath $ReleaseDir) {
        throw (
            "SignedRelease refuses to replace an existing release directory: $ReleaseDir. " +
            'Move or remove it explicitly after checking its contents.'
        )
    }

    $ResolvedSignTool = Resolve-SignToolPath -SignToolPath $SignToolPath
    $ResolvedInnoCompiler = Resolve-InnoCompiler -InnoCompilerPath $InnoCompilerPath
    $SigningCertificate = Assert-CodeSigningCertificate `
        -Thumbprint $SigningCertificateThumbprint `
        -StoreLocation $SigningCertificateStore
    Assert-TimestampServer -TimestampServer $TimestampServer | Out-Null
}
elseif ($SigningCertificateThumbprint -or $TimestampServer -or $SignToolPath) {
    throw (
        'Signing parameters were supplied to an unsigned build. ' +
        'Pass -BuildMode SignedRelease to enable the fail-closed signing pipeline.'
    )
}

if ($IsUnsignedRelease) {
    if ($SkipTests) {
        throw 'UnsignedRelease cannot be built with -SkipTests.'
    }
    if ([string]::IsNullOrWhiteSpace($InnoCompilerPath)) {
        throw 'UnsignedRelease requires a preinstalled, verified -InnoCompilerPath.'
    }
    if (-not $SkipToolInstall) {
        throw 'UnsignedRelease requires -SkipToolInstall so release tools stay pinned.'
    }
    if (Test-Path -LiteralPath $ReleaseDir) {
        throw (
            "UnsignedRelease refuses to replace an existing release directory: $ReleaseDir. " +
            'Move or remove it explicitly after checking its contents.'
        )
    }
    $ResolvedInnoCompiler = Resolve-InnoCompiler -InnoCompilerPath $InnoCompilerPath
}

Set-Location $Root
& (Join-Path $PSScriptRoot 'test-release-version.ps1') -Version $Version

$AppExe = Join-Path $DistDir 'Mowik\Mowik.exe'

if (-not $UsePreparedApplication) {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw 'Brak .venv. Najpierw uruchom ZAINSTALUJ.cmd.'
    }

    Write-Host "[1/7] Przygotowuję zależności wydania..." -ForegroundColor Cyan
    Invoke-Checked $Python @('-m', 'pip', 'install', '--disable-pip-version-check', 'PyInstaller==6.21.0')
    Invoke-Checked $Python @('-m', 'pip', 'install', '--disable-pip-version-check', '--prefer-binary', '-r', 'requirements.txt', '-r', 'requirements-gpu.txt')

    Write-Host "[2/7] Generuję ikonę i uruchamiam testy..." -ForegroundColor Cyan
    Invoke-Checked $Python @('scripts\generate-icon.py')
    if (-not $SkipTests) {
        Invoke-Checked $Python @('-m', 'unittest', 'discover', '-s', 'tests', '-v')
    }

    Write-Host "[3/7] Buduję aplikację Windows..." -ForegroundColor Cyan
    Remove-ProjectDirectory $BuildDir
    Remove-ProjectDirectory $DistDir
    if ((-not $IsReleaseBuild) -and (-not $PrepareApplicationOnly)) {
        Remove-ProjectDirectory $ReleaseDir
    }
    Invoke-Checked $Python @('-m', 'PyInstaller', '--noconfirm', '--clean', 'packaging\Mowik.spec')
    if (-not (Test-Path -LiteralPath $AppExe -PathType Leaf)) {
        throw "PyInstaller nie utworzył pliku: $AppExe"
    }
    $BuiltVersion = (Get-Item -LiteralPath $AppExe).VersionInfo.ProductVersion
    if ($BuiltVersion -cne $Version) {
        throw "Metadane Mowik.exe mają wersję '$BuiltVersion', oczekiwano '$Version'."
    }
    Invoke-Checked $Python @('scripts\test-exe-manifest.py', $AppExe)
    $SmokeProcess = Start-Process -FilePath $AppExe -ArgumentList '--version' -Wait -PassThru
    if ($SmokeProcess.ExitCode -ne 0) {
        throw "Test Mowik.exe --version zakończył się kodem $($SmokeProcess.ExitCode)."
    }

    if ($PrepareApplicationOnly) {
        Write-DirectoryIntegrityManifest `
            -Directory (Join-Path $DistDir 'Mowik') `
            -ManifestPath $PreparedAppManifestPath
        $ManifestHash = (
            Get-FileHash -LiteralPath $PreparedAppManifestPath -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        Write-Host "Prepared unsigned application: $AppExe" -ForegroundColor Green
        Write-Host "Directory manifest SHA-256: $ManifestHash"
        return
    }
}
else {
    Write-Host "[1-3/7] Weryfikuję wcześniej zbudowaną aplikację..." -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $AppExe -PathType Leaf)) {
        throw "Prepared Mowik.exe was not found: $AppExe"
    }
    $BuiltVersion = (Get-Item -LiteralPath $AppExe).VersionInfo.ProductVersion
    if ($BuiltVersion -cne $Version) {
        throw "Prepared Mowik.exe has version '$BuiltVersion', expected '$Version'."
    }
    Assert-DirectoryIntegrityManifest `
        -Directory (Join-Path $DistDir 'Mowik') `
        -ManifestPath $PreparedAppManifestPath
    $PreparedSignature = Get-AuthenticodeSignature -LiteralPath $AppExe
    if ($PreparedSignature.Status -ne [System.Management.Automation.SignatureStatus]::NotSigned) {
        throw "Prepared Mowik.exe must be unsigned, got $($PreparedSignature.Status)."
    }
    $ReparsePoint = Get-ChildItem -LiteralPath (Join-Path $DistDir 'Mowik') -Recurse -Force |
        Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 } |
        Select-Object -First 1
    if ($null -ne $ReparsePoint) {
        throw 'Prepared application directory must not contain reparse points.'
    }
}

if ($IsSignedRelease) {
    Write-Host "[4/7] Podpisuję i weryfikuję Mowik.exe..." -ForegroundColor Cyan
    Invoke-AuthenticodeSign `
        -Path $AppExe `
        -CertificateThumbprint $SigningCertificate.Thumbprint `
        -CertificateStoreLocation $SigningCertificateStore `
        -TimestampServer $TimestampServer `
        -SignToolPath $ResolvedSignTool

    $SignedAppManifestPath = "$PreparedAppManifestPath.signed"
    Write-DirectoryIntegrityManifest `
        -Directory (Join-Path $DistDir 'Mowik') `
        -ManifestPath $SignedAppManifestPath
    Assert-DirectoryIntegrityManifestTransition `
        -BeforeManifestPath $PreparedAppManifestPath `
        -AfterManifestPath $SignedAppManifestPath `
        -AllowedChangedPath 'Mowik.exe'
}
else {
    $UnsignedBuildLabel = if ($IsUnsignedRelease) { 'release' } else { 'lokalny build deweloperski' }
    Write-Host "[4/7] Pomijam podpis: jawny unsigned $UnsignedBuildLabel." -ForegroundColor Yellow
}

if ($IsUnsignedRelease) {
    $AppSignature = Get-AuthenticodeSignature -LiteralPath $AppExe
    if ($AppSignature.Status -ne [System.Management.Automation.SignatureStatus]::NotSigned) {
        throw "UnsignedRelease expected an unsigned Mowik.exe, got $($AppSignature.Status)."
    }
}

Write-Host "[5/7] Przygotowuję Inno Setup..." -ForegroundColor Cyan
$Iscc = if ($null -ne $ResolvedInnoCompiler) {
    $ResolvedInnoCompiler
}
else {
    Resolve-InnoCompiler -InnoCompilerPath $InnoCompilerPath
}
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
    $Iscc = Resolve-InnoCompiler
}
if ($null -eq $Iscc) {
    throw 'Nie znaleziono ISCC.exe (Inno Setup 6).'
}

Write-Host "[6/7] Buduję właściwy instalator..." -ForegroundColor Cyan
if ($IsReleaseBuild) {
    # No -Force: if anything recreated release/ during the build, fail instead
    # of compiling over a potentially published or externally supplied asset.
    New-Item -ItemType Directory -Path $ReleaseDir -ErrorAction Stop | Out-Null
}
else {
    New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
}
$InstallerBaseName = if ($IsSignedRelease) {
    "Mowik-$Version-Setup"
}
else {
    "Mowik-$Version-Setup-UNSIGNED"
}
$InnoArguments = @(
    "/DMyAppVersion=$Version",
    "/DMyOutputBaseFilename=$InstallerBaseName"
)
if ($IsSignedRelease) {
    $InnoSignCommand = New-InnoSignToolCommand `
        -CertificateThumbprint $SigningCertificate.Thumbprint `
        -CertificateStoreLocation $SigningCertificateStore `
        -TimestampServer $TimestampServer `
        -SignToolPath $ResolvedSignTool
    $InnoArguments += '/DSignedRelease=1'
    $InnoArguments += "/SMowikAuthenticode=$InnoSignCommand"
}
$InnoArguments += 'packaging\Mowik.iss'
if ($IsSignedRelease) {
    Assert-DirectoryIntegrityManifest `
        -Directory (Join-Path $DistDir 'Mowik') `
        -ManifestPath $SignedAppManifestPath
}
Invoke-Checked $Iscc $InnoArguments
$InstallerFileName = "$InstallerBaseName.exe"
$Installer = Join-Path $ReleaseDir $InstallerFileName
if (-not (Test-Path -LiteralPath $Installer)) {
    throw "Inno Setup nie utworzył pliku: $Installer"
}

if ($IsSignedRelease) {
    Assert-AuthenticodeSignature `
        -Path $Installer `
        -ExpectedSignerThumbprint $SigningCertificate.Thumbprint `
        -SignToolPath $ResolvedSignTool
}
elseif ($IsUnsignedRelease) {
    $InstallerSignature = Get-AuthenticodeSignature -LiteralPath $Installer
    if ($InstallerSignature.Status -ne [System.Management.Automation.SignatureStatus]::NotSigned) {
        throw "UnsignedRelease expected an unsigned installer, got $($InstallerSignature.Status)."
    }
}

Write-Host "[7/7] Zapisuję i weryfikuję sumę kontrolną..." -ForegroundColor Cyan
$Hash = Get-FileHash -LiteralPath $Installer -Algorithm SHA256
$HashLine = "$($Hash.Hash.ToLowerInvariant())  $([IO.Path]::GetFileName($Installer))"
$HashPath = Join-Path $ReleaseDir 'SHA256SUMS.txt'
Write-NewAsciiFile -Path $HashPath -Value ($HashLine + "`n")

$ArtifactTestArguments = @{
    Version = $Version
    InstallerFileName = $InstallerFileName
}
if ($IsSignedRelease) {
    $ArtifactTestArguments.RequireAuthenticode = $true
    $ArtifactTestArguments.ExpectedSignerThumbprint = $SigningCertificate.Thumbprint
    $ArtifactTestArguments.SignToolPath = $ResolvedSignTool
}
$ArtifactTestScript = Join-Path $PSScriptRoot 'test-release-artifacts.ps1'
$global:LASTEXITCODE = 0
& $ArtifactTestScript @ArtifactTestArguments
if ((-not $?) -or ($LASTEXITCODE -ne 0)) {
    throw "Release artifact verification failed with exit code $LASTEXITCODE."
}

$SizeMiB = [Math]::Round((Get-Item -LiteralPath $Installer).Length / 1MB, 1)
Write-Host "Gotowe: $Installer ($SizeMiB MiB)" -ForegroundColor Green
Write-Host "SHA-256: $($Hash.Hash.ToLowerInvariant())"
if ($IsUnsignedRelease) {
    Write-Warning (
        'UNSIGNED RELEASE BUILD - publishing is allowed only with a prominent ' +
        'Unknown publisher/SmartScreen warning and SHA-256 verification guidance.'
    )
}
elseif (-not $IsSignedRelease) {
    Write-Warning 'UNSIGNED LOCAL DEVELOPER BUILD - do not publish this installer as a release.'
}
