# whisperer-tray-windows

A Windows system-tray app that turns push-to-talk into clipboard-paste dictation, using Groq's hosted Whisper API. Hold a key, talk, release — the transcription is pasted at your cursor.

Forked from [KennyVaneetvelde/groq_whisperer](https://github.com/KennyVaneetvelde/groq_whisperer). Adds a system-tray UI, configurable trigger key with on-the-fly capture, layered hallucination filtering, audio feedback, and `.env`-based configuration.

## Features

- **System-tray only**: lives in the notification area, no taskbar entry, no Alt+Tab.
- **Hold-to-talk**: hold a key (default: right Ctrl), speak, release. Transcription is copied to the clipboard and pasted at the cursor.
- **Choose-key dialog**: right-click the tray icon → "Choose key" → press the key you want; press Esc to confirm. Saved to `config.json` (per-user, gitignored).
- **Audio cues**: a soft TIC when the mic opens, a TOC when it closes — tied to the actual mic state.
- **Color states**: green (ready) / red (recording) / yellow (capturing a new key) / gray-with-slash (paused).
- **Hallucination filtering** in three layers:
  1. Pre-filter: drop audio shorter than `WHISPERER_MIN_DURATION_SEC` or quieter than `WHISPERER_MIN_RMS`.
  2. Whisper called with `response_format=verbose_json`; segments dropped if `no_speech_prob > 0.6 AND avg_logprob < -1.0`, or if `compression_ratio > 2.4`.
  3. Output substring/phrase blocklist for the well-known Whisper hallucinations ("Subtitles by Amara.org community", "¡Suscríbete!", "Thanks for watching", etc.).
- **Configurable language and prompt** via `.env` — default is Spanish, override `WHISPERER_LANGUAGE` and `WHISPERER_PROMPT` for any language.
- **Runtime log** at `whisperer.log` (gitignored) shows every API call's metrics and every dropped clip's reason — useful for tuning thresholds.

## Prerequisites

- Windows 10 or 11.
- Python 3.10+.
- A Groq API key — get one at https://console.groq.com/keys.

## Installation

```bash
git clone https://github.com/carloscascos/whisperer-tray-windows
cd whisperer-tray-windows

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt

copy .env.example .env
# edit .env and paste your GROQ_API_KEY
```

## Usage

### Tray app (recommended)

```bash
.venv\Scripts\pythonw.exe tray_app.py
```

Use `pythonw.exe` (not `python.exe`) so no console window opens. The tray icon should appear; right-click for the menu.

To launch on login, drop a shortcut to `pythonw.exe tray_app.py` in `shell:startup`. The shortcut already in this repo (`whisperer.bat`) wraps that for you.

### Simple CLI (original behavior)

```bash
python main.py
```

Hold right Ctrl, speak, release. Transcription is printed and copied to the clipboard.

## Configuration (`.env`)

See `.env.example` for the full list. Common ones:

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — (required) | Your Groq API key. |
| `WHISPERER_LANGUAGE` | `es` | ISO-639-1 language code passed to Whisper. |
| `WHISPERER_PROMPT` | `""` | Sentence biasing the transcription. Use a natural sentence in the target language; avoid lists. |
| `WHISPERER_MIN_DURATION_SEC` | `0.4` | Drop audio shorter than this. |
| `WHISPERER_MIN_RMS` | `250` | Drop audio quieter than this. |

Re-launch the tray app after editing `.env`.

## Tips

- **Use headphones during video calls.** The mic is shared with Teams/Zoom; if you use speakers, the remote person's voice will leak into your dictation.
- **Mute on the call before dictating** if you don't want the other side to hear your prompts. Whisperer can't mute Teams/Zoom for you.
- **Check `whisperer.log`** after a confusing transcription. Lines like `to API: dur=1.15s rms=906` for a hallucinated reply tell you the audio's metrics — raise `WHISPERER_MIN_RMS` if needed.

## License

MIT — see [LICENSE](LICENSE). Original work © Kenny Vaneetvelde, modifications © Carlos Cascos.
