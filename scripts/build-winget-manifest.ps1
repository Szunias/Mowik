[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '2.8.0',

    [Parameter()]
    [string]$InstallerPath,

    [Parameter()]
    [ValidatePattern('^[0-9A-Fa-f]{64}$')]
    [string]$InstallerSha256,

    [Parameter()]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$ReleaseDate,

    [Parameter()]
    [string]$OutputRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$TemplateDir = Join-Path $Root 'packaging\winget'
$PackageIdentifier = 'Szunias.Mowik'
$InstallerName = "Mowik-$Version-Setup-UNSIGNED.exe"
$InstallerUrl = (
    'https://github.com/Szunias/Mowik/releases/download/' +
    "v$Version/$InstallerName"
)

# Hash musi pochodzić z opublikowanego pliku, nie z lokalnej kopii o tej samej
# nazwie, więc podajemy albo gotową sumę, albo plik do policzenia.
if (-not $InstallerSha256) {
    if (-not $InstallerPath) {
        $InstallerPath = Join-Path $Root "release\$InstallerName"
    }
    if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
        throw (
            "Nie znaleziono instalatora '$InstallerPath'. Podaj -InstallerPath " +
            'albo -InstallerSha256 z opublikowanego wydania.'
        )
    }
    $InstallerSha256 = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash
}
$InstallerSha256 = $InstallerSha256.ToUpperInvariant()

if (-not $ReleaseDate) {
    $ReleaseDate = (Get-Date).ToString('yyyy-MM-dd')
}

if (-not $OutputRoot) {
    $OutputRoot = Join-Path $Root 'release\winget'
}
$ManifestDir = Join-Path $OutputRoot "manifests\s\Szunias\Mowik\$Version"
if (Test-Path -LiteralPath $ManifestDir) {
    Remove-Item -LiteralPath $ManifestDir -Recurse -Force
}
$null = New-Item -ItemType Directory -Path $ManifestDir -Force

$Replacements = @{
    '__VERSION__' = $Version
    '__INSTALLER_URL__' = $InstallerUrl
    '__INSTALLER_SHA256__' = $InstallerSha256
    '__RELEASE_DATE__' = $ReleaseDate
}

$Templates = @(
    "$PackageIdentifier.yaml",
    "$PackageIdentifier.installer.yaml",
    "$PackageIdentifier.locale.en-US.yaml",
    "$PackageIdentifier.locale.pl-PL.yaml"
)

foreach ($Template in $Templates) {
    $Source = Join-Path $TemplateDir $Template
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Brakuje szablonu manifestu: $Source"
    }
    $Content = Get-Content -LiteralPath $Source -Raw
    # Nota o szablonie jest wskazówką dla repozytorium Mówika, a nie dla
    # zgłoszenia w winget-pkgs.
    $Content = [regex]::Replace(
        $Content,
        '^# Szablon manifestu;[^\r\n]*\r?\n',
        '',
        [Text.RegularExpressions.RegexOptions]::Multiline
    )
    foreach ($Placeholder in $Replacements.Keys) {
        $Content = $Content.Replace($Placeholder, $Replacements[$Placeholder])
    }
    if ($Content -match '__[A-Z0-9_]+__') {
        throw "Szablon $Template zawiera nieuzupełniony znacznik $($Matches[0])."
    }
    $Target = Join-Path $ManifestDir $Template
    # winget-pkgs wymaga UTF-8 z BOM dla plików z treścią spoza ASCII.
    [IO.File]::WriteAllText($Target, $Content, [Text.UTF8Encoding]::new($true))
}

Write-Host "Manifesty winget dla $PackageIdentifier $Version zapisano w:" -ForegroundColor Green
Write-Host "  $ManifestDir"
Write-Host ''
Write-Host 'Dalej:' -ForegroundColor Cyan
Write-Host "  winget validate --manifest `"$ManifestDir`""
Write-Host "  winget install --manifest `"$ManifestDir`"   # test lokalny"
Write-Host '  następnie PR z katalogiem manifests\ do microsoft/winget-pkgs'
