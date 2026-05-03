import ctypes
import sys

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
import struct
import threading
import time
import tkinter as tk
import unicodedata
import wave
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
    freq_hz, ms, sample_rate=22050, volume=6000, attack_ms=4, release_ms=15
):
    n = int(sample_rate * ms / 1000)
    attack = max(1, int(sample_rate * attack_ms / 1000))
    release = max(1, int(sample_rate * release_ms / 1000))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for i in range(n):
            if i < attack:
                env = i / attack
            elif i > n - release:
                env = max(0.0, (n - i) / release)
            else:
                env = 1.0
            v = int(volume * env * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            w.writeframesraw(struct.pack("<h", v))
    return buf.getvalue()


_TIC_WAV = _make_tone_wav(420, 90) if sys.platform == "win32" else b""
_TOC_WAV = _make_tone_wav(350, 130) if sys.platform == "win32" else b""


def _play_async(blob):
    if sys.platform != "win32" or not blob:
        return

    def _play():
        try:
            winsound.PlaySound(blob, winsound.SND_MEMORY)
        except Exception:
            pass

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

LANGUAGE = os.environ.get("WHISPERER_LANGUAGE", "es")
PROMPT = os.environ.get("WHISPERER_PROMPT", "")


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




class Recorder:
    _MAX_SAMPLES = int(MAX_RECORDING_SEC * SAMPLE_RATE)

    def __init__(self):
        self._audio = pyaudio.PyAudio()
        self._stream = None
        self._frames = []
        self._sample_count = 0
        self._lock = threading.Lock()

    def start(self):
        opened = False
        with self._lock:
            if self._stream is not None:
                return
            self._frames = []
            self._sample_count = 0
            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK,
                stream_callback=self._on_data,
            )
            self._stream.start_stream()
            opened = True
        if opened:
            _play_async(_TIC_WAV)

    def _on_data(self, in_data, frame_count, time_info, status):
        if self._sample_count < self._MAX_SAMPLES:
            self._frames.append(in_data)
            self._sample_count += frame_count
            if self._sample_count >= self._MAX_SAMPLES:
                log(f"recording cap hit: {MAX_RECORDING_SEC}s")
                _play_async(_TOC_WAV)
        return (in_data, pyaudio.paContinue)

    def stop(self):
        frames = None
        with self._lock:
            if self._stream is None:
                return None
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
            frames = self._frames
        _play_async(_TOC_WAV)
        return frames


def build_wav(raw):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(raw)
    return buf.getvalue()


def transcribe_and_paste(frames, language):
    if not frames:
        return
    raw = b"".join(frames)
    duration = len(raw) / (SAMPLE_RATE * 2 * CHANNELS)
    if duration < MIN_DURATION_SEC:
        log(f"skip transcribe: too short ({duration:.2f}s)")
        return
    rms = audioop.rms(raw, 2)
    if rms < MIN_RMS:
        log(f"skip transcribe: too quiet (rms={rms}, dur={duration:.2f}s)")
        return
    log(f"to API: dur={duration:.2f}s rms={rms} lang={language or 'auto'}")
    wav_bytes = build_wav(raw)
    try:
        api_kwargs = {
            "file": ("audio.wav", wav_bytes),
            "model": "whisper-large-v3",
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
        if text:
            prev_clipboard = pyperclip.paste()
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            if prev_clipboard:
                time.sleep(0.2)
                pyperclip.copy(prev_clipboard)
    except Exception as e:
        print(f"Transcription error: {e}", file=sys.stderr)


ACTIVE_COLOR = (40, 200, 80, 255)
PAUSED_COLOR = (140, 140, 140, 255)
CAPTURING_COLOR = (240, 180, 40, 255)
RECORDING_COLOR = (220, 50, 50, 255)


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


def make_icon(active):
    return _ACTIVE_ICON if active else _PAUSED_ICON


GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]


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
        self._capturing = False
        self._hook_installed = False
        self.recorder = Recorder()
        self._tk = TkBackend()
        self.icon = pystray.Icon(
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

    def _build_menu_items(self):
        items = [
            pystray.MenuItem(self._toggle_label, self.on_toggle, default=True),
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
        items.extend(
            [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", self.on_quit),
            ]
        )
        return items

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
            self.on_press(event)
        elif event.event_type == "up":
            self.on_release(event)

    def on_press(self, _event):
        if not self.enabled:
            return
        self.recorder.start()
        try:
            self.icon.icon = _RECORDING_ICON
        except Exception:
            pass

    def on_release(self, _event):
        if not self.enabled:
            return
        try:
            self.icon.icon = make_icon(self.enabled)
        except Exception:
            pass
        frames = self.recorder.stop()
        if frames:
            threading.Thread(
                target=transcribe_and_paste,
                args=(frames, self.language),
                daemon=True,
            ).start()

    def on_toggle(self, icon, _item):
        self.enabled = not self.enabled
        if not self.enabled:
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
