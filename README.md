<div align="center">

# 🎤 Mówik

**Private, local push-to-talk dictation for Windows 10/11**

**English** · [Polski](README.pl.md)

[**Download the latest Windows installer**](https://github.com/Szunias/Mowik/releases/latest)

[![Release](https://img.shields.io/github/v/release/Szunias/Mowik?label=release&color=1f6feb)](https://github.com/Szunias/Mowik/releases/latest)
[![MIT License](https://img.shields.io/badge/license-MIT-2ea44f)](LICENSE.txt)
[![Python 3.11-3.12](https://img.shields.io/badge/python-3.11--3.12-3776ab)](https://www.python.org/)
[![Windows 10/11](https://img.shields.io/badge/windows-10%20%7C%2011-0078d6)](#)
[![Works offline](https://img.shields.io/badge/works-offline-success)](#privacy)

Hold a key, speak, and release it. A local Whisper model turns your speech
into text and inserts it wherever you are typing.
No cloud, no subscription, and no voice uploads.

</div>

> [!NOTE]
> The application interface is available in English and Polish. By default, Mówik follows the Windows display language; you can override it in Mówik Center with **Save and restart**.

## Interface preview

Mówik Center keeps everyday controls clear and moves technical options into expandable advanced sections. Select any preview to open it at full size.

<table>
  <tr>
    <td width="50%" align="center">
      <a href="assets/screenshots/mowik-home-pl.png">
        <img src="assets/screenshots/mowik-home-pl.png" alt="Mówik Center home screen in Polish" width="100%">
      </a>
      <br><sub><strong>Home overview</strong> · Polish interface</sub>
    </td>
    <td width="50%" align="center">
      <a href="assets/screenshots/mowik-dictation-en.png">
        <img src="assets/screenshots/mowik-dictation-en.png" alt="Mówik Center dictation settings in English" width="100%">
      </a>
      <br><sub><strong>Dictation and performance</strong> · English interface</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <a href="assets/screenshots/mowik-sounds-pl.png">
        <img src="assets/screenshots/mowik-sounds-pl.png" alt="Mówik visual feedback settings in Polish" width="100%">
      </a>
      <br><sub><strong>Visual feedback</strong> · Polish interface</sub>
    </td>
    <td width="50%" align="center">
      <a href="assets/screenshots/mowik-sounds-en.png">
        <img src="assets/screenshots/mowik-sounds-en.png" alt="Mówik visual feedback settings in English" width="100%">
      </a>
      <br><sub><strong>Visual feedback</strong> · English interface</sub>
    </td>
  </tr>
</table>

## Features

- **Push-to-talk**: Mówik transcribes a dictation only while you hold the selected keyboard key or mouse button.
- **Fully local transcription**: Speech recognition runs on your computer using faster-whisper. Once a model has been downloaded, no internet connection is required.
- **One-click key detection**: Select **Detect…** and press any keyboard key or mouse button.
- **Clear graphical settings panel** in the Windows system tray: everyday choices stay visible, while model, GPU/CPU, voice detection, and custom connection details are available on demand under **Advanced settings**.
- **English and Polish interface** with automatic Windows-language detection and a persistent language selector.
- **Subtle built-in sound cues and custom WAV files** for recording start, key release, completed text, and errors, with previews and optional looping.
- **Optional on-screen dictation indicator** with a green recording dot, a processing animation, a success check mark, and an error X.
- **Jarvis-style custom commands** on a separate push-to-talk shortcut: insert saved text, open an app/file/website, or open a terminal in the active File Explorer folder with a safe clipboard draft.
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

1. Download `Mowik-x.y.z-Setup-UNSIGNED.exe` from the [latest release](https://github.com/Szunias/Mowik/releases/latest).
2. Run the installer and follow the short setup wizard. Python and manual file extraction are not required.
3. Leave **Launch Mówik** selected and click **Finish**. In the Polish installer these labels are **Uruchom Mówika** and **Zakończ**.
4. Hold **F8**, speak a sentence, and release the key.

The installer requires 64-bit Windows 10 version 1809 or later, or Windows 11. It does not require administrator privileges, adds Mówik to the Start menu, and creates a standard entry under **Settings → Apps**. You can also choose to create a desktop shortcut and launch Mówik automatically after signing in.

On first launch, Mówik downloads the selected local speech model to `%LOCALAPPDATA%\Mowik\models`. Transcription works offline after that download is complete.

> [!WARNING]
> The public Mówik 2.7.3 installer is **not digitally signed**. Windows may therefore show **Unknown publisher** or a Microsoft Defender SmartScreen warning. Download the installer only from the [official Mówik GitHub release](https://github.com/Szunias/Mowik/releases/latest) and verify its SHA-256 hash against `SHA256SUMS.txt` from the same release before running it. In PowerShell, use `Get-FileHash .\Mowik-2.7.3-Setup-UNSIGNED.exe -Algorithm SHA256` and compare the complete value. Do not disable Windows security protections to install Mówik.

### Updating an existing installation

Download and run the newer `Mowik-x.y.z-Setup-UNSIGNED.exe`. The installer detects the existing version, closes it during the update, and replaces only the application files. Your configuration, vocabulary, custom sounds, and downloaded models remain in place.

If you are upgrading from the old ZIP-based version 2.2.0 or earlier, use the new installer as well. Existing AppData files are reused automatically, and the installer removes the old startup shortcut. After confirming that the new version works, you may manually delete the old folder containing `.venv`.

## Mówik Center

Right-click the microphone icon next to the Windows clock. It may be hidden under **Show hidden icons**. Select **Settings…**. In the Polish interface these labels are **Pokaż ukryte ikony** and **Panel ustawień…**.

Mówik Center opens with an overview of the active push-to-talk key, microphone, and quality profile. Its sidebar provides access to the remaining settings. Technical controls stay in expandable **Advanced settings** sections, so the default view contains only the choices needed for everyday dictation.

| English UI | Polish UI | Contents |
|---|---|---|
| Home | Start | active shortcut, microphone, quality profile, interface language, and essential privacy information |
| Dictation | Dyktowanie | quality profile, shortcut, microphone, and language; model, GPU/CPU, accuracy, and threads under Advanced settings |
| Microphone and speech | Mikrofon i mowa | automatic speech detection; recording buffers, sensitivity, and detailed silence controls under Advanced settings |
| Text and dictionary | Tekst i słownik | pasting, copying, trailing space, voice commands, and private vocabulary |
| Custom commands | Własne komendy | separate shortcut, spoken phrases, text templates, app/file/website launchers, and safe “terminal here” drafts |
| Sounds | Dźwięki | sound cues and notifications; custom WAV files, previews, and looping under Advanced settings |
| Integrations | Integracje | optional local LLM correction through Ollama, with connection details under Advanced settings |
| Help and diagnostics | Pomoc i diagnostyka | privacy-safe log and application data first; direct `config.json` access under Advanced settings |

A colored badge on the system-tray icon indicates the current state: ready, recording, processing, or error.

### Binding any keyboard key or mouse button

Open **Dictation** (**Dyktowanie** in Polish), select **Detect…** (**Wykryj…**), wait for **Listening…** (**Nasłuchuję…**), and press the keyboard key or mouse button you want to use. Press `Esc` to cancel.

Convenient options include F6–F12, Pause/Break, Scroll Lock, and the X1/X2 side buttons found on many mice.

## Quick profiles

Quick profiles are available from the system-tray menu under **Quick profile** (**Szybki profil** in Polish).

| English profile | Polish profile | Model | Accuracy | Recommended use |
|---|---|---|---:|---|
| Fast | Szybki | `small` | 1 | slower computers and the lowest latency |
| Recommended | Zalecany | `large-v3-turbo` | 2 | the best balance of speed and quality |
| Most accurate | Najdokładniejszy | `large-v3` | 5 | maximum quality at the cost of speed and approximately 3.1 GB of disk space |

Selecting a model that is not already stored locally starts a one-time download. When using CPU-only processing, begin with **Recommended** (**Zalecany** in Polish). The full `large-v3` model can be noticeably slower on a CPU.

## Clipboard and pasting

The **Text and dictionary** (**Tekst i słownik** in Polish) section contains two independent output settings:

| Paste (`Wklejanie`) | Clipboard (`Schowek`) | Behavior |
|---|---|---|
| enabled | enabled | text is pasted and copied |
| enabled | disabled | text is typed without changing the clipboard |
| disabled | enabled | text is copied to the clipboard only |

Both options cannot be disabled at the same time.

When clipboard copying is enabled, the clipboard contains the exact transcription. The optional trailing space is sent separately to the active window and is not included in the copied text.

## Visual feedback and custom sounds

The optional **On-screen dictation indicator** under **Sounds → Feedback** provides immediate visual confirmation without taking focus from the application where you are typing. It shows a small green dot for regular dictation and a distinct violet indicator for custom commands. Both modes have their own processing animation and success check; errors use an X. Clear the checkbox in Mówik Settings to hide the indicator; sound cues and Windows notifications can be configured independently.

In **Sounds** (**Dźwięki** in Polish), expand **Advanced settings** to assign a separate sound to each event: push-to-talk pressed, push-to-talk released, text ready, and error.

Mówik supports uncompressed PCM `.wav` files up to 50 MB. After you save the setting, the selected file is copied to `%APPDATA%\Mowik\sounds`, so it remains available even if the original file is removed. The field displays **Built-in** (**Wbudowany** in Polish) when the default cue is active; choose **Reset** (**Przywróć**) to return to it.

## Vocabulary for names and specialist terms

Open **Text and dictionary** (**Tekst i słownik** in Polish), select **Edit dictionary…** (**Edytuj słownik…**), and enter one phrase per line:

```text
Kowalski
Żyrardów
PostgreSQL
Mówik
```

The vocabulary is passed to the speech model as a prompt. It can improve the recognition of names, brands, abbreviations, and specialist terminology, but it cannot guarantee a specific spelling in every transcription.

## Voice commands

When voice commands are enabled under **Text and dictionary**, Mówik recognizes `new line` and `new paragraph` for English transcription, and `nowa linia` and `nowy akapit` for Polish transcription.

Voice commands are disabled by default so that ordinary sentences containing these phrases are not transformed unexpectedly.

## Custom commands and actions

Custom commands use a second push-to-talk shortcut, **F7** by default, while **F8** remains regular dictation. Open **Custom commands**, add a spoken phrase, and choose one of three actions:

- **Insert text** — paste a saved block exactly as written. Multi-line text always requires confirmation because pasting it into a terminal could execute content.
- **Open an app, file, or website** — launch an existing absolute local path or an HTTPS URL; confirmation is always required, while scripts, shortcuts, and network paths are blocked.
- **Open terminal** — start Windows Terminal or a visible classic console in the folder captured from the active File Explorer window, a selected fixed folder, or the home folder.

Terminal commands deliberately use a draft workflow. A command can match the exact phrase and only open the terminal, or treat the remainder of the utterance as a single-line draft. Mówik validates that draft, copies it to the clipboard, and never types it into the terminal or presses Enter. You review it, paste it with `Ctrl+V`, and submit it yourself. Arbitrary saved `cmd.exe` execution and legacy `run_command` entries are disabled.

Exact matching is used for text and open actions. Terminal drafts use explicit prefix-and-tail matching: the configured phrase must be at the start on a complete token boundary, and the longest matching phrase wins. There is no fuzzy or substring execution. If an active-Explorer command was started from a virtual location, network path, missing folder, or an unidentifiable window, the action fails closed instead of falling back to another directory.

Mówik captures the Explorer identity when F7 is pressed, not after transcription completes. Open and terminal actions are also blocked if Mówik is running with administrator privileges, preventing child programs from silently inheriting an elevated token. Multi-line insert actions are length-bounded so their confirmation shows the complete content. Command phrases and content remain plain local data in `%APPDATA%\Mowik\config.json`; do not store passwords, tokens, or other secrets there.

The violet on-screen indicator distinguishes command capture from regular green dictation. It uses its own processing animation and success mark, and can be disabled under **Sounds → Feedback**.

### Antivirus and SmartScreen transparency

Mówik does not obfuscate code, disable antivirus protection, create Defender exclusions, hide command shells, or download executable updates. The Windows build is one-directory rather than a self-extracting one-file binary, uses an `asInvoker` manifest, keeps autostart opt-in, and does not execute terminal drafts. These choices reduce suspicious behavior but cannot replace Authenticode signing and normal reputation building. The public 2.7.3 installer is unsigned, so an Unknown publisher or SmartScreen warning is possible even when its SHA-256 matches the official release. If antivirus reports the verified official file, submit that exact file as a possible false positive instead of weakening the user's security settings.

Hold F7, speak the configured phrase, and release it. If no valid exact or terminal-prefix match is found, Mówik performs no action and never falls back to inserting the utterance as dictation. Command recognition also bypasses voice-command replacements and Ollama correction so the trigger phrase cannot be rewritten unexpectedly.

Speech recognition remains local, but an action that opens a website or starts a network-aware program may of course use that program's internet connection.

## Optional LLM correction with Ollama

Ollama is not required for speech recognition. It can optionally correct punctuation and obvious spelling mistakes after transcription:

1. Install Ollama separately and download a model through Ollama.
2. Open **Integrations** (**Integracje** in Polish), enable correction, and enter the name of the downloaded model.

Mówik rejects the corrected result if it changes the original text, numbers, or negations too extensively. For legal, medical, and financial text, leaving LLM correction disabled is recommended.

## Privacy

- Audio is kept temporarily in RAM and recordings are never saved.
- Technical logs do not contain dictated text.
- Custom-command phrases and payloads are not written to the technical log, but they are stored locally in `config.json` so they can be edited.
- Transcription runs locally. Once the speech model has been downloaded, no internet connection is required.
- If enabled, Ollama is contacted through the local address `127.0.0.1`.

The microphone remains open while Mówik is running so that it can maintain the short pre-roll buffer. This reduces clipped first and last syllables. The buffer is never written to disk.

## Accuracy and performance

The `auto` model setting selects the low-latency `large-v3-turbo` model for both GPU and CPU processing. The full `large-v3` model remains available through the **Most accurate** (**Najdokładniejszy** in Polish) profile.

Mówik first attempts to load the model exclusively from its local cache. Once the model has been downloaded, application startup therefore does not depend on a response from the Hugging Face server.

For the best results, use a microphone close to your mouth, reduce background noise, select the language you are speaking instead of automatic detection, maintain a custom vocabulary, and speak in short, clear phrases. No speech-recognition system can guarantee 100% accuracy.

The installer includes its own CUDA 12.9, cuBLAS, and cuDNN runtime, so Mówik does not depend on CUDA libraries installed by other applications. A compatible NVIDIA GPU is selected automatically; CUDA processing uses `float16`, while the automatic CPU fallback uses `int8`. The bundled CUDA runtime supports RTX 50-series GPUs. If the GPU encoder test fails, Mówik records the technical details in the log and continues on the CPU.

In CPU mode, a thread count of `0` enables automatic selection based on the number of physical CPU cores, up to a maximum of 16 threads.

## Diagnostics and files

| Item | Location |
|---|---|
| Settings panel | Start menu → **Mówik → Mówik Settings** |
| Audio devices | **Mówik Settings → Dictation → Microphone** |
| Log | `%LOCALAPPDATA%\Mowik\mowik.log` |
| Configuration | `%APPDATA%\Mowik\config.json` |
| Vocabulary | `%APPDATA%\Mowik\slownik.txt` |
| Sounds | `%APPDATA%\Mowik\sounds` |
| Models | `%LOCALAPPDATA%\Mowik\models` |

Mówik cannot insert text into an application running as administrator unless Mówik itself is also running as administrator. This is a Windows security restriction on simulated keyboard input between processes running with different privilege levels.

## Repair, startup, and building

- Running the same installer again repairs application files without deleting user data.
- Automatic startup is an unchecked, explicit opt-in in the setup wizard. Running the installer again allows you to change that option.
- The interface language can follow Windows automatically or be set explicitly to English or Polish in Mówik Center.
- `BUDUJ_EXE.cmd` builds the application directory at `dist\Mowik`.
- `BUDUJ_INSTALATOR.cmd` runs the tests, builds the application, and creates the explicitly local-only `release\Mowik-x.y.z-Setup-UNSIGNED.exe` together with its SHA-256 checksum. Do not upload an ad-hoc local build as an official release. Official artifacts are built through the GitHub release workflow together with `SHA256SUMS.txt`; the public 2.7.3 installer is also unsigned and may trigger Windows warnings.
- Reproducible release definitions are stored in `packaging`, and the GitHub Actions workflow is located at `.github/workflows/windows-release.yml`.

## License

Mówik is available under the [MIT License](LICENSE.txt). Libraries and models retain their respective licenses. The most important third-party licensing information is collected in [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
