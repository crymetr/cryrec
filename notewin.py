"""Voice notes on the desktop: the clean note (not the audio), its to-do items with where each goes, things added
later (photos, links, text), and Send: one share to the server, where the PC's watcher files each item.

A voice note is a recording folder with note.json: {title, text, items: [{text, target, sent}]}; extras.json and
attach/ hold what was added. Same layout as the phone."""
import json
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import summarize as sm
from viewer import _TextBox

TARGETS = {"tracker": "METS Tracker", "crytodo": "CryTodo", "claude": "Claude Code"}


def is_note(d):
    return (Path(d) / "note.json").exists()


def load(d):
    try:
        return json.loads((Path(d) / "note.json").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def save(d, note):
    tmp = Path(d) / "note.tmp"
    tmp.write_text(json.dumps(note, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(Path(d) / "note.json")


def extras(d):
    try:
        return json.loads((Path(d) / "extras.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_extras(d, items):
    (Path(d) / "extras.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def written(d):
    """True once the server or the AI has turned the transcript into the note."""
    return "text" in load(d)


def open_count(d):
    return sum(1 for i in load(d).get("items") or [] if not i.get("sent"))


def run_note(transcript_path, ask, glossary, d):
    """Worker thread: note.json from transcript.txt with ask(system, user) (the notes engine from Settings)."""
    note = load(d)
    note.update(sm.voice_note(Path(transcript_path).read_text(encoding="utf-8"), ask, glossary))
    save(d, note)
    return note


def share_payload(d, note, indexes, claude_mode):
    """(share.json extra keys, photo paths) for the items at indexes, like the phone builds it."""
    ex = extras(d)
    photos = [Path(d) / "attach" / e["value"] for e in ex if e.get("kind") == "photo"]
    photos = [p for p in photos if p.exists()]
    items = note.get("items") or []
    vn = {"title": note.get("title") or Path(d).name, "text": note.get("text", ""), "recorded": Path(d).name,
          "claude_mode": claude_mode, "items": [{"text": items[i]["text"], "target": items[i].get("target", "crytodo")}
                                                for i in indexes],
          "extras": [e for e in ex if e.get("kind") != "photo"], "photos": [p.name for p in photos]}
    return {"kind": "voice-note", "voice_note": vn}, photos


class NoteWindow:
    def __init__(self, app, d):
        self.app, self.d = app, Path(d)
        theme, px = app.theme, app.theme.px
        w = self.win = tk.Toplevel(app.root)
        theme.register(w)
        w.columnconfigure(0, weight=1)
        w.rowconfigure(1, weight=1)
        self.title = tk.StringVar()
        ttk.Label(w, textvariable=self.title, style="Title.TLabel").grid(row=0, column=0, sticky="w", padx=px(12),
                                                                          pady=(px(10), 0))
        f = ttk.Frame(w, padding=(px(12), px(6), px(12), px(12)))
        f.grid(row=1, column=0, sticky="nsew")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        self.box = _TextBox(f, theme, row=0, height=10, width=70)
        ttk.Label(f, text="To do", style="Section.TLabel").grid(row=1, column=0, sticky="w", pady=(px(10), px(2)))
        self.items = ttk.Frame(f)
        self.items.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.items.columnconfigure(0, weight=1)
        ttk.Label(f, text="Added", style="Section.TLabel").grid(row=3, column=0, sticky="w", pady=(px(10), px(2)))
        self.added = ttk.Frame(f)
        self.added.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.added.columnconfigure(0, weight=1)
        bar = ttk.Frame(f)
        bar.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(px(12), 0))
        bar.columnconfigure(0, weight=1)
        self.status = tk.StringVar()
        ttk.Label(bar, textvariable=self.status, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        for col, (label, fn) in enumerate((("Photo...", self.add_photo), ("Link...", self.add_link),
                                           ("Text...", self.add_text), ("+ Task", lambda: self.edit_item(-1))), 1):
            ttk.Button(bar, text=label, command=fn).grid(row=0, column=col, padx=(px(4), 0))
        self.send_btn = ttk.Button(bar, text="Send", style="Accent.TButton", command=self.send)
        self.send_btn.grid(row=0, column=5, padx=(px(8), 0))
        w.geometry(f"{px(640)}x{px(620)}")
        self.render()

    def render(self):
        if not self.win.winfo_exists():
            return
        px = self.app.theme.px
        note = load(self.d)
        title = note.get("title") or "Voice note"
        self.title.set(title)
        self.win.title(f"{title} · CryRec")
        t = self.box.text
        t.config(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", note.get("text") or "The note is being written. It shows here when it is ready.")
        t.config(state="disabled")
        for w in self.items.winfo_children() + self.added.winfo_children():
            w.destroy()
        items = note.get("items") or []
        if not items:
            ttk.Label(self.items, text="No tasks. Add one with + Task.", style="Muted.TLabel").grid(sticky="w")
        for i, it in enumerate(items):
            sent = bool(it.get("sent"))
            lbl = ttk.Label(self.items, text=("✓ " if sent else "") + it.get("text", ""), wraplength=px(430),
                            style="Muted.TLabel" if sent else "TLabel")
            lbl.grid(row=i, column=0, sticky="w", pady=px(2))
            if not sent:
                lbl.bind("<Button-1>", lambda e, i=i: self.edit_item(i))
                lbl.config(cursor="hand2")
            var = tk.StringVar(value=TARGETS.get(it.get("target"), "CryTodo"))
            cb = ttk.Combobox(self.items, textvariable=var, values=list(TARGETS.values()), state="disabled" if sent
                              else "readonly", width=14)
            cb.grid(row=i, column=1, padx=(px(8), 0))
            cb.bind("<<ComboboxSelected>>", lambda e, i=i, v=var: self.set_target(i, v.get()))
        ex = extras(self.d)
        if not ex:
            ttk.Label(self.added, text="Nothing yet: photos, links and text you add go along when you send.",
                      style="Muted.TLabel").grid(sticky="w")
        for i, e in enumerate(ex):
            icon = {"photo": "📷", "link": "🔗"}.get(e.get("kind"), "✏️")
            ttk.Label(self.added, text=f"{icon}  {e.get('value', '')}", wraplength=px(480)).grid(row=i, column=0,
                                                                                                sticky="w")
            ttk.Button(self.added, text="Remove", style="Link.TButton",
                       command=lambda i=i: self.remove_extra(i)).grid(row=i, column=1)
        n = sum(1 for it in items if not it.get("sent"))
        self.send_btn.config(text=f"Send {n}" if n else "Send", state="normal" if n and written(self.d) else "disabled")

    def set_target(self, i, label):
        note = load(self.d)
        note["items"][i]["target"] = next(k for k, v in TARGETS.items() if v == label)
        save(self.d, note)

    def edit_item(self, i):
        note = load(self.d)
        items = note.setdefault("items", [])
        old = items[i]["text"] if i >= 0 else ""
        text = simpledialog.askstring("Task", "What needs doing? (empty = delete)" if i >= 0 else "What needs doing?",
                                      initialvalue=old, parent=self.win)
        if text is None:
            return
        text = text.strip()
        if i < 0 and text:
            items.append({"text": text, "target": "crytodo"})
        elif i >= 0 and text:
            items[i]["text"] = text
        elif i >= 0:
            items.pop(i)
        save(self.d, note)
        self.render()

    def _add(self, kind, value):
        ex = extras(self.d)
        ex.append({"kind": kind, "value": value})
        save_extras(self.d, ex)
        self.render()

    def add_photo(self):
        files = filedialog.askopenfilenames(parent=self.win, title="Add photos",
                                            filetypes=[("Images", "*.jpg *.jpeg *.png *.webp"), ("All files", "*.*")])
        dest = self.d / "attach"
        for f in files:
            dest.mkdir(exist_ok=True)
            n = len(list(dest.iterdir())) + 1
            target = dest / f"photo_{n:02d}{Path(f).suffix.lower()}"
            while target.exists():
                n += 1
                target = dest / f"photo_{n:02d}{Path(f).suffix.lower()}"
            shutil.copy2(f, target)
            self._add("photo", target.name)

    def add_link(self):
        v = simpledialog.askstring("Add a link", "Link:", parent=self.win)
        if v and v.strip():
            self._add("link", v.strip())

    def add_text(self):
        v = simpledialog.askstring("Add text", "Anything to go with this note:", parent=self.win)
        if v and v.strip():
            self._add("text", v.strip())

    def remove_extra(self, i):
        ex = extras(self.d)
        e = ex.pop(i)
        if e.get("kind") == "photo":
            (self.d / "attach" / e["value"]).unlink(missing_ok=True)
        save_extras(self.d, ex)
        self.render()

    def send(self):
        pair = self.app.server_pair()
        if not pair:
            messagebox.showinfo("CryRec", "Sending needs your server: set its address and key in Settings > Transcript.",
                                parent=self.win)
            return
        note = load(self.d)
        indexes = [i for i, it in enumerate(note.get("items") or []) if not it.get("sent")]
        mode = "brief"
        if any(note["items"][i].get("target") == "claude" for i in indexes):
            ans = messagebox.askyesnocancel(
                "Claude Code tasks", "Start working on the Claude Code tasks right away?\n\n"
                "Yes: a session starts on them now.\nNo: write a brief and a CryTodo card, you start it later.",
                parent=self.win)
            if ans is None:
                return
            mode = "now" if ans else "brief"
        url, key = pair
        extra, photos = share_payload(self.d, note, indexes, mode)
        self.status.set("Sending...")
        self.send_btn.config(state="disabled")

        def work():
            try:
                sid = sm.remote_share(url, key, note.get("text", ""), "", extra["voice_note"]["title"], extra, photos)
                n = load(self.d)
                for i in indexes:
                    n["items"][i]["sent"] = sid
                save(self.d, n)
                msg = "Sent to your PC. Each task goes where you picked."
            except Exception as e:
                msg = f"Could not send: {e}"
            self.win.after(0, lambda: (self.status.set(msg), self.render()))
        threading.Thread(target=work, daemon=True).start()
