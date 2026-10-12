# CryRec

Small Windows meeting recorder. Pick a mic and a speaker, press Record.

## Run

```
pip install -r requirements.txt
pythonw cryrec.pyw
```

Needs `ffmpeg` on PATH (or `ffmpeg.exe` next to `cryrec.pyw`) to make the MP3.

## Output

Each recording gets its own folder under the save location (default `Documents\Recordings`):

- `meeting.mp3`: mic and speaker mixed together
- `me.flac`: mic only
- `them.flac`: speaker only (the other people)

The split tracks are kept for the transcript feature (who said what).

## Roadmap

- v0.1: record chosen input + output (this)
- v0.2: local transcript with speaker labels
- v0.3: dictation
