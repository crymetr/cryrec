"""Notice when a meeting app starts or stops using the microphone (Windows only, no admin, no hooks).

Windows records microphone use per app under
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone.
While an app holds the mic, its LastUsedTimeStop is 0. Teams, Zoom and Webex open the mic for the
whole call, so "mic in use" is a good sign that a call is running.
"""

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


def active_calls():
    """Names of meeting apps that hold the microphone right now, e.g. {"Teams"}."""
    found = set()
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
                    app = DESKTOP.get(exe)
                    if app:
                        with winreg.OpenKey(np, name) as k:
                            if _in_use(k):
                                found.add(app)
        except OSError:
            pass
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
