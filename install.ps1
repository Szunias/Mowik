[CmdletBinding()]
param(
    [Parameter()]
    [switch]$RemovePrivateEnvironmentOnly
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
$LogPath = Join-Path $Root "instalacja.log"
$ExitCode = 0
$TranscriptStarted = $false

$env:PYTHONUTF8 = "1"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
$env:PIP_NO_INPUT = "1"

function Assert-NonElevatedInstaller {
    $Identity = $null
    try {
        $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
        if ($Principal.IsInRole(
                [Security.Principal.WindowsBuiltInRole]::Administrator
            )) {
            throw (
                "Ze wzgledow bezpieczenstwa instalatora Mowika nie wolno " +
                "uruchamiac jako administrator. Uruchom go normalnie."
            )
        }
    } catch {
        if ($null -ne $Identity) {
            $Identity.Dispose()
            $Identity = $null
        }
        throw
    } finally {
        if ($null -ne $Identity) {
            $Identity.Dispose()
        }
    }
}

try {
    Assert-NonElevatedInstaller
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}

function Remove-PrivateEnvironment {
    param([Parameter(Mandatory = $true)][string]$Path)

    $ResolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $RootPrefix = $ResolvedRoot + [IO.Path]::DirectorySeparatorChar
    $ExpectedPath = [IO.Path]::GetFullPath(
        (Join-Path $ResolvedRoot ".venv")
    ).TrimEnd('\', '/')
    $ResolvedPath = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if (-not $ResolvedPath.StartsWith(
            $RootPrefix,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not [string]::Equals(
            $ResolvedPath,
            $ExpectedPath,
            [StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Odmowa usuniecia katalogu poza prywatnym .venv projektu: $ResolvedPath"
    }
    if (-not (Test-Path -LiteralPath $ResolvedPath)) {
        return
    }

    $Item = Get-Item -LiteralPath $ResolvedPath -Force -ErrorAction Stop
    if (-not $Item.PSIsContainer -or
        ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Odmowa usuniecia .venv, ktory nie jest zwyklym katalogiem."
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
                    "Odmowa rekurencyjnego usuniecia .venv zawierajacego " +
                    "dowiazanie/reparse point: $($Child.FullName)"
                )
            }
            if ($Child.PSIsContainer) {
                $Pending.Push($Child)
            }
        }
    }

    Remove-Item -LiteralPath $Item.FullName -Recurse -Force -ErrorAction Stop
    if (Test-Path -LiteralPath $ResolvedPath) {
        throw "Nie udalo sie calkowicie usunac prywatnego .venv."
    }
}

if ($RemovePrivateEnvironmentOnly) {
    try {
        Remove-PrivateEnvironment -Path (Join-Path $Root ".venv")
        Write-Host "Prywatne srodowisko .venv zostalo bezpiecznie usuniete."
        exit 0
    } catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
        exit 1
    }
}

try {
    try {
        Start-Transcript -Path $LogPath -Append -Force | Out-Null
        $TranscriptStarted = $true
    } catch {
        Write-Host "Uwaga: nie udalo sie uruchomic pelnego logu instalacji."
    }

    Write-Host ""
    Write-Host "MOWIK - INSTALACJA LOKALNA" -ForegroundColor Cyan
    Write-Host "Folder: $Root"
    Write-Host ""

    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "Wymagany jest 64-bitowy Windows."
    }

    function Test-TrustedPublisherExecutable {
        param(
            [Parameter(Mandatory = $true)][string]$Exe,
            [Parameter(Mandatory = $true)][string]$ExpectedFileName,
            [Parameter(Mandatory = $true)][string]$PublisherOrganization
        )
        try {
            $item = Get-Item -LiteralPath $Exe -Force -ErrorAction Stop
            if ($item.PSIsContainer -or
                $item.Name -ine $ExpectedFileName -or
                ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                return $false
            }
            $signature = Get-AuthenticodeSignature -LiteralPath $item.FullName
            return (
                $signature.Status -eq
                    [Management.Automation.SignatureStatus]::Valid -and
                $null -ne $signature.SignerCertificate -and
                $signature.SignerCertificate.Subject -match
                    ("(?:^|,\s*)O=" + [regex]::Escape($PublisherOrganization) + "(?:,|$)")
            )
        } catch {
            return $false
        }
    }

    function Find-TrustedWinget {
        try {
            $package = Get-AppxPackage -Name Microsoft.DesktopAppInstaller |
                Where-Object {
                    [string]$_.Status -eq 'Ok' -and
                    $_.Publisher -match '(?:^|,\s*)O=Microsoft Corporation(?:,|$)'
                } |
                Sort-Object Version -Descending |
                Select-Object -First 1
            if ($null -eq $package) {
                return $null
            }
            $candidate = Join-Path $package.InstallLocation "winget.exe"
            if (Test-TrustedPublisherExecutable `
                -Exe $candidate `
                -ExpectedFileName "winget.exe" `
                -PublisherOrganization "Microsoft Corporation") {
                return $candidate
            }
        } catch {}
        return $null
    }

    function Test-PythonCandidate {
        param(
            [Parameter(Mandatory = $true)][string]$Exe,
            [string[]]$Prefix = @()
        )
        try {
            & $Exe @Prefix -I -S -c "import _tkinter,struct,sys,tkinter; ok=((3,11) <= sys.version_info[:2] < (3,13)); tkinter.Tcl(); raise SystemExit(0 if ok and struct.calcsize('P') == 8 and _tkinter.TK_VERSION else 1)" *> $null
            return ($LASTEXITCODE -eq 0)
        } catch {
            return $false
        }
    }

    function Resolve-TrustedLauncherPython {
        param(
            [Parameter(Mandatory = $true)][string]$Launcher,
            [Parameter(Mandatory = $true)][string]$Selector
        )
        try {
            $Output = @(
                & $Launcher $Selector -I -S -c `
                    "import pathlib,sys; print(pathlib.Path(sys.executable).resolve())" `
                    2>$null
            )
            if ($LASTEXITCODE -ne 0 -or $Output.Count -ne 1) {
                return $null
            }
            $ResolvedPython = ([string]$Output[0]).Trim()
            if ([string]::IsNullOrWhiteSpace($ResolvedPython)) {
                return $null
            }
            if (Test-TrustedPublisherExecutable `
                -Exe $ResolvedPython `
                -ExpectedFileName "python.exe" `
                -PublisherOrganization "Python Software Foundation") {
                return $ResolvedPython
            }
        } catch {}
        return $null
    }

    function Find-Python {
        $items = New-Object System.Collections.ArrayList
        $launchers = @(
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"),
            (Join-Path $env:ProgramFiles "Python Launcher\py.exe")
        )
        foreach ($launcher in $launchers) {
            if (Test-TrustedPublisherExecutable `
                -Exe $launcher `
                -ExpectedFileName "py.exe" `
                -PublisherOrganization "Python Software Foundation") {
                foreach ($version in ("3.11", "3.12")) {
                    $resolved = Resolve-TrustedLauncherPython `
                        -Launcher $launcher `
                        -Selector ("-" + $version)
                    if ($null -ne $resolved) {
                        [void]$items.Add(@{
                            Exe = $resolved
                            Prefix = @()
                            Label = "Python $version (zweryfikowany przez py.exe)"
                        })
                    }
                }
            }
        }

        $known = @(
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
            (Join-Path $env:ProgramFiles "Python311\python.exe"),
            (Join-Path $env:ProgramFiles "Python312\python.exe")
        )
        foreach ($path in $known) {
            if (Test-TrustedPublisherExecutable `
                -Exe $path `
                -ExpectedFileName "python.exe" `
                -PublisherOrganization "Python Software Foundation") {
                [void]$items.Add(@{ Exe = $path; Prefix = @(); Label = $path })
            }
        }

        foreach ($item in $items) {
            if (Test-PythonCandidate -Exe $item.Exe -Prefix $item.Prefix) {
                return $item
            }
        }
        return $null
    }

    Write-Host "[1/6] Szukam 64-bitowego Pythona 3.11-3.12..."
    $Python = Find-Python

    if ($null -eq $Python) {
        $winget = Find-TrustedWinget
        if ($null -eq $winget) {
            throw "Nie znaleziono zgodnego Pythona ani narzedzia winget. Zainstaluj 64-bitowy Python 3.11 i uruchom instalator ponownie."
        }

        Write-Host "Nie znaleziono zgodnego Pythona. Instaluje Python 3.11 przez winget..."
        & $winget install --exact --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements --silent
        $WingetCode = $LASTEXITCODE
        Write-Host "winget zakonczyl dzialanie z kodem: $WingetCode"

        $launcher = Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"
        if (Test-Path -LiteralPath $launcher) {
            $env:Path = (Split-Path -Parent $launcher) + ";" + $env:Path
        }
        $py311dir = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311"
        if (Test-Path -LiteralPath $py311dir) {
            $env:Path = $py311dir + ";" + (Join-Path $py311dir "Scripts") + ";" + $env:Path
        }

        $Python = Find-Python
        if ($null -eq $Python) {
            throw "Python 3.11 nie zostal znaleziony po instalacji winget. Uruchom ponownie Windows albo zainstaluj Python 3.11 recznie."
        }
    }

    Write-Host ("Wybrano: " + $Python.Label)
    $PythonExe = [string]$Python.Exe
    $PythonPrefix = [string[]]$Python.Prefix
    & $PythonExe @PythonPrefix --version
    if ($LASTEXITCODE -ne 0) {
        throw "Nie udalo sie uruchomic wybranego Pythona."
    }

    $Venv = Join-Path $Root ".venv"
    $VenvPython = Join-Path $Venv "Scripts\python.exe"
    $VenvPythonW = Join-Path $Venv "Scripts\pythonw.exe"

    Write-Host "[2/6] Sprawdzam prywatne srodowisko programu..."
    if (Test-Path -LiteralPath $Venv) {
        Write-Host "Odtwarzam prywatne srodowisko .venv od zera..."
        Remove-PrivateEnvironment -Path $Venv
    }
    & $PythonExe @PythonPrefix -I -S -m venv $Venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        throw "Nie udalo sie utworzyc srodowiska .venv."
    }

    Write-Host "[3/6] Weryfikuje narzedzia i blokade bibliotek..."
    $BootstrapRequirements = Join-Path $Root "requirements-bootstrap-hashed.txt"
    $RuntimeRequirements = Join-Path $Root "requirements-runtime-hashed.txt"
    if (-not (Test-Path -LiteralPath $BootstrapRequirements -PathType Leaf)) {
        throw "Brak zweryfikowanej blokady instalatora: $BootstrapRequirements"
    }
    if (-not (Test-Path -LiteralPath $RuntimeRequirements -PathType Leaf)) {
        throw "Brak zweryfikowanej blokady bibliotek: $RuntimeRequirements"
    }

    & $VenvPython -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --no-deps -r $BootstrapRequirements
    if ($LASTEXITCODE -ne 0) {
        throw "Bezpieczna aktualizacja pip nie powiodla sie."
    }

    Write-Host "[4/6] Instaluje biblioteki Mowika..."
    & $VenvPython -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --no-deps -r $RuntimeRequirements
    if ($LASTEXITCODE -ne 0) {
        throw "Instalacja bibliotek nie powiodla sie."
    }

    & $VenvPython -c "import importlib.metadata as m; raise SystemExit(0 if any(d.metadata.get('Name','').lower().replace('_','-') == 'nvidia-cudnn-cu12' for d in m.distributions()) else 1)" *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Usuwam nieuzywany, pozostaly runtime cuDNN..."
        & $VenvPython -m pip uninstall --yes nvidia-cudnn-cu12
        if ($LASTEXITCODE -ne 0) {
            throw "Nie udalo sie usunac pozostalego pakietu nvidia-cudnn-cu12."
        }
    }

    $HasNvidiaGpu = $false
    try {
        $NvidiaController = Get-CimInstance Win32_VideoController -ErrorAction Stop |
            Where-Object { [string]$_.Name -match "(?i)NVIDIA" } |
            Select-Object -First 1
        $HasNvidiaGpu = $null -ne $NvidiaController
    } catch {
        Write-Host "Nie udalo sie sprawdzic karty NVIDIA przez WMI; probuje sterownik CUDA."
    }
    if (-not $HasNvidiaGpu) {
        & $VenvPython -c "import ctranslate2; raise SystemExit(0 if ctranslate2.get_cuda_device_count() > 0 else 1)" *> $null
        $HasNvidiaGpu = $LASTEXITCODE -eq 0
    }
    $GpuRequirements = Join-Path $Root "requirements-gpu-hashed.txt"
    $GpuRuntimeInstalled = $false
    if ($HasNvidiaGpu -and (Test-Path -LiteralPath $GpuRequirements)) {
        Write-Host "Wykryto NVIDIA. Instaluje lokalny runtime CUDA 12 (jednorazowo)..."
        & $VenvPython -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --no-deps -r $GpuRequirements
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Runtime NVIDIA nie zainstalowal sie; Mowik nadal zadziala na CPU."
        } else {
            $GpuRuntimeInstalled = $true
        }
    }

    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Zainstalowane biblioteki maja niespojne zaleznosci."
    }

    $EnvironmentValidator = Join-Path $Root "scripts\test-release-environment.py"
    $EnvironmentLocks = @($BootstrapRequirements, $RuntimeRequirements)
    if ($GpuRuntimeInstalled) {
        $EnvironmentLocks += $GpuRequirements
    }
    if (-not (Test-Path -LiteralPath $EnvironmentValidator -PathType Leaf)) {
        throw "Brak walidatora prywatnego srodowiska: $EnvironmentValidator"
    }
    & $VenvPython $EnvironmentValidator @EnvironmentLocks
    if ($LASTEXITCODE -ne 0) {
        throw "Prywatne srodowisko nie odpowiada dokladnie blokadzie bibliotek."
    }

    Write-Host "Sprawdzam import bibliotek..."
    & $VenvPython -c "import faster_whisper,ctranslate2,numpy,sounddevice,pynput,pystray,pyperclip,PIL; print('Biblioteki: OK')"
    if ($LASTEXITCODE -ne 0) {
        throw "Biblioteki zostaly pobrane, ale test importu nie przeszedl."
    }

    Write-Host "[5/6] Tworze konfiguracje..."
    & $VenvPython (Join-Path $Root "mowik.py") --create-config
    if ($LASTEXITCODE -ne 0) {
        throw "Nie udalo sie utworzyc konfiguracji."
    }

    Write-Host "[6/6] Sprawdzam lokalny model rozpoznawania mowy..."
    Write-Host "Jesli modelu brakuje, zostanie pobrany jednorazowo. Nie zamykaj tego okna."
    & $VenvPython (Join-Path $Root "mowik.py") --ensure-model --console-log
    if ($LASTEXITCODE -ne 0) {
        throw "Pobieranie albo uruchomienie modelu nie powiodlo sie."
    }

    if (-not (Test-Path -LiteralPath $VenvPythonW)) {
        throw "Brakuje pythonw.exe w srodowisku .venv."
    }

    Set-Content -LiteralPath (Join-Path $Root ".installed") -Value "Mowik 2.7.4" -Encoding ASCII
    Write-Host ""
    Write-Host "INSTALACJA ZAKONCZONA POMYSLNIE" -ForegroundColor Green
    Write-Host "Przytrzymaj F8, powiedz zdanie i pusc F8."
    Write-Host ""
} catch {
    $ExitCode = 1
    Write-Host ""
    Write-Host "BLAD INSTALACJI" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "Pelny zapis: $LogPath"
    Write-Host ""
} finally {
    if ($TranscriptStarted) {
        try { Stop-Transcript | Out-Null } catch {}
    }
}

exit $ExitCode
