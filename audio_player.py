"""
Local Audio Player (v9)
-----------------------
Run from source:  python3 audio_player.py
Put ffmpeg and ffprobe in the same folder as this file (or have them on PATH).
Packaged builds are made automatically by GitHub Actions (see build.yml).
"""

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, simpledialog

os.environ.setdefault("PYGAME_HIDE_SUPPORT", "1")
import pygame

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    os.environ["PATH"] = sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")

if FROZEN:
    APP_DIR = Path.home() / "AudioPlayer_data"
else:
    APP_DIR = Path(__file__).resolve().parent / "player_data"

SONG_DIR = APP_DIR / "songs"       # the player's own mp3 copies
COVER_DIR = APP_DIR / "covers"
CACHE_DIR = APP_DIR / "cache"      # temporary renders (speed, Twin Peak, chunks)
LIB_FILE = APP_DIR / "library.json"

# ---- limits (change here if you want) ----
MAX_UPLOAD_GB = 20         # largest single original file accepted
MAX_LIBRARY_GB = 100       # total size of all stored mp3 copies
MAX_COVER_MB = 10          # largest picture accepted as a cover
MIN_FREE_GB = 3            # refuse to convert/render if the disk has less free space
REVERSE_CHUNK_SEC = 300    # Twin Peak works in 5-minute pieces to save memory
CHUNK_SRC_SEC = 180        # with speed != 1.0, audio is rendered in 3-minute pieces

# ---- shuffle weights (fixed values, no stacking) ----
UP_WEIGHT = 1.4
DOWN_WEIGHT = 0.6

SINGLES = "__singles__"
MODES = ["Sequential", "Repeat one", "Repeat list", "Shuffle"]
SPEED_CHOICES = ["0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x"]

NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


# ============================================================ tool lookup
def find_tool(name):
    """Find ffmpeg or ffprobe: next to this script / exe, inside the bundle, or on PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    candidates = []
    if FROZEN:
        candidates.append(Path(sys.executable).resolve().parent / exe)
        candidates.append(Path(sys._MEIPASS) / exe)
    else:
        candidates.append(Path(__file__).resolve().parent / exe)
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which(name)


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")


def ffmpeg_ok():
    return bool(FFMPEG and FFPROBE)


def ffmpeg_search_report():
    folder = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
    return (f"ffmpeg: {FFMPEG or 'NOT FOUND'}\n"
            f"ffprobe: {FFPROBE or 'NOT FOUND'}\n\n"
            f"Put ffmpeg and ffprobe in this folder:\n{folder}")


def free_gb():
    target = APP_DIR if APP_DIR.exists() else APP_DIR.parent
    return shutil.disk_usage(target).free / 1024 ** 3


def run_ffmpeg(args, pump=None):
    """Run ffmpeg without freezing the window. Returns (ok, error_text)."""
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="ignore", **NO_WINDOW)
    while proc.poll() is None:
        if pump:
            pump()
        time.sleep(0.1)
    err = proc.stderr.read() if proc.stderr else ""
    return proc.returncode == 0, err.strip()


def last_line(text):
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def probe_duration(path):
    """Length of an audio file in seconds (0.0 if unknown)."""
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, **NO_WINDOW)
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def fmt_time(sec):
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def file_mb(path):
    return Path(path).stat().st_size / 1024 / 1024


def file_gb(path):
    return file_mb(path) / 1024


def clear_cache():
    if not CACHE_DIR.exists():
        return
    for f in CACHE_DIR.glob("*.mp3"):
        try:
            f.unlink()
        except OSError:
            pass


def render_chunk(src, out, start, length, speed, pump):
    """Render one piece of the song from `start`, at the given speed (pitch kept)."""
    args = ["-ss", str(start), "-i", str(src), "-t", str(length),
            "-af", f"atempo={speed}", "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(out)]
    return run_ffmpeg(args, pump)


def render_reverse(src, out, speed, pump):
    """Twin Peak: reverse each 5-minute piece, join them backwards, apply speed."""
    length = probe_duration(src)
    tmp = CACHE_DIR / f"{out.stem}_parts"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        parts, n, t = [], 0, 0.0
        while t < length:
            seg = min(REVERSE_CHUNK_SEC, length - t)
            part = tmp / f"p{n:05d}.flac"
            ok, err = run_ffmpeg(["-ss", str(t), "-i", str(src), "-t", str(seg),
                                  "-vn", "-af", "areverse", "-c:a", "flac", str(part)], pump)
            if not ok:
                return False, err
            parts.append(part)
            n += 1
            t += seg
        listfile = tmp / "list.txt"
        listfile.write_text("".join(f"file '{p.as_posix()}'\n" for p in reversed(parts)),
                            encoding="utf-8")
        args = ["-f", "concat", "-safe", "0", "-i", str(listfile)]
        if speed != 1.0:
            args += ["-af", f"atempo={speed}"]
        args += ["-vn", "-c:a", "libmp3lame", "-q:a", "2", str(out)]
        return run_ffmpeg(args, pump)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================ data layer
class Library:
    """Everything that is saved: songs, lists, covers and shuffle weights."""

    def __init__(self):
        SONG_DIR.mkdir(parents=True, exist_ok=True)
        COVER_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if LIB_FILE.exists():
            self.data = json.loads(LIB_FILE.read_text(encoding="utf-8"))
        else:
            self.data = {}
        # song id -> {"title", "file", "cover"}
        self.data.setdefault("songs", {})
        self.data.setdefault("lists", {})         # list name -> [song ids]
        self.data.setdefault("list_covers", {})   # list name -> cover file
        self.data.setdefault("weights", {})       # list name -> {song id: weight}

    @property
    def songs(self):
        return self.data["songs"]

    @property
    def lists(self):
        return self.data["lists"]

    def save(self):
        LIB_FILE.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    def singles(self):
        in_lists = {sid for ids in self.lists.values() for sid in ids}
        ids = [sid for sid in self.songs if sid not in in_lists]
        return sorted(ids, key=lambda s: self.songs[s]["title"].lower())

    def list_songs(self, name):
        if name == SINGLES:
            return self.singles()
        return [sid for sid in self.lists.get(name, []) if sid in self.songs]

    def list_cover(self, name):
        return self.data["list_covers"].get(name)

    def get_weight(self, ctx, sid):
        return self.data["weights"].get(ctx, {}).get(sid, 1.0)

    def set_weight(self, ctx, sid, value):
        self.data["weights"].setdefault(ctx, {})[sid] = value

    def library_mb(self):
        return sum(f.stat().st_size for f in SONG_DIR.glob("*") if f.is_file()) / 1024 / 1024

    def delete_song(self, sid):
        info = self.songs.pop(sid)
        (SONG_DIR / info["file"]).unlink(missing_ok=True)
        if info.get("cover"):
            (COVER_DIR / info["cover"]).unlink(missing_ok=True)
        for name, ids in self.lists.items():
            self.lists[name] = [x for x in ids if x != sid]
        for w in self.data["weights"].values():
            w.pop(sid, None)


# ============================================================ the app
class PlayerApp:
    def __init__(self, root):
        self.root = root
        root.title("Local Audio Player")
        root.geometry("1040x720")
        root.minsize(940, 660)

        pygame.mixer.init()
        self.lib = Library()

        self.ctx = SINGLES
        self.queue = []
        self.current_id = None
        self.state = "stopped"        # "playing" | "paused" | "stopped"
        self.base = 0.0               # file position where the current pygame play started
        self.acc = 0.0                # seconds played before the last pause
        self.t_resume = 0.0           # time.time() when the current play (re)started
        self.resume_src = 0.0         # where Play continues after Stop (source seconds)
        self.seg_start = 0.0          # source second that file position 0 corresponds to
        self.seg_speed = 1.0          # speed of the file being played
        self.seg_len = 0.0            # length of the file being played
        self.total_src = 0.0          # length of the stored song
        self.dur = 0.0                # length shown on the progress bar
        self.chunk_path = None        # current rendered piece (only when speed != 1.0)
        self.busy = False             # True while rendering
        self.dragging = False
        self.cover_img = None
        self.mode = tk.StringVar(value=MODES[0])
        self.speed_var = tk.StringVar(value="1.0x")
        self.reverse = False          # Twin Peak
        self.node_map = {}

        # pending trim marks (not applied until "Confirm trim")
        self.trim_sid = None
        self.trim_start = None
        self.trim_end = None

        self._build_ui()
        self.refresh_tree()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self._tick)

        if not ffmpeg_ok():
            messagebox.showwarning("ffmpeg missing",
                                   "ffmpeg/ffprobe were not found.\n\n" + ffmpeg_search_report())

    @property
    def speed_value(self):
        return float(self.speed_var.get().rstrip("x"))

    # ------------------------------------------------------------ status
    def set_status(self, text):
        self.status.config(text=text)
        self.root.update_idletasks()

    def pump(self):
        self.root.update()

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        bar = ttk.Frame(self.root, padding=6)
        bar.pack(side="top", fill="x")
        buttons = [
            ("Upload songs", self.upload),
            ("New list", self.new_list),
            ("Add to list", self.add_to_list),
            ("Move to list", self.move_to_list),
            ("Remove from list", self.remove_from_list),
            ("Rename", self.rename),
            ("Set cover", self.set_cover),
            ("Delete song", self.delete_song),
            ("Delete list", self.delete_list),
        ]
        for text, cmd in buttons:
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)

        self.status = ttk.Label(self.root, text="", anchor="w", padding=(8, 2))
        self.status.pack(side="bottom", fill="x")

        body = ttk.Frame(self.root, padding=6)
        body.pack(fill="both", expand=True)

        # left: Finder-like tree
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        self.tree = ttk.Treeview(left, show="tree", selectmode="extended")
        scroll = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_cover_display())

        # right: now playing
        right = ttk.Frame(body, padding=(12, 0))
        right.pack(side="right", fill="y")

        cover_box = tk.Frame(right, width=280, height=280, bg="#222")
        cover_box.pack_propagate(False)
        cover_box.pack()
        self.cover_label = tk.Label(cover_box, bg="#222", fg="#ccc", text="No cover")
        self.cover_label.pack(expand=True)

        self.now_label = ttk.Label(right, text="Nothing playing",
                                   font=("TkDefaultFont", 12, "bold"),
                                   wraplength=300, justify="center")
        self.now_label.pack(pady=(4, 0))

        self.progress = ttk.Scale(right, from_=0, to=1, orient="horizontal", length=300)
        self.progress.pack(pady=(4, 0))
        # click anywhere on the bar to jump there; press and drag also works
        self.progress.bind("<ButtonPress-1>", self.progress_press)
        self.progress.bind("<B1-Motion>", self.progress_drag)
        self.progress.bind("<ButtonRelease-1>", self.on_progress_release)
        self.time_label = ttk.Label(right, text="0:00 / 0:00")
        self.time_label.pack()

        ctrl = ttk.Frame(right)
        ctrl.pack(pady=4)
        ttk.Button(ctrl, text="Prev", width=7, command=self.prev_song).grid(row=0, column=0, padx=2)
        self.play_btn = ttk.Button(ctrl, text="Play", width=7, command=self.toggle_play)
        self.play_btn.grid(row=0, column=1, padx=2)
        ttk.Button(ctrl, text="Stop", width=7, command=self.stop).grid(row=0, column=2, padx=2)
        ttk.Button(ctrl, text="Restart", width=7, command=self.restart).grid(row=1, column=0, padx=2, pady=4)
        ttk.Button(ctrl, text="Next", width=7, command=self.next_song).grid(row=1, column=1, padx=2)

        ttk.Label(right, text="Play mode").pack()
        ttk.Combobox(right, values=MODES, state="readonly",
                     textvariable=self.mode, width=22).pack(pady=(0, 4))

        speed_row = ttk.Frame(right)
        speed_row.pack(pady=4)
        ttk.Label(speed_row, text="Speed").pack(side="left", padx=4)
        speed_box = ttk.Combobox(speed_row, values=SPEED_CHOICES, state="readonly",
                                 textvariable=self.speed_var, width=8)
        speed_box.pack(side="left")
        speed_box.bind("<<ComboboxSelected>>", lambda e: self.on_effect_change())
        self.twin_btn = ttk.Button(right, text="Twin Peak: OFF", command=self.toggle_twin_peak)
        self.twin_btn.pack(pady=4)

        # trimming
        ttk.Label(right, text="Trim: pause, drag the bar, then mark",
                  font=("TkDefaultFont", 10, "bold")).pack(pady=(10, 0))
        trim_row = ttk.Frame(right)
        trim_row.pack(pady=2)
        ttk.Button(trim_row, text="Trim start here", command=self.trim_start_here).pack(side="left", padx=2)
        ttk.Button(trim_row, text="Trim end here", command=self.trim_end_here).pack(side="left", padx=2)
        self.trim_info = ttk.Label(right, text="", wraplength=300, justify="center")
        self.trim_info.pack(pady=2)
        ttk.Button(right, text="Confirm trim (replaces the file)",
                   command=self.confirm_trim).pack(pady=2)

        # shuffle buttons (fixed values, no weight shown)
        weight_row = ttk.Frame(right)
        weight_row.pack(pady=(10, 2))
        ttk.Button(weight_row, text="▲ more often",
                   command=lambda: self.set_weight_fixed(UP_WEIGHT)).pack(side="left", padx=4)
        ttk.Button(weight_row, text="▼ less often",
                   command=lambda: self.set_weight_fixed(DOWN_WEIGHT)).pack(side="left", padx=4)

        ttk.Label(right, text="Volume").pack(pady=(10, 0))
        self.vol_label = ttk.Label(right, text="70%")
        self.vol_label.pack()
        ttk.Scale(right, from_=0, to=100, orient="horizontal", length=260,
                  value=70, command=self.on_volume).pack()
        pygame.mixer.music.set_volume(0.7)

    # ------------------------------------------------------------ progress bar clicks
    def _x_to_time(self, x):
        """Convert a mouse x position on the progress bar into a time in seconds."""
        pad = 10                              # half the slider width, so the ends line up
        width = max(self.progress.winfo_width() - 2 * pad, 1)
        frac = min(max((x - pad) / width, 0.0), 1.0)
        top = float(self.progress.cget("to"))
        return frac * top

    def progress_press(self, event):
        """Click on the bar: jump straight to that spot."""
        if self.current_id is None:
            return "break"
        self.dragging = True
        self.progress.set(self._x_to_time(event.x))
        return "break"                        # stop the default ttk behaviour

    def progress_drag(self, event):
        if self.dragging:
            self.progress.set(self._x_to_time(event.x))
        return "break"

    def on_progress_release(self, event):
        if not self.dragging:
            return
        self.dragging = False
        if self.current_id is None:
            return
        target = self.progress.get()
        if self.state == "stopped":
            self.resume_src = target
            self.base = target
            self.update_progress()
            return
        self._play_at(self.current_id, target, paused=(self.state == "paused"))

    # ------------------------------------------------------------ tree
    def refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.node_map = {}
        for name in list(self.lib.lists):
            self._insert_folder(name, name)
        self._insert_folder(SINGLES, "Singles (not in any list)")
        self.update_cover_display()

    def _insert_folder(self, name, label):
        folder = self.tree.insert("", "end", text=label, open=True)
        self.node_map[folder] = ("list", name, None)
        for sid in self.lib.list_songs(name):
            item = self.tree.insert(folder, "end", text=self.lib.songs[sid]["title"])
            self.node_map[item] = ("song", name, sid)

    def selected(self):
        return [self.node_map[i] for i in self.tree.selection() if i in self.node_map]

    def selected_song_ids(self):
        return list({sid for kind, _, sid in self.selected() if kind == "song"})

    def save_and_refresh(self):
        self.lib.save()
        self.queue = self.lib.list_songs(self.ctx)
        self.refresh_tree()

    def on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid in self.node_map and self.node_map[iid][0] == "song":
            _, ctx, sid = self.node_map[iid]
            self.start_playing(ctx, sid)

    # ------------------------------------------------------------ upload
    def upload(self):
        if not ffmpeg_ok():
            messagebox.showerror("ffmpeg missing", "ffmpeg is missing, so files cannot be converted.\n\n"
                                 + ffmpeg_search_report())
            return
        paths = filedialog.askopenfilenames(title="Choose audio or video files")
        if not paths:
            return

        added, failures = 0, []
        for p in paths:
            src = Path(p)
            size_gb = file_gb(src)
            if size_gb > MAX_UPLOAD_GB:
                failures.append(f"{src.name}: too large ({size_gb:.1f} GB, limit {MAX_UPLOAD_GB} GB)")
                continue
            if free_gb() < MIN_FREE_GB:
                failures.append(f"{src.name}: not enough free disk space (need at least {MIN_FREE_GB} GB)")
                continue

            sid = uuid.uuid4().hex
            dst = SONG_DIR / f"{sid}.mp3"
            self.set_status(f"Converting {src.name} to mp3... (long files take a few minutes)")
            ok, err = run_ffmpeg(["-i", str(src), "-vn", "-map", "0:a:0",
                                  "-c:a", "libmp3lame", "-q:a", "2", str(dst)], self.pump)
            self.set_status("")

            if ok and dst.exists() and dst.stat().st_size > 0:
                if self.lib.library_mb() > MAX_LIBRARY_GB * 1024:
                    dst.unlink(missing_ok=True)
                    failures.append(f"{src.name}: library is full (limit {MAX_LIBRARY_GB} GB)")
                    continue
                self.lib.songs[sid] = {"title": src.stem, "file": dst.name, "cover": None}
                added += 1
            else:
                dst.unlink(missing_ok=True)
                reason = last_line(err) or "conversion failed (no audio track found?)"
                failures.append(f"{src.name}: {reason}")

        self.save_and_refresh()

        if added:
            messagebox.showinfo(
                "Import finished",
                f"Converted {added} file(s) to mp3 and stored them in the player.\n\n"
                "The player keeps its own copy, so you can delete the original "
                "files to free up disk space.")
        if failures:
            messagebox.showerror("Some files were not imported", "\n".join(failures))

    # ------------------------------------------------------------ list actions
    def new_list(self):
        name = simpledialog.askstring("New list", "List name:", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        if name == SINGLES or name in self.lib.lists:
            messagebox.showwarning("Name taken", "Pick a different name.")
            return
        self.lib.lists[name] = []
        self.save_and_refresh()

    def choose_from(self, title, options):
        if not options:
            messagebox.showinfo("No lists", "Create a list first with 'New list'.")
            return None
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.transient(self.root)
        dlg.grab_set()
        lb = tk.Listbox(dlg, width=32, height=min(10, len(options)))
        for o in options:
            lb.insert("end", o)
        lb.selection_set(0)
        lb.pack(padx=8, pady=8)
        chosen = {}

        def ok(_=None):
            picked = lb.curselection()
            if picked:
                chosen["v"] = options[picked[0]]
            dlg.destroy()

        lb.bind("<Double-Button-1>", ok)
        ttk.Button(dlg, text="OK", command=ok).pack(pady=(0, 8))
        dlg.wait_window()
        return chosen.get("v")

    def add_to_list(self):
        sids = self.selected_song_ids()
        if not sids:
            messagebox.showinfo("Select songs", "Select one or more songs first.")
            return
        target = self.choose_from("Add to which list?", list(self.lib.lists))
        if not target:
            return
        for sid in sids:  # reference only, the stored file is not copied
            if sid not in self.lib.lists[target]:
                self.lib.lists[target].append(sid)
        self.save_and_refresh()

    def move_to_list(self):
        items = [(ctx, sid) for kind, ctx, sid in self.selected() if kind == "song"]
        if not items:
            messagebox.showinfo("Select songs", "Select one or more songs first.")
            return
        target = self.choose_from("Move to which list?", list(self.lib.lists))
        if not target:
            return
        for ctx, sid in items:
            if ctx != SINGLES and ctx != target:
                self.lib.lists[ctx] = [x for x in self.lib.lists[ctx] if x != sid]
            if sid not in self.lib.lists[target]:
                self.lib.lists[target].append(sid)
        self.save_and_refresh()

    def remove_from_list(self):
        items = [(ctx, sid) for kind, ctx, sid in self.selected()
                 if kind == "song" and ctx != SINGLES]
        if not items:
            messagebox.showinfo("Nothing to remove",
                                "Select songs inside a list first. They go back to Singles "
                                "if they are in no other list.")
            return
        for ctx, sid in items:
            self.lib.lists[ctx] = [x for x in self.lib.lists[ctx] if x != sid]
        self.save_and_refresh()

    def delete_song(self):
        sids = self.selected_song_ids()
        if not sids:
            messagebox.showinfo("Select songs", "Select one or more songs first.")
            return
        if not messagebox.askyesno(
                "Delete songs",
                f"Delete {len(sids)} song(s) from the player?\n"
                "Your original files on the computer are not touched."):
            return
        for sid in sids:
            if sid == self.current_id:
                self._release_playback()
                self.current_id = None
            self.lib.delete_song(sid)
        clear_cache()
        self.save_and_refresh()
        self.update_now_playing()

    def delete_list(self):
        names = [ctx for kind, ctx, _ in self.selected()
                 if kind == "list" and ctx != SINGLES]
        if not names:
            messagebox.showinfo("Select a list", "Select a list folder first.")
            return
        if not messagebox.askyesno("Delete lists",
                                   "Delete the selected list(s)? Songs stay in the player."):
            return
        for name in names:
            cover = self.lib.data["list_covers"].pop(name, None)
            if cover:
                (COVER_DIR / cover).unlink(missing_ok=True)
            self.lib.lists.pop(name, None)
            self.lib.data["weights"].pop(name, None)
        if self.ctx in names:
            self.ctx = SINGLES
        self.save_and_refresh()

    def rename(self):
        nodes = self.selected()
        if len(nodes) != 1:
            messagebox.showinfo("Rename", "Select exactly one song or list.")
            return
        kind, ctx, sid = nodes[0]
        if kind == "song":
            new = simpledialog.askstring("Rename song", "New title:", parent=self.root,
                                         initialvalue=self.lib.songs[sid]["title"])
            if new and new.strip():
                self.lib.songs[sid]["title"] = new.strip()
        else:
            if ctx == SINGLES:
                return
            new = simpledialog.askstring("Rename list", "New name:", parent=self.root,
                                         initialvalue=ctx)
            new = (new or "").strip()
            if not new or new == SINGLES or (new != ctx and new in self.lib.lists):
                messagebox.showwarning("Name taken", "Pick a different name.")
                return
            self.lib.data["lists"] = {(new if k == ctx else k): v
                                      for k, v in self.lib.lists.items()}
            for key in ("weights", "list_covers"):
                table = self.lib.data[key]
                if ctx in table:
                    table[new] = table.pop(ctx)
            if self.ctx == ctx:
                self.ctx = new
        self.save_and_refresh()
        self.update_now_playing()

    # ------------------------------------------------------------ covers
    def set_cover(self):
        """Upload a picture for one song or one list (replaces the old one). No naming."""
        nodes = self.selected()
        if len(nodes) != 1 or (nodes[0][0] == "list" and nodes[0][1] == SINGLES):
            messagebox.showinfo("Set cover", "Select exactly one song or one list folder.")
            return
        if not ffmpeg_ok():
            messagebox.showerror("ffmpeg missing", "ffmpeg is missing.\n\n" + ffmpeg_search_report())
            return
        path = filedialog.askopenfilename(
            title="Choose a picture",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("All files", "*.*")])
        if not path:
            return
        if file_mb(path) > MAX_COVER_MB:
            messagebox.showerror("Picture too large", f"Covers must be under {MAX_COVER_MB} MB.")
            return

        new_file = f"{uuid.uuid4().hex}.png"
        ok, err = run_ffmpeg(["-i", path, "-vf",
                              "scale=300:300:force_original_aspect_ratio=decrease",
                              "-frames:v", "1", str(COVER_DIR / new_file)])
        if not ok:
            (COVER_DIR / new_file).unlink(missing_ok=True)
            messagebox.showerror("Cover failed", f"Could not read that picture.\n\n{last_line(err)}")
            return

        kind, ctx, sid = nodes[0]
        if kind == "song":
            old = self.lib.songs[sid].get("cover")
            self.lib.songs[sid]["cover"] = new_file
        else:
            old = self.lib.list_cover(ctx)
            self.lib.data["list_covers"][ctx] = new_file
        if old:
            (COVER_DIR / old).unlink(missing_ok=True)
        self.save_and_refresh()
        self.update_cover_display()

    def update_cover_display(self):
        """Selected song/list cover if one is selected, otherwise the playing song/list cover."""
        cover = None
        sel = self.selected()
        if len(sel) == 1:
            kind, ctx, sid = sel[0]
            if kind == "song":
                cover = self.lib.songs[sid].get("cover")
            elif ctx != SINGLES:
                cover = self.lib.list_cover(ctx)
        else:
            if self.current_id in self.lib.songs:
                cover = self.lib.songs[self.current_id].get("cover")
            if not cover and self.ctx != SINGLES:
                cover = self.lib.list_cover(self.ctx)
        self._set_cover(cover)

    def _set_cover(self, filename):
        if filename and (COVER_DIR / filename).exists():
            self.cover_img = tk.PhotoImage(file=str(COVER_DIR / filename))
            self.cover_label.config(image=self.cover_img, text="")
        else:
            self.cover_img = None
            self.cover_label.config(image="", text="No cover")

    # ------------------------------------------------------------ shuffle weight
    def set_weight_fixed(self, value):
        if self.current_id is None:
            messagebox.showinfo("Shuffle", "Play a song first.")
            return
        self.lib.set_weight(self.ctx, self.current_id, value)
        self.lib.save()

    # ------------------------------------------------------------ trimming
    def _trim_ready(self):
        if self.current_id is None:
            messagebox.showinfo("Trim", "Play a song first, then pause where you want to cut.")
            return False
        if self.speed_value != 1.0 or self.reverse:
            messagebox.showinfo("Trim", "Set Speed to 1.0x and turn Twin Peak OFF, then trim.")
            return False
        if self.state == "playing":
            messagebox.showwarning(
                "Pause first",
                "The progress bar is still moving, so the cut point can't be set.\n\n"
                "Press Pause, click the bar at the spot, then click the trim button.")
            return False
        if self.trim_sid not in (None, self.current_id):
            self._clear_marks()
        self.trim_sid = self.current_id
        return True

    def _clear_marks(self):
        self.trim_sid = None
        self.trim_start = None
        self.trim_end = None

    def _show_trim(self):
        s = fmt_time(self.trim_start) if self.trim_start is not None else "not set"
        e = fmt_time(self.trim_end) if self.trim_end is not None else "not set"
        self.trim_info.config(
            text=f"Start: {s}   End: {e}\n"
                 "Marks can be changed freely. Nothing changes until you press Confirm.\n"
                 "Applies to this song in every list that contains it.")

    def trim_start_here(self):
        if not self._trim_ready():
            return
        point = round(self.src_pos(), 2)
        if self.trim_end is not None and point >= self.trim_end - 0.5:
            messagebox.showwarning("Trim", "The start must be before the end mark.")
            return
        self.trim_start = point
        self._show_trim()

    def trim_end_here(self):
        if not self._trim_ready():
            return
        point = round(self.src_pos(), 2)
        if self.trim_start is not None and point <= self.trim_start + 0.5:
            messagebox.showwarning("Trim", "The end must be after the start mark.")
            return
        self.trim_end = point
        self._show_trim()

    def confirm_trim(self):
        sid = self.current_id
        if sid is None or self.trim_sid != sid or (self.trim_start is None and self.trim_end is None):
            messagebox.showinfo("Trim", "Set a start or end mark first.")
            return
        if self.state == "playing":
            messagebox.showwarning("Pause first", "Pause the music before confirming the trim.")
            return

        info = self.lib.songs[sid]
        src = SONG_DIR / info["file"]
        total = probe_duration(src)
        lo = self.trim_start if self.trim_start is not None else 0.0
        hi = self.trim_end if self.trim_end is not None else total
        if lo <= 0.05 and hi >= total - 0.05:
            messagebox.showinfo("Trim", "The marks cover the whole song. Nothing to cut.")
            return

        using = [name for name, ids in self.lib.lists.items() if sid in ids]
        where = ("This song is in these lists: " + ", ".join(using) + ".\n"
                 if using else "This song is only in Singles.\n")
        if not messagebox.askyesno(
                "Confirm trim - cannot be undone",
                f"Keep only {fmt_time(lo)} to {fmt_time(hi)} of this song?\n\n"
                f"{where}"
                "The trim applies to this song EVERYWHERE: every list that contains it "
                "plays the same cut version, because they share one stored copy.\n\n"
                "The stored file will be REPLACED. The removed parts are gone for good, "
                "and from now on you can only cut the song shorter.\n\n"
                "Continue?"):
            return

        if free_gb() < MIN_FREE_GB:
            messagebox.showerror("Trim", f"Not enough free disk space (need at least {MIN_FREE_GB} GB).")
            return

        self._release_playback()
        new_name = f"{uuid.uuid4().hex}.mp3"
        dst = SONG_DIR / new_name
        self.busy = True
        self.set_status("Cutting the song... (long files take a few minutes)")
        try:
            ok, err = run_ffmpeg(["-ss", str(lo), "-i", str(src), "-t", str(max(0.1, hi - lo)),
                                  "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(dst)], self.pump)
        finally:
            self.set_status("")
            self.busy = False

        if not ok or not dst.exists() or dst.stat().st_size == 0:
            dst.unlink(missing_ok=True)
            messagebox.showerror("Trim failed", f"Could not cut the song.\n\n{last_line(err)}")
            return

        src.unlink(missing_ok=True)
        info["file"] = new_name
        self._clear_marks()
        clear_cache()
        self.lib.save()

        self.resume_src = 0.0
        self.total_src = probe_duration(dst) or 0.0
        self.dur = self.total_src
        self.progress.configure(to=max(self.dur, 1))
        self.progress.set(0)
        self.update_now_playing()
        self.trim_info.config(text="Trim saved. Press Play to listen.")
        messagebox.showinfo("Trim saved", "The song has been cut. Every list that contains it now uses the cut version.")

    # ------------------------------------------------------------ playback core
    def file_pos(self):
        """Position inside the file currently loaded in pygame, in seconds."""
        if self.state == "playing":
            return self.base + self.acc + (time.time() - self.t_resume)
        return self.base + self.acc

    def src_pos(self):
        """Position in the SONG's own timeline (what the progress bar shows)."""
        if self.current_id is None:
            return 0.0
        if self.state == "stopped":
            return self.resume_src
        return self.seg_start + self.file_pos() * self.seg_speed

    def _need_space(self):
        if free_gb() < MIN_FREE_GB:
            raise RuntimeError(f"Not enough free disk space (need at least {MIN_FREE_GB} GB).")

    def _play_at(self, sid, src_t=0.0, paused=False):
        """
        Load and play `sid` starting at source second `src_t`.
        Speed != 1.0 (not Twin Peak): a 3-minute piece is rendered from src_t at that speed.
        Twin Peak: the whole reversed song is rendered (cached) and played from src_t.
        """
        stored = SONG_DIR / self.lib.songs[sid]["file"]
        if not stored.exists():
            messagebox.showerror("Missing file", "The stored file for this song is missing.")
            return False
        total = probe_duration(stored)
        if total <= 0:
            messagebox.showerror("Could not read", self.lib.songs[sid]["title"])
            return False
        src_t = min(max(src_t, 0.0), max(total - 0.1, 0.0))
        speed, rev = self.speed_value, self.reverse
        new_chunk = None
        self.busy = True
        try:
            if rev:
                key = hashlib.md5(f"{stored.name}|{speed}|rev".encode()).hexdigest()
                path = CACHE_DIR / f"{key}.mp3"
                if not path.exists():
                    self._need_space()
                    self.set_status(f"Preparing '{self.lib.songs[sid]['title']}'... (long songs take a few minutes)")
                    ok, err = render_reverse(stored, path, speed, self.pump)
                    if not ok:
                        path.unlink(missing_ok=True)
                        raise RuntimeError(last_line(err) or "ffmpeg failed")
                seg_start, seg_speed, base = 0.0, 1.0, src_t
                seg_len = probe_duration(path)
                dur = seg_len
            elif speed == 1.0:
                path = stored
                seg_start, seg_speed, base = 0.0, 1.0, src_t
                seg_len = total
                dur = total
            else:
                length = min(CHUNK_SRC_SEC, total - src_t)
                new_chunk = CACHE_DIR / f"chunk_{uuid.uuid4().hex}.mp3"
                self._need_space()
                ok, err = render_chunk(stored, new_chunk, src_t, length, speed, self.pump)
                if not ok:
                    new_chunk.unlink(missing_ok=True)
                    raise RuntimeError(last_line(err) or "ffmpeg failed")
                path = new_chunk
                seg_start, seg_speed, base = src_t, speed, 0.0
                seg_len = probe_duration(path) or length / speed
                dur = total
        except RuntimeError as e:
            messagebox.showerror("Could not prepare audio", f"{self.lib.songs[sid]['title']}\n\n{e}")
            return False
        finally:
            self.set_status("")
            self.busy = False

        pygame.mixer.music.load(str(path))
        pygame.mixer.music.play(start=base)
        if paused:
            pygame.mixer.music.pause()

        old = self.chunk_path
        self.chunk_path = new_chunk
        if old and old != new_chunk:
            try:
                Path(old).unlink(missing_ok=True)
            except OSError:
                pass   # still in use; cleared when the program closes

        self.current_id = sid
        self.state = "paused" if paused else "playing"
        self.base, self.acc, self.t_resume = base, 0.0, time.time()
        self.seg_start, self.seg_speed, self.seg_len = seg_start, seg_speed, seg_len
        self.total_src, self.dur = total, dur
        self.resume_src = src_t
        self.update_now_playing()
        return True

    def _release_playback(self):
        """Stop pygame and forget the rendered piece (used before the file is replaced/deleted)."""
        if self.current_id is not None:
            self.resume_src = self.src_pos()
        try:
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
        except AttributeError:
            pygame.mixer.music.stop()
        self.state = "stopped"
        self.base, self.acc = self.resume_src, 0.0
        if self.chunk_path:
            try:
                Path(self.chunk_path).unlink(missing_ok=True)
            except OSError:
                pass
            self.chunk_path = None

    def _begin(self, sid, src_t=0.0):
        if sid != self.trim_sid:
            self._clear_marks()
        self._play_at(sid, src_t)

    def start_playing(self, ctx, sid):
        self.ctx = ctx
        self.queue = self.lib.list_songs(ctx)
        self._begin(sid, 0.0)
        self.update_cover_display()

    def toggle_play(self):
        if self.state == "playing":
            self.acc += time.time() - self.t_resume
            pygame.mixer.music.pause()
            self.state = "paused"
        elif self.state == "paused":
            self.t_resume = time.time()
            pygame.mixer.music.unpause()
            self.state = "playing"
        else:
            sel = [(ctx, sid) for kind, ctx, sid in self.selected() if kind == "song"]
            if sel:
                self.start_playing(*sel[0])
            elif self.current_id:
                self._begin(self.current_id, self.resume_src)   # continue after Stop
            else:
                queue = self.lib.list_songs(self.ctx)
                if queue:
                    self.start_playing(self.ctx, queue[0])
                else:
                    messagebox.showinfo("Nothing to play", "Add a song first.")
                    return
        self.update_now_playing()

    def stop(self):
        if self.current_id is None:
            return
        self.resume_src = self.src_pos()
        pygame.mixer.music.stop()
        self.base, self.acc = self.resume_src, 0.0
        self.state = "stopped"
        self.update_now_playing()

    def restart(self):
        if self.current_id is not None:
            self._begin(self.current_id, 0.0)

    def pick_next(self, auto):
        if not self.queue:
            return None
        mode = self.mode.get()
        if auto and mode == "Repeat one" and self.current_id:
            return self.current_id
        if mode == "Shuffle":
            choices = [s for s in self.queue if s != self.current_id] or self.queue
            weights = [self.lib.get_weight(self.ctx, s) for s in choices]
            return random.choices(choices, weights=weights, k=1)[0]
        index = self.queue.index(self.current_id) if self.current_id in self.queue else -1
        nxt = index + 1
        if nxt >= len(self.queue):
            if auto and mode != "Repeat list":
                return None
            nxt = 0
        return self.queue[nxt]

    def next_song(self, auto=False):
        sid = self.pick_next(auto)
        if sid:
            self._begin(sid, 0.0)
            self.update_cover_display()
        else:
            self.stop()

    def prev_song(self):
        if not self.queue:
            return
        index = self.queue.index(self.current_id) if self.current_id in self.queue else 0
        self._begin(self.queue[index - 1], 0.0)
        self.update_cover_display()

    def on_effect_change(self):
        """Speed changed: keep the position and play the rest at the new speed."""
        if self.current_id is None or self.state == "stopped":
            return      # a stopped song simply uses the new speed on the next Play
        self._play_at(self.current_id, self.src_pos(), paused=(self.state == "paused"))

    def toggle_twin_peak(self):
        """Twin Peak changes the time direction, so the song restarts from the beginning."""
        self.reverse = not self.reverse
        self.twin_btn.config(text=f"Twin Peak: {'ON' if self.reverse else 'OFF'}")
        if self.current_id is None:
            return
        if self.state == "stopped":
            self.resume_src = 0.0
            self.update_now_playing()
        else:
            self._play_at(self.current_id, 0.0, paused=(self.state == "paused"))

    def on_volume(self, value):
        v = float(value)
        pygame.mixer.music.set_volume(v / 100)
        self.vol_label.config(text=f"{int(v)}%")

    def _tick(self):
        if self.state == "playing" and self.current_id is not None and not self.busy:
            if self.chunk_path is not None:
                # speed mode: move on to the next piece when this one has played out
                if self.file_pos() >= self.seg_len - 0.05:
                    nxt = self.seg_start + self.seg_len * self.seg_speed
                    if nxt >= self.total_src - 0.05:
                        self.next_song(auto=True)
                    else:
                        self._play_at(self.current_id, nxt)
            else:
                ended = (not pygame.mixer.music.get_busy()
                         and time.time() - self.t_resume > 1.0)
                if ended or self.file_pos() >= self.seg_len - 0.05:
                    self.next_song(auto=True)
        self.update_progress()
        self.root.after(200, self._tick)

    # ------------------------------------------------------------ display
    def update_progress(self):
        if self.current_id is None:
            return
        p = min(max(self.src_pos(), 0.0), self.dur or 0.0)
        if not self.dragging:
            self.progress.set(p)
        self.time_label.config(text=f"{fmt_time(p)} / {fmt_time(self.dur)}")

    def update_now_playing(self):
        self.play_btn.config(text="Pause" if self.state == "playing" else "Play")
        if self.current_id is None or self.current_id not in self.lib.songs:
            self.now_label.config(text="Nothing playing")
            self.trim_info.config(text="")
            self.progress.set(0)
            self.time_label.config(text="0:00 / 0:00")
        else:
            info = self.lib.songs[self.current_id]
            self.now_label.config(text=info["title"])
            self.progress.configure(to=max(self.dur, 1))
            if self.trim_sid == self.current_id:
                self._show_trim()
            else:
                self.trim_info.config(text="")
            self.update_progress()
        self.update_cover_display()

    def on_close(self):
        self.lib.save()
        self._release_playback()
        pygame.mixer.music.stop()
        pygame.mixer.quit()
        clear_cache()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    PlayerApp(root)
    root.mainloop()
