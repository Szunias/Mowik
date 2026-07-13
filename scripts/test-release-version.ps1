[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '2.4.0'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$VersionParts = $Version.Split('.')
$VersionTuple = "$($VersionParts[0]), $($VersionParts[1]), $($VersionParts[2]), 0"

function Assert-FileContains {
    param(
        [Parameter(Mandatory)] [string]$RelativePath,
        [Parameter(Mandatory)] [string]$Expected,
        [Parameter(Mandatory)] [string]$Description
    )

    $Path = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing file required by release preflight: $RelativePath"
    }

    $Content = Get-Content -LiteralPath $Path -Raw
    if (-not $Content.Contains($Expected)) {
        throw "Version mismatch in ${RelativePath}: expected $Description '$Version'."
    }
}

$Checks = @(
    @{
        RelativePath = 'mowik.py'
        Expected = "APP_VERSION = `"$Version`""
        Description = 'APP_VERSION'
    },
    @{
        RelativePath = 'packaging\version_info.txt'
        Expected = "filevers=($VersionTuple),"
        Description = 'filevers'
    },
    @{
        RelativePath = 'packaging\version_info.txt'
        Expected = "prodvers=($VersionTuple),"
        Description = 'prodvers'
    },
    @{
        RelativePath = 'packaging\version_info.txt'
        Expected = "StringStruct('FileVersion', '$Version')"
        Description = 'FileVersion'
    },
    @{
        RelativePath = 'packaging\version_info.txt'
        Expected = "StringStruct('ProductVersion', '$Version')"
        Description = 'ProductVersion'
    },
    @{
        RelativePath = 'packaging\Mowik.iss'
        Expected = "#define MyAppVersion `"$Version`""
        Description = 'MyAppVersion'
    },
    @{
        RelativePath = 'scripts\build-release.ps1'
        Expected = "[string]`$Version = '$Version'"
        Description = 'the build-release default'
    },
    @{
        RelativePath = 'scripts\test-installer.ps1'
        Expected = "[string]`$Version = '$Version'"
        Description = 'the installer-test default'
    },
    @{
        RelativePath = '.github\workflows\windows-release.yml'
        Expected = "default: `"$Version`""
        Description = 'the workflow default'
    },
    @{
        RelativePath = 'BUDUJ_INSTALATOR.cmd'
        Expected = "-Version `"$Version`""
        Description = 'the local build command version'
    },
    @{
        RelativePath = 'WERSJA.txt'
        Expected = "Mowik $Version"
        Description = 'the distribution version'
    },
    @{
        RelativePath = 'install.ps1'
        Expected = "-Value `"Mowik $Version`""
        Description = 'the source-install marker'
    }
)

foreach ($Check in $Checks) {
    Assert-FileContains @Check
}

Write-Host "Mowik $Version release preflight: OK" -ForegroundColor Green
