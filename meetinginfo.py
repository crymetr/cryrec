"""Which meeting is this? Title and attendees for a recording, read only on this PC (Windows only).

1. Classic Outlook, if it is already running: the calendar item that is on now (or starts within 15 min).
   Read through Outlook's COM interface; Outlook is never started for this. New Outlook has no COM.
2. Teams, if it holds the microphone: the meeting name from its window title.
Nothing here leaves the PC; the title names the recording folder, the attendee names help the transcript
and the notes spell them right.
"""

import ctypes
import json
import re
import threading
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

import callwatch

MEETING_FILE = "meeting.json"
EARLY = timedelta(minutes=15)  # recording started a bit before the meeting
# Teams window titles that are app pages, not meetings ("Chat | Ayse | Microsoft Teams")
TEAMS_PAGES = {"chat", "activity", "calendar", "teams", "calls", "files", "apps", "onedrive", "copilot", "meet",
               "people", "communities", "help", "settings", "notifications", "search", "microsoft teams"}
OL_RESOURCE = 3  # Recipient.Type for a room or other resource


def _local_format(dt):
    """Outlook's Restrict wants dates the way Windows writes them for this user (e.g. 10.11.2026 14:00)."""
    k32 = ctypes.windll.kernel32
    st = wintypes.WORD * 8
    t = st(dt.year, dt.month, dt.isoweekday() % 7, dt.day, dt.hour, dt.minute, 0, 0)
    buf = ctypes.create_unicode_buffer(64)
    k32.GetDateFormatEx(None, 1, ctypes.byref(t), None, buf, 64, None)  # DATE_SHORTDATE
    date = buf.value
    k32.GetTimeFormatEx(None, 2, ctypes.byref(t), None, buf, 64)  # TIME_NOSECONDS
    return f"{date} {buf.value}"


def _person(name):
    """'Doe, John (EEB)' -> 'John Doe'; addresses and empty names -> None."""
    name = re.sub(r"\s*[\(\[].*?[\)\]]", "", str(name or "")).strip().strip("'\"")
    if not name or "@" in name or "/" in name:
        return None
    if name.count(",") == 1:
        last, first = (p.strip() for p in name.split(","))
        name = f"{first} {last}".strip()
    return name


def _naive(t):
    # pywin32 hands Outlook local times back tagged as UTC; the wall-clock value is what counts
    return datetime(t.year, t.month, t.day, t.hour, t.minute, t.second)


def outlook_meeting(now=None):
    """{'title', 'attendees', 'source': 'outlook'} for the calendar item on now, else None."""
    now = now or datetime.now()
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return None
    pythoncom.CoInitialize()
    try:
        try:
            ol = win32com.client.GetActiveObject("Outlook.Application")  # never start Outlook
        except Exception:
            return None
        items = ol.GetNamespace("MAPI").GetDefaultFolder(9).Items  # olFolderCalendar
        items.Sort("[Start]")
        items.IncludeRecurrences = True
        found = items.Restrict(f"[Start] <= '{_local_format(now + EARLY)}' AND [End] >= '{_local_format(now)}'")
        best = None
        item = found.GetFirst()
        while item is not None:
            try:
                start, end = _naive(item.Start), _naive(item.End)
                if not item.AllDayEvent and start - EARLY <= now <= end:
                    if best is None or abs(start - now) < abs(_naive(best.Start) - now):
                        best = item
            except Exception:
                pass
            item = found.GetNext()
        if best is None:
            return None
        people = []
        for p in [best.Organizer] + [r.Name for r in best.Recipients if r.Type != OL_RESOURCE]:
            p = _person(p)
            if p and p not in people:
                people.append(p)
        title = str(best.Subject or "").strip()
        return {"title": title, "attendees": people, "source": "outlook"} if title or people else None
    except Exception:
        return None
    finally:
        pythoncom.CoUninitialize()


def _window_titles(exe_names):
    """Titles of visible top-level windows whose process exe is one of exe_names (lower case)."""
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    titles = []

    def exe_of(hwnd):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(520)
            size = wintypes.DWORD(520)
            return buf.value.rsplit("\\", 1)[-1].lower() if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
        finally:
            k32.CloseHandle(h)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        n = u32.GetWindowTextLengthW(hwnd)
        if n and u32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            if exe_of(hwnd) in exe_names:
                titles.append(buf.value)
        return True

    u32.EnumWindows(each, 0)
    return titles


def teams_title(titles):
    """Meeting name from Teams window titles ('Weekly sync | Microsoft Teams'), skipping app pages."""
    for t in titles:
        parts = [p.strip() for p in t.split("|") if p.strip()]
        if len(parts) < 2 or parts[-1].lower() != "microsoft teams" or parts[0].lower() in TEAMS_PAGES:
            continue
        return parts[0]
    return None


def teams_meeting():
    if "Teams" not in callwatch.active_calls():
        return None  # Teams is open but not in a call: its window title is a chat, not a meeting
    title = teams_title(_window_titles({"ms-teams.exe", "teams.exe"}))
    return {"title": title, "attendees": [], "source": "teams"} if title else None


def current_meeting(timeout=3.0):
    """Outlook first, then Teams. Gives up after timeout seconds (a busy Outlook can block COM)."""
    out = {}

    def look():
        out["m"] = outlook_meeting() or teams_meeting()

    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(timeout)
    return out.get("m")


def folder_suffix(title, limit=60):
    """Title as a safe folder name part: no Windows-reserved characters, no trailing dots or spaces."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title or "")
    s = re.sub(r"\s+", " ", s).strip()[:limit].rstrip(" .")
    return s


def save(session_dir, meeting):
    (Path(session_dir) / MEETING_FILE).write_text(json.dumps(meeting, ensure_ascii=False, indent=2), encoding="utf-8")


def load(path):
    """meeting.json of a recording folder; also found from a live part folder (<session>/live/partNNN)."""
    p = Path(path)
    for d in (p, p.parent, p.parent.parent):
        f = d / MEETING_FILE
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None
