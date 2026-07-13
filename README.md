<div align="center">

# 🎤 Mówik

**Private, local push-to-talk dictation for Windows 10/11**

**English** · [Polski](README.pl.md)

[**Download the latest Windows installer**](https://github.com/Szunias/Mowik/releases/latest)

[![Release](https://img.shields.io/github/v/release/Szunias/Mowik?label=release&color=1f6feb)](https://github.com/Szunias/Mowik/releases/latest)
[![MIT License](https://img.shields.io/badge/license-MIT-2ea44f)](LICENSE.txt)
[![Python 3.10-3.12](https://img.shields.io/badge/python-3.10--3.12-3776ab)](https://www.python.org/)
[![Windows 10/11](https://img.shields.io/badge/windows-10%20%7C%2011-0078d6)](#)
[![Works offline](https://img.shields.io/badge/works-offline-success)](#privacy)

Hold a key, speak, and release it. A local Whisper model turns your speech
into text and inserts it wherever you are typing.
No cloud, no subscription, and no voice uploads.

</div>

> [!NOTE]
> The application interface is currently available in Polish. This English guide includes the exact Polish labels for the controls you need.

## Features

- **Push-to-talk**: Mówik transcribes a dictation only while you hold the selected keyboard key or mouse button.
- **Fully local transcription**: Speech recognition runs on your computer using faster-whisper. Once a model has been downloaded, no internet connection is required.
- **One-click key detection**: Select `Wykryj…` and press any keyboard key or mouse button.
- **Graphical settings panel** in the Windows system tray: configure the model, microphone, voice activity detection, clipboard behavior, sounds, and more without editing JSON files.
- **Subtle built-in sound cues and custom WAV files** for recording start, key release, completed text, and errors, with previews and optional looping.
- **Flexible text output**: paste into the active window, copy to the clipboard, or do both.
- **Private vocabulary**: provide names, brands, and specialist terms as hints for the speech model.
- **Pre-roll audio buffer**: reduces clipped first syllables by keeping a short microphone buffer in RAM.
- **Optional local LLM correction** through Ollama, disabled by default.

## How it works

1. The microphone maintains a short audio buffer exclusively in RAM.
2. When you release the push-to-talk button, a local **Whisper** model transcribes the recording.
3. An optional local LLM running through Ollama can apply conservative text corrections.
4. The result is inserted into the active window and/or copied to the clipboard, depending on your settings.

Recordings are never written to disk, and technical logs do not contain the text of dictated sentences.

## Quick start

### Fresh installation

1. Download `Mowik-x.y.z-Setup.exe` from the [latest release](https://github.com/Szunias/Mowik/releases/latest).
2. Run the installer and follow the short setup wizard. Python and manual file extraction are not required.
3. Leave **Uruchom Mówika** (“Launch Mówik”) selected and click **Zakończ** (“Finish”).
4. Hold **F8**, speak a sentence, and release the key.

The installer does not require administrator privileges. It adds Mówik to the Start menu and creates a standard entry under **Settings → Apps**. You can also choose to create a desktop shortcut and launch Mówik automatically after signing in.

On first launch, Mówik downloads the selected local speech model to `%LOCALAPPDATA%\Mowik\models`. Transcription works offline after that download is complete.

The current release is not yet signed with a paid Authenticode certificate, so Windows SmartScreen may display an “Unknown publisher” warning. Download the installer only from the official GitHub release. If in doubt, compare its SHA-256 checksum with the included `SHA256SUMS.txt`.

### Updating an existing installation

Download and run the newer `Mowik-x.y.z-Setup.exe`. The installer detects the existing version, closes it during the update, and replaces only the application files. Your configuration, vocabulary, custom sounds, and downloaded models remain in place.

If you are upgrading from the old ZIP-based version 2.2.0 or earlier, use the new installer as well. Existing AppData files are reused automatically, and the installer removes the old startup shortcut. After confirming that the new version works, you may manually delete the old folder containing `.venv`.

## Mówik Center

Right-click the microphone icon next to the Windows clock. It may be hidden under **Pokaż ukryte ikony** (“Show hidden icons”). Select **Panel ustawień…** (“Settings panel”).

Mówik Center opens with an overview of the active push-to-talk key, microphone, and model. Its sidebar provides access to the remaining settings.

| Polish UI label | English meaning | Contents |
|---|---|---|
| Start | Home | active shortcut, microphone, model, and essential privacy information |
| Dyktowanie | Dictation | push-to-talk button, microphone, model, processing device, language, and accuracy |
| Mikrofon i mowa | Microphone and speech | recording pre-roll and tail buffers, sensitivity, and silence detection |
| Tekst i słownik | Text and vocabulary | pasting, copying, trailing space, voice commands, and private vocabulary |
| Dźwięki | Sounds | built-in cues, custom WAV files, previews, looping, and notifications |
| Integracje | Integrations | optional local LLM correction through Ollama |
| Pomoc i diagnostyka | Help and diagnostics | quick access to the configuration, privacy-safe log, and application data folder |

A colored badge on the system-tray icon indicates the current state: ready, recording, processing, or error.

### Binding any keyboard key or mouse button

Open **Dyktowanie** (“Dictation”), select **Wykryj…** (“Detect”), wait for **Nasłuchuję** (“Listening”), and press the keyboard key or mouse button you want to use. Press `Esc` to cancel.

Convenient options include F6–F12, Pause/Break, Scroll Lock, and the X1/X2 side buttons found on many mice.

## Quick profiles

Quick profiles are available from the system-tray menu under **Szybki profil** (“Quick profile”).

| Polish profile | English meaning | Model | Accuracy | Recommended use |
|---|---|---|---:|---|
| Szybki | Fast | `small` | 1 | slower computers and the lowest latency |
| Zalecany | Recommended | `large-v3-turbo` | 2 | the best balance of speed and quality |
| Najdokładniejszy | Most accurate | `large-v3` | 5 | maximum quality at the cost of speed and approximately 3.1 GB of disk space |

Selecting a model that is not already stored locally starts a one-time download. When using CPU-only processing, begin with **Zalecany** (“Recommended”). The full `large-v3` model can be noticeably slower on a CPU.

## Clipboard and pasting

The **Tekst i słownik** (“Text and vocabulary”) section contains two independent output settings:

| Paste (`Wklejanie`) | Clipboard (`Schowek`) | Behavior |
|---|---|---|
| enabled | enabled | text is pasted and copied |
| enabled | disabled | text is typed without changing the clipboard |
| disabled | enabled | text is copied to the clipboard only |

Both options cannot be disabled at the same time.

When clipboard copying is enabled, the clipboard contains the exact transcription. The optional trailing space is sent separately to the active window and is not included in the copied text.

## Custom sounds

In **Dźwięki** (“Sounds”), you can assign a separate sound to each event: push-to-talk pressed, push-to-talk released, text ready, and error.

Mówik supports uncompressed PCM `.wav` files up to 50 MB. After you save the setting, the selected file is copied to `%APPDATA%\Mowik\sounds`, so it remains available even if the original file is removed. Select **Wbudowany** (“Built-in”) to restore the application’s short default tone.

## Vocabulary for names and specialist terms

Open **Tekst i słownik** (“Text and vocabulary”), select **Edytuj słownik…** (“Edit vocabulary”), and enter one phrase per line:

```text
Kowalski
Żyrardów
PostgreSQL
Mówik
```

The vocabulary is passed to the speech model as a prompt. It can improve the recognition of names, brands, abbreviations, and specialist terminology, but it cannot guarantee a specific spelling in every transcription.

## Voice commands

When voice commands are enabled under **Tekst i słownik** (“Text and vocabulary”), Mówik recognizes the Polish phrases `nowa linia` (“new line”) and `nowy akapit` (“new paragraph”).

Voice commands are disabled by default so that ordinary sentences containing these phrases are not transformed unexpectedly.

## Optional LLM correction with Ollama

Ollama is not required for speech recognition. It can optionally correct punctuation and obvious spelling mistakes after transcription:

1. Install Ollama separately and download a model through Ollama.
2. Open **Integracje** (“Integrations”), enable correction, and enter the name of the downloaded model.

Mówik rejects the corrected result if it changes the original text, numbers, or negations too extensively. For legal, medical, and financial text, leaving LLM correction disabled is recommended.

## Privacy

- Audio is kept temporarily in RAM and recordings are never saved.
- Technical logs do not contain dictated text.
- Transcription runs locally. Once the speech model has been downloaded, no internet connection is required.
- If enabled, Ollama is contacted through the local address `127.0.0.1`.

The microphone remains open while Mówik is running so that it can maintain the short pre-roll buffer. This reduces clipped first and last syllables. The buffer is never written to disk.

## Accuracy and performance

The `auto` model setting selects the low-latency `large-v3-turbo` model for both GPU and CPU processing. The full `large-v3` model remains available through the **Najdokładniejszy** (“Most accurate”) profile.

Mówik first attempts to load the model exclusively from its local cache. Once the model has been downloaded, application startup therefore does not depend on a response from the Hugging Face server.

For the best results, use a microphone close to your mouth, reduce background noise, select language `pl`, maintain a custom vocabulary, and speak in short, clear phrases. No speech-recognition system can guarantee 100% accuracy.

The installer includes its own CUDA 12.9, cuBLAS, and cuDNN runtime, so Mówik does not depend on CUDA libraries installed by other applications. A compatible NVIDIA GPU is selected automatically; CUDA processing uses `float16`, while the automatic CPU fallback uses `int8`. The bundled CUDA runtime supports RTX 50-series GPUs. If the GPU encoder test fails, Mówik records the technical details in the log and continues on the CPU.

In CPU mode, a thread count of `0` enables automatic selection based on the number of physical CPU cores, up to a maximum of 16 threads.

## Diagnostics and files

| Item | Location |
|---|---|
| Settings panel | Start menu → **Mówik → Centrum Mówika** |
| Audio devices | **Centrum Mówika → Dyktowanie → Mikrofon** |
| Log | `%LOCALAPPDATA%\Mowik\mowik.log` |
| Configuration | `%APPDATA%\Mowik\config.json` |
| Vocabulary | `%APPDATA%\Mowik\slownik.txt` |
| Sounds | `%APPDATA%\Mowik\sounds` |
| Models | `%LOCALAPPDATA%\Mowik\models` |

Mówik cannot insert text into an application running as administrator unless Mówik itself is also running as administrator. This is a Windows security restriction on simulated keyboard input between processes running with different privilege levels.

## Repair, startup, and building

- Running the same installer again repairs application files without deleting user data.
- Automatic startup can be selected in the setup wizard. Running the installer again allows you to change that option.
- `BUDUJ_EXE.cmd` builds the application directory at `dist\Mowik`.
- `BUDUJ_INSTALATOR.cmd` runs the tests, builds the application, and creates `release\Mowik-x.y.z-Setup.exe` together with its SHA-256 checksum.
- Reproducible release definitions are stored in `packaging`, and the GitHub Actions workflow is located at `.github/workflows/windows-release.yml`.

## License

Mówik is available under the [MIT License](LICENSE.txt). Libraries and models retain their respective licenses. The most important third-party licensing information is collected in [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
