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
- **Łagodne sygnały wbudowane i własne dźwięki WAV** dla startu nagrywania, puszczenia przycisku, gotowego tekstu i błędu, z odsłuchem i opcjonalnym zapętleniem.
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

1. Pobierz `Mowik-x.y.z-Setup.exe` z [najnowszego wydania](https://github.com/Szunias/Mowik/releases/latest).
2. Uruchom plik i przejdź przez krótki kreator. Nie potrzebujesz Pythona ani ręcznego rozpakowywania plików.
3. Zostaw zaznaczone **Uruchom Mówika** i kliknij **Zakończ**.
4. Przytrzymaj **F8**, powiedz zdanie i puść klawisz.

Instalator działa bez uprawnień administratora, dodaje Mówika do menu Start i tworzy normalny wpis w **Ustawienia → Aplikacje**. Opcjonalnie może dodać skrót na pulpicie i uruchamiać program po zalogowaniu. Pierwszy start jednorazowo pobiera lokalny model mowy do `%LOCALAPPDATA%\Mowik\models`; później rozpoznawanie działa offline.

Obecne wydanie nie jest jeszcze podpisane płatnym certyfikatem Authenticode, dlatego Windows SmartScreen może pokazać komunikat „Nieznany wydawca”. Pobieraj instalator wyłącznie z oficjalnego wydania GitHub i w razie wątpliwości porównaj SHA-256 z dołączonym `SHA256SUMS.txt`.

### Aktualizacja istniejącej instalacji

Pobierz nowszy `Mowik-x.y.z-Setup.exe` i uruchom go. Instalator rozpozna poprzednią wersję, zamknie ją na czas aktualizacji i podmieni tylko pliki programu. Konfiguracja, słownik, własne dźwięki i pobrane modele zostają na miejscu.

Jeśli przechodzisz ze starej wersji ZIP 2.2.0 lub wcześniejszej, również użyj nowego instalatora. Istniejące dane z AppData zostaną wykorzystane automatycznie, a instalator usunie stary skrót autostartu. Po sprawdzeniu nowej wersji możesz ręcznie usunąć dawny folder z `.venv`.

## Centrum Mówika

Kliknij prawym przyciskiem ikonę mikrofonu przy zegarze Windows (czasem pod strzałką **Pokaż ukryte ikony**) i wybierz **Panel ustawień...**. Centrum Mówika ma ekran startowy z aktywnym skrótem, mikrofonem i modelem oraz boczną nawigację do pozostałych opcji.

| Sekcja | Zawartość |
|---|---|
| Start | aktywny skrót, mikrofon, model i najważniejsze informacje o prywatności |
| Dyktowanie | przycisk dyktowania, mikrofon, model, miejsce przetwarzania, język i dokładność |
| Mikrofon i mowa | bufory początku i końca nagrania, czułość i wykrywanie ciszy |
| Tekst i słownik | wklejanie, kopiowanie, końcowa spacja, komendy głosowe i prywatny słownik |
| Dźwięki | sygnały wbudowane, własne WAV-y, odsłuch, zapętlenie, powiadomienia |
| Integracje | opcjonalny lokalny korektor LLM przez Ollamę |
| Pomoc i diagnostyka | szybki dostęp do konfiguracji, bezpiecznego logu i folderu danych |

Kolorowa plakietka ikony w zasobniku pokazuje bieżący stan: gotowość, nagrywanie, przetwarzanie albo błąd.

### Zbindowanie dowolnego przycisku

W sekcji **Dyktowanie** kliknij **Wykryj...**, zaczekaj na napis "Nasłuchuję" i naciśnij wybrany klawisz albo przycisk myszy. `Esc` anuluje. Najwygodniejsze są klawisze F6-F12, Pause/Break, Scroll Lock oraz boczne przyciski myszy X1/X2.

## Szybkie profile

Dostępne z menu ikony w zasobniku (**Szybki profil**):

| Profil | Model | Dokładność | Zastosowanie |
|---|---|---:|---|
| Szybki | `small` | 1 | słabszy komputer, najmniejsze opóźnienie |
| Zalecany | `large-v3-turbo` | 2 | najlepszy kompromis szybkości i jakości |
| Najdokładniejszy | `large-v3` | 5 | najwyższa jakość kosztem czasu i ok. 3,1 GB na dysku |

Wybranie modelu, którego nie ma jeszcze na dysku, uruchamia jego jednorazowe pobranie. Na samym CPU najlepiej zacząć od profilu **Zalecany**; pełny `large-v3` bywa wtedy wyraźnie wolniejszy.

## Schowek i wklejanie

Dwa niezależne ustawienia w sekcji **Tekst i słownik**:

| Wklejanie | Schowek | Zachowanie |
|---|---|---|
| włączone | włączony | tekst zostaje wklejony i skopiowany |
| włączone | wyłączony | tekst jest wpisywany bez zmiany schowka |
| wyłączone | włączony | tekst trafia tylko do schowka |

Nie można wyłączyć obu opcji naraz. Gdy kopiowanie jest włączone, schowek zawiera dokładną transkrypcję; opcjonalna końcowa spacja jest wysyłana osobno do aktywnego okna i nie trafia do kopiowanego tekstu.

## Własne dźwięki

W sekcji **Dźwięki** możesz przypisać osobny plik do każdego zdarzenia: naciśnięcie, puszczenie, gotowy tekst, błąd. Obsługiwane są nieskompresowane pliki `.wav` (PCM) do 50 MB. Po zapisaniu plik jest kopiowany do `%APPDATA%\Mowik\sounds`, więc działa nawet po usunięciu oryginału. Przycisk **Wbudowany** przywraca krótki ton programu.

## Słownik nazw i terminów

Wybierz **Tekst i słownik**, potem **Edytuj słownik…** i wpisuj jedną frazę w wierszu:

```text
Kowalski
Żyrardów
PostgreSQL
Mówik
```

Słownik jest przekazywany modelowi jako podpowiedź. Pomaga przy nazwiskach, markach i skrótach, ale nie gwarantuje konkretnego zapisu w 100%.

## Komendy głosowe

Po włączeniu w sekcji **Tekst i słownik** rozpoznawane są komendy "nowa linia" i "nowy akapit". Domyślnie są wyłączone, aby zwykłe zdania z tymi słowami nie były zamieniane.

## Opcjonalna korekta LLM (Ollama)

Ollama nie jest potrzebna do rozpoznawania mowy. Może jedynie poprawić interpunkcję i oczywiste literówki po transkrypcji:

1. Zainstaluj Ollamę osobno i pobierz w niej wybrany model.
2. W sekcji **Integracje** zaznacz korektę i wpisz nazwę pobranego modelu.

Korektor odrzuca wynik, gdy zbyt mocno zmienia tekst, liczby albo negacje. Przy tekstach prawnych, medycznych i finansowych najlepiej zostawić go wyłączonego.

## Prywatność

- Dźwięk jest przechowywany tylko chwilowo w pamięci RAM; nagrania nie są zapisywane.
- Log techniczny nie zawiera treści dyktowanych zdań.
- Transkrypcja działa lokalnie; po pobraniu modelu internet nie jest potrzebny.
- Ollama, jeżeli ją włączysz, jest wywoływana pod lokalnym adresem `127.0.0.1`.

Mikrofon pozostaje otwarty podczas działania programu, aby utrzymać krótki bufor sprzed naciśnięcia klawisza. Dzięki temu pierwsza i ostatnia sylaba są rzadziej ucinane. Bufor nie jest zapisywany na dysku.

## Dokładność i wydajność

Wartość modelu `auto` wybiera niskoopóźnieniowy `large-v3-turbo` zarówno na GPU, jak i CPU. Pełny `large-v3` pozostaje w profilu **Najdokładniejszy**. Model jest najpierw ładowany wyłącznie z lokalnego cache, więc po instalacji start aplikacji nie zależy od odpowiedzi serwera Hugging Face. Najlepsze wyniki dają: mikrofon blisko ust, ciche otoczenie, język `pl`, własny słownik oraz krótkie, wyraźne wypowiedzi. Żaden system rozpoznawania mowy nie gwarantuje 100% poprawności.

Instalator zawiera własny runtime CUDA 12.9, cuBLAS i cuDNN, dzięki czemu Mówik nie zależy od bibliotek dołączonych przez inne programy. Aplikacja sama wykrywa kartę NVIDIA; na RTX 50xx używa `float16`, a po błędzie testu encodera automatycznie wraca do CPU `int8` i zapisuje szczegóły w logu. W trybie CPU liczba wątków `0` oznacza automatyczny dobór do liczby rdzeni fizycznych, maksymalnie 16.

## Diagnostyka i pliki

| Co | Gdzie |
|---|---|
| Panel ustawień | menu Start → **Mówik → Centrum Mówika** |
| Urządzenia audio | **Centrum Mówika → Dyktowanie → Mikrofon** |
| Log | `%LOCALAPPDATA%\Mowik\mowik.log` |
| Konfiguracja | `%APPDATA%\Mowik\config.json` |
| Słownik | `%APPDATA%\Mowik\slownik.txt` |
| Dźwięki | `%APPDATA%\Mowik\sounds` |
| Modele | `%LOCALAPPDATA%\Mowik\models` |

Mówik nie wklei tekstu do aplikacji uruchomionej jako administrator, jeżeli sam nie działa jako administrator. To zabezpieczenie Windows dotyczące symulowania klawiatury między procesami o różnych uprawnieniach.

## Naprawa, autostart i budowanie

- Ponowne uruchomienie tego samego instalatora naprawia pliki programu bez usuwania danych użytkownika.
- Autostart można wybrać w kreatorze instalacji; ponowne uruchomienie instalatora pozwala zmienić tę opcję.
- `BUDUJ_EXE.cmd` buduje katalog aplikacji `dist\Mowik`.
- `BUDUJ_INSTALATOR.cmd` uruchamia testy, buduje aplikację i tworzy gotowy `release\Mowik-x.y.z-Setup.exe` wraz z sumą SHA-256.
- Definicje powtarzalnego wydania znajdują się w `packaging`, a workflow GitHub Actions w `.github/workflows/windows-release.yml`.

## Licencja

Kod Mówika jest dostępny na licencji [MIT](LICENSE.txt). Biblioteki i modele zachowują własne licencje; najważniejsze informacje są zebrane w [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
