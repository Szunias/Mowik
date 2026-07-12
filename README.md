# Mówik 2.2.0

Lokalne dyktowanie push-to-talk dla Windows 10/11.

Przytrzymujesz skonfigurowany klawisz lub przycisk myszy, mówisz, puszczasz, a lokalny model Whisper rozpoznaje wypowiedź. Mówik może wkleić wynik do aktywnego pola, pozostawić go w schowku albo wykonać obie czynności naraz. Po jednorazowym pobraniu modelu rozpoznawanie działa bez internetu.

## Nowości w wersji 2.2.0

- **wykrywanie przycisku jednym kliknięciem** — wybierz `Wykryj…`, a następnie naciśnij dowolny klawisz lub przycisk myszy;
- własne dźwięki WAV dla rozpoczęcia nagrywania, puszczenia przycisku, gotowego tekstu i błędu;
- odsłuch dźwięku bezpośrednio w panelu;
- opcjonalne zapętlenie własnego dźwięku podczas trzymania przycisku;
- osobne ustawienia: automatyczne wklejanie oraz kopiowanie do schowka;
- po wklejeniu w schowku pozostaje dokładna transkrypcja bez automatycznie dodanej końcowej spacji;
- zachowanie obecnej konfiguracji, słownika, środowiska i pobranych modeli podczas aktualizacji.

## Aktualizacja istniejącego Mówika

Użyj małej paczki `Mowik-2.2.0-AKTUALIZACJA.zip`:

1. Wyodrębnij całą paczkę do dowolnego tymczasowego folderu.
2. Uruchom `AKTUALIZUJ.cmd`.
3. Aktualizator spróbuje sam odnaleźć działającą instalację. Jeżeli otworzy wybór folderu, wskaż stary folder Mówika — ten, w którym znajdują się `mowik.py`, `URUCHOM.cmd` i folder `.venv`.

Aktualizator zatrzyma program, zrobi małą kopię zapasową kodu, wgra wersję 2.2.0, sprawdzi ją i ponownie uruchomi Mówika. Nie usuwa ani nie pobiera ponownie:

- konfiguracji z `%APPDATA%\Mowik`;
- słownika `slownik.txt`;
- własnych dźwięków;
- modeli z `%LOCALAPPDATA%\Mowik\models`;
- środowiska `.venv`.

## Świeża instalacja

1. Kliknij ZIP prawym przyciskiem i wybierz **Wyodrębnij wszystkie**. Nie uruchamiaj instalatora bezpośrednio z podglądu archiwum.
2. Przenieś rozpakowany folder w stałe miejsce, na przykład `C:\Mowik`.
3. Uruchom `ZAINSTALUJ.cmd`.
4. Po instalacji przytrzymaj domyślny klawisz **F8**, powiedz zdanie i puść klawisz.

Instalator wykrywa zgodnego 64-bitowego Pythona 3.10–3.12, a gdy go nie ma, próbuje zainstalować Python 3.11 przez `winget`. Pierwsza instalacja pobiera lokalny model mowy. Kolejne uruchomienia korzystają z modelu zapisanego w `%LOCALAPPDATA%\Mowik\models`.

## Panel ustawień

1. Znajdź ikonę mikrofonu Mówika przy zegarze Windows. Czasami znajduje się pod strzałką **Pokaż ukryte ikony**.
2. Kliknij ikonę prawym przyciskiem.
3. Wybierz **Panel ustawień…**.
4. Zmień parametry i kliknij **Zapisz i zastosuj**.

Panel zawiera zakładki:

- **Ogólne** — przycisk dyktowania, mikrofon, model, CPU/GPU, język, `beam size` i liczba wątków;
- **Mikrofon i VAD** — bufor początku i końca nagrania, czułość oraz wykrywanie ciszy;
- **Tekst i schowek** — wklejanie, kopiowanie, końcowa spacja, komendy głosowe i słownik;
- **Dźwięki** — sygnały wbudowane, własne WAV-y, odsłuch, zapętlenie i powiadomienia;
- **Ollama** — opcjonalny lokalny korektor LLM;
- **Pliki** — dostęp do konfiguracji, logu i folderu danych.

Surowy `config.json` nadal można otworzyć z menu, ale do zwykłego używania nie jest potrzebny.

## Zbindowanie dowolnego przycisku

W zakładce **Ogólne**:

1. kliknij **Wykryj…**;
2. zaczekaj na napis „Nasłuchuję”;
3. naciśnij wybrany klawisz albo przycisk myszy;
4. kliknij **Zapisz i zastosuj**.

`Esc` anuluje wykrywanie. Najwygodniejsze są klawisze F6–F12, Pause/Break, Scroll Lock albo boczne przyciski myszy X1/X2. Zwykła litera, lewy przycisk myszy i prawy przycisk myszy również mogą zostać wykryte, ale mogą kolidować z normalną obsługą innych programów.

## Własne dźwięki

Otwórz zakładkę **Dźwięki**. Dla każdego zdarzenia możesz wybrać osobny plik:

- **Naciśnięcie / trzymanie**;
- **Puszczenie przycisku**;
- **Tekst gotowy**;
- **Błąd lub brak mowy**.

Obsługiwane są pliki `.wav` w nieskompresowanym formacie PCM, maksymalnie 50 MB. Po zapisaniu plik jest kopiowany do:

```text
%APPDATA%\Mowik\sounds
```

Dzięki temu dźwięk nadal działa po przeniesieniu lub usunięciu oryginału. Przycisk **Wbudowany** usuwa przypisanie i przywraca krótki ton programu. Opcja zapętlenia dotyczy własnego dźwięku rozpoczęcia nagrywania; odtwarzanie kończy się po puszczeniu przycisku.

## Schowek i wklejanie

W zakładce **Tekst i schowek** są dwa niezależne ustawienia:

- **Automatycznie wklejaj tekst do aktywnego okna**;
- **Kopiuj rozpoznany tekst również do schowka**.

Możliwe tryby:

| Wklejanie | Schowek | Zachowanie |
|---|---|---|
| włączone | włączony | tekst zostaje wklejony i skopiowany |
| włączone | wyłączony | tekst jest wpisywany bez zmiany schowka |
| wyłączone | włączony | tekst trafia tylko do schowka |

Nie można wyłączyć obu opcji jednocześnie. Gdy włączone jest kopiowanie, schowek zawiera dokładną transkrypcję. Opcjonalna spacja jest wysyłana osobno do aktywnego okna, więc nie trafia do kopiowanego tekstu.

## Szybkie profile

Po kliknięciu prawym przyciskiem ikony wybierz **Szybki profil**:

| Profil | Model | Beam | Zastosowanie |
|---|---|---:|---|
| Lekki | `small` | 1 | słabszy komputer i najmniejsze opóźnienie |
| Zbalansowany | `large-v3-turbo` | 2 | zalecany kompromis szybkości i jakości |
| Dokładny | `large-v3` | 5 | najwyższa jakość kosztem czasu i miejsca |

Wybranie modelu, którego nie ma jeszcze na dysku, uruchomi jego jednorazowe pobranie podczas restartu programu.

## Co jest pod spodem

Mówik ma cztery etapy:

1. mikrofon przechowuje krótki bufor wyłącznie w pamięci RAM;
2. lokalny model **Whisper** rozpoznaje mowę;
3. opcjonalnie lokalny LLM przez Ollamę może bardzo zachowawczo poprawić zapis;
4. wynik jest kopiowany i/lub wprowadzany do aktywnego programu zgodnie z ustawieniami.

**Ollama ani LLM nie są potrzebne do rozpoznawania mowy.** Whisper jest modelem ASR wyspecjalizowanym w zamianie dźwięku na tekst. Korektor LLM jest domyślnie wyłączony, ponieważ model językowy może czasem zmienić sens wypowiedzi.

## Dokładność a szybkość

Domyślna wartość modelu `auto` wybiera:

- `large-v3` przy działającym GPU NVIDIA/CUDA;
- `large-v3-turbo` na CPU.

Najczęściej polecane ustawienie na procesorze to:

- model: `large-v3-turbo`;
- urządzenie: `auto` albo `cpu`;
- beam size: `2`.

Pełny `large-v3` jest dokładniejszy, lecz na samym CPU może działać wyraźnie wolniej. Profil **Dokładny** może również pobrać około 3,1 GB modelu, jeżeli nie jest jeszcze zapisany lokalnie.

## Słownik nazw i fachowych słów

W panelu wybierz **Tekst i schowek → Otwórz słownik**. Wpisuj jedną nazwę lub frazę w wierszu, na przykład:

```text
Kowalski
Żyrardów
PostgreSQL
Mówik
```

Słownik jest przekazywany do modelu jako podpowiedź. Pomaga przy nazwiskach, markach, skrótach i terminach branżowych, lecz nie gwarantuje konkretnego zapisu w 100%.

## Wybór mikrofonu

W zakładce **Ogólne** wybierz mikrofon z listy. Przycisk **Odśwież** ponownie odczytuje urządzenia z Windows. Po podłączeniu nowego mikrofonu użyj odświeżenia, wybierz urządzenie i kliknij **Zapisz i zastosuj**.

## Opcjonalny lokalny LLM przez Ollamę

Ollama jest wyłącznie dodatkowym korektorem. Aby ją włączyć:

1. zainstaluj Ollamę osobno i pobierz w niej wybrany model;
2. otwórz zakładkę **Ollama (opcjonalnie)**;
3. zaznacz korektę, wpisz nazwę już pobranego modelu i kliknij **Zapisz i zastosuj**.

Korektor odrzuca wynik, gdy zbyt mocno zmienia tekst, liczby albo negacje. Przy tekstach prawnych, medycznych, finansowych i hasłach najlepiej pozostawić korektę LLM wyłączoną.

## Komendy głosowe

W zakładce **Tekst i schowek** można włączyć komendy:

- „nowa linia”;
- „nowy akapit”.

Domyślnie są wyłączone, aby te słowa nie zostały przypadkiem zamienione podczas zwykłego zdania.

## Prywatność

- dźwięk jest przechowywany tylko chwilowo w pamięci RAM;
- program nie zapisuje nagrań;
- log techniczny nie zapisuje treści dyktowanych zdań;
- transkrypcja Whisper działa lokalnie;
- po pobraniu modelu internet nie jest potrzebny;
- własne dźwięki są przechowywane lokalnie;
- Ollama, jeżeli ją włączysz, jest wywoływana pod lokalnym adresem `127.0.0.1`.

Mikrofon pozostaje otwarty podczas działania programu, aby zachować krótki bufor sprzed naciśnięcia klawisza. Dzięki temu pierwsza i ostatnia sylaba są rzadziej ucinane. Bufor nie jest zapisywany na dysku.

## Ograniczenia dokładności

Żaden system rozpoznawania mowy nie gwarantuje 100% poprawności. Najczęstsze przyczyny pomyłek to hałas, mikrofon daleko od ust, podobnie brzmiące słowa, nazwiska i brak kontekstu. Największą poprawę dają:

- mikrofon blisko ust;
- ciche otoczenie;
- model `large-v3` albo `large-v3-turbo`;
- język `pl`;
- własny słownik;
- krótkie i wyraźne wypowiedzi.

## GPU

CPU działa bez dodatkowej konfiguracji. Do wykorzystania GPU NVIDIA potrzebne są zgodne biblioteki CUDA i cuDNN. Gdy CUDA zostanie wykryta, ale model nie uruchomi się poprawnie, Mówik próbuje wrócić do CPU i zapisuje szczegóły w logu.

## Diagnostyka

- `URUCHOM_KONSOLA.cmd` — uruchamia program z widocznym logiem;
- `DIAGNOSTYKA.cmd` — pokazuje urządzenia audio i otwiera log;
- log: `%LOCALAPPDATA%\Mowik\mowik.log`;
- konfiguracja: `%APPDATA%\Mowik\config.json`;
- słownik: `%APPDATA%\Mowik\slownik.txt`;
- dźwięki: `%APPDATA%\Mowik\sounds`;
- modele: `%LOCALAPPDATA%\Mowik\models`.

Mówik nie wklei tekstu do aplikacji uruchomionej jako administrator, jeżeli sam nie jest uruchomiony jako administrator. Jest to zabezpieczenie Windows dotyczące symulowania klawiatury między procesami o różnym poziomie uprawnień.

## Naprawa instalacji

Jeżeli brakuje bibliotek albo folder `.venv` jest uszkodzony, uruchom `NAPRAW_INSTALACJE.cmd`. Skrypt odbudowuje prywatne środowisko Pythona, ale nie usuwa konfiguracji, słownika, dźwięków ani pobranych modeli.

## Autostart

- `AUTOSTART_WLACZ.cmd` — włącza uruchamianie po zalogowaniu;
- `AUTOSTART_WYLACZ.cmd` — wyłącza autostart.

## Budowanie wersji EXE

Po działającej instalacji uruchom `BUDUJ_EXE.cmd`. Powstanie katalog:

```text
dist\Mowik\Mowik.exe
```

Model nie jest pakowany do EXE; pozostaje w `%LOCALAPPDATA%\Mowik\models`.

## Licencja

Kod Mówika: MIT. Biblioteki i modele zachowują własne licencje.
