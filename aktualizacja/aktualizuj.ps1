[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Payload = Join-Path $PackageRoot "payload"
$LogPath = Join-Path $PackageRoot "aktualizacja.log"
$ExitCode = 0
$TranscriptStarted = $false

function Test-MowikFolder {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $false
    }
    return (
        (Test-Path -LiteralPath (Join-Path $Path "mowik.py")) -and
        (Test-Path -LiteralPath (Join-Path $Path "URUCHOM.cmd")) -and
        (Test-Path -LiteralPath (Join-Path $Path ".venv\Scripts\python.exe")) -and
        (Test-Path -LiteralPath (Join-Path $Path ".venv\Scripts\pythonw.exe"))
    )
}

function Find-RunningMowikFolder {
    try {
        $Items = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $null -ne $_.CommandLine -and
            $_.CommandLine -match "(?i)mowik\.py" -and
            $null -ne $_.ExecutablePath
        }
        foreach ($Item in $Items) {
            $Scripts = Split-Path -Parent ([string]$Item.ExecutablePath)
            $Venv = Split-Path -Parent $Scripts
            $Candidate = Split-Path -Parent $Venv
            if (Test-MowikFolder -Path $Candidate) {
                return $Candidate
            }
        }
    } catch {
        # WMI moze byc wylaczone. Pozostaja inne sposoby wykrywania.
    }
    return $null
}

function Find-AutostartMowikFolder {
    try {
        $Startup = [Environment]::GetFolderPath("Startup")
        foreach ($ShortcutName in @("Mówik.lnk", "Mowik.lnk")) {
            $ShortcutPath = Join-Path $Startup $ShortcutName
            if (Test-Path -LiteralPath $ShortcutPath) {
                $Shell = New-Object -ComObject WScript.Shell
                $Shortcut = $Shell.CreateShortcut($ShortcutPath)
                if (Test-MowikFolder -Path $Shortcut.WorkingDirectory) {
                    return [string]$Shortcut.WorkingDirectory
                }
            }
        }
    } catch {
    }
    return $null
}

function Select-MowikFolder {
    Add-Type -AssemblyName System.Windows.Forms
    $Dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $Dialog.Description = "Wskaz stary folder Mowik (z plikiem mowik.py i folderem .venv)."
    $Dialog.ShowNewFolderButton = $false
    $Desktop = [Environment]::GetFolderPath("Desktop")
    if (-not [string]::IsNullOrWhiteSpace($Desktop)) {
        $Dialog.SelectedPath = $Desktop
    }
    $Result = $Dialog.ShowDialog()
    if ($Result -ne [System.Windows.Forms.DialogResult]::OK) {
        throw "Anulowano wybor folderu."
    }
    return [string]$Dialog.SelectedPath
}

try {
    try {
        Start-Transcript -Path $LogPath -Append -Force | Out-Null
        $TranscriptStarted = $true
    } catch {
        Write-Host "Uwaga: nie udalo sie uruchomic pelnego logu aktualizacji."
    }

    Write-Host ""
    Write-Host "MOWIK - AKTUALIZACJA 2.3.0" -ForegroundColor Cyan
    Write-Host ""

    $Files = @(
        "mowik.py",
        "README.md",
        "WERSJA.txt",
        "install.ps1",
        "USTAWIENIA.cmd",
        "config.example.json",
        "requirements.txt",
        "requirements-gpu.txt"
    )
    foreach ($Name in $Files) {
        if (-not (Test-Path -LiteralPath (Join-Path $Payload $Name))) {
            throw "Brakuje pliku payload\$Name. Wyodrebnij ponownie cala paczke."
        }
    }

    Write-Host "[1/6] Szukam obecnej instalacji..."
    $Target = Find-RunningMowikFolder
    if ([string]::IsNullOrWhiteSpace($Target)) {
        $Target = Find-AutostartMowikFolder
    }

    if ([string]::IsNullOrWhiteSpace($Target)) {
        $KnownCandidates = @(
            $PackageRoot,
            (Split-Path -Parent $PackageRoot),
            "C:\Mowik",
            (Join-Path ([Environment]::GetFolderPath("Desktop")) "Mowik"),
            (Join-Path $env:USERPROFILE "Downloads\Mowik")
        )
        $ValidCandidates = @()
        foreach ($Candidate in $KnownCandidates) {
            if (Test-MowikFolder -Path $Candidate) {
                if ($ValidCandidates -notcontains $Candidate) {
                    $ValidCandidates += $Candidate
                }
            }
        }
        if ($ValidCandidates.Count -eq 1) {
            $Target = $ValidCandidates[0]
        }
    }

    if ([string]::IsNullOrWhiteSpace($Target)) {
        Write-Host "Nie znalazlem instalacji automatycznie. Otwieram wybor folderu..."
        $Target = Select-MowikFolder
    }

    if (-not (Test-MowikFolder -Path $Target)) {
        throw "Wybrany folder nie wyglada na zainstalowanego Mowika: $Target"
    }

    $Target = (Resolve-Path -LiteralPath $Target).Path
    Write-Host "Instalacja: $Target"

    $VenvPython = Join-Path $Target ".venv\Scripts\python.exe"
    $VenvPythonW = Join-Path $Target ".venv\Scripts\pythonw.exe"
    $TargetScript = Join-Path $Target "mowik.py"

    Write-Host "[2/6] Zatrzymuje dzialajacego Mowika..."
    $Processes = @()
    foreach ($Process in (Get-Process -Name "python", "pythonw" -ErrorAction SilentlyContinue)) {
        try {
            if ($null -ne $Process.Path -and
                ($Process.Path -ieq $VenvPython -or $Process.Path -ieq $VenvPythonW)) {
                $Processes += $Process
            }
        } catch {
        }
    }
    foreach ($Process in $Processes) {
        Write-Host ("Zatrzymuje proces PID " + $Process.Id)
        Stop-Process -Id $Process.Id -Force -ErrorAction Stop
    }
    if ($Processes.Count -gt 0) {
        Start-Sleep -Milliseconds 900
    }

    Write-Host "[3/6] Tworze mala kopie zapasowa kodu..."
    $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $Backup = Join-Path $Target ("backup-przed-2.3.0-" + $Stamp)
    New-Item -ItemType Directory -Path $Backup -Force | Out-Null
    $OriginallyPresent = @{}
    foreach ($Name in $Files) {
        $Destination = Join-Path $Target $Name
        $OriginallyPresent[$Name] = Test-Path -LiteralPath $Destination
        if ($OriginallyPresent[$Name]) {
            Copy-Item -LiteralPath $Destination -Destination (Join-Path $Backup $Name) -Force
        }
    }

    try {
        Write-Host "[4/6] Kopiuje wersje 2.3.0..."
        foreach ($Name in $Files) {
            Copy-Item -LiteralPath (Join-Path $Payload $Name) -Destination (Join-Path $Target $Name) -Force
        }

        Write-Host "[5/6] Aktualizuje biblioteki i sprawdzam nowa wersje..."
        & $VenvPython -m pip install --prefer-binary -r (Join-Path $Target "requirements.txt")
        if ($LASTEXITCODE -ne 0) {
            throw "Aktualizacja podstawowych bibliotek nie powiodla sie."
        }
        try {
            $HasNvidiaGpu = $null -ne (
                Get-CimInstance Win32_VideoController -ErrorAction Stop |
                    Where-Object { [string]$_.Name -match "(?i)NVIDIA" } |
                    Select-Object -First 1
            )
        } catch {
            $HasNvidiaGpu = $false
        }
        if ($HasNvidiaGpu) {
            & $VenvPython -m pip install --prefer-binary -r (Join-Path $Target "requirements-gpu.txt")
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Runtime NVIDIA nie zainstalowal sie; pozostaje bezpieczny tryb CPU."
            }
        }
        & $VenvPython -m py_compile $TargetScript
        if ($LASTEXITCODE -ne 0) {
            throw "Nowy plik mowik.py nie przeszedl kontroli skladni."
        }
        $VersionOutput = (& $VenvPython $TargetScript --version 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Nie udalo sie uruchomic nowej wersji. Wynik: $VersionOutput"
        }
        if ($VersionOutput -notmatch "2\.3\.0") {
            throw "Plik nie jest wersja 2.3.0. Wynik: $VersionOutput"
        }
        Write-Host $VersionOutput
        Set-Content -LiteralPath (Join-Path $Target ".installed") -Value "Mowik 2.3.0" -Encoding ASCII
    } catch {
        Write-Host "Kontrola nie przeszla. Przywracam stare pliki..." -ForegroundColor Yellow
        foreach ($Name in $Files) {
            $Destination = Join-Path $Target $Name
            if ($OriginallyPresent[$Name]) {
                Copy-Item -LiteralPath (Join-Path $Backup $Name) -Destination $Destination -Force
            } elseif (Test-Path -LiteralPath $Destination) {
                Remove-Item -LiteralPath $Destination -Force
            }
        }
        if (Test-Path -LiteralPath $TargetScript) {
            $QuotedOldScript = '"' + $TargetScript + '"'
            Start-Process -FilePath $VenvPythonW -ArgumentList $QuotedOldScript -WorkingDirectory $Target
        }
        throw
    }

    Write-Host "[6/6] Uruchamiam Mowika..."
    $QuotedScript = '"' + $TargetScript + '"'
    Start-Process -FilePath $VenvPythonW -ArgumentList $QuotedScript -WorkingDirectory $Target

    Write-Host ""
    Write-Host "AKTUALIZACJA ZAKONCZONA POMYSLNIE" -ForegroundColor Green
    Write-Host "Kopia starego kodu: $Backup"
    Write-Host "Prawy przycisk na ikonie przy zegarze -> Panel ustawien."
} catch {
    $ExitCode = 1
    Write-Host ""
    Write-Host "BLAD AKTUALIZACJI" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "Pelny zapis: $LogPath"
} finally {
    if ($TranscriptStarted) {
        try { Stop-Transcript | Out-Null } catch {}
    }
}

exit $ExitCode
