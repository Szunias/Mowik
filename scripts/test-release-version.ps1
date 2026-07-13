[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '2.7.0'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$VersionParts = $Version.Split('.')
$VersionTuple = "$($VersionParts[0]), $($VersionParts[1]), $($VersionParts[2]), 0"

function Assert-SingleVersionValue {
    param(
        [Parameter(Mandatory)] [string]$RelativePath,
        [Parameter(Mandatory)] [string]$Pattern,
        [Parameter(Mandatory)] [string]$ExpectedValue,
        [Parameter(Mandatory)] [string]$Description
    )

    $Path = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing file required by release preflight: $RelativePath"
    }

    $Content = Get-Content -LiteralPath $Path -Raw
    $Matches = [regex]::Matches(
        $Content,
        $Pattern,
        [Text.RegularExpressions.RegexOptions]::Multiline
    )
    if ($Matches.Count -ne 1) {
        throw (
            "Version preflight expected exactly one $Description in ${RelativePath}; " +
            "found $($Matches.Count)."
        )
    }
    $ActualValue = $Matches[0].Groups['value'].Value
    if ($ActualValue -cne $ExpectedValue) {
        throw (
            "Version mismatch in ${RelativePath}: $Description is '$ActualValue', " +
            "expected '$ExpectedValue'."
        )
    }
}

$Checks = @(
    @{
        RelativePath = 'mowik.py'
        Pattern = '^APP_VERSION = "(?<value>[^"\r\n]+)"\r?$'
        ExpectedValue = $Version
        Description = 'APP_VERSION'
    },
    @{
        RelativePath = 'packaging\version_info.txt'
        Pattern = '^\s*filevers=\((?<value>[0-9]+,\s*[0-9]+,\s*[0-9]+,\s*[0-9]+)\),\r?$'
        ExpectedValue = $VersionTuple
        Description = 'filevers'
    },
    @{
        RelativePath = 'packaging\version_info.txt'
        Pattern = '^\s*prodvers=\((?<value>[0-9]+,\s*[0-9]+,\s*[0-9]+,\s*[0-9]+)\),\r?$'
        ExpectedValue = $VersionTuple
        Description = 'prodvers'
    },
    @{
        RelativePath = 'packaging\version_info.txt'
        Pattern = "^\s*StringStruct\('FileVersion', '(?<value>[^']+)'\),?\r?`$"
        ExpectedValue = $Version
        Description = 'FileVersion'
    },
    @{
        RelativePath = 'packaging\version_info.txt'
        Pattern = "^\s*StringStruct\('ProductVersion', '(?<value>[^']+)'\),?\r?`$"
        ExpectedValue = $Version
        Description = 'ProductVersion'
    },
    @{
        RelativePath = 'packaging\Mowik.iss'
        Pattern = '^\s*#define MyAppVersion "(?<value>[^"\r\n]+)"\r?$'
        ExpectedValue = $Version
        Description = 'MyAppVersion'
    },
    @{
        RelativePath = 'scripts\build-release.ps1'
        Pattern = "^\s*\[string\]\`$Version = '(?<value>[^']+)',?\r?`$"
        ExpectedValue = $Version
        Description = 'the build-release default'
    },
    @{
        RelativePath = 'scripts\test-installer.ps1'
        Pattern = "^\s*\[string\]\`$Version = '(?<value>[^']+)',?\r?`$"
        ExpectedValue = $Version
        Description = 'the installer-test default'
    },
    @{
        RelativePath = 'scripts\test-release-artifacts.ps1'
        Pattern = "^\s*\[string\]\`$Version = '(?<value>[^']+)',?\r?`$"
        ExpectedValue = $Version
        Description = 'the release-artifact-test default'
    },
    @{
        RelativePath = 'scripts\test-release-version.ps1'
        Pattern = "^\s*\[string\]\`$Version = '(?<value>[^']+)'\r?`$"
        ExpectedValue = $Version
        Description = 'the release-version-test default'
    },
    @{
        RelativePath = 'BUDUJ_INSTALATOR.cmd'
        Pattern = '-Version\s+"(?<value>[0-9]+\.[0-9]+\.[0-9]+)"'
        ExpectedValue = $Version
        Description = 'the local build command version'
    },
    @{
        RelativePath = 'WERSJA.txt'
        Pattern = '^Mowik (?<value>[^\s\r\n]+)\r?$'
        ExpectedValue = $Version
        Description = 'the distribution version'
    },
    @{
        RelativePath = 'install.ps1'
        Pattern = '-Value\s+"Mowik (?<value>[^"\r\n]+)"'
        ExpectedValue = $Version
        Description = 'the source-install marker'
    }
)

foreach ($Check in $Checks) {
    Assert-SingleVersionValue @Check
}

Write-Host "Mowik $Version release preflight: OK" -ForegroundColor Green
