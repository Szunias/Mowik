# Publikowanie Mówika w Windows Package Manager

Docelowo instalacja ma wyglądać tak:

```
winget install Szunias.Mowik
```

Katalog zawiera szablony manifestów. Konkretne pliki dla wydania generuje
`scripts/build-winget-manifest.ps1`, podstawiając wersję, adres pliku, sumę
kontrolną i datę.

## Jednorazowo: pierwsze zgłoszenie pakietu

1. Opublikuj wydanie na GitHubie (`v<wersja>`) razem z plikiem
   `Mowik-<wersja>-Setup-UNSIGNED.exe`. Manifest wskazuje adres tego pliku,
   więc wydanie musi istnieć **przed** wysłaniem zgłoszenia.
2. Wygeneruj manifesty z sumą kontrolną opublikowanego pliku:

   ```powershell
   scripts\build-winget-manifest.ps1 -Version 2.7.6 -InstallerSha256 <SHA-256 z SHA256SUMS.txt>
   ```

   Bez `-InstallerSha256` skrypt policzy sumę z `release\Mowik-<wersja>-Setup-UNSIGNED.exe`.
   Nie używaj do tego pliku `…-LOCAL-UNSIGNED.exe` — to inny build i inna suma.
3. Sprawdź i przetestuj lokalnie:

   ```powershell
   winget validate --manifest release\winget\manifests\s\Szunias\Mowik\2.7.6
   winget install --manifest release\winget\manifests\s\Szunias\Mowik\2.7.6
   ```

4. Zrób forka `microsoft/winget-pkgs`, skopiuj katalog `manifests\s\Szunias\Mowik\<wersja>`
   do forka z zachowaniem struktury i wyślij pull request. Alternatywnie
   `wingetcreate submit` zrobi to samo jednym poleceniem.
5. Zgłoszenie przechodzi automatyczną walidację (skan pliku, instalacja na
   maszynie testowej) i moderację. Zwykle trwa to od kilku godzin do kilku dni.

## Przy każdym kolejnym wydaniu

Wystarczy nowa wersja manifestu z nowym adresem i sumą kontrolną:

```powershell
wingetcreate update Szunias.Mowik --version 2.7.7 --urls <adres instalatora> --submit
```

Ten sam efekt daje ponowne uruchomienie `build-winget-manifest.ps1` i ręczny
pull request.

## Czego pilnować

- **Instalator nie jest podpisany.** Windows Package Manager to dopuszcza, ale
  SmartScreen nadal może ostrzegać przy uruchomieniu. Podpisanie pliku
  (np. przez Azure Artifact Signing) usuwa ten komunikat.
- **Cicha instalacja musi działać przy uruchomionej aplikacji**, bo na niej
  opiera się `winget upgrade`. Dlatego `packaging/Mowik.iss` nie ustawia już
  `AppMutex` — zamykaniem działającego Mówika zajmuje się Restart Manager
  (`CloseApplications=force`). Po zmianach w instalatorze warto to sprawdzić:

  ```powershell
  Start-Process "$env:LOCALAPPDATA\Programs\Mowik\Mowik.exe"
  .\release\Mowik-<wersja>-Setup-LOCAL-UNSIGNED.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
  ```

  Kod wyjścia musi wynosić 0.
- **`ProductCode`** w manifeście to `AppId` z `packaging/Mowik.iss` z dopiskiem
  `_is1`. Zmiana `AppId` wymaga poprawienia manifestu, inaczej `winget` przestanie
  rozpoznawać zainstalowaną wersję.
- **Pakiet instaluje się dla użytkownika** (`Scope: user`,
  `%LOCALAPPDATA%\Programs\Mowik`), bez podnoszenia uprawnień.
