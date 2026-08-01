[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '2.7.4',

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
    [string]$PreparedAppManifestPath,

    [Parameter()]
    [ValidatePattern('^[0-9A-Fa-f]{64}$')]
    [string]$ExpectedPreparedAppManifestSha256,

    [Parameter()]
    [switch]$ReuseVerifiedReleaseEnvironment
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$BootstrapPython = Join-Path $Root '.venv\Scripts\python.exe'
$ReleaseVenv = Join-Path $Root '.release-venv'
$Python = $BootstrapPython
$BuildDir = Join-Path $Root 'build'
$DistDir = Join-Path $Root 'dist'
$ReleaseDir = Join-Path $Root 'release'
$ReleaseTools = Join-Path $PSScriptRoot 'WindowsReleaseTools.psm1'
$TkPayloadValidator = Join-Path $PSScriptRoot 'test-tk-payload.ps1'
$EnvironmentValidator = Join-Path $PSScriptRoot 'test-release-environment.py'
$BootstrapRequirements = Join-Path $Root 'requirements-bootstrap-hashed.txt'
$ReleaseConstraints = Join-Path $Root 'constraints-release-hashed.txt'
$PreparedSourceInfoName = 'MOWIK-BUILD-SOURCE.txt'
$AllowedRemovalDirectories = @(
    $ReleaseVenv,
    $BuildDir,
    $DistDir,
    $ReleaseDir
) | ForEach-Object {
    [IO.Path]::GetFullPath($_).TrimEnd('\', '/')
}

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

    $ResolvedPath = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $Allowed = $false
    foreach ($AllowedPath in $AllowedRemovalDirectories) {
        if ([string]::Equals(
                $ResolvedPath,
                $AllowedPath,
                [StringComparison]::OrdinalIgnoreCase
            )) {
            $Allowed = $true
            break
        }
    }
    if (-not $Allowed) {
        throw "Odmowa usunięcia niezatwierdzonego katalogu: $ResolvedPath"
    }
    if (-not (Test-Path -LiteralPath $ResolvedPath)) {
        return
    }

    $Item = Get-Item -LiteralPath $ResolvedPath -Force -ErrorAction Stop
    if (-not $Item.PSIsContainer -or
        ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Odmowa usunięcia dowiązania/reparse point: $ResolvedPath"
    }
    $Pending = New-Object `
        'System.Collections.Generic.Stack[System.IO.DirectoryInfo]'
    $Pending.Push($Item)
    while ($Pending.Count -gt 0) {
        $Directory = $Pending.Pop()
        foreach ($Child in Get-ChildItem `
                -LiteralPath $Directory.FullName `
                -Force `
                -ErrorAction Stop) {
            if (($Child.Attributes -band
                    [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw (
                    "Odmowa usunięcia katalogu zawierającego " +
                    "dowiązanie/reparse point: $($Child.FullName)"
                )
            }
            if ($Child.PSIsContainer) {
                $Pending.Push($Child)
            }
        }
    }
    Remove-Item -LiteralPath $Item.FullName -Recurse -Force -ErrorAction Stop
    if (Test-Path -LiteralPath $ResolvedPath) {
        throw "Nie udało się całkowicie usunąć katalogu: $ResolvedPath"
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

function Initialize-ReleaseDependencies {
    if (-not (Test-Path -LiteralPath $BootstrapPython -PathType Leaf)) {
        throw 'Brak .venv. Najpierw uruchom ZAINSTALUJ.cmd.'
    }
    if (-not (Test-Path -LiteralPath $BootstrapRequirements -PathType Leaf)) {
        throw "Brak blokady instalatora pakietów: $BootstrapRequirements"
    }
    if (-not (Test-Path -LiteralPath $ReleaseConstraints -PathType Leaf)) {
        throw "Brak pliku blokady zależności wydania: $ReleaseConstraints"
    }

    if ($UseCleanReleaseEnvironment) {
        if (-not $ReuseVerifiedReleaseEnvironment) {
            Remove-ProjectDirectory $ReleaseVenv
            Invoke-Checked $BootstrapPython @(
                '-I', '-S', '-c',
                (
                    "import pathlib, sys, venv; " +
                    "venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(" +
                    "pathlib.Path(sys.argv[1]))"
                ),
                $ReleaseVenv
            )
        }
        $script:Python = Join-Path $ReleaseVenv 'Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
            throw "Nie udało się utworzyć czystego środowiska release: $ReleaseVenv"
        }
    }
    if (-not $ReuseVerifiedReleaseEnvironment) {
        Invoke-Checked $Python @(
            '-m', 'pip', 'install', '--disable-pip-version-check',
            '--require-hashes', '--only-binary=:all:', '--no-deps',
            '-r', 'requirements-bootstrap-hashed.txt'
        )
        Invoke-Checked $Python @(
            '-m', 'pip', 'install', '--disable-pip-version-check',
            '--require-hashes', '--only-binary=:all:', '--no-deps',
            '-r', 'constraints-release-hashed.txt'
        )
    }
    & $Python -c (
        "import importlib.metadata as m; " +
        "raise SystemExit(0 if any(d.metadata.get('Name','').lower().replace('_','-') == " +
        "'nvidia-cudnn-cu12' for d in m.distributions()) else 1)"
    ) *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Usuwam osierocony pakiet nvidia-cudnn-cu12 ze środowiska build..." -ForegroundColor Cyan
        Invoke-Checked $Python @('-m', 'pip', 'uninstall', '--yes', 'nvidia-cudnn-cu12')
    }
    Invoke-Checked $Python @('-m', 'pip', 'check')
    if ($UseCleanReleaseEnvironment) {
        Invoke-Checked $Python @(
            'scripts\test-release-environment.py',
            'requirements-bootstrap-hashed.txt',
            'constraints-release-hashed.txt'
        )
    }
    Invoke-Checked $Python @('mowik.py', '--runtime-gui-smoke-test')
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Instalator Mówika można zbudować wyłącznie w Windows.'
}

$IsSignedRelease = $BuildMode -eq 'SignedRelease'
$IsUnsignedRelease = $BuildMode -eq 'UnsignedRelease'
$IsReleaseBuild = $IsSignedRelease -or $IsUnsignedRelease
# Local and official builds share one exact, disposable dependency environment.
# The ordinary application .venv is only the isolated bootstrap interpreter.
$UseCleanReleaseEnvironment = $true
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
if ($PrepareApplicationOnly -and $SkipTests) {
    throw '-PrepareApplicationOnly cannot be combined with -SkipTests.'
}
if ($UsePreparedApplication -and
    [string]::IsNullOrWhiteSpace($ExpectedPreparedAppManifestSha256)) {
    throw (
        '-UsePreparedApplication requires an out-of-band ' +
        '-ExpectedPreparedAppManifestSha256.'
    )
}
if ((-not $UsePreparedApplication) -and
    -not [string]::IsNullOrWhiteSpace($ExpectedPreparedAppManifestSha256)) {
    throw (
        '-ExpectedPreparedAppManifestSha256 is valid only with ' +
        '-UsePreparedApplication.'
    )
}
if ($ReuseVerifiedReleaseEnvironment -and (-not $IsReleaseBuild)) {
    throw '-ReuseVerifiedReleaseEnvironment is valid only for a release build.'
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

$AppDirectory = Join-Path $DistDir 'Mowik'
$AppExe = Join-Path $AppDirectory 'Mowik.exe'

Write-Host "[1/7] Przygotowuję zależności wydania..." -ForegroundColor Cyan
Initialize-ReleaseDependencies

if (-not $UsePreparedApplication) {
    Write-Host "[2/7] Generuję ikonę i uruchamiam testy..." -ForegroundColor Cyan
    Invoke-Checked $Python @('scripts\generate-icon.py')
    $SourceIdentityBefore = Get-ReleaseSourceIdentity -ProjectRoot $Root
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
    & $TkPayloadValidator -ApplicationDirectory $AppDirectory
    $BuiltVersion = (Get-Item -LiteralPath $AppExe).VersionInfo.ProductVersion
    if ($BuiltVersion -cne $Version) {
        throw "Metadane Mowik.exe mają wersję '$BuiltVersion', oczekiwano '$Version'."
    }
    Invoke-Checked $Python @('scripts\test-exe-manifest.py', $AppExe)
    $SmokeProcess = Start-Process -FilePath $AppExe -ArgumentList '--version' -Wait -PassThru
    if ($SmokeProcess.ExitCode -ne 0) {
        throw "Test Mowik.exe --version zakończył się kodem $($SmokeProcess.ExitCode)."
    }
    $GuiSmokeProcess = Start-Process `
        -FilePath $AppExe `
        -ArgumentList '--runtime-gui-smoke-test' `
        -Wait `
        -PassThru
    if ($GuiSmokeProcess.ExitCode -ne 0) {
        throw "Test GUI spakowanego Mowik.exe zakończył się kodem $($GuiSmokeProcess.ExitCode)."
    }
    $SourceIdentityAfterBuild = Get-ReleaseSourceIdentity -ProjectRoot $Root
    if ($SourceIdentityAfterBuild -cne $SourceIdentityBefore) {
        throw 'Release source changed while the application was being built.'
    }
    $PreparedSourceInfoPath = Join-Path $AppDirectory $PreparedSourceInfoName
    $PreparedSourceInfo = @(
        'MOWIK-PREPARED-SOURCE-V1'
        "version`t$Version"
        "source-sha256`t$SourceIdentityBefore"
    ) -join "`n"
    Write-NewAsciiFile -Path $PreparedSourceInfoPath -Value ($PreparedSourceInfo + "`n")
    $ReleaseSourceIdentity = $SourceIdentityBefore

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
    $SourceIdentityBeforeTests = Get-ReleaseSourceIdentity -ProjectRoot $Root
    Invoke-Checked $Python @('-m', 'unittest', 'discover', '-s', 'tests', '-v')
    if ((Get-ReleaseSourceIdentity -ProjectRoot $Root) -cne $SourceIdentityBeforeTests) {
        throw 'Release source changed while tests were running on the signing host.'
    }
    if (-not (Test-Path -LiteralPath $AppExe -PathType Leaf)) {
        throw "Prepared Mowik.exe was not found: $AppExe"
    }
    $PreparedManifestItem = Get-Item -LiteralPath $PreparedAppManifestPath -Force -ErrorAction Stop
    if ($PreparedManifestItem.PSIsContainer -or
        ($PreparedManifestItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Prepared application manifest must be a regular file.'
    }
    $ActualPreparedManifestHash = (
        Get-FileHash -LiteralPath $PreparedManifestItem.FullName -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($ActualPreparedManifestHash -cne
        $ExpectedPreparedAppManifestSha256.ToLowerInvariant()) {
        throw (
            'Prepared application manifest SHA-256 does not match the ' +
            'out-of-band expected value.'
        )
    }
    Assert-DirectoryIntegrityManifest `
        -Directory $AppDirectory `
        -ManifestPath $PreparedManifestItem.FullName
    $BuiltVersion = (Get-Item -LiteralPath $AppExe).VersionInfo.ProductVersion
    if ($BuiltVersion -cne $Version) {
        throw "Prepared Mowik.exe has version '$BuiltVersion', expected '$Version'."
    }
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
    & $TkPayloadValidator -ApplicationDirectory $AppDirectory
    $PreparedSourceInfoPath = Join-Path $AppDirectory $PreparedSourceInfoName
    if (-not (Test-Path -LiteralPath $PreparedSourceInfoPath -PathType Leaf)) {
        throw "Prepared source identity is missing: $PreparedSourceInfoPath"
    }
    $CurrentSourceIdentity = Get-ReleaseSourceIdentity -ProjectRoot $Root
    $ExpectedPreparedSourceInfo = @(
        'MOWIK-PREPARED-SOURCE-V1'
        "version`t$Version"
        "source-sha256`t$CurrentSourceIdentity"
    ) -join "`n"
    $RawPreparedSourceInfo = [IO.File]::ReadAllText(
        $PreparedSourceInfoPath,
        [Text.Encoding]::ASCII
    )
    if ($RawPreparedSourceInfo -cne ($ExpectedPreparedSourceInfo + "`n")) {
        throw 'Prepared application was built from a different release source identity.'
    }
    $ReleaseSourceIdentity = $CurrentSourceIdentity
}

if ($IsSignedRelease) {
    $ResolvedSignTool = Resolve-SignToolPath -SignToolPath $SignToolPath
    $ResolvedInnoCompiler = Resolve-InnoCompiler -InnoCompilerPath $InnoCompilerPath
    $SigningCertificate = Assert-CodeSigningCertificate `
        -Thumbprint $SigningCertificateThumbprint `
        -StoreLocation $SigningCertificateStore
    Assert-TimestampServer -TimestampServer $TimestampServer | Out-Null
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
elseif ($IsUnsignedRelease) {
    "Mowik-$Version-Setup-UNSIGNED"
}
else {
    "Mowik-$Version-Setup-LOCAL-UNSIGNED"
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
if (-not $UsePreparedApplication) {
    $SourceIdentityAfterInstaller = Get-ReleaseSourceIdentity -ProjectRoot $Root
    if ($SourceIdentityAfterInstaller -cne $SourceIdentityBefore) {
        throw 'Release source changed while the installer was being built.'
    }
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

Write-Host "[7/7] Zapisuję tożsamość źródła i sumy kontrolne..." -ForegroundColor Cyan
$Hash = Get-FileHash -LiteralPath $Installer -Algorithm SHA256
$SourceIdentity = $ReleaseSourceIdentity
$BuildInfoFileName = 'BUILD-INFO.txt'
$BuildInfoPath = Join-Path $ReleaseDir $BuildInfoFileName
$BuildInfo = @(
    'MOWIK-RELEASE-BUILD-INFO-V2'
    "version`t$Version"
    "build-mode`t$BuildMode"
    "installer`t$InstallerFileName"
    "installer-sha256`t$($Hash.Hash.ToLowerInvariant())"
    "source-sha256`t$SourceIdentity"
) -join "`n"
Write-NewAsciiFile -Path $BuildInfoPath -Value ($BuildInfo + "`n")
$BuildInfoHash = Get-FileHash -LiteralPath $BuildInfoPath -Algorithm SHA256
$HashLines = @(
    "$($Hash.Hash.ToLowerInvariant())  $InstallerFileName"
    "$($BuildInfoHash.Hash.ToLowerInvariant())  $BuildInfoFileName"
)
$HashPath = Join-Path $ReleaseDir 'SHA256SUMS.txt'
Write-NewAsciiFile -Path $HashPath -Value (($HashLines -join "`n") + "`n")

$ArtifactTestArguments = @{
    Version = $Version
    InstallerFileName = $InstallerFileName
    ExpectedBuildMode = $BuildMode
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
