[CmdletBinding()]
param()

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

    function Test-PythonCandidate {
        param(
            [Parameter(Mandatory = $true)][string]$Exe,
            [string[]]$Prefix = @()
        )
        try {
            & $Exe @Prefix -c "import struct,sys; ok=((3,10) <= sys.version_info[:2] < (3,13)); raise SystemExit(0 if ok and struct.calcsize('P') == 8 else 1)" *> $null
            return ($LASTEXITCODE -eq 0)
        } catch {
            return $false
        }
    }

    function Find-Python {
        $items = New-Object System.Collections.ArrayList
        $py = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($null -ne $py) {
            [void]$items.Add(@{ Exe = $py.Source; Prefix = @("-3.11"); Label = "Python 3.11 przez py.exe" })
            [void]$items.Add(@{ Exe = $py.Source; Prefix = @("-3.12"); Label = "Python 3.12 przez py.exe" })
            [void]$items.Add(@{ Exe = $py.Source; Prefix = @("-3.10"); Label = "Python 3.10 przez py.exe" })
        }

        $known = @(
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
            (Join-Path $env:ProgramFiles "Python311\python.exe"),
            (Join-Path $env:ProgramFiles "Python312\python.exe"),
            (Join-Path $env:ProgramFiles "Python310\python.exe")
        )
        foreach ($path in $known) {
            if (Test-Path -LiteralPath $path) {
                [void]$items.Add(@{ Exe = $path; Prefix = @(); Label = $path })
            }
        }

        $python = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($null -ne $python) {
            [void]$items.Add(@{ Exe = $python.Source; Prefix = @(); Label = $python.Source })
        }

        foreach ($item in $items) {
            if (Test-PythonCandidate -Exe $item.Exe -Prefix $item.Prefix) {
                return $item
            }
        }
        return $null
    }

    Write-Host "[1/6] Szukam 64-bitowego Pythona 3.10-3.12..."
    $Python = Find-Python

    if ($null -eq $Python) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if ($null -eq $winget) {
            throw "Nie znaleziono zgodnego Pythona ani narzedzia winget. Zainstaluj 64-bitowy Python 3.11 i uruchom instalator ponownie."
        }

        Write-Host "Nie znaleziono zgodnego Pythona. Instaluje Python 3.11 przez winget..."
        & $winget.Source install --exact --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements --silent
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
    $VenvOk = $false
    if (Test-Path -LiteralPath $VenvPython) {
        $VenvOk = Test-PythonCandidate -Exe $VenvPython -Prefix @()
    }
    if ((Test-Path -LiteralPath $Venv) -and -not $VenvOk) {
        Write-Host "Usuwam niepelne albo uszkodzone srodowisko .venv..."
        Remove-Item -LiteralPath $Venv -Recurse -Force
    }
    if (-not $VenvOk) {
        & $PythonExe @PythonPrefix -m venv $Venv
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
            throw "Nie udalo sie utworzyc srodowiska .venv."
        }
    }

    Write-Host "[3/6] Aktualizuje narzedzia instalacyjne..."
    & $VenvPython -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) {
        throw "Aktualizacja pip/setuptools/wheel nie powiodla sie."
    }

    Write-Host "[4/6] Instaluje biblioteki Mowika..."
    & $VenvPython -m pip install --prefer-binary -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Instalacja bibliotek nie powiodla sie."
    }

    $HasNvidiaGpu = $false
    try {
        $NvidiaController = Get-CimInstance Win32_VideoController -ErrorAction Stop |
            Where-Object { [string]$_.Name -match "(?i)NVIDIA" } |
            Select-Object -First 1
        $HasNvidiaGpu = $null -ne $NvidiaController
    } catch {
        Write-Host "Nie udalo sie automatycznie sprawdzic karty NVIDIA; pozostaje tryb CPU."
    }
    $GpuRequirements = Join-Path $Root "requirements-gpu.txt"
    if ($HasNvidiaGpu -and (Test-Path -LiteralPath $GpuRequirements)) {
        Write-Host "Wykryto NVIDIA. Instaluje lokalny runtime CUDA 12 (jednorazowo)..."
        & $VenvPython -m pip install --prefer-binary -r $GpuRequirements
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Runtime NVIDIA nie zainstalowal sie; Mowik nadal zadziala na CPU."
        }
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

    Write-Host "[6/6] Pobieram lokalny model rozpoznawania mowy..."
    Write-Host "To jest duzy, jednorazowy download. Nie zamykaj tego okna."
    & $VenvPython (Join-Path $Root "mowik.py") --download-model --console-log
    if ($LASTEXITCODE -ne 0) {
        throw "Pobieranie albo uruchomienie modelu nie powiodlo sie."
    }

    if (-not (Test-Path -LiteralPath $VenvPythonW)) {
        throw "Brakuje pythonw.exe w srodowisku .venv."
    }

    Set-Content -LiteralPath (Join-Path $Root ".installed") -Value "Mowik 2.3.0" -Encoding ASCII
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
