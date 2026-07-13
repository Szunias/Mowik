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

    throw (
        'signtool.exe was not found in a trusted Windows SDK directory. ' +
        'Install the Windows SDK or pass -SignToolPath explicitly.'
    )
}

function Assert-TrustedInnoCompiler {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    $Compiler = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($Compiler.PSIsContainer -or $Compiler.Name -ine 'ISCC.exe') {
        throw "The Inno Setup compiler path is not ISCC.exe: $Path"
    }

    $Signature = Get-AuthenticodeSignature -LiteralPath $Compiler.FullName
    if ($Signature.Status -ne [Management.Automation.SignatureStatus]::Valid -or
        $null -eq $Signature.SignerCertificate) {
        throw "ISCC.exe does not have a valid Authenticode signature: $($Compiler.FullName)"
    }
    if ($Signature.SignerCertificate.Subject -notmatch '(?:^|,\s*)O=Pyrsys B\.V\.(?:,|$)') {
        throw (
            "ISCC.exe is not signed by the expected Inno Setup publisher, Pyrsys B.V.: " +
            $Signature.SignerCertificate.Subject
        )
    }

    return $Compiler.FullName
}

function Resolve-InnoCompiler {
    [CmdletBinding()]
    param(
        [Parameter()]
        [string]$InnoCompilerPath
    )

    if ($InnoCompilerPath) {
        return Assert-TrustedInnoCompiler -Path $InnoCompilerPath
    }

    # Prefer the vendor executable over a package-manager shim. Chocolatey
    # registers ISCC.exe in its bin directory, but that shim is not signed by
    # the Inno Setup publisher and must never be treated as the compiler.
    $Candidates = @()
    if ($env:LOCALAPPDATA) {
        $Candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    }
    if (${env:ProgramFiles(x86)}) {
        $Candidates += (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
    }
    if ($env:ProgramFiles) {
        $Candidates += (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    }
    $Command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($null -ne $Command) {
        $Candidates += $Command.Source
    }

    foreach ($Candidate in ($Candidates | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return Assert-TrustedInnoCompiler -Path $Candidate
        }
    }
    return $null
}

function Get-CanonicalDirectoryManifestContent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Directory
    )

    $RootItem = Get-Item -LiteralPath $Directory -Force -ErrorAction Stop
    if (-not $RootItem.PSIsContainer) {
        throw "The integrity manifest root is not a directory: $Directory"
    }
    if (($RootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The integrity manifest root must not be a reparse point: $Directory"
    }

    $Separators = [char[]]@(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $RootPath = [IO.Path]::GetFullPath($RootItem.FullName).TrimEnd($Separators)
    $RootPrefix = $RootPath + [IO.Path]::DirectorySeparatorChar
    $Children = @(Get-ChildItem -LiteralPath $RootPath -Recurse -Force -ErrorAction Stop)
    foreach ($Child in $Children) {
        if (($Child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "The application directory must not contain reparse points: $($Child.FullName)"
        }
    }

    [string[]]$Lines = @(
        foreach ($File in ($Children | Where-Object { -not $_.PSIsContainer })) {
            $FullPath = [IO.Path]::GetFullPath($File.FullName)
            if (-not $FullPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "A manifest entry escaped the application directory: $FullPath"
            }
            $RelativePath = $FullPath.Substring($RootPrefix.Length).Replace('\', '/')
            if ([string]::IsNullOrWhiteSpace($RelativePath) -or
                $RelativePath -match '[\x00-\x1F]' -or
                $RelativePath.StartsWith('/') -or
                $RelativePath.Contains('\') -or
                $RelativePath -match '(^|/)\.\.?(/|$)') {
                throw "The application directory contains a non-canonical path: $RelativePath"
            }
            $Length = $File.Length.ToString([Globalization.CultureInfo]::InvariantCulture)
            $Hash = (Get-FileHash -LiteralPath $FullPath -Algorithm SHA256).Hash.ToLowerInvariant()
            "$RelativePath`t$Length`t$Hash"
        }
    )
    [Array]::Sort($Lines, [StringComparer]::Ordinal)

    $Content = "MOWIK-DIRECTORY-MANIFEST-V1`n"
    if ($Lines.Count -gt 0) {
        $Content += ($Lines -join "`n") + "`n"
    }
    return $Content
}

function ConvertFrom-CanonicalDirectoryManifestContent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Content,

        [Parameter(Mandatory)]
        [string]$Description
    )

    if ($Content.Contains("`r") -or -not $Content.EndsWith("`n", [StringComparison]::Ordinal)) {
        throw "$Description is not a canonical LF-terminated manifest."
    }
    $Lines = $Content.Split([string[]]@("`n"), [StringSplitOptions]::None)
    if ($Lines.Count -lt 2 -or $Lines[0] -cne 'MOWIK-DIRECTORY-MANIFEST-V1' -or
        $Lines[$Lines.Count - 1] -cne '') {
        throw "$Description has an invalid manifest header or terminator."
    }

    $Entries = [Collections.Generic.Dictionary[string, string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    for ($Index = 1; $Index -lt ($Lines.Count - 1); $Index++) {
        $Line = $Lines[$Index]
        $Parts = $Line.Split([char]9)
        if ($Parts.Count -ne 3) {
            throw "$Description has an invalid entry at line $($Index + 1)."
        }
        $RelativePath, $Length, $Hash = $Parts
        if ([string]::IsNullOrWhiteSpace($RelativePath) -or
            $RelativePath -match '[\x00-\x1F]' -or
            $RelativePath.StartsWith('/') -or
            $RelativePath.Contains('\') -or
            $RelativePath -match '(^|/)\.\.?(/|$)' -or
            $Length -notmatch '^(0|[1-9][0-9]*)$' -or
            $Hash -notmatch '^[0-9a-f]{64}$') {
            throw "$Description has a non-canonical entry at line $($Index + 1)."
        }
        if ($Entries.ContainsKey($RelativePath)) {
            throw "$Description contains a duplicate path: $RelativePath"
        }
        $Entries.Add($RelativePath, $Line)
    }
    return ,$Entries
}

function Read-CanonicalDirectoryManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ManifestPath
    )

    $Manifest = Get-Item -LiteralPath $ManifestPath -Force -ErrorAction Stop
    if ($Manifest.PSIsContainer -or
        ($Manifest.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The integrity manifest must be a regular file: $ManifestPath"
    }
    $Utf8 = [Text.UTF8Encoding]::new($false, $true)
    $Content = [IO.File]::ReadAllText($Manifest.FullName, $Utf8)
    $Entries = ConvertFrom-CanonicalDirectoryManifestContent `
        -Content $Content `
        -Description $Manifest.FullName
    return [pscustomobject]@{
        Content = $Content
        Entries = $Entries
    }
}

function Write-DirectoryIntegrityManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Directory,

        [Parameter(Mandatory)]
        [string]$ManifestPath
    )

    $RootPath = [IO.Path]::GetFullPath((Get-Item -LiteralPath $Directory -Force).FullName)
    $OutputPath = [IO.Path]::GetFullPath($ManifestPath)
    $RootPrefix = $RootPath.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if ($OutputPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $OutputPath -ieq $RootPath) {
        throw 'The integrity manifest must be stored outside the directory it describes.'
    }
    $Parent = Split-Path -Parent $OutputPath
    if (-not (Test-Path -LiteralPath $Parent -PathType Container)) {
        throw "The integrity manifest parent directory does not exist: $Parent"
    }

    $Content = Get-CanonicalDirectoryManifestContent -Directory $RootPath
    $Utf8 = [Text.UTF8Encoding]::new($false)
    $Stream = [IO.File]::Open(
        $OutputPath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $Bytes = $Utf8.GetBytes($Content)
        $Stream.Write($Bytes, 0, $Bytes.Length)
        $Stream.Flush($true)
    }
    finally {
        $Stream.Dispose()
    }
    Write-Host "Application directory manifest written: $OutputPath" -ForegroundColor Green
}

function Assert-DirectoryIntegrityManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Directory,

        [Parameter(Mandatory)]
        [string]$ManifestPath
    )

    $Expected = Read-CanonicalDirectoryManifest -ManifestPath $ManifestPath
    $ActualContent = Get-CanonicalDirectoryManifestContent -Directory $Directory
    if ($ActualContent -cne $Expected.Content) {
        throw (
            'The application directory changed after verification: a file was added, removed, ' +
            'resized, renamed, or its SHA-256 digest changed.'
        )
    }
    Write-Host "Application directory integrity verified: $Directory" -ForegroundColor Green
}

function Assert-DirectoryIntegrityManifestTransition {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$BeforeManifestPath,

        [Parameter(Mandatory)]
        [string]$AfterManifestPath,

        [Parameter(Mandatory)]
        [string[]]$AllowedChangedPath
    )

    $Before = Read-CanonicalDirectoryManifest -ManifestPath $BeforeManifestPath
    $After = Read-CanonicalDirectoryManifest -ManifestPath $AfterManifestPath
    if ($Before.Entries.Count -ne $After.Entries.Count) {
        throw 'The signed application tree added or removed files.'
    }

    $Allowed = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($Path in $AllowedChangedPath) {
        $CanonicalPath = $Path.Replace('\', '/')
        if (-not $Allowed.Add($CanonicalPath) -or -not $Before.Entries.ContainsKey($CanonicalPath)) {
            throw "The allowed changed path is duplicated or absent from the prepared tree: $Path"
        }
    }

    foreach ($Entry in $Before.Entries.GetEnumerator()) {
        if (-not $After.Entries.ContainsKey($Entry.Key)) {
            throw "The signed application tree removed a file: $($Entry.Key)"
        }
        if ($Allowed.Contains($Entry.Key)) {
            if ($Entry.Value -ceq $After.Entries[$Entry.Key]) {
                throw "The expected signing change did not occur: $($Entry.Key)"
            }
        }
        elseif ($Entry.Value -cne $After.Entries[$Entry.Key]) {
            throw "Signing changed an unexpected application file: $($Entry.Key)"
        }
    }
    Write-Host 'Only the explicitly signed application file changed.' -ForegroundColor Green
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
    'Assert-TrustedInnoCompiler',
    'Resolve-InnoCompiler',
    'Write-DirectoryIntegrityManifest',
    'Assert-DirectoryIntegrityManifest',
    'Assert-DirectoryIntegrityManifestTransition',
    'Assert-CodeSigningCertificate',
    'Assert-AuthenticodeSignature',
    'Invoke-AuthenticodeSign',
    'New-InnoSignToolCommand'
)
