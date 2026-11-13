"""Notice when a meeting app starts or stops using the microphone (Windows only, no admin, no hooks).

Windows records microphone use per app under
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone.
While an app holds the mic, its LastUsedTimeStop is 0. Teams, Zoom and Webex open the mic for the
whole call, so "mic in use" is a good sign that a call is running.
"""

import re
import winreg

MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"

# packaged (Store) apps: key name prefix -> display name
PACKAGED = {"MSTeams_": "Teams", "Microsoft.Teams": "Teams", "Zoom": "Zoom", "Cisco.Webex": "Webex"}
# classic desktop apps: exe file name -> display name
DESKTOP = {"teams.exe": "Teams", "ms-teams.exe": "Teams", "zoom.exe": "Zoom", "cpthost.exe": "Zoom",
           "webexmta.exe": "Webex", "ciscocollabhost.exe": "Webex", "atmgr.exe": "Webex", "slack.exe": "Slack"}


def _in_use(key):
    try:
        stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
        start, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
    except OSError:
        return False
    return stop == 0 and start > 0


def _subkeys(key):
    i = 0
    while True:
        try:
            yield winreg.EnumKey(key, i)
        except OSError:
            return
        i += 1


BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe"}
# a browser holding the mic is a call only when one of its windows shows a meeting page
WEB_CALLS = [(re.compile(r"^Meet\b|Google Meet", re.I), "Google Meet"),
             (re.compile(r"\|\s*Microsoft Teams|Microsoft Teams\s*\(?(meeting|call)", re.I), "Teams (web)"),
             (re.compile(r"\bZoom\b", re.I), "Zoom (web)"),
             (re.compile(r"\bWebex\b", re.I), "Webex (web)"),
             (re.compile(r"\bJitsi\b|\bWhereby\b|Discord", re.I), "Web call")]


_web_app = None  # the web call seen while the browser has been holding the mic


def web_call(titles):
    """Meeting app name if one of these browser window titles is a meeting page, else None."""
    for t in titles:
        for pat, name in WEB_CALLS:
            if pat.search(t):
                return name
    return None


def active_calls():
    """Names of meeting apps that hold the microphone right now, e.g. {"Teams"}."""
    found = set()
    browser_mic = False
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, MIC_KEY)
    except OSError:
        return found
    with root:
        for name in _subkeys(root):
            if name == "NonPackaged":
                continue
            app = next((v for k, v in PACKAGED.items() if name.startswith(k)), None)
            if app:
                with winreg.OpenKey(root, name) as k:
                    if _in_use(k):
                        found.add(app)
        try:
            with winreg.OpenKey(root, "NonPackaged") as np:
                for name in _subkeys(np):
                    exe = name.replace("#", "\\").rsplit("\\", 1)[-1].lower()
                    if exe in BROWSERS:
                        with winreg.OpenKey(np, name) as k:
                            browser_mic = browser_mic or _in_use(k)
                        continue
                    app = DESKTOP.get(exe)
                    if app:
                        with winreg.OpenKey(np, name) as k:
                            if _in_use(k):
                                found.add(app)
        except OSError:
            pass
    global _web_app
    if browser_mic:
        import meetinginfo  # lazy: meetinginfo imports this module
        # window titles show only the active tab: once a meeting page was seen, the call lasts for as long as
        # the browser holds the mic (switching tabs to present a document must not look like the call ended)
        _web_app = web_call(meetinginfo._window_titles(BROWSERS)) or _web_app
        if _web_app:
            found.add(_web_app)
    else:
        _web_app = None
    return found


class CallWatcher:
    """Polls active_calls(); calls on_start(app) / on_end(app) when a call appears or goes away."""

    def __init__(self, on_start, on_end):
        self.on_start, self.on_end = on_start, on_end
        self.current = set()

    def poll(self):
        now = active_calls()
        for app in sorted(now - self.current):
            self.on_start(app)
        for app in sorted(self.current - now):
            self.on_end(app)
        self.current = now
        return now
