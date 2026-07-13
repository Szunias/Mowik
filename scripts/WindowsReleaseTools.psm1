Set-StrictMode -Version Latest

function Normalize-CodeSigningThumbprint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Thumbprint
    )

    $Normalized = ($Thumbprint -replace '\s', '').ToUpperInvariant()
    if ($Normalized -notmatch '^[0-9A-F]{40}$') {
        throw 'The signing certificate thumbprint must contain exactly 40 hexadecimal characters.'
    }
    return $Normalized
}

function Assert-TimestampServer {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$TimestampServer
    )

    $Uri = $null
    if (-not [Uri]::TryCreate($TimestampServer, [UriKind]::Absolute, [ref]$Uri)) {
        throw "The timestamp server is not an absolute URI: $TimestampServer"
    }
    if ($Uri.Scheme -notin @('http', 'https')) {
        throw "The timestamp server must use HTTP or HTTPS: $TimestampServer"
    }
    if ($TimestampServer -notmatch '^[A-Za-z0-9:/?&=._%+~-]+$') {
        throw 'The timestamp server URI contains characters that are unsafe in a signing command.'
    }
    return $TimestampServer
}

function Resolve-SignToolPath {
    [CmdletBinding()]
    param(
        [Parameter()]
        [string]$SignToolPath
    )

    if ($SignToolPath) {
        $Explicit = Get-Item -LiteralPath $SignToolPath -ErrorAction Stop
        if ($Explicit.PSIsContainer -or $Explicit.Name -ine 'signtool.exe') {
            throw "The explicit SignTool path is not signtool.exe: $SignToolPath"
        }
        return $Explicit.FullName
    }

    $Command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($null -ne $Command) {
        return $Command.Source
    }

    $SdkRoots = @()
    if (${env:ProgramFiles(x86)}) {
        $SdkRoots += (Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin')
    }
    if ($env:ProgramFiles) {
        $SdkRoots += (Join-Path $env:ProgramFiles 'Windows Kits\10\bin')
    }

    foreach ($SdkRoot in ($SdkRoots | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $SdkRoot -PathType Container)) {
            continue
        }

        $VersionDirectories = Get-ChildItem -LiteralPath $SdkRoot -Directory -ErrorAction SilentlyContinue |
            Sort-Object -Property Name -Descending
        foreach ($VersionDirectory in $VersionDirectories) {
            foreach ($Architecture in @('x64', 'x86', 'arm64')) {
                $Candidate = Join-Path $VersionDirectory.FullName "$Architecture\signtool.exe"
                if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
                    return (Get-Item -LiteralPath $Candidate).FullName
                }
            }
        }

        foreach ($Architecture in @('x64', 'x86', 'arm64')) {
            $Candidate = Join-Path $SdkRoot "$Architecture\signtool.exe"
            if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
                return (Get-Item -LiteralPath $Candidate).FullName
            }
        }
    }

    throw 'signtool.exe was not found. Install the Windows SDK or pass -SignToolPath explicitly.'
}

function Assert-CodeSigningCertificate {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Thumbprint,

        [Parameter()]
        [ValidateSet('CurrentUser', 'LocalMachine')]
        [string]$StoreLocation = 'CurrentUser'
    )

    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Authenticode signing is supported only on Windows.'
    }

    $Normalized = Normalize-CodeSigningThumbprint -Thumbprint $Thumbprint
    $CertificatePath = "Cert:\$StoreLocation\My\$Normalized"
    $Certificate = Get-Item -LiteralPath $CertificatePath -ErrorAction SilentlyContinue
    if ($null -eq $Certificate) {
        throw "The requested certificate was not found in $StoreLocation\My: $Normalized"
    }
    if (-not $Certificate.HasPrivateKey) {
        throw "The signing certificate has no accessible private key: $Normalized"
    }

    $Now = Get-Date
    if (($Certificate.NotBefore -gt $Now) -or ($Certificate.NotAfter -le $Now)) {
        throw "The signing certificate is not currently valid: $Normalized"
    }

    $CodeSigningOid = '1.3.6.1.5.5.7.3.3'
    $HasCodeSigningEku = @($Certificate.EnhancedKeyUsageList | Where-Object {
        $_.ObjectId.Value -eq $CodeSigningOid
    }).Count -gt 0
    if (-not $HasCodeSigningEku) {
        throw "The certificate is not valid for code signing: $Normalized"
    }

    $KeyUsage = @($Certificate.Extensions | Where-Object {
        $_.Oid.Value -eq '2.5.29.15'
    })
    if (($KeyUsage.Count -gt 0) -and
        (($KeyUsage[0].KeyUsages -band [Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature) -eq 0)) {
        throw "The certificate key usage does not permit digital signatures: $Normalized"
    }

    return $Certificate
}

function Invoke-NativeSigningTool {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,

        [Parameter(Mandatory)]
        [string[]]$ArgumentList
    )

    $global:LASTEXITCODE = 0
    & $FilePath @ArgumentList
    if ((-not $?) -or ($LASTEXITCODE -ne 0)) {
        throw "SignTool failed with exit code $LASTEXITCODE."
    }
}

function Assert-AuthenticodeSignature {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string[]]$Path,

        [Parameter(Mandatory)]
        [string]$ExpectedSignerThumbprint,

        [Parameter()]
        [string]$SignToolPath
    )

    $ResolvedSignTool = Resolve-SignToolPath -SignToolPath $SignToolPath
    $ExpectedThumbprint = Normalize-CodeSigningThumbprint -Thumbprint $ExpectedSignerThumbprint

    foreach ($RequestedPath in $Path) {
        $File = Get-Item -LiteralPath $RequestedPath -ErrorAction Stop
        if ($File.PSIsContainer) {
            throw "Authenticode verification requires a file: $RequestedPath"
        }

        $Signature = Get-AuthenticodeSignature -LiteralPath $File.FullName
        if ($Signature.Status -ne [Management.Automation.SignatureStatus]::Valid) {
            throw "Invalid Authenticode signature on $($File.FullName): $($Signature.StatusMessage)"
        }
        if ($null -eq $Signature.SignerCertificate) {
            throw "Authenticode signer certificate is missing on $($File.FullName)."
        }
        $ActualThumbprint = Normalize-CodeSigningThumbprint -Thumbprint $Signature.SignerCertificate.Thumbprint
        if ($ActualThumbprint -ne $ExpectedThumbprint) {
            throw (
                "Unexpected Authenticode signer on $($File.FullName). " +
                "Expected $ExpectedThumbprint, got $ActualThumbprint."
            )
        }
        if ($null -eq $Signature.TimeStamperCertificate) {
            throw "The Authenticode signature has no trusted timestamp: $($File.FullName)"
        }

        Invoke-NativeSigningTool -FilePath $ResolvedSignTool -ArgumentList @(
            'verify', '/pa', '/all', '/tw', '/v', $File.FullName
        )
        Write-Host "Authenticode signature verified: $($File.FullName)" -ForegroundColor Green
    }
}

function Invoke-AuthenticodeSign {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$CertificateThumbprint,

        [Parameter(Mandatory)]
        [string]$TimestampServer,

        [Parameter()]
        [ValidateSet('CurrentUser', 'LocalMachine')]
        [string]$CertificateStoreLocation = 'CurrentUser',

        [Parameter()]
        [string]$SignToolPath
    )

    $File = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($File.PSIsContainer) {
        throw "Authenticode signing requires a file: $Path"
    }

    $ResolvedSignTool = Resolve-SignToolPath -SignToolPath $SignToolPath
    $Certificate = Assert-CodeSigningCertificate `
        -Thumbprint $CertificateThumbprint `
        -StoreLocation $CertificateStoreLocation
    $ValidatedTimestampServer = Assert-TimestampServer -TimestampServer $TimestampServer

    $Arguments = @('sign')
    if ($CertificateStoreLocation -eq 'LocalMachine') {
        $Arguments += '/sm'
    }
    $Arguments += @(
        '/s', 'My',
        '/sha1', $Certificate.Thumbprint,
        '/fd', 'SHA256',
        '/tr', $ValidatedTimestampServer,
        '/td', 'SHA256',
        '/d', 'Mowik',
        '/du', 'https://github.com/Szunias/Mowik',
        '/v',
        $File.FullName
    )

    Invoke-NativeSigningTool -FilePath $ResolvedSignTool -ArgumentList $Arguments
    Assert-AuthenticodeSignature `
        -Path $File.FullName `
        -ExpectedSignerThumbprint $Certificate.Thumbprint `
        -SignToolPath $ResolvedSignTool
}

function New-InnoSignToolCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$CertificateThumbprint,

        [Parameter(Mandatory)]
        [string]$TimestampServer,

        [Parameter()]
        [ValidateSet('CurrentUser', 'LocalMachine')]
        [string]$CertificateStoreLocation = 'CurrentUser',

        [Parameter()]
        [string]$SignToolPath
    )

    $ResolvedSignTool = Resolve-SignToolPath -SignToolPath $SignToolPath
    $NormalizedThumbprint = Normalize-CodeSigningThumbprint -Thumbprint $CertificateThumbprint
    $ValidatedTimestampServer = Assert-TimestampServer -TimestampServer $TimestampServer

    # Inno Setup expands $q to a quote and $f to the quoted file it is signing.
    # Escape a literal dollar sign before handing the command to its preprocessor.
    $EscapedSignTool = $ResolvedSignTool.Replace('$', '$$')
    $EscapedTimestampServer = $ValidatedTimestampServer.Replace('$', '$$')
    $StoreArguments = if ($CertificateStoreLocation -eq 'LocalMachine') {
        '/sm /s My'
    }
    else {
        '/s My'
    }

    return (
        '$q{0}$q sign {1} /sha1 {2} /fd SHA256 /tr $q{3}$q /td SHA256 ' +
        '/d $qMowik$q /du $qhttps://github.com/Szunias/Mowik$q /v $f'
    ) -f $EscapedSignTool, $StoreArguments, $NormalizedThumbprint, $EscapedTimestampServer
}

Export-ModuleMember -Function @(
    'Normalize-CodeSigningThumbprint',
    'Assert-TimestampServer',
    'Resolve-SignToolPath',
    'Assert-CodeSigningCertificate',
    'Assert-AuthenticodeSignature',
    'Invoke-AuthenticodeSign',
    'New-InnoSignToolCommand'
)
