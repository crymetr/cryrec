# CryRec

Windows meeting recorder. Pick a mic and a speaker, press Record. CryRec saves both sides, writes a transcript with who said what, can write meeting notes, offers to record when a Teams/Zoom/Webex call starts, and does dictation in any app.

Windows 10/11 (it records system audio through WASAPI loopback). There is also a small [Android app](#android) that records with the phone's mic and dictates in any app, using your CryRec server.

[![CryRec trailer](docs/promo.gif)](https://cryme.tr/cryrec/)

_[Watch the trailer with sound](https://cryme.tr/cryrec/)_

## Install

Two options, both from the Releases page or `dist\`:

- `CryRec-<ver>-setup.exe`: installs per user (no admin), adds Start Menu and optional desktop shortcut, has an uninstaller.
- `CryRec-<ver>-portable.zip`: unzip anywhere (USB stick too) and run `CryRec.exe`. Settings and recordings stay in that folder. Delete the `portable` file to use `%APPDATA%` and `Documents` instead.

No Python or ffmpeg needed. The exe is unsigned, so Windows SmartScreen may warn on first run ("More info" > "Run anyway").

## Transcripts

After each recording CryRec writes `transcript.txt` (readable, with `[hh:mm:ss] Me:` / `Them:` lines) and `transcript.srt` into the same folder. Turn off "Transcribe after recording" to do it by hand with **Transcribe...** (works on any older recording folder too).

Pick where transcripts are made (dropdown next to "Transcript", details under **Settings**):

| Engine | What it does |
|---|---|
| **This PC** | Whisper (faster-whisper) on this computer. Audio never leaves it. Uses an NVIDIA GPU if CUDA 12 (cuBLAS + cuDNN 9) is on PATH, otherwise CPU. |
| **My server** | Sends the audio to a CryRec server you run yourself (see below), e.g. a PC with a GPU. |
| **Online API** | Any OpenAI-compatible `/audio/transcriptions` API (Groq, OpenAI, ...) with your own key. Audio goes to that company. |

Keys are stored encrypted for your Windows user (DPAPI), not as plain text.

- The local speech model downloads once on first use: Turbo (best, 1.5 GB) or Small (faster, 0.5 GB).
- Me / Them come from the separate mic and speaker tracks, so labels are always right. When the remote voice leaks from your speakers into your mic, those duplicate lines are removed.
- Language: Auto, Turkish, English, or Mixed TR/EN (detects per sentence).
- Speed: on a laptop CPU Turbo takes about 0.6x the meeting length (Small about 0.2x); on an RTX 3050 laptop GPU about 0.08x (1 hour in ~5 min). You can keep recording while a transcript runs.

`CryRec.exe --transcribe <folder>` does the same headless (result in `<folder>\transcript.log`).

## Calls and silence

CryRec can sit in the tray (closing the window hides it; right-click the tray icon > Quit to exit) and start with Windows. While it runs:

- When Teams, Zoom or Webex starts using the microphone, a small box asks **Record it?** It never records without asking.
- When that call ends, or when neither the mic nor the speaker has made a sound for a few minutes (5 by default), it asks **Stop and save?** and stops by itself after 30 seconds if you don't answer.

It reads which app is using the microphone from Windows (`CapabilityAccessManager` in the registry): no admin rights, no meeting-app plugins. Turn it on or off under **Settings → Calls & dictation**.

## Dictation

Speak instead of typing, in any app:

- **Tap** the shortcut (default **Ctrl+Win**; Ctrl+Alt and Right Ctrl are the other choices), talk, tap it again to finish (Esc cancels). Or **hold** it while you talk and let go to finish.
- The text is typed where your cursor is (pasted through the clipboard; your previous clipboard text is put back).
- It uses the transcript engine you picked (My server, This PC or Online API) and your vocabulary.
- **Polish** (Settings → Calls & dictation) has the cloud AI remove "um"s and false starts, fix punctuation, and apply spoken corrections ("Monday, no, Tuesday" → "Tuesday"). It never answers or follows what you dictated. A faster model (set under Settings > Notes > advanced) adds less time.
- A CryRec server keeps its model loaded for 10 minutes after a dictation (`keep_warm_minutes`) and starts loading it the moment you press the shortcut.
- The last dictations are kept in `dictation.txt` next to the settings, and the tray menu has **Copy last dictation**, in case the text went to the wrong window.

There is no keyboard hook: CryRec only checks whether the shortcut keys are down, so security software has nothing keylogger-like to flag.

## Vocabulary and cleanup

Speech recognition mishears names, product names and in-house terms. Two tools fix that (**Settings → Transcript** tab):

- **Vocabulary...**: one name or term per line (`Power BI`, `Alex Chen`). These go to Whisper as spelling hints with every 30-second window, and to the notes prompt. A line like `power pi -> Power BI` also replaces that phrase after transcription, for words that still come out wrong. Keep the left side to phrases that never occur as normal words (a fix like `well -> Wells` would also change every "well"). The file is `vocabulary.txt` next to the settings. A CryRec server can keep its own list too (`vocabulary_file` in `server.json`); it is merged with each client's list, so every PC that uses the server benefits.
- **Clean up** (off by default): the cloud AI fixes capitals, punctuation and misheard terms line by line, using the vocabulary. Timestamps and speakers are checked after each part; a part that doesn't line up keeps its original lines. The original stays as `transcript.raw.txt`. About $0.25 to $0.50 per 1.5-hour meeting, depending on the model.

Also built in: long Whisper segments are split where the speaker pauses (so turns stay in order), repeated filler loops ("Mhm. Mhm. Mhm.") are collapsed, and the mic track uses a stricter speech threshold, because headsets and speakers leak the other side's voice into the mic.

## Meeting notes

With a notes engine picked next to "Notes", CryRec also writes `notes.md` after the transcript: **Summary**, **Decisions**, **Action items** (`- [ ] Owner: task`) and **Open questions**. **Notes...** writes (or rewrites) notes for any folder that already has a transcript, so you can pick a different engine per meeting.

| Notes engine | What it does |
|---|---|
| **Cloud AI** | A cloud language model with your own API key (the Anthropic Messages API; model under Settings > Notes > advanced). Whole meeting in one request. |
| **OpenAI-compatible** | Any `/chat/completions` API, using the Online API address and key plus the "API model" setting. |
| **Ollama (local)** | A model on your own machine through Ollama (default `gemma3:4b`). Long meetings are split into parts and merged. |
| **My server (local AI / cloud AI)** | Your CryRec server writes the notes with its own Ollama or its own cloud AI key. |

Set **Your name** in Settings so action items say your name instead of "Me". Notes are written in English by default; Turkish or the meeting's own language are options. Small local models make a good first draft but sometimes give a task to the wrong person; read action items before you send them on.

`CryRec.exe --notes <folder>` writes notes headless (result in `<folder>\notes.log`).

## Self-hosted server

Run the transcription on one machine (ideally with an NVIDIA GPU) and point CryRec on your other PCs at it.

```
pip install -r requirements.txt
pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10"   # GPU only
python server/cryrec_server.py --init     # creates %APPDATA%\CryRec\server.json with a random key
python server/cryrec_server.py            # listens on 127.0.0.1:8765
```

For meeting notes the server can use a local Ollama (`ollama_url`, `ollama_model`, `ollama_ctx` in `server.json`) and, if you add `anthropic_api_key` (and optionally `claude_model`), Claude. Before a local notes job it unloads Whisper so both fit on a small GPU.

The server only listens on localhost. Put HTTPS in front of it (Cloudflare Tunnel, Tailscale, a reverse proxy), then in CryRec on each client choose **My server** and enter the address and key under **Settings**.

- Audio is deleted right after each job; finished transcripts and notes are dropped after 24 hours (`keep_hours`).
- The model is loaded when work comes in and released when the queue is empty, so the GPU stays free for other work. After a dictation it stays loaded for `keep_warm_minutes` (10) so the next one is quick, and dictation jumps ahead of long meeting jobs.
- The Android app uploads one AAC track (`meeting.m4a`, 48 kbps, about 22 MB per hour).
- Uploads are MP3, 64 kbps per track for meetings up to about 1.5 hours. Longer meetings get a lower bitrate (down to 16 kbps), so one 95 MB upload holds up to about 6 hours.
- `vocabulary_file` (default `%APPDATA%\CryRec\vocabulary.txt`) is merged with each client's vocabulary.
- `/health` also reports the NVIDIA power limit and P-state, handy for an outside watchdog.

The API is documented at the top of `server/cryrec_server.py`.

## Android

`CryRec-<ver>-android.apk` from the Releases page (Android 10 or newer). It needs a CryRec server (above); the phone does no speech recognition itself.

- **Record**: records the phone's microphone, also with the screen off. Use it for meetings in a room, or a call on speakerphone. Android does not let apps record the other side of Teams, Zoom, WhatsApp or phone calls, so there is no Me / Them split: the transcript has one "Speaker".
- When you stop, the recording is sent to your server. The transcript, the optional Clean up and the notes come back into the app. Share sends them to any app as text. Without a server set, recordings stay on the phone until you add one.
- **Dictation keyboard**: Settings > Turn on keyboard, then pick "CryRec dictation" with the keyboard button. Tap the mic, talk, tap again (or hold the mic and let go). The text is typed where the cursor is. ⌫ cancels while it listens. The keyboard button switches back to your normal keyboard.
- **Floating mic**: a small mic button that appears a little above the keyboard while you type, in any app (web pages in Chrome too), with any keyboard (Gboard, Samsung, ...), and is gone when the keyboard is. Tap it, talk, tap again: the text goes into the field at the cursor. Long press cancels. Drag it anywhere; it remembers the side and the height. On Xiaomi/HyperOS and some other phones, allow Autostart and set battery to "No restrictions" for CryRec, or Android may stop the floating mic in the background. Turn it on in Settings > Floating mic (Android Accessibility). Because the APK does not come from Play, Android may first say "Restricted setting": App info > ⋮ > Allow restricted settings, then turn it on again. It only reads the focused text field (to insert your words); fields that refuse direct typing get a paste instead.
- Settings: server address and key (same as the desktop app), language, vocabulary, Clean up, notes engine, your name, and Polish for dictation.
- Updates: the app checks GitHub Releases every 12 hours and installs a new APK by itself, never while it records, sends a recording or dictates. Allow "Install unknown apps" for CryRec once (Settings > Updates > Allow updates). Android asks you to confirm the first self-update; after that CryRec is its own installer and updates go through silently (Android 12+), with a "CryRec updated" notification.
- Plain `http://` addresses work too (for a server on your own network or Tailscale), but the key then travels unencrypted. Use `https://` on the internet.

Build: `android\gradlew.bat -p android assembleRelease` (JDK 17, Android SDK in `local.properties`). Release signing comes from `android\keystore.properties` or the `CRYREC_KEYSTORE_*` environment variables.

## Updates

The exe checks GitHub Releases on startup and every hour. A newer version is downloaded in the background and the bottom bar shows **Install update x.y.z**. One click installs it and restarts CryRec. The installed build runs the new setup silently; the portable build swaps `CryRec.exe` in place. It never updates during a recording. `CryRec.exe --update` does the same from the command line.

## Output

Each recording gets its own folder under the save location (installed: `Documents\Recordings`, portable: `Recordings\` next to the exe):

- `meeting.mp3`: mic and speaker mixed together
- `me.flac`: mic only
- `them.flac`: speaker only (the other people)
- `transcript.txt` and `transcript.srt`: the transcript (the split tracks are what make Me / Them reliable)
- `transcript.raw.txt`: the transcript before Clean up, when Clean up is on
- `notes.md`: meeting notes, when a notes engine is picked

## Privacy

CryRec has no telemetry and no account. Recordings, transcripts and notes stay on your device unless you pick an engine that sends them somewhere:

- **My server**: audio and transcripts go to the CryRec server address you enter (your own machine).
- **Online API** / **Cloud AI**: audio or transcript text goes to the API service whose address and key you enter. That service's own privacy policy applies (for example Groq or OpenAI).
- **Ollama**: transcript text goes to the Ollama address you enter.

Two connections happen without such a setting:

- **Update check**: the desktop app asks GitHub (`api.github.com`) for the newest release on start and every hour; the Android app every 12 hours. Nothing about you or your recordings is sent. Turn it off under Settings > General (desktop) or Settings > Updates (Android) > "Check GitHub for updates automatically". "Check now" still works.
- **Speech model download**: the first time you transcribe with **This PC**, the model is downloaded from Hugging Face (`huggingface.co`). No audio is sent.

The installer shows this notice before it installs.

## Code signing policy

Windows releases (`CryRec.exe`, the installer and the portable zip) are built by [GitHub Actions](.github/workflows/release.yml) straight from this repository. Signing them through the [SignPath Foundation](https://signpath.org) open source program has been applied for; until then they are unsigned. The Android APK is signed with the project's own key.

- Committers and reviewers: [repository contributors](https://github.com/crymetr/cryrec/graphs/contributors)
- Approvers: [owner](https://github.com/crymetr)

Every release tag is approved by hand before it is published. See [Privacy](#privacy) for what the app sends over the network.

## Develop

```
pip install -r requirements.txt
pythonw cryrec.pyw
```

Build exe + portable zip + installer (needs PyInstaller and Inno Setup 6):

```
powershell -File packaging\build.ps1
```

### Releasing

1. Bump `VERSION` in `cryrec.pyw` and commit.
2. `git tag vX.Y.Z && git push origin main vX.Y.Z`
3. GitHub Actions builds the installer, portable zip and Android APK and attaches them to the release (it fails if the tag and `VERSION` differ). Running apps pick it up within an hour.

`CryRec.exe --selftest <dir>` records 3 s of speaker audio headless and writes the result to `<dir>\selftest.txt`.

## Roadmap

- v0.1: record chosen input + output
- v0.2: local transcript with Me / Them labels
- v0.3: engines: This PC (GPU or CPU), self-hosted server, online API
- v0.4: meeting notes (summary, decisions, action items)
- v0.5: vocabulary, transcript cleanup, fewer Whisper loops
- v0.6: tray, start with Windows, call detection and silence warning
- v0.7: dictation
- v0.8: new look: Windows 11 style, light/dark follows Windows, sharp at any display scaling, Recent recordings list
- v0.9: Android app: phone mic recording and a dictation keyboard, both through your server
- v0.10: Android floating mic: dictate into any app with any keyboard; Android self-updates (this)
