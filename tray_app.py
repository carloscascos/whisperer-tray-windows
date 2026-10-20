import ctypes
import sys
from pathlib import Path as _Path

# Allow running with the base interpreter directly (no venv activation):
# the uv venv's pythonw.exe is a trampoline that spawns console python.exe,
# which flashes a Windows Terminal window the app cannot hide.
_site = _Path(__file__).parent / ".venv" / "Lib" / "site-packages"
if _site.is_dir() and str(_site) not in sys.path:
    sys.path.insert(0, str(_site))

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32_early = ctypes.WinDLL("user32", use_last_error=True)
    _con_hwnd = _k32.GetConsoleWindow()
    if _con_hwnd:
        _u32_early.ShowWindow(_con_hwnd, 0)
    _k32.FreeConsole()

import audioop
import io
import math
import os
import re
import struct
import subprocess
import threading
import time
import tkinter as tk
import unicodedata
import wave
import webbrowser
from ctypes import wintypes
from pathlib import Path

if sys.platform == "win32":
    import winsound

import keyboard
import pyaudio
import pyautogui
import pyperclip
import pystray
from dotenv import load_dotenv, set_key
from groq import Groq
from PIL import Image, ImageDraw

ICON_PATH = Path(__file__).with_name("icon.png")
ENV_PATH = Path(__file__).with_name(".env")

load_dotenv(ENV_PATH)


def _make_tone_wav(
    freq_hz, ms, sample_rate=22050, volume=6000, attack_ms=4, release_ms=15,
    lead_ms=0, tail_ms=0, lead_volume=0,
):
    lead = int(sample_rate * lead_ms / 1000)
    n = int(sample_rate * ms / 1000)
    attack = max(1, int(sample_rate * attack_ms / 1000))
    release = max(1, int(sample_rate * release_ms / 1000))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        # The lead pad carries the tone at near-inaudible level rather than
        # pure zeros: silent samples can be optimized away, so they don't
        # reliably wake a sleeping Bluetooth link.
        for i in range(lead):
            v = int(lead_volume * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            w.writeframesraw(struct.pack("<h", v))
        for i in range(n):
            if i < attack:
                env = i / attack
            elif i > n - release:
                env = max(0.0, (n - i) / release)
            else:
                env = 1.0
            v = int(volume * env * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            w.writeframesraw(struct.pack("<h", v))
        w.writeframesraw(b"\x00\x00" * int(sample_rate * tail_ms / 1000))
    return buf.getvalue()


# Bluetooth outputs sleep between sounds and swallow (or clip into a click)
# the first few hundred ms of playback while the link wakes up. Each cue
# carries ~500ms of near-inaudible lead tone to wake the link with real
# audio, then fades in gently so a late link entry never clicks.
_TIC_WAV = (
    _make_tone_wav(420, 200, attack_ms=60, lead_ms=500, tail_ms=80, lead_volume=80)
    if sys.platform == "win32" else b""
)
# Long low buzz: the recording was discarded or failed — nothing was pasted.
# Immediate feedback so a long dictation is never lost silently.
_ERR_WAV = (
    _make_tone_wav(170, 500, attack_ms=20, release_ms=120, lead_ms=500,
                   tail_ms=80, lead_volume=80)
    if sys.platform == "win32" else b""
)

# The stop cue plays while the headset is switching back from hands-free to
# music mode, whose endpoint re-activation pops: longer quiet lead and a
# slower fade-in absorb it. (Fallback path only — normally the stop cue goes
# out through the still-open hands-free link, see Recorder._play_toc_hfp.)
_TOC_WAV = (
    _make_tone_wav(350, 200, attack_ms=100, lead_ms=800, tail_ms=80, lead_volume=80)
    if sys.platform == "win32" else b""
)


def _make_tone_raw(freq_hz, ms, sample_rate=22050, volume=6000,
                   attack_ms=30, release_ms=30, tail_ms=80):
    """Raw PCM frames (no WAV container) for pyaudio playback."""
    n = int(sample_rate * ms / 1000)
    attack = max(1, int(sample_rate * attack_ms / 1000))
    release = max(1, int(sample_rate * release_ms / 1000))
    out = bytearray()
    for i in range(n):
        if i < attack:
            env = i / attack
        elif i > n - release:
            env = max(0.0, (n - i) / release)
        else:
            env = 1.0
        out += struct.pack(
            "<h", int(volume * env * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        )
    out += b"\x00\x00" * int(sample_rate * tail_ms / 1000)
    return bytes(out)


# The hands-free channel is 16kHz narrowband and plays quieter than the
# music channel: generate at its native rate (resampling smears the tone
# into a faint buzz), louder, and as a double beep so it is unmistakable.
_TOC_RAW_RATE = 16000
_TOC_RAW = (
    _make_tone_raw(550, 120, sample_rate=_TOC_RAW_RATE, volume=18000, tail_ms=70)
    + _make_tone_raw(550, 120, sample_rate=_TOC_RAW_RATE, volume=18000, tail_ms=100)
)


def _play_sync(blob):
    if sys.platform != "win32" or not blob:
        return
    try:
        winsound.PlaySound(blob, winsound.SND_MEMORY)
    except Exception:
        pass


def _play_async(blob, delay=0.0):
    if sys.platform != "win32" or not blob:
        return

    def _play():
        if delay:
            time.sleep(delay)
        _play_sync(blob)

    threading.Thread(target=_play, daemon=True).start()


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


DEFAULT_TRIGGER = "ctrl derecha"
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK = 1024
MIN_DURATION_SEC = _env_float("WHISPERER_MIN_DURATION_SEC", 0.4)
MIN_RMS = _env_float("WHISPERER_MIN_RMS", 250)
MAX_RECORDING_SEC = _env_float("WHISPERER_MAX_RECORDING_SEC", 300.0)
# Bluetooth headsets switch profiles (A2DP <-> HFP) when the mic opens or
# closes, leaving a dead window where sounds routed to the default output are
# dropped. The start cue must play BEFORE the mic opens; the stop cue must
# wait until the headset has switched back, which takes over a second.
TOC_DELAY_SEC = _env_float("WHISPERER_TOC_DELAY_SEC", 0.6)
# Below this: silence/background (icon blue). Between this and MIN_RMS:
# audible but too quiet to pass the gate (icon red — speech being lost).
NOISE_RMS = _env_float("WHISPERER_NOISE_RMS", max(20.0, MIN_RMS * 0.4))

LANGUAGE = os.environ.get("WHISPERER_LANGUAGE", "es")
PROMPT = os.environ.get("WHISPERER_PROMPT", "")


def _parse_replacements(raw):
    """Parse 'origen => destino' rules (split on newline or comma)."""
    rules = []
    for line in re.split(r"[\n,]", raw or ""):
        line = line.strip()
        if not line or "=>" not in line:
            continue
        src, _, tgt = line.partition("=>")
        src, tgt = src.strip(), tgt.strip()
        if src:
            rules.append((re.compile(r"\b" + re.escape(src) + r"\b", re.IGNORECASE), tgt))
    return rules


REPLACEMENTS = _parse_replacements(os.environ.get("WHISPERER_REPLACEMENTS", ""))


def apply_replacements(text):
    for pattern, repl in REPLACEMENTS:
        text = pattern.sub(repl, text)
    return text

MODEL_NORMAL = "whisper-large-v3"
MODEL_TURBO = "whisper-large-v3-turbo"

GROQ_USAGE_URL = "https://console.groq.com/dashboard/usage"


def _parse_languages():
    raw = os.environ.get("WHISPERER_LANGUAGES", "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if parts:
        return parts
    return [LANGUAGE] if LANGUAGE else []


LANGUAGES = _parse_languages()

HALLUCINATION_SUBSTRINGS = ("amara.org",)
HALLUCINATION_PHRASES = {
    # Amara / subtitles
    "subtitulos por la comunidad de amara.org",
    "subtitulos realizados por la comunidad de amara.org",
    "subtitulado por la comunidad de amara.org",
    "subtitles by the amara.org community",
    # Gracias por ver
    "gracias por ver",
    "gracias por ver el video",
    "muchas gracias por ver el video",
    "muchas gracias por ver",
    "gracias por vernos",
    # Suscribete (varias formas)
    "suscribete",
    "suscribanse",
    "subscribete",
    "suscribete al canal",
    "suscribete a nuestro canal",
    "suscribete a mi canal",
    "no te olvides de suscribirte",
    "no olvides suscribirte",
    "no se olviden de suscribirse",
    # Like / share
    "dale like",
    "dale like al video",
    "like y suscribete",
    "comparte el video",
    # Cierre
    "hasta la proxima",
    "hasta luego",
    "nos vemos en el proximo video",
    "nos vemos pronto",
    "bienvenidos a un nuevo video",
    # English equivalents
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "dont forget to subscribe",
    "like and subscribe",
    "see you next time",
}


def _normalize_for_match(text):
    s = text.strip().lower().strip(" .!?¡¿,")
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def is_hallucination(text):
    norm = _normalize_for_match(text)
    if any(sub in norm for sub in HALLUCINATION_SUBSTRINGS):
        return True
    return norm in HALLUCINATION_PHRASES


def _seg_attr(seg, name, default):
    if isinstance(seg, dict):
        return seg.get(name, default)
    val = getattr(seg, name, default)
    return default if val is None else val

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))


def reload_prompt():
    """Re-read prompt + replacement rules from .env without restarting."""
    global PROMPT, REPLACEMENTS
    load_dotenv(ENV_PATH, override=True)
    PROMPT = os.environ.get("WHISPERER_PROMPT", "")
    REPLACEMENTS = _parse_replacements(os.environ.get("WHISPERER_REPLACEMENTS", ""))
    return PROMPT


def load_trigger_key():
    return os.environ.get("WHISPERER_TRIGGER_KEY") or DEFAULT_TRIGGER


def save_trigger_key(key):
    if not ENV_PATH.exists():
        ENV_PATH.touch()
    set_key(str(ENV_PATH), "WHISPERER_TRIGGER_KEY", key, quote_mode="never")
    os.environ["WHISPERER_TRIGGER_KEY"] = key


LOG_PATH = Path(__file__).with_name("whisperer.log")


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


class CaptureHandle:
    def __init__(self, root, top, var):
        self._root = root
        self._top = top
        self._var = var

    def set_key(self, name):
        if self._var is None or self._root is None:
            return
        try:
            self._root.after(0, lambda: self._var.set(name))
        except Exception as e:
            log(f"tk set_key error: {e!r}")

    def close(self):
        if self._top is None or self._root is None:
            return
        try:
            self._root.after(0, self._top.destroy)
        except Exception as e:
            log(f"tk close error: {e!r}")


class TkBackend:
    """Persistent hidden Tk root so Tcl never finalizes mid-app."""

    def __init__(self):
        self._root = None
        ready = threading.Event()

        def run():
            try:
                self._root = tk.Tk()
                self._root.withdraw()
                ready.set()
                self._root.mainloop()
            except Exception as e:
                log(f"tk backend error: {e!r}")
                ready.set()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(timeout=3)

    @property
    def ready(self):
        return self._root is not None

    def show_capture_window(self, initial_key):
        if not self.ready:
            return None
        holder = {"handle": None}
        done = threading.Event()

        def open_top():
            try:
                top = tk.Toplevel(self._root)
                top.title("Whisperer — choose key")
                top.attributes("-topmost", True)
                top.resizable(False, False)
                top.protocol("WM_DELETE_WINDOW", lambda: None)
                tk.Label(
                    top,
                    text=(
                        "Press the key you want to use.\n"
                        "Try several if you want; the last one wins.\n"
                        "Press Esc when you are happy with it."
                    ),
                    font=("Segoe UI", 12),
                    padx=30,
                    pady=15,
                    justify="center",
                ).pack()
                tk.Label(
                    top,
                    text="Selected key:",
                    font=("Segoe UI", 10),
                ).pack()
                var = tk.StringVar(value=initial_key)
                tk.Label(
                    top,
                    textvariable=var,
                    font=("Consolas", 18, "bold"),
                    fg="#1a7f37",
                    padx=30,
                    pady=15,
                ).pack()
                top.update_idletasks()
                w, h = top.winfo_width(), top.winfo_height()
                sw = top.winfo_screenwidth()
                sh = top.winfo_screenheight()
                top.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")
                holder["handle"] = CaptureHandle(self._root, top, var)
            except Exception as e:
                log(f"tk open error: {e!r}")
            finally:
                done.set()

        self._root.after(0, open_top)
        done.wait(timeout=2)
        return holder["handle"]




def scan_input_device(pa):
    """Pick the mic to record from. Preference order: WHISPERER_INPUT_DEVICE
    (name substring) if set, else the first Bluetooth hands-free mic (Windows
    names these "Headset (...)"), else the system default. MME host API only,
    to see each physical device once."""
    want = (os.environ.get("WHISPERER_INPUT_DEVICE") or "").strip().lower()
    headset = None
    try:
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d.get("maxInputChannels", 0) < 1:
                continue
            if pa.get_host_api_info_by_index(d["hostApi"])["name"] != "MME":
                continue
            name = str(d.get("name", ""))
            if want and want in name.lower():
                return i, name
            if headset is None and name.lower().startswith("headset ("):
                headset = (i, name)
    except Exception as e:
        log(f"device scan failed: {e!r}")
    if want:
        log(f"input device {want!r} not found; using auto/default")
    return headset if headset else (None, "system default")


def current_devices():
    """Return (mic_name, output_name) as they would be used right now."""
    try:
        pa = pyaudio.PyAudio()
        try:
            _, mic = scan_input_device(pa)
            if mic == "system default":
                mic = str(pa.get_default_input_device_info()["name"])
            out = str(pa.get_default_output_device_info()["name"])
        finally:
            pa.terminate()
        return mic, out
    except Exception as e:
        log(f"device status failed: {e!r}")
        return "?", "?"


class Recorder:
    _MAX_SAMPLES = int(MAX_RECORDING_SEC * SAMPLE_RATE)

    def __init__(self):
        self._audio = pyaudio.PyAudio()
        self._stream = None
        self._frames = []
        self._sample_count = 0
        self._was_headset = False
        self._lock = threading.Lock()
        # Live level feedback: on_level(0|1|2) fires on band changes —
        # 0 silence/background, 1 audible-but-below-gate, 2 voice registering.
        self.on_level = None
        self._level = 0
        self._level_t = 0.0

    def _pick_input_device(self):
        """Choose the input device for this recording, hot-plug aware.

        Recreates the PyAudio instance because it snapshots the device list
        at construction — without this, a headset connected after app start
        is invisible.
        """
        try:
            self._audio.terminate()
        except Exception:
            pass
        self._audio = pyaudio.PyAudio()
        return scan_input_device(self._audio)

    def _open_stream(self, device_index):
        return self._audio.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=CHUNK,
            stream_callback=self._on_data,
        )

    def start(self):
        with self._lock:
            if self._stream is not None:
                return
            device_index, device_name = self._pick_input_device()
            # Play the start cue before opening the mic: opening a Bluetooth
            # mic switches the headset to hands-free mode, which mutes normal
            # output, so playing it after means it is never heard.
            _play_sync(_TIC_WAV)
            self._frames = []
            self._sample_count = 0
            self._level = 0
            self._level_t = 0.0
            try:
                self._stream = self._open_stream(device_index)
            except Exception as e:
                if device_index is None:
                    raise
                log(f"open {device_name!r} failed ({e!r}); using default mic")
                device_index, device_name = None, "system default"
                self._stream = self._open_stream(None)
            log(f"recording from: {device_name}")
            self._was_headset = device_name.lower().startswith("headset (")
            self._stream.start_stream()

    def _play_toc_hfp(self):
        """Play the stop cue through the headset's hands-free output while
        the link is still in hands-free mode — instant and click-free,
        instead of waiting for the switch back to music mode."""
        try:
            idx = None
            for i in range(self._audio.get_device_count()):
                d = self._audio.get_device_info_by_index(i)
                if d.get("maxOutputChannels", 0) < 1:
                    continue
                if self._audio.get_host_api_info_by_index(d["hostApi"])["name"] != "MME":
                    continue
                if str(d.get("name", "")).lower().startswith("headset ("):
                    idx = i
                    break
            if idx is None:
                return False
            out = self._audio.open(
                format=pyaudio.paInt16, channels=1, rate=_TOC_RAW_RATE,
                output=True, output_device_index=idx,
            )
            out.write(_TOC_RAW)
            # write() returns when the buffers are queued, not played;
            # stopping right away discards the queued audio unplayed.
            time.sleep(len(_TOC_RAW) / (2 * _TOC_RAW_RATE) + 0.1)
            out.stop_stream()
            out.close()
            return True
        except Exception as e:
            log(f"hfp toc failed: {e!r}")
            return False

    def _on_data(self, in_data, frame_count, time_info, status):
        if self._sample_count < self._MAX_SAMPLES:
            self._frames.append(in_data)
            self._sample_count += frame_count
            if self._sample_count >= self._MAX_SAMPLES:
                log(f"recording cap hit: {MAX_RECORDING_SEC}s")
                _play_async(_TOC_WAV)
        self._update_voice(audioop.rms(in_data, 2))
        return (in_data, pyaudio.paContinue)

    def _update_voice(self, rms):
        if rms >= MIN_RMS:
            level = 2
        elif rms >= NOISE_RMS:
            level = 1
        else:
            level = 0
        # Upgrade immediately; downgrade only after 0.4s, so the indicator
        # stays steady across natural word gaps instead of flickering.
        now = time.monotonic()
        if level >= self._level:
            self._level_t = now
            if level > self._level:
                self._level = level
                self._notify_level(level)
        elif now - self._level_t > 0.4:
            self._level = level
            self._level_t = now
            self._notify_level(level)

    def _notify_level(self, level):
        cb = self.on_level
        if cb is None:
            return
        try:
            cb(level)
        except Exception as e:
            log(f"on_level callback failed: {e!r}")

    def stop(self):
        frames = None
        toc_played = False
        with self._lock:
            if self._stream is None:
                return None
            # Cue first, then close: while the mic is open the hands-free
            # link is up, so the cue plays immediately on the headset.
            if self._was_headset:
                toc_played = self._play_toc_hfp()
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
            self._level = 0
            frames = self._frames
        if not toc_played:
            _play_async(
                _TOC_WAV, delay=TOC_DELAY_SEC if self._was_headset else 0.0
            )
        return frames


def build_wav(raw):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw)
    return buf.getvalue()


def transcribe_and_paste(frames, language, model, on_text=None):
    if not frames:
        return
    raw = b"".join(frames)
    duration = len(raw) / (SAMPLE_RATE * 2 * CHANNELS)
    if duration < MIN_DURATION_SEC:
        log(f"skip transcribe: too short ({duration:.2f}s)")
        return
    # Gate on the loudest 0.5s window, not the whole-take average: a long
    # quiet dictation with pauses averages below threshold even though the
    # speech itself is fine, and Whisper handles quiet speech well.
    window = int(SAMPLE_RATE * 2 * CHANNELS * 0.5)
    peak = max(
        audioop.rms(raw[off:off + window], 2)
        for off in range(0, len(raw), window)
    )
    rms = audioop.rms(raw, 2)
    if peak < MIN_RMS:
        log(f"skip transcribe: too quiet (peak={peak}, rms={rms}, dur={duration:.2f}s)")
        _play_async(_ERR_WAV)
        return
    log(
        f"to API: dur={duration:.2f}s rms={rms} peak={peak} "
        f"lang={language or 'auto'} model={model}"
    )
    wav_bytes = build_wav(raw)
    try:
        api_kwargs = {
            "file": ("audio.wav", wav_bytes),
            "model": model,
            "prompt": PROMPT,
            "response_format": "verbose_json",
            "temperature": 0,
        }
        if language:
            api_kwargs["language"] = language
        response = client.audio.transcriptions.create(**api_kwargs)
        segments = getattr(response, "segments", None) or []
        if segments:
            kept = []
            for s in segments:
                no_speech = float(_seg_attr(s, "no_speech_prob", 0.0))
                avg_lp = float(_seg_attr(s, "avg_logprob", 0.0))
                comp = float(_seg_attr(s, "compression_ratio", 0.0))
                seg_text = str(_seg_attr(s, "text", "") or "").strip()
                if no_speech > 0.6 and avg_lp < -1.0:
                    log(
                        f"drop seg (silence-like): {seg_text!r} "
                        f"no_speech={no_speech:.2f} logprob={avg_lp:.2f}"
                    )
                    continue
                if comp > 2.4:
                    log(f"drop seg (compression): {seg_text!r} cr={comp:.2f}")
                    continue
                if seg_text and is_hallucination(seg_text):
                    log(f"drop seg (hallucination): {seg_text!r}")
                    continue
                kept.append(seg_text)
            text = " ".join(kept).strip()
        else:
            text = str(getattr(response, "text", "") or "").strip()
        if text and is_hallucination(text):
            log(f"skip transcribe: hallucination {text!r}")
            text = ""
        if not text:
            log("nothing pasted (all segments dropped)")
            _play_async(_ERR_WAV)
        if text:
            text = apply_replacements(text)
            if on_text is not None:
                try:
                    on_text(text)
                except Exception as e:
                    log(f"on_text callback failed: {e!r}")
            prev_clipboard = pyperclip.paste()
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            if prev_clipboard:
                time.sleep(0.2)
                pyperclip.copy(prev_clipboard)
    except Exception as e:
        log(f"transcription error: {e!r}")
        print(f"Transcription error: {e}", file=sys.stderr)
        _play_async(_ERR_WAV)


ACTIVE_COLOR = (40, 200, 80, 255)
PAUSED_COLOR = (140, 140, 140, 255)
CAPTURING_COLOR = (240, 180, 40, 255)
# While recording, the icon is a live level meter:
#   blue  = recording, only silence/background noise (nothing being lost)
#   green = voice above MIN_RMS is registering ("I can hear you")
#   red   = audible sound BELOW MIN_RMS — speech is being lost, speak up
RECORDING_COLOR = (0, 140, 255, 255)
VOICE_COLOR = (60, 220, 80, 255)
LOSING_COLOR = (220, 50, 50, 255)


ICON_SIZE = 256
INNER_SCALE = 0.62
SLASH_COLOR = (220, 50, 50, 255)


def _load_silhouette_mask():
    img = Image.open(ICON_PATH).convert("RGBA")
    alpha = img.split()[-1]
    if alpha.getextrema() == (255, 255):
        gray = img.convert("L")
        alpha = gray.point(lambda p: 255 - p)
    target = int(ICON_SIZE * INNER_SCALE)
    mask = alpha.resize((target, target), Image.LANCZOS)
    canvas = Image.new("L", (ICON_SIZE, ICON_SIZE), 0)
    offset = (ICON_SIZE - target) // 2
    canvas.paste(mask, (offset, offset))
    return canvas


_SILHOUETTE_MASK = _load_silhouette_mask()


def _make_icon(bg_color, slashed):
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, ICON_SIZE - 1, ICON_SIZE - 1), fill=bg_color)
    white = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (255, 255, 255, 255))
    img.paste(white, (0, 0), _SILHOUETTE_MASK)
    if slashed:
        slash_stroke = max(3, int(ICON_SIZE * 0.10))
        margin = ICON_SIZE * 0.18
        d.line(
            (margin, ICON_SIZE - margin, ICON_SIZE - margin, margin),
            fill=SLASH_COLOR,
            width=slash_stroke,
        )
    return img


_ACTIVE_ICON = _make_icon(ACTIVE_COLOR, slashed=False)
_PAUSED_ICON = _make_icon(PAUSED_COLOR, slashed=True)
_CAPTURING_ICON = _make_icon(CAPTURING_COLOR, slashed=False)
_RECORDING_ICON = _make_icon(RECORDING_COLOR, slashed=False)
_VOICE_ICON = _make_icon(VOICE_COLOR, slashed=False)
_LOSING_ICON = _make_icon(LOSING_COLOR, slashed=False)
_LEVEL_ICONS = {0: _RECORDING_ICON, 1: _LOSING_ICON, 2: _VOICE_ICON}


def make_icon(active):
    return _ACTIVE_ICON if active else _PAUSED_ICON


GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

if sys.platform == "win32":
    from pystray._util import win32 as _pswin32

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]


class WhispererIcon(pystray.Icon):
    """Tray icon with swapped mouse buttons on Windows.

    pystray's default is left-click = default menu item, right-click = menu.
    We swap them: left-click opens the menu, right-click runs the default
    action (toggle Whisperer).
    """

    def _on_notify(self, wparam, lparam):
        if sys.platform != "win32":
            return super()._on_notify(wparam, lparam)
        if lparam == _pswin32.WM_RBUTTONUP:
            self()
        elif self._menu_handle and lparam == _pswin32.WM_LBUTTONUP:
            # TrackPopupMenuEx misbehaves unless our systray window is foreground
            _pswin32.SetForegroundWindow(self._hwnd)
            point = wintypes.POINT()
            _pswin32.GetCursorPos(ctypes.byref(point))
            hmenu, descriptors = self._menu_handle
            index = _pswin32.TrackPopupMenuEx(
                hmenu,
                _pswin32.TPM_RIGHTALIGN
                | _pswin32.TPM_BOTTOMALIGN
                | _pswin32.TPM_RETURNCMD,
                point.x,
                point.y,
                self._menu_hwnd,
                None,
            )
            if index > 0:
                descriptors[index - 1](self)


def _hide_from_taskbar(icon):
    if sys.platform == "win32":
        hwnd = None
        for _ in range(50):
            hwnd = getattr(icon, "_hwnd", None)
            if hwnd:
                break
            time.sleep(0.1)
        if hwnd:
            ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            _user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            )
    icon.visible = True


class TrayApp:
    def __init__(self):
        self.enabled = True
        self.trigger_key = load_trigger_key()
        self.language = LANGUAGES[0] if LANGUAGES else ""
        self._languages = self._build_language_list()
        self.turbo = True
        self._capturing = False
        self._hook_installed = False
        self._key_down = False
        self.last_text = ""
        self._devices_cache = ("?", "?")
        self._devices_at = 0.0
        self.recorder = Recorder()
        self.recorder.on_level = self._on_level
        self._tk = TkBackend()
        self.icon = WhispererIcon(
            "groq_whisperer",
            make_icon(self.enabled),
            self._tooltip(),
            menu=pystray.Menu(*self._build_menu_items()),
        )

    def _build_language_list(self):
        if len(LANGUAGES) <= 1:
            return LANGUAGES
        return [""] + LANGUAGES

    def _language_display(self, lang):
        return "Auto (detect)" if lang == "" else lang.upper()

    def _devices(self):
        # Enumerating audio devices takes ~100ms; cache briefly so opening
        # the menu (which evaluates both labels) scans only once.
        now = time.monotonic()
        if now - self._devices_at > 2.0:
            self._devices_cache = current_devices()
            self._devices_at = now
        return self._devices_cache

    def _mic_label(self, _item):
        return f"Mic: {self._devices()[0]}"

    def _out_label(self, _item):
        return f"Out: {self._devices()[1]}"

    def _build_menu_items(self):
        items = [
            pystray.MenuItem(self._mic_label, None, enabled=False),
            pystray.MenuItem(self._out_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._toggle_label, self.on_toggle, default=True),
            pystray.MenuItem("Restart Whisperer", self.on_restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._choose_key_label, self.on_choose_key),
        ]
        if len(self._languages) > 1:
            items.append(
                pystray.MenuItem(
                    self._language_label,
                    pystray.Menu(*self._language_submenu_items()),
                )
            )
        items.append(
            pystray.MenuItem(
                "Turbo model (faster, cheaper)",
                self.on_toggle_turbo,
                checked=lambda _item: self.turbo,
            )
        )
        items.extend(
            [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    self._copy_last_label,
                    self.on_copy_last,
                    enabled=lambda _item: bool(self.last_text),
                ),
                pystray.MenuItem("Groq usage dashboard", self.on_open_usage),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", self.on_quit),
            ]
        )
        return items

    def _copy_last_label(self, _item):
        if not self.last_text:
            return "Copy last transcription"
        preview = self.last_text[:40].replace("\n", " ")
        if len(self.last_text) > 40:
            preview += "..."
        return f'Copy last: "{preview}"'

    def on_copy_last(self, _icon, _item):
        if not self.last_text:
            return
        try:
            pyperclip.copy(self.last_text)
            log(f"copied last transcription ({len(self.last_text)} chars)")
        except Exception as e:
            log(f"copy last failed: {e!r}")

    def _set_last_text(self, text):
        self.last_text = text
        try:
            self.icon.update_menu()
        except Exception as e:
            log(f"update_menu after transcript failed: {e!r}")

    def on_open_usage(self, _icon, _item):
        try:
            webbrowser.open(GROQ_USAGE_URL, new=2)
        except Exception as e:
            log(f"open usage failed: {e!r}")

    def on_toggle_turbo(self, icon, _item):
        self.turbo = not self.turbo
        icon.update_menu()

    def _language_submenu_items(self):
        return [
            pystray.MenuItem(
                self._language_display(lang),
                self._make_language_handler(lang),
                checked=lambda item, l=lang: self.language == l,
                radio=True,
            )
            for lang in self._languages
        ]

    def _make_language_handler(self, lang):
        def handler(icon, _item):
            self.language = lang
            icon.title = self._tooltip()
            icon.update_menu()

        return handler

    def _tooltip(self):
        state = "active" if self.enabled else "paused"
        base = f"Groq Whisperer ({state}) — hold {self.trigger_key}"
        if len(self._languages) > 1:
            base += f" — lang: {self._language_display(self.language)}"
        return base

    def _toggle_label(self, _item):
        return "Stop Whisperer" if self.enabled else "Activate Whisperer"

    def _choose_key_label(self, _item):
        return f"Choose key ({self.trigger_key})"

    def _language_label(self, _item):
        return f"Target language: {self._language_display(self.language)}"

    def _on_keyboard_event(self, event):
        if self._capturing:
            return
        if event.name != self.trigger_key:
            return
        if event.event_type == "down":
            if self._key_down:
                return
            self._key_down = True
            self.on_press(event)
        elif event.event_type == "up":
            if not self._key_down:
                return
            self._key_down = False
            self.on_release(event)

    def on_press(self, _event):
        if not self.enabled:
            return
        # Icon first, then start: recording callbacks repaint the icon with
        # the live voice level, and must not be overwritten afterwards.
        try:
            self.icon.icon = _RECORDING_ICON
        except Exception:
            pass
        self.recorder.start()

    def _on_level(self, level):
        log(f"level -> {('silence', 'losing', 'voice')[level]}")
        try:
            self.icon.icon = _LEVEL_ICONS[level]
        except Exception as e:
            log(f"level icon update failed: {e!r}")

    def on_release(self, _event):
        if not self.enabled:
            return
        # Stop first, icon after: a late audio callback during shutdown
        # could otherwise repaint the icon green and leave it stuck.
        frames = self.recorder.stop()
        try:
            self.icon.icon = make_icon(self.enabled)
        except Exception:
            pass
        if frames:
            model = MODEL_TURBO if self.turbo else MODEL_NORMAL
            threading.Thread(
                target=transcribe_and_paste,
                args=(frames, self.language, model, self._set_last_text),
                daemon=True,
            ).start()

    def on_toggle(self, icon, _item):
        self.enabled = not self.enabled
        if self.enabled:
            new_prompt = reload_prompt()
            log(
                f"reloaded on start: prompt {len(new_prompt)} chars, "
                f"{len(REPLACEMENTS)} replacement rule(s)"
            )
        else:
            self.recorder.stop()
        icon.icon = make_icon(self.enabled)
        icon.title = self._tooltip()
        icon.update_menu()

    def on_choose_key(self, _icon, _item):
        if self._capturing:
            return
        threading.Thread(target=self._capture_key, daemon=True).start()

    def _capture_key(self):
        self._capturing = True
        candidate = None
        old_key = self.trigger_key
        handle = None
        try:
            self.recorder.stop()
            log(f"capture: started (current={old_key!r})")
            try:
                self.icon.icon = _CAPTURING_ICON
            except Exception as e:
                log(f"capture: icon swap failed: {e!r}")
            handle = self._tk.show_capture_window(old_key)
            log("capture: dialog shown, waiting for keys...")
            while True:
                event = keyboard.read_event(suppress=False)
                if event.event_type != "down" or not event.name:
                    continue
                log(f"capture: got key {event.name!r}")
                if event.name in ("esc", "escape"):
                    break
                candidate = event.name
                if handle is not None:
                    handle.set_key(candidate)
        except Exception as e:
            log(f"capture: error in capture phase: {e!r}")
        finally:
            if handle is not None:
                handle.close()
            if candidate and candidate != old_key:
                self.trigger_key = candidate
                try:
                    save_trigger_key(candidate)
                except Exception as e:
                    log(f"capture: save failed: {e!r}")
                log(f"capture: switched to {candidate!r}")
            try:
                self.icon.icon = make_icon(self.enabled)
                self.icon.title = self._tooltip()
            except Exception as e:
                log(f"capture: icon restore failed: {e!r}")
            try:
                self.icon.update_menu()
            except Exception as e:
                log(f"capture: update_menu failed: {e!r}")
            self._capturing = False
            log(f"capture: end (active={self.trigger_key!r})")

    def on_restart(self, icon, _item):
        """Full process restart, so every .env setting is re-read."""
        log("restart requested from tray menu")
        self.recorder.stop()
        try:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve())],
                cwd=str(Path(__file__).parent),
                close_fds=True,
            )
        except Exception as e:
            log(f"restart failed to spawn: {e!r}")
            return
        icon.stop()

    def on_quit(self, icon, _item):
        self.recorder.stop()
        icon.stop()

    def install_hooks(self):
        if self._hook_installed:
            return
        keyboard.hook(self._on_keyboard_event)
        self._hook_installed = True

    def run(self):
        self.install_hooks()
        self.icon.run(setup=_hide_from_taskbar)


def main():
    if not os.environ.get("GROQ_API_KEY"):
        print("ERROR: GROQ_API_KEY is not set (check .env).", file=sys.stderr)
        sys.exit(1)
    app = TrayApp()
    try:
        app.run()
    except KeyboardInterrupt:
        app.recorder.stop()
        app.icon.stop()


if __name__ == "__main__":
    main()
