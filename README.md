<div align="center">

# 🎤 Mówik

**Lokalne dyktowanie push-to-talk dla Windows 10/11**

[![Wydanie](https://img.shields.io/github/v/release/Szunias/Mowik?label=wydanie&color=1f6feb)](https://github.com/Szunias/Mowik/releases/latest)
[![Licencja MIT](https://img.shields.io/badge/licencja-MIT-2ea44f)](LICENSE.txt)
[![Python 3.10-3.12](https://img.shields.io/badge/python-3.10--3.12-3776ab)](https://www.python.org/)
[![Windows 10/11](https://img.shields.io/badge/windows-10%20%7C%2011-0078d6)](#)
[![Działa offline](https://img.shields.io/badge/dzia%C5%82a-offline-success)](#prywatność)

Przytrzymaj klawisz, powiedz zdanie, puść klawisz. Lokalny model Whisper
zamienia mowę na tekst i wkleja go tam, gdzie właśnie piszesz.
Bez chmury, bez abonamentu i bez wysyłania głosu do internetu.

</div>

## Możliwości

- **Push-to-talk**: dyktujesz tylko wtedy, gdy trzymasz wybrany klawisz lub przycisk myszy.
- **W pełni lokalnie**: rozpoznawanie mowy (faster-whisper) działa na Twoim komputerze; po jednorazowym pobraniu modelu internet nie jest potrzebny.
- **Wykrywanie przycisku jednym kliknięciem**: wybierz `Wykryj...` i naciśnij dowolny klawisz albo przycisk myszy.
- **Graficzny panel ustawień** w zasobniku systemowym: model, mikrofon, VAD, schowek, dźwięki i więcej, bez edytowania JSON-a.
- **Własne dźwięki WAV** dla startu nagrywania, puszczenia przycisku, gotowego tekstu i błędu, z odsłuchem i opcjonalnym zapętleniem.
- **Elastyczne wyjście**: automatyczne wklejanie do aktywnego okna, kopiowanie do schowka albo jedno i drugie.
- **Prywatny słownik**: nazwiska, marki i fachowe terminy jako podpowiedź dla modelu.
- **Bufor sprzed naciśnięcia**: pierwsza sylaba nie jest ucinana, bo mikrofon trzyma krótki bufor w pamięci RAM.
- **Opcjonalna korekta LLM** przez lokalną Ollamę (domyślnie wyłączona).

## Jak to działa

1. Mikrofon utrzymuje krótki bufor dźwięku wyłącznie w pamięci RAM.
2. Po puszczeniu przycisku lokalny model **Whisper** rozpoznaje wypowiedź.
3. Opcjonalnie lokalny LLM (Ollama) może bardzo zachowawczo poprawić zapis.
4. Wynik trafia do aktywnego okna i/lub do schowka, zgodnie z ustawieniami.

Żadne nagranie nie jest zapisywane na dysku, a log techniczny nie zawiera treści dyktowanych zdań.

## Szybki start

### Świeża instalacja

1. Pobierz `Mowik-x.y.z-Windows.zip` z [najnowszego wydania](https://github.com/Szunias/Mowik/releases/latest).
2. Kliknij ZIP prawym przyciskiem i wybierz **Wyodrębnij wszystkie** (nie uruchamiaj z podglądu archiwum).
3. Przenieś rozpakowany folder w stałe miejsce, na przykład `C:\Mowik`.
4. Uruchom `ZAINSTALUJ.cmd`.
5. Po instalacji przytrzymaj **F8**, powiedz zdanie i puść klawisz.

Instalator wykrywa zgodnego 64-bitowego Pythona 3.10-3.12, a gdy go nie ma, instaluje Python 3.11 przez `winget`. Pierwsza instalacja pobiera lokalny model mowy do `%LOCALAPPDATA%\Mowik\models`.

### Aktualizacja istniejącej instalacji

1. Pobierz `Mowik-x.y.z-AKTUALIZACJA.zip` z najnowszego wydania.
2. Wyodrębnij całość do tymczasowego folderu i uruchom `AKTUALIZUJ.cmd`.
3. Aktualizator sam znajdzie instalację; gdy poprosi o folder, wskaż ten z plikami `mowik.py` i `.venv`.

Aktualizacja zachowuje konfigurację, słownik, własne dźwięki, pobrane modele i całe środowisko `.venv`. Przed podmianą plików robi kopię zapasową, a po niej sprawdza nową wersję i w razie problemu przywraca poprzednią.

## Panel ustawień

Kliknij prawym przyciskiem ikonę mikrofonu przy zegarze Windows (czasem pod strzałką **Pokaż ukryte ikony**) i wybierz **Panel ustawień...**.

| Zakładka | Zawartość |
|---|---|
| Ogólne | przycisk dyktowania, mikrofon, model, CPU/GPU, język, beam size, wątki |
| Mikrofon i VAD | bufory początku i końca nagrania, czułość, wykrywanie ciszy |
| Tekst i schowek | wklejanie, kopiowanie, końcowa spacja, komendy głosowe, słownik |
| Dźwięki | sygnały wbudowane, własne WAV-y, odsłuch, zapętlenie, powiadomienia |
| Ollama | opcjonalny lokalny korektor LLM |
| Pliki | szybki dostęp do konfiguracji, logu i folderu danych |

### Zbindowanie dowolnego przycisku

W zakładce **Ogólne** kliknij **Wykryj...**, zaczekaj na napis "Nasłuchuję" i naciśnij wybrany klawisz albo przycisk myszy. `Esc` anuluje. Najwygodniejsze są klawisze F6-F12, Pause/Break, Scroll Lock oraz boczne przyciski myszy X1/X2.

## Szybkie profile

Dostępne z menu ikony w zasobniku (**Szybki profil**):

| Profil | Model | Beam | Zastosowanie |
|---|---|---:|---|
| Lekki | `small` | 1 | słabszy komputer, najmniejsze opóźnienie |
| Zbalansowany | `large-v3-turbo` | 2 | zalecany kompromis szybkości i jakości |
| Dokładny | `large-v3` | 5 | najwyższa jakość kosztem czasu i ok. 3,1 GB na dysku |

Wybranie modelu, którego nie ma jeszcze na dysku, uruchamia jego jednorazowe pobranie. Na samym CPU zalecany jest profil **Zbalansowany**; pełny `large-v3` bywa wtedy wyraźnie wolniejszy.

## Schowek i wklejanie

Dwa niezależne ustawienia w zakładce **Tekst i schowek**:

| Wklejanie | Schowek | Zachowanie |
|---|---|---|
| włączone | włączony | tekst zostaje wklejony i skopiowany |
| włączone | wyłączony | tekst jest wpisywany bez zmiany schowka |
| wyłączone | włączony | tekst trafia tylko do schowka |

Nie można wyłączyć obu opcji naraz. Gdy kopiowanie jest włączone, schowek zawiera dokładną transkrypcję; opcjonalna końcowa spacja jest wysyłana osobno do aktywnego okna i nie trafia do kopiowanego tekstu.

## Własne dźwięki

W zakładce **Dźwięki** możesz przypisać osobny plik do każdego zdarzenia: naciśnięcie, puszczenie, gotowy tekst, błąd. Obsługiwane są nieskompresowane pliki `.wav` (PCM) do 50 MB. Po zapisaniu plik jest kopiowany do `%APPDATA%\Mowik\sounds`, więc działa nawet po usunięciu oryginału. Przycisk **Wbudowany** przywraca krótki ton programu.

## Słownik nazw i terminów

Wybierz **Tekst i schowek**, potem **Otwórz słownik** i wpisuj jedną frazę w wierszu:

```text
Kowalski
Żyrardów
PostgreSQL
Mówik
```

Słownik jest przekazywany modelowi jako podpowiedź. Pomaga przy nazwiskach, markach i skrótach, ale nie gwarantuje konkretnego zapisu w 100%.

## Komendy głosowe

Po włączeniu w zakładce **Tekst i schowek** rozpoznawane są komendy "nowa linia" i "nowy akapit". Domyślnie są wyłączone, aby zwykłe zdania z tymi słowami nie były zamieniane.

## Opcjonalna korekta LLM (Ollama)

Ollama nie jest potrzebna do rozpoznawania mowy. Może jedynie poprawić interpunkcję i oczywiste literówki po transkrypcji:

1. Zainstaluj Ollamę osobno i pobierz w niej wybrany model.
2. W zakładce **Ollama** zaznacz korektę i wpisz nazwę pobranego modelu.

Korektor odrzuca wynik, gdy zbyt mocno zmienia tekst, liczby albo negacje. Przy tekstach prawnych, medycznych i finansowych najlepiej zostawić go wyłączonego.

## Prywatność

- Dźwięk jest przechowywany tylko chwilowo w pamięci RAM; nagrania nie są zapisywane.
- Log techniczny nie zawiera treści dyktowanych zdań.
- Transkrypcja działa lokalnie; po pobraniu modelu internet nie jest potrzebny.
- Ollama, jeżeli ją włączysz, jest wywoływana pod lokalnym adresem `127.0.0.1`.

Mikrofon pozostaje otwarty podczas działania programu, aby utrzymać krótki bufor sprzed naciśnięcia klawisza. Dzięki temu pierwsza i ostatnia sylaba są rzadziej ucinane. Bufor nie jest zapisywany na dysku.

## Dokładność i wydajność

Wartość modelu `auto` wybiera `large-v3` przy działającym GPU NVIDIA/CUDA, a `large-v3-turbo` na CPU. Najlepsze wyniki dają: mikrofon blisko ust, ciche otoczenie, język `pl`, własny słownik oraz krótkie, wyraźne wypowiedzi. Żaden system rozpoznawania mowy nie gwarantuje 100% poprawności.

Do wykorzystania GPU NVIDIA potrzebne są zgodne biblioteki CUDA i cuDNN. Gdy CUDA zostanie wykryta, ale model nie wystartuje, Mówik automatycznie wraca na CPU i zapisuje szczegóły w logu.

## Diagnostyka i pliki

| Co | Gdzie |
|---|---|
| Uruchomienie z widocznym logiem | `URUCHOM_KONSOLA.cmd` |
| Urządzenia audio i log | `DIAGNOSTYKA.cmd` |
| Log | `%LOCALAPPDATA%\Mowik\mowik.log` |
| Konfiguracja | `%APPDATA%\Mowik\config.json` |
| Słownik | `%APPDATA%\Mowik\slownik.txt` |
| Dźwięki | `%APPDATA%\Mowik\sounds` |
| Modele | `%LOCALAPPDATA%\Mowik\models` |

Mówik nie wklei tekstu do aplikacji uruchomionej jako administrator, jeżeli sam nie działa jako administrator. To zabezpieczenie Windows dotyczące symulowania klawiatury między procesami o różnych uprawnieniach.

## Naprawa, autostart, EXE

- `NAPRAW_INSTALACJE.cmd` odbudowuje środowisko `.venv` bez usuwania konfiguracji, słownika, dźwięków i modeli.
- `AUTOSTART_WLACZ.cmd` / `AUTOSTART_WYLACZ.cmd` włączają i wyłączają start po zalogowaniu.
- `BUDUJ_EXE.cmd` buduje `dist\Mowik\Mowik.exe` (model pozostaje w `%LOCALAPPDATA%\Mowik\models`).

## Licencja

Kod Mówika jest dostępny na licencji [MIT](LICENSE.txt). Biblioteki i modele zachowują własne licencje.
