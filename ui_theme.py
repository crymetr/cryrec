"""Look and feel for CryRec: Windows 11 style (Sun Valley ttk theme), light/dark that follows Windows,
dark title bars, DPI awareness, and a few custom widgets (record button, level meter, tooltip)."""

import ctypes
import tkinter as tk
import winreg
from ctypes import wintypes
from tkinter import font as tkfont
from tkinter import ttk

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetParent.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]

PALETTE = {
    "light": {"bg": "#fafafa", "surface": "#ffffff", "text": "#1a1a1a", "muted": "#5f5f5f", "border": "#e0e0e0",
              "accent": "#005fb8", "record": "#c42b1c", "record_hover": "#a52417", "stop": "#3b3b3b",
              "stop_hover": "#1f1f1f", "ok": "#0f7b0f", "warn": "#9d5d00", "error": "#c42b1c",
              "meter_off": "#e6e6e6", "meter_low": "#2e9b4a", "meter_mid": "#d1a000", "meter_high": "#d13438",
              "overlay": "#202020", "overlay_text": "#ffffff", "field": "#ffffff"},
    "dark": {"bg": "#1c1c1c", "surface": "#2b2b2b", "text": "#ffffff", "muted": "#a6a6a6", "border": "#3a3a3a",
             "accent": "#4cc2ff", "record": "#e0474c", "record_hover": "#f05a5f", "stop": "#4a4a4a",
             "stop_hover": "#5c5c5c", "ok": "#6ccb5f", "warn": "#fce100", "error": "#ff99a4",
             "meter_off": "#333333", "meter_low": "#4cc95f", "meter_mid": "#f0c419", "meter_high": "#ff6b6b",
             "overlay": "#2b2b2b", "overlay_text": "#ffffff", "field": "#2b2b2b"},
}
THEME_MODES = {"system": "Follow Windows", "light": "Light", "dark": "Dark"}


def enable_dpi_awareness():
    """System DPI aware: crisp text at 125-200 %. Call before creating the Tk root."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def windows_is_dark():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def hwnd_of(win):
    win.update_idletasks()
    return user32.GetParent(win.winfo_id()) or win.winfo_id()


def set_titlebar_dark(win, dark):
    """Dark or light caption bar on Windows 10 2004+ / 11 (DWMWA_USE_IMMERSIVE_DARK_MODE = 20)."""
    try:
        val = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd_of(win)), 20, ctypes.byref(val),
                                                   ctypes.sizeof(val))
    except Exception:
        pass


def work_area():
    """(left, top, right, bottom) of the primary screen without the taskbar."""
    r = wintypes.RECT()
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
        return r.left, r.top, r.right, r.bottom
    return None


def first_font(*names):
    have = set(tkfont.families())
    return next((n for n in names if n in have), names[-1])


class Theme:
    """One per app. Knows light/dark, keeps title bars and custom widgets in step, follows Windows live."""

    def __init__(self, root, mode="system"):
        self.root = root
        self.mode = mode if mode in THEME_MODES else "system"
        self.dark = None
        self.listeners = []
        self.windows = []
        self.scale = root.winfo_fpixels("1i") / 96.0
        text = first_font("Segoe UI Variable Text", "Segoe UI")
        display = first_font("Segoe UI Variable Display", "Segoe UI")
        self.icon_family = first_font("Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI Symbol")
        self.fonts = {
            "body": tkfont.Font(root=root, family=text, size=10),
            "small": tkfont.Font(root=root, family=text, size=9),
            "strong": tkfont.Font(root=root, family=text, size=10, weight="bold"),
            "title": tkfont.Font(root=root, family=display, size=14, weight="bold"),
            "section": tkfont.Font(root=root, family=display, size=11, weight="bold"),
            "timer": tkfont.Font(root=root, family=display, size=30, weight="bold"),
            "button": tkfont.Font(root=root, family=text, size=11, weight="bold"),
            "icon": tkfont.Font(root=root, family=self.icon_family, size=11),
        }
        self.apply()
        self._watch()

    # sizes in logical pixels -> device pixels (the app is DPI aware)
    def px(self, n):
        return int(round(n * self.scale))

    def c(self, key):
        return PALETTE["dark" if self.dark else "light"][key]

    def set_mode(self, mode):
        self.mode = mode if mode in THEME_MODES else "system"
        self.apply(force=True)

    def apply(self, force=False):
        dark = windows_is_dark() if self.mode == "system" else self.mode == "dark"
        if dark == self.dark and not force:
            return
        self.dark = dark
        try:
            import sv_ttk
            sv_ttk.set_theme("dark" if dark else "light", root=self.root)
        except Exception:
            pass
        self._fix_fonts()
        st = ttk.Style(self.root)
        st.configure("Muted.TLabel", foreground=self.c("muted"), font=self.fonts["small"])
        st.configure("Strong.TLabel", font=self.fonts["strong"])
        st.configure("Title.TLabel", font=self.fonts["title"])
        st.configure("Section.TLabel", font=self.fonts["section"])
        st.configure("Timer.TLabel", font=self.fonts["timer"])
        st.configure("Error.TLabel", foreground=self.c("error"), font=self.fonts["small"])
        st.configure("Ok.TLabel", foreground=self.c("ok"), font=self.fonts["small"])
        st.configure("Icon.TButton", font=self.fonts["icon"], padding=self.px(4))
        st.configure("Link.TButton", font=self.fonts["small"], padding=(self.px(6), self.px(2)))
        for w in list(self.windows):
            try:
                set_titlebar_dark(w, dark)
            except tk.TclError:
                self.windows.remove(w)
        for fn in list(self.listeners):
            try:
                fn()
            except tk.TclError:
                self.listeners.remove(fn)

    def _fix_fonts(self):
        """Sun Valley sizes its fonts in pixels, which shrink to ~7 pt in a DPI-aware app at 150 %.
        Give every named font a point size so text is the same size everywhere and at any scaling."""
        text = self.fonts["body"].actual("family")
        sizes = {"SunValleyCaptionFont": (9, "normal"), "SunValleyBodyFont": (10, "normal"),
                 "SunValleyBodyStrongFont": (10, "bold"), "SunValleyBodyLargeFont": (11, "normal"),
                 "SunValleySubtitleFont": (13, "bold"), "SunValleyTitleFont": (16, "bold"),
                 "SunValleyTitleLargeFont": (22, "bold"), "SunValleyDisplayFont": (30, "bold")}
        for name in tkfont.names(self.root):
            try:
                f = tkfont.nametofont(name, root=self.root)
            except tk.TclError:
                continue
            if name in sizes:
                f.configure(size=sizes[name][0], weight=sizes[name][1])
            elif name.startswith("Tk") and name != "TkFixedFont":
                f.configure(family=text, size=10 if name not in ("TkTooltipFont", "TkSmallCaptionFont") else 9)

    def register(self, win):
        """Theme a Toplevel's caption bar now and on every switch."""
        self.windows.append(win)
        set_titlebar_dark(win, self.dark)

    def on_change(self, fn):
        self.listeners.append(fn)

    def bg(self):
        return ttk.Style(self.root).lookup("TFrame", "background") or self.c("bg")

    def _watch(self):
        if self.mode == "system":
            self.apply()
        self.root.after(2000, self._watch)


class PillButton(tk.Canvas):
    """Big rounded record/stop button drawn on a canvas (ttk can't recolour its themed buttons)."""

    def __init__(self, parent, theme, text, command, width=220, height=50, kind="record"):
        self.theme, self.command, self.kind, self.text = theme, command, kind, text
        self.w, self.h = theme.px(width), theme.px(height)
        super().__init__(parent, width=self.w, height=self.h, highlightthickness=0, bd=0, cursor="hand2")
        self.hover = False
        self.enabled = True
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        theme.on_change(self.redraw)
        self.redraw()

    def configure_button(self, text=None, kind=None, enabled=None):
        if text is not None:
            self.text = text
        if kind is not None:
            self.kind = kind
        if enabled is not None:
            self.enabled = enabled
            self.config(cursor="hand2" if enabled else "arrow")
        self.redraw()

    def _set_hover(self, on):
        self.hover = on
        self.redraw()

    def _click(self, e):
        if self.enabled and 0 <= e.x <= self.w and 0 <= e.y <= self.h:
            self.command()

    def redraw(self):
        t = self.theme
        self.delete("all")
        self.config(bg=t.bg())
        base = "record" if self.kind == "record" else "stop"
        fill = t.c(base + "_hover") if self.hover and self.enabled else t.c(base)
        if not self.enabled:
            fill = t.c("meter_off")
        r = self.h // 2
        self.create_oval(0, 0, self.h, self.h, fill=fill, outline=fill)
        self.create_oval(self.w - self.h, 0, self.w, self.h, fill=fill, outline=fill)
        self.create_rectangle(r, 0, self.w - r, self.h, fill=fill, outline=fill)
        self.create_text(self.w // 2, self.h // 2, text=self.text, fill="#ffffff" if self.enabled else t.c("muted"),
                         font=t.fonts["button"])


class LevelMeter(tk.Canvas):
    """Segmented level bar: green, then amber, then red near clipping."""

    SEGMENTS = 24

    def __init__(self, parent, theme, width=300, height=8):
        self.theme = theme
        self.h = theme.px(height)
        super().__init__(parent, width=theme.px(width), height=self.h, highlightthickness=0, bd=0)
        self.level = 0.0
        self.bind("<Configure>", lambda e: self.redraw())
        theme.on_change(self.redraw)

    def set(self, level):
        level = max(0.0, min(1.0, level))
        if abs(level - self.level) > 0.01:
            self.level = level
            self.redraw()

    def redraw(self):
        t = self.theme
        self.delete("all")
        self.config(bg=t.bg())
        w = max(1, self.winfo_width())
        gap = max(1, t.px(2))
        seg = (w - gap * (self.SEGMENTS - 1)) / self.SEGMENTS
        lit = round(self.level * self.SEGMENTS)
        for i in range(self.SEGMENTS):
            x = i * (seg + gap)
            if i < lit:
                color = t.c("meter_low") if i < self.SEGMENTS * 0.7 else (
                    t.c("meter_mid") if i < self.SEGMENTS * 0.9 else t.c("meter_high"))
            else:
                color = t.c("meter_off")
            self.create_rectangle(x, 0, x + seg, self.h, fill=color, outline=color)


class Tooltip:
    def __init__(self, widget, theme, text_fn):
        self.widget, self.theme, self.text_fn = widget, theme, text_fn
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, e):
        text = self.text_fn() if callable(self.text_fn) else self.text_fn
        if not text:
            return
        t = self.theme
        self.tip = tk.Toplevel(self.widget)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=text, bg=t.c("surface"), fg=t.c("text"), font=t.fonts["small"], justify="left",
                 wraplength=t.px(360), padx=t.px(8), pady=t.px(5), relief="solid", bd=1).pack()
        self.tip.geometry(f"+{e.x_root + t.px(12)}+{e.y_root + t.px(16)}")

    def _hide(self, e):
        if self.tip:
            self.tip.destroy()
            self.tip = None
