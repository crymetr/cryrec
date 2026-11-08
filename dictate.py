"""Dictation for CryRec: tap or hold a shortcut, speak, and the text lands where the cursor is.

No keyboard hook: a thread polls GetAsyncKeyState for the shortcut keys only, so security tools
don't see a keylogger-style hook. The text is pasted with the clipboard (Ctrl+V), which handles any
language, and the previous clipboard text is put back afterwards.
"""

import ctypes
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32")

VK_CONTROL, VK_SHIFT, VK_MENU, VK_ESCAPE, VK_V = 0x11, 0x10, 0x12, 0x1B, 0x56
VK_LWIN, VK_RWIN, VK_RCONTROL = 0x5B, 0x5C, 0xA3
MODIFIERS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, VK_LWIN, VK_RWIN}
MOUSE = {0x01, 0x02, 0x04, 0x05, 0x06}

# shortcut name -> groups of keys; every group needs one of its keys down
HOTKEYS = {
    "Ctrl+Win": [(VK_CONTROL,), (VK_LWIN, VK_RWIN)],
    "Ctrl+Alt": [(VK_CONTROL,), (VK_MENU,)],
    "Right Ctrl": [(VK_RCONTROL,)],
}
TAP_SEC = 0.35      # released faster than this = tap (start/stop mode); longer = hold to talk
MAX_SEC = 900       # a forgotten tap-mode dictation stops by itself
MIN_SEC = 0.4       # shorter clips are ignored (accidental presses)


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def combo_down(groups):
    return all(any(key_down(v) for v in g) for g in groups)


def keys_down(groups):
    """Non-shortcut keys held right now."""
    ours = {v for g in groups for v in g}
    return {vk for vk in range(0x08, 0xFF) if vk not in ours and vk not in MODIFIERS and vk not in MOUSE and key_down(vk)}


def other_key_down(groups, ignore=frozenset()):
    """True if a key outside the shortcut went down (e.g. Ctrl+Win+D for a new desktop): then it's not dictation.
    `ignore` holds keys that were already down when the shortcut started (stuck key states happen,
    e.g. after remote-desktop sessions)."""
    return bool(keys_down(groups) - set(ignore))


class HotkeyWatcher:
    """States: idle -> pressed -> (hold: stop on release) | (tap: record until pressed again)."""

    def __init__(self, hotkey, on_start, on_mode, on_stop, on_cancel):
        self.groups = HOTKEYS.get(hotkey, HOTKEYS["Ctrl+Win"])
        self.on_start, self.on_mode, self.on_stop, self.on_cancel = on_start, on_mode, on_stop, on_cancel
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _loop(self):
        # on_start opens the mic from this thread; WASAPI fails (-9999) on a thread without COM
        ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        state, t0, announced, stuck = "idle", 0.0, False, frozenset()
        while self.running:
            time.sleep(0.02)
            now = time.monotonic()
            down = combo_down(self.groups)
            try:
                if state == "idle":
                    if down:
                        state, t0, announced = "pressed", now, False
                        stuck = frozenset(keys_down(self.groups))
                        if self.on_start() is False:  # no mic: don't wait for a second press that stops nothing
                            state = "wait_release"
                elif state == "pressed":
                    if other_key_down(self.groups, stuck):
                        state = "wait_release"
                        self.on_cancel()
                    elif not down:
                        if now - t0 < TAP_SEC:
                            state = "tap"
                            self.on_mode("tap")
                        else:
                            state = "idle"
                            self.on_stop()
                    elif not announced and now - t0 >= TAP_SEC:
                        announced = True
                        self.on_mode("hold")
                elif state == "tap":
                    if down:
                        state = "wait_release"
                        self.on_stop()
                    elif key_down(VK_ESCAPE):
                        state = "idle"
                        self.on_cancel()
                    elif now - t0 > MAX_SEC:
                        state = "idle"
                        self.on_stop()
                elif state == "wait_release" and not down:
                    state = "idle"
            except Exception:
                state = "idle"


class Recorder:
    """Captures the mic into memory; stop() returns 16 kHz mono float32."""

    def __init__(self, pa, device):
        import pyaudiowpatch as pyaudio
        self.rate = int(device["defaultSampleRate"])
        self.channels = max(1, min(2, int(device["maxInputChannels"])))
        self.frames = []
        self._read = 0  # frames already handed out by drain()
        self._resampler = None
        self.stream = pa.open(format=pyaudio.paInt16, channels=self.channels, rate=self.rate, input=True,
                              input_device_index=device["index"], frames_per_buffer=int(self.rate * 0.05),
                              stream_callback=self._cb)

    def _cb(self, data, frame_count, time_info, status):
        import pyaudiowpatch as pyaudio
        self.frames.append(data)
        return (None, pyaudio.paContinue)

    def stop(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass

    def drain(self, final=False):
        """16 kHz mono float32 recorded since the last call."""
        n = len(self.frames)
        raw = b"".join(self.frames[self._read:n])
        self._read = n
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        x = x.reshape(-1, self.channels).mean(axis=1)
        if self.rate != 16000:
            import soxr
            if self._resampler is None:
                self._resampler = soxr.ResampleStream(self.rate, 16000, 1, dtype="float32")
            x = self._resampler.resample_chunk(x, last=final)
        return x.astype(np.float32)


class ChunkedDictation:
    """Writes a long dictation while it is spoken: about every 45 s the new audio is cut at a pause and
    sent to run(audio) -> text, so after the last word only the last part is left. Under 45 s it is one
    part, the same as a single request. One worker keeps the parts in order and the engine single-use."""

    SR = 16000
    TARGET_SEC, FORCE_SEC = 45, 90  # cut at the first good pause after 45 s, at the quietest moment by 90 s
    SEARCH_SEC, FRAME_SEC, QUIET_FRAMES = 15, 0.1, 5  # a pause: 0.5 s, within the last 15 s
    MIN_PEAK = 0.01  # quieter than this is not speech

    def __init__(self, rec, run):
        self.rec, self.run = rec, run
        self.pending = np.zeros(0, np.float32)
        self.seconds = 0.0
        self.peak = 0.0
        self.parts = []  # (audio, future)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self._stop = threading.Event()
        self._cancelled = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _take(self, final=False):
        x = self.rec.drain(final)
        self.seconds += len(x) / self.SR
        if len(x):
            self.peak = max(self.peak, float(np.abs(x).max()))
        self.pending = np.concatenate([self.pending, x])

    def _loop(self):
        while not self._stop.wait(2):
            self._take()
            n = self._cut_point()
            if n:
                self._submit(self.pending[:n])
                self.pending = self.pending[n:]

    def _cut_point(self):
        """Sample index of a pause in the last SEARCH_SEC, or None to keep listening."""
        if len(self.pending) < self.TARGET_SEC * self.SR:
            return None
        lo = len(self.pending) - self.SEARCH_SEC * self.SR
        size = int(self.FRAME_SEC * self.SR)
        frames = self.pending[lo:]
        frames = frames[:len(frames) // size * size].reshape(-1, size)
        level = np.abs(frames).mean(axis=1)
        rolling = np.convolve(level, np.ones(self.QUIET_FRAMES) / self.QUIET_FRAMES, mode="valid")
        i = int(np.argmin(rolling))
        quiet = rolling[i] < max(0.002, 0.15 * float(np.median(level)))
        if not quiet and len(self.pending) < self.FORCE_SEC * self.SR:
            return None
        return lo + (i + self.QUIET_FRAMES // 2) * size

    def _submit(self, audio):
        if float(np.abs(audio).max(initial=0)) >= self.MIN_PEAK:
            self.parts.append((audio, self.pool.submit(self.run, audio)))

    def close(self):
        """Recording ended. Returns False if nothing worth writing was heard."""
        self._stop.set()
        self._thread.join()
        self._take(final=True)
        return self.seconds >= MIN_SEC and self.peak >= self.MIN_PEAK

    def finish(self):
        """Waits for every part. Returns (text, error); a part that fails twice is left out and reported."""
        self._submit(self.pending)
        texts, error = [], None
        for audio, future in self.parts:
            try:
                texts.append(future.result())
            except Exception:
                try:
                    texts.append(self.run(audio))
                except Exception as e:
                    error = e
        self.pool.shutdown(wait=False)
        return " ".join(t.strip() for t in texts if t and t.strip()), error

    def cancel(self):
        self._stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)


# ---------- clipboard and paste ----------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KEYEVENTF_KEYUP = 0x0002

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]


def _open_clipboard():
    for _ in range(20):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def get_clipboard_text():
    """Current clipboard text, or None if it holds something else (image, files) or is busy."""
    if not _open_clipboard():
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else None
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text):
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, size)
        kernel32.GlobalUnlock(h)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        user32.CloseClipboard()


def foreground_window():
    return user32.GetForegroundWindow()


def paste_text(text, target=None):
    """Type `text` into the window that had focus when dictation started."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(key_down(v) for v in (VK_CONTROL, VK_LWIN, VK_RWIN, VK_MENU, VK_SHIFT)):
        time.sleep(0.02)  # never send Ctrl+V while Win/Alt is still held (Win+V = clipboard history)
    if target:
        user32.SetForegroundWindow(target)
        time.sleep(0.05)
    old = get_clipboard_text()
    if not set_clipboard_text(text):
        return False
    time.sleep(0.05)
    for vk, flags in ((VK_CONTROL, 0), (VK_V, 0), (VK_V, KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP)):
        user32.keybd_event(vk, 0, flags, 0)
    time.sleep(0.4)  # let the app read the clipboard before we put the old text back
    if old is not None:
        set_clipboard_text(old)
    return True


# ---------- overlay helpers ----------

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x08000000, 0x00000080, 0x00000008
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


def make_no_activate(tk_window):
    """Let a Tk toplevel float on top without ever taking focus from the app you are typing in."""
    tk_window.update_idletasks()
    hwnd = user32.GetParent(tk_window.winfo_id()) or tk_window.winfo_id()
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
    return hwnd


def show_no_activate(hwnd, show=True):
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE if show else SW_HIDE)


HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]


def place_no_activate(hwnd, x, y, w, h):
    """Move, size, show and keep on top in one call, without ever activating the window."""
    user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
