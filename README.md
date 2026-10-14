# CryRec

Small Windows meeting recorder. Pick a mic and a speaker, press Record.

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

## Vocabulary and cleanup

Speech recognition mishears names, product names and in-house terms. Two tools fix that (**Settings → Transcript quality**):

- **Vocabulary...**: one name or term per line (`Power BI`, `Alex Chen`). These go to Whisper as spelling hints with every 30-second window, and to the notes prompt. A line like `power pi -> Power BI` also replaces that phrase after transcription, for words that still come out wrong. Keep the left side to phrases that never occur as normal words (a fix like `well -> Wells` would also change every "well"). The file is `vocabulary.txt` next to the settings. A CryRec server can keep its own list too (`vocabulary_file` in `server.json`); it is merged with each client's list, so every PC that uses the server benefits.
- **Clean up** (off by default): Claude fixes capitals, punctuation and misheard terms line by line, using the vocabulary. Timestamps and speakers are checked after each part; a part that doesn't line up keeps its original lines. The original stays as `transcript.raw.txt`. About $0.25 to $0.50 per 1.5-hour meeting, depending on the model.

Also built in: long Whisper segments are split where the speaker pauses (so turns stay in order), repeated filler loops ("Mhm. Mhm. Mhm.") are collapsed, and the mic track uses a stricter speech threshold, because headsets and speakers leak the other side's voice into the mic.

## Meeting notes

With a notes engine picked next to "Notes", CryRec also writes `notes.md` after the transcript: **Summary**, **Decisions**, **Action items** (`- [ ] Owner: task`) and **Open questions**. **Notes...** writes (or rewrites) notes for any folder that already has a transcript, so you can pick a different engine per meeting.

| Notes engine | What it does |
|---|---|
| **Claude** | Anthropic API with your key (default model `claude-opus-5-5`, change it in Settings). Whole meeting in one request. |
| **OpenAI-compatible** | Any `/chat/completions` API, using the Online API address and key plus the "API model" setting. |
| **Ollama (local)** | A model on your own machine through Ollama (default `gemma3:4b`). Long meetings are split into parts and merged. |
| **My server (local / Claude)** | Your CryRec server writes the notes with its own Ollama or its own Anthropic key. |

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

The server only listens on localhost. Put HTTPS in front of it (Cloudflare Tunnel, Tailscale, a reverse proxy), then in CryRec on each client choose **My server** and enter the address and key under **Settings**. Audio is deleted right after each job, and transcripts are dropped after 24 hours. The model is loaded per batch of jobs and released afterwards, so the GPU stays free for other work. Uploads are MP3 at up to 64 kbps, which fits about 3 hours of meeting into one 95 MB request.

## Updates

The exe checks GitHub Releases on startup and every hour. A newer version is downloaded in the background and the bottom bar shows **Install update x.y.z**. One click installs it and restarts CryRec. The installed build runs the new setup silently; the portable build swaps `CryRec.exe` in place. It never updates during a recording. `CryRec.exe --update` does the same from the command line.

## Output

Each recording gets its own folder under the save location (installed: `Documents\Recordings`, portable: `Recordings\` next to the exe):

- `meeting.mp3`: mic and speaker mixed together
- `me.flac`: mic only
- `them.flac`: speaker only (the other people)

The split tracks are kept for the transcript feature (who said what).

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
3. GitHub Actions builds the installer and portable zip and attaches them to the release (it fails if the tag and `VERSION` differ). Running apps pick it up within an hour.

`CryRec.exe --selftest <dir>` records 3 s of speaker audio headless and writes the result to `<dir>\selftest.txt`.

## Roadmap

- v0.1: record chosen input + output
- v0.2: local transcript with Me / Them labels
- v0.3: engines: This PC (GPU or CPU), self-hosted server, online API
- v0.4: meeting notes (summary, decisions, action items)
- v0.5: vocabulary, transcript cleanup, fewer Whisper loops
- v0.6: tray, start with Windows, call detection and silence warning (this)
- v0.7: dictation
