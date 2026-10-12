# CryRec

Small Windows meeting recorder. Pick a mic and a speaker, press Record.

## Install

Two options, both from the Releases page or `dist\`:

- `CryRec-<ver>-setup.exe`: installs per user (no admin), adds Start Menu and optional desktop shortcut, has an uninstaller.
- `CryRec-<ver>-portable.zip`: unzip anywhere (USB stick too) and run `CryRec.exe`. Settings and recordings stay in that folder. Delete the `portable` file to use `%APPDATA%` and `Documents` instead.

No Python or ffmpeg needed. The exe is unsigned, so Windows SmartScreen may warn on first run ("More info" > "Run anyway").

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

`CryRec.exe --selftest <dir>` records 3 s of speaker audio headless and writes the result to `<dir>\selftest.txt`.

## Roadmap

- v0.1: record chosen input + output (this)
- v0.2: local transcript with speaker labels
- v0.3: dictation
