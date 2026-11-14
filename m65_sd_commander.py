#!/usr/bin/env python3
"""
MEGA65 SD Commander
A Norton Commander style file manager for the MEGA65's SD card over Ethernet.

Left panel  : your PC
Right panel : the MEGA65 SD card (via mega65_ftp, kept running in the background)

Requires: Python 3.8+ (tkinter included), mega65_ftp(.exe) from mega65-tools,
          ETHLOAD.M65 in the root of the MEGA65 SD card.
Optional: send2trash  (pip install send2trash) - local deletes go to the Recycle Bin.
"""

import collections
import json
import os
import queue
import shutil
import string
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m65ftp import (Mega65FTP, FTPError, NotConnected, safe_remote_name,  # noqa: E402
                    remote_join, remote_parent, MAX_FILE_SIZE, too_big_text)
from m65board import (Board, BoardError, LAUNCH_EXTS, UPLOAD_EXTS, find_board,  # noqa: E402
                     board_join, board_parent)
import tempfile  # noqa: E402
import textwrap  # noqa: E402
import zipfile  # noqa: E402
from m65cbm import DiskImage, DiskImageError, is_disk_image  # noqa: E402
import m65cbm  # noqa: E402


def zip_parts(name):
    """Split a zip member name into safe path parts, or None if it tries to escape
    the target folder (absolute path, drive letter or '..')."""
    n = name.replace("\\", "/")
    if n.startswith("/") or (len(n) > 1 and n[1] == ":"):
        return None
    parts = [x for x in n.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return parts


def safe_pc_name(name):
    """A PETSCII-derived name that Windows will accept as a file name."""
    out = "".join("_" if c in '\\/:*?"<>|' or ord(c) < 32 else c for c in name).strip().rstrip(".")
    return out or "_"


def disk_out_name(entry):
    """File name for a file copied out of a disk image: NAME.prg, NAME.seq, ..."""
    ref = entry.ref
    base = safe_pc_name(ref.name if ref is not None else entry.name)
    if ref is not None and getattr(entry, "name", "").endswith(tuple(f" #{n}" for n in range(2, 300))):
        base += "_" + entry.name.rsplit("#", 1)[1]          # keep repeated names apart
    return f"{base}.{ref.extension}" if ref is not None else base


def zip_inner_parts(inner):
    return [x for x in inner.split("/") if x]


def list_zip(zip_path, inner):
    """Entries directly inside folder 'inner' ("" = root) of a zip file."""
    ip = zip_inner_parts(inner)
    dirs, files = {}, {}
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            parts = zip_parts(info.filename)
            if not parts or parts[:len(ip)] != ip:
                continue
            rest = parts[len(ip):]
            if not rest:
                continue
            y, mo, d, h, mi, _sec = info.date_time
            date = f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}"
            if len(rest) > 1 or info.is_dir():
                dirs.setdefault(rest[0], date if len(rest) == 1 else "")
            else:
                files[rest[0]] = Entry(rest[0], False, info.file_size, date)
    return [Entry(n, True, 0, dt) for n, dt in dirs.items()] + \
        [e for n, e in files.items() if n not in dirs]


def wrap_lines(text, width=86, max_lines=16):
    out = []
    for line in str(text).splitlines():
        out.extend(textwrap.wrap(line, width, subsequent_indent="  ") or [""])
    return "\n".join(out[:max_lines])

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None

APP_NAME = "MEGA65 SD Commander"
APP_VERSION = "0.7.10"
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
# names mega65_ftp comes with: Windows .exe, macOS .osx, Linux plain
FTP_NAMES = ("mega65_ftp.exe", "mega65_ftp.osx", "mega65_ftp") if IS_WINDOWS else \
            ("mega65_ftp.osx", "mega65_ftp", "mega65_ftp.exe") if IS_MAC else \
            ("mega65_ftp", "mega65_ftp.osx", "mega65_ftp.exe")
FTP_NAME = FTP_NAMES[0]
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".m65sd.config")
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "m65sd.log")

# ---------------------------------------------------------------- NC palette
NAVY = "#0000AA"
CYAN = "#55FFFF"
DCYAN = "#00AAAA"
WHITE = "#FFFFFF"
YELLOW = "#FFFF55"
BLACK = "#000000"
GREY = "#AAAAAA"
DGREY = "#555555"
RED = "#AA0000"

DEFAULT_CONFIG = {
    "exe_path": "",
    "ip_iface": "",
    "device": "",
    "extra_args": "",
    "connect_on_start": False,
    "font_size": 11,
    "local_path": os.path.expanduser("~"),
    "remote_path": "/",
    "left_mode": "pc",               # "pc", "mega65" or "board"
    "right_mode": "mega65",
    "right_local_path": "",
    "board_path": "",
    "board_url": "",                 # blank = read from .m65j.config
    "board_user": "",
    "board_password": "",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if not cfg["exe_path"]:
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in FTP_NAMES:
            p = os.path.join(here, cand)
            if os.path.isfile(p):
                cfg["exe_path"] = p
                break
        else:
            cfg["exe_path"] = next((w for w in map(shutil.which, FTP_NAMES) if w), "")
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def fmt_size(n):
    if n < 10_000_000:
        return str(n)
    if n < 10_000 * 1024 * 1024:
        return f"{n // (1024 * 1024)}M"
    return f"{n // (1024 * 1024 * 1024)}G"


def pick_font(size):
    fams = set(tkfont.families())
    for f in ("Consolas", "Lucida Console", "DejaVu Sans Mono", "Liberation Mono", "Courier New"):
        if f in fams:
            return (f, size)
    return ("Courier", size)


class Entry:
    __slots__ = ("name", "is_dir", "size", "date", "up", "title", "ref", "blocks", "ftype")

    def __init__(self, name, is_dir, size=0, date="", up=False, title="", ref=None, blocks=None, ftype=""):
        self.name, self.is_dir, self.size, self.date, self.up = name, is_dir, size, date, up
        self.title = title
        self.ref, self.blocks, self.ftype = ref, blocks, ftype   # disk image entries


KIND_PC, KIND_MEGA65, KIND_BOARD = "pc", "mega65", "board"
KIND_LABEL = {KIND_MEGA65: "MEGA65 SD card", KIND_BOARD: "EXP Board SD card"}


# ======================================================================
#  Background worker - every mega65_ftp call runs here, never on the UI
# ======================================================================
class Worker:
    def __init__(self, app):
        self.app = app
        self.jobs = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, fn, on_done=None, on_error=None):
        self.jobs.put((fn, on_done, on_error))

    def _run(self):
        while True:
            fn, on_done, on_error = self.jobs.get()
            try:
                res = fn()
                self.app.post(on_done, res) if on_done else None
            except Exception as e:  # noqa: BLE001
                import traceback
                App._logfile("[worker error] " + "".join(traceback.format_exception(type(e), e, e.__traceback__)))
                if on_error:
                    self.app.post(on_error, e)
                else:
                    self.app.post(self.app.show_error, e)


# ----------------------------------------------------------------------
#  Windows: keep message boxes above the main window
# ----------------------------------------------------------------------
class _Win32Stack:
    """Tk can't tell where a borderless box sits in the Windows window stack, and
    Windows doesn't always keep it above the main window (e.g. when the Firewall prompt
    closes, Windows brings the main window forward over it). So ask Windows directly."""

    def __init__(self):
        self.ok = False
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            H = wintypes.HWND
            u.GetTopWindow.restype = H
            u.GetTopWindow.argtypes = [H]
            u.GetWindow.restype = H
            u.GetWindow.argtypes = [H, wintypes.UINT]
            u.GetForegroundWindow.restype = H
            u.GetParent.restype = H
            u.GetParent.argtypes = [H]
            u.GetAncestor.restype = H
            u.GetAncestor.argtypes = [H, wintypes.UINT]
            u.SetWindowPos.argtypes = [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, wintypes.UINT]
            u.SetForegroundWindow.argtypes = [H]
            u.IsWindowVisible.argtypes = [H]
            self.set_owner_fn = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
            self.set_owner_fn.argtypes = [H, ctypes.c_int, ctypes.c_void_p]
            self.u = u
            self.ok = True
        except Exception:
            self.ok = False

    def hwnd(self, w):
        """Top-level Windows handle of a Tk toplevel."""
        try:
            h = int(w.wm_frame(), 16)
            if h:
                return h
        except (tk.TclError, ValueError):
            pass
        try:
            return self.u.GetAncestor(w.winfo_id(), 2) or None     # GA_ROOT
        except Exception:
            return None

    def set_owner(self, box, root):
        """Make the box an 'owned' window of the main window: Windows then always keeps
        it above the main window, whatever gets activated."""
        if not self.ok:
            return
        try:
            hb, hr = self.hwnd(box), self.hwnd(root)
            if hb and hr:
                self.set_owner_fn(hb, -8, hr)                         # GWLP_HWNDPARENT
        except Exception:
            pass

    def box_hidden(self, box, root):
        """Return a reason string if the box is below the main window or the main
        window has the keyboard while the box is open, else ''."""
        if not self.ok:
            return ""
        try:
            hb, hr = self.hwnd(box), self.hwnd(root)
            if not hb or not hr:
                return ""
            if self.u.GetForegroundWindow() == hr:
                return "main window active"
            h, n = self.u.GetTopWindow(None), 0
            while h and n < 10000:
                if h == hb:
                    return ""
                if h == hr:
                    return "box below main window"
                h, n = self.u.GetWindow(h, 2), n + 1                  # GW_HWNDNEXT
        except Exception:
            pass
        return ""

    def bring_up(self, box):
        if not self.ok:
            return
        try:
            hb = self.hwnd(box)
            if hb:
                # HWND_TOP, SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
                self.u.SetWindowPos(hb, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
                self.u.SetForegroundWindow(hb)
        except Exception:
            pass


WIN32 = _Win32Stack()


# ======================================================================
#  Norton Commander style dialogs
# ======================================================================
class NCDialog(tk.Toplevel):
    """Grey (or red) box, double border, centred title, [ Buttons ]."""

    def __init__(self, app, title, style="grey", width=None):
        super().__init__(app.root)
        self.app = app
        self.result = None
        self.bg = GREY if style == "grey" else RED
        self.fg = BLACK if style == "grey" else WHITE
        self.overrideredirect(True)
        self.transient(app.root)
        self.configure(bg=BLACK)
        f = app.font
        outer = tk.Frame(self, bg=self.bg, padx=6, pady=8)
        outer.pack()
        b1 = tk.Frame(outer, bg=self.fg, padx=1, pady=1)
        b1.pack()
        gap = tk.Frame(b1, bg=self.bg, padx=2, pady=2)
        gap.pack()
        b2 = tk.Frame(gap, bg=self.fg, padx=1, pady=1)
        b2.pack()
        self.body = tk.Frame(b2, bg=self.bg, padx=14, pady=10)
        if width:
            self.body.configure(width=width)
        self.body.pack()
        lsp = app.font_obj.metrics("linespace") or 16
        tk.Frame(self.body, bg=self.bg, height=lsp // 2 + 2).pack(fill="x")   # room under the title
        tk.Label(outer, text=f" {title} ", bg=self.bg, fg=self.fg, font=f).place(relx=0.5, y=0, anchor="n")
        self.buttons = []
        self.btn_index = 0
        self.bind("<Escape>", lambda e: self.close(None))
        self.bind("<Tab>", lambda e: self._move(1))
        self.bind("<Shift-Tab>", lambda e: self._move(-1))
        self.bind("<ISO_Left_Tab>", lambda e: self._move(-1))

    def label(self, text, **kw):
        lab = tk.Label(self.body, text=text, bg=self.bg, fg=kw.pop("fg", self.fg),
                       font=self.app.font, justify=kw.pop("justify", "center"), **kw)
        lab.pack(fill="x")
        return lab

    def entry(self, value="", width=46):
        var = tk.StringVar(value=value)
        e = tk.Entry(self.body, textvariable=var, width=width, font=self.app.font,
                     bg=DCYAN, fg=BLACK, insertbackground=BLACK, relief="flat",
                     selectbackground=BLACK, selectforeground=CYAN, highlightthickness=0)
        e.pack(fill="x", pady=(4, 6))
        return var, e

    def add_buttons(self, names, default=0, hotkeys=True):
        row = tk.Frame(self.body, bg=self.bg)
        row.pack(pady=(10, 0))
        for i, n in enumerate(names):
            b = tk.Label(row, text=f"[ {n} ]", font=self.app.font, bg=self.bg, fg=self.fg, cursor="hand2")
            b.pack(side="left", padx=6)
            b.bind("<Button-1>", lambda e, i=i: self.close(i))
            self.buttons.append(b)
        self.btn_index = default
        self._paint()
        self.bind("<Left>", lambda e: self._move(-1))
        self.bind("<Right>", lambda e: self._move(1))
        self.bind("<Return>", lambda e: self.close(self.btn_index))
        self.bind("<KP_Enter>", lambda e: self.close(self.btn_index))
        # hot keys: first letter of each button (first one wins on a clash)
        used = set()
        for i, n in enumerate(names if hotkeys else []):
            k = n[0].lower()
            if not k.isalpha() or k in used:
                continue
            used.add(k)
            self.bind(f"<KeyPress-{k}>", lambda e, i=i: None if self._typing(e) else self.close(i))
            self.bind(f"<KeyPress-{k.upper()}>", lambda e, i=i: None if self._typing(e) else self.close(i))

    @staticmethod
    def _typing(e):
        return isinstance(e.widget, tk.Entry)

    def _move(self, d):
        if self.buttons and not isinstance(self.focus_get(), tk.Entry):
            self.btn_index = (self.btn_index + d) % len(self.buttons)
            self._paint()
            return "break"
        return None

    def _paint(self):
        for i, b in enumerate(self.buttons):
            if i == self.btn_index:
                b.configure(bg=DCYAN if self.bg == GREY else WHITE, fg=BLACK)
            else:
                b.configure(bg=self.bg, fg=self.fg)

    def show(self, focus=None):
        self.update_idletasks()
        r = self.app.root
        x = r.winfo_rootx() + (r.winfo_width() - self.winfo_reqwidth()) // 2
        y = r.winfo_rooty() + (r.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.deiconify()
        self.lift()
        self.app.dialogs.append(self)
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        WIN32.set_owner(self, r)
        WIN32.bring_up(self)
        self.focus_widget = focus or self
        self.focus_widget.focus_force()
        self.bind("<FocusIn>", self._remember_focus, add="+")

    def _remember_focus(self, e):
        if e.widget is not self:
            self.focus_widget = e.widget

    def run(self, focus=None):
        self.show(focus)
        self.wait_window()
        return self.result

    def close(self, result):
        self.result = result
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        if self in self.app.dialogs:
            self.app.dialogs.remove(self)
        if self.app.dialogs:                      # back to the dialog underneath
            top = self.app.dialogs[-1]
            try:
                top.grab_set()
                top.focus_force()
            except tk.TclError:
                pass
        else:
            # Windows doesn't give the keyboard back to the main window when a
            # borderless box closes - take it back explicitly once Tk has settled.
            self.app.root.after(10, self.app.restore_focus)
        return "break"


class ProgressDialog(NCDialog):
    def __init__(self, app, title, text="", cancellable=True):
        super().__init__(app, title)
        self.cancelled = False
        self.msg = self.label(text, width=50, anchor="w", justify="left")
        self.name = self.label("", width=50, anchor="w", justify="left", fg=BLACK)
        self.bar = self.label("░" * 50, fg=NAVY)
        self.pct = self.label("")
        if cancellable:
            self.add_buttons(["Cancel"], hotkeys=False)
            self.bind("<Return>", self._cancel)
            self.buttons[0].bind("<Button-1>", self._cancel)
        self.bind("<Escape>", self._cancel if cancellable else (lambda e: "break"))
        self.show()
        self._spin = 0
        self._indeterminate = True
        self._tick()

    def _cancel(self, e=None):
        self.cancelled = True
        self.msg.configure(text="Cancelling after the current file...")
        return "break"

    def _tick(self):
        if not self.winfo_exists():
            return
        if self._indeterminate:
            self._spin = (self._spin + 1) % 50
            s = ["░"] * 50
            for i in range(6):
                s[(self._spin + i) % 50] = "█"
            self.bar.configure(text="".join(s))
        self._tick_id = self.app.root.after(120, self._tick)

    def update_progress(self, text=None, name=None, frac=None):
        if not self.winfo_exists():
            return
        if text is not None and not self.cancelled:
            self.msg.configure(text=text)
        if name is not None:
            self.name.configure(text=(name if len(name) <= 50 else "…" + name[-49:]))
        if frac is not None:
            self._indeterminate = False
            n = int(max(0.0, min(1.0, frac)) * 50)
            self.bar.configure(text="█" * n + "░" * (50 - n))
            self.pct.configure(text=f"{int(frac * 100)}%")

    def finish(self):
        tid = getattr(self, "_tick_id", None)
        if tid:
            try:
                self.app.root.after_cancel(tid)
            except tk.TclError:
                pass
            self._tick_id = None
        if self.winfo_exists():
            self.close(None)


# ======================================================================
#  C64 font (Style's "C64 Pro Mono") for the PETSCII and BASIC viewer modes
# ======================================================================
C64_FONT_FAMILY = "C64 Pro Mono"
C64_FONT_FILE = "C64_Pro_Mono-STYLE.ttf"
C64_PUA_UPPER = 0xEE00      # upper case / graphics set, by screen code (checked in the font)
C64_PUA_LOWER = 0xEF00      # lower / upper case set, by screen code
C64_BLUE, C64_LIGHT_BLUE = "#352879", "#6C5EB5"      # the C64's screen colours


def find_c64_font_file():
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.join(here, "fonts")):
        p = os.path.join(d, C64_FONT_FILE)
        if os.path.isfile(p):
            return p
    return None


def load_c64_font():
    """Make the C64 font usable without installing it. Returns the family name or None.
    Must run before the Tk root window exists."""
    path = find_c64_font_file()
    if path and IS_WINDOWS:
        try:
            import ctypes
            FR_PRIVATE = 0x10             # only this program sees it - nothing is installed
            ctypes.windll.gdi32.AddFontResourceExW(path, FR_PRIVATE, 0)
        except Exception:  # noqa: BLE001
            pass
    elif path and IS_MAC:
        try:
            # Core Text "process" scope: only this program sees it - nothing is installed
            import ctypes
            cf = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            ct = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreText.framework/CoreText")
            cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
            cf.CFURLCreateFromFileSystemRepresentation.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
            ct.CTFontManagerRegisterFontsForURL.restype = ctypes.c_bool
            ct.CTFontManagerRegisterFontsForURL.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
            raw = os.fsencode(path)
            url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False)
            if url:
                ct.CTFontManagerRegisterFontsForURL(url, 1, None)    # kCTFontManagerScopeProcess
                cf.CFRelease.argtypes = [ctypes.c_void_p]
                cf.CFRelease(url)
        except Exception:  # noqa: BLE001
            pass
    return path


# ======================================================================
#  File viewer (F3) - text, PETSCII, C64 BASIC and hex
# ======================================================================
VIEW_LIMIT = 8 * 1024 * 1024      # bytes read for viewing
HEX_LIMIT = 1024 * 1024           # bytes shown in hex mode
CBM_EXTS = (".prg", ".seq", ".usr", ".rel", ".p00", ".s00", ".u00")


def looks_binary(data):
    sample = data[:4096]
    if not sample:
        return False
    if sample.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        return False
    ctrl = sum(1 for b in sample if b < 9 or 13 < b < 32)
    return b"\x00" in sample or ctrl > len(sample) // 20


def decode_text(data):
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "UTF-16"
        except UnicodeDecodeError:
            pass
    try:
        txt, enc = data.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        txt, enc = data.decode("cp1252", errors="replace"), "Windows-1252"
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    txt = "".join(c if (c >= " " or c in "\n\t") else "·" for c in txt)
    return txt, enc


def hex_dump(data):
    lines = []
    for off in range(0, min(len(data), HEX_LIMIT), 16):
        chunk = data[off:off + 16]
        hx = " ".join(f"{b:02X}" for b in chunk[:8]) + "  " + " ".join(f"{b:02X}" for b in chunk[8:])
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:08X}  {hx:<49} {asc}")
    return "\n".join(lines)


class Viewer(tk.Frame):
    """Full-window viewer laid over the panels (like Norton Commander's F3)."""

    def __init__(self, app, name, data, total, cbm=False):
        super().__init__(app.root, bg=NAVY)
        self.app, self.name, self.data, self.total = app, name, data, total
        self.wrap = False
        self.lower = False
        self.search_term = ""
        low = name.lower()
        cbm = cbm or low.endswith(CBM_EXTS)
        basic = m65cbm.looks_like_basic_v2(data)
        self.modes = (["BASIC"] if basic else []) + (["PETSCII"] if cbm else []) + ["Text", "Hex"]
        if basic:
            self.mode = "BASIC"
        elif cbm and low.endswith((".prg", ".p00")):
            self.mode = "Hex"                       # machine code
        elif cbm:
            self.mode = "PETSCII"
            self.lower = True                       # text files read best in lower/upper case
        elif looks_binary(data):
            self.mode = "Hex"
        else:
            self.mode = "Text"
        f = app.font
        self.titlebar = tk.Label(self, text="", bg=DCYAN, fg=BLACK, font=f, anchor="w")
        self.titlebar.pack(fill="x")
        self.text = tk.Text(self, font=f, bg=NAVY, fg=CYAN, bd=0, highlightthickness=0, wrap="none",
                            insertwidth=0, selectbackground=DCYAN, selectforeground=BLACK, padx=4,
                            cursor="arrow")
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("hit", background=YELLOW, foreground=BLACK)
        self.text.tag_configure("note", foreground=YELLOW)
        self.text.tag_configure("rev", background=C64_LIGHT_BLUE, foreground=C64_BLUE)
        bar = tk.Frame(self, bg=BLACK)
        bar.pack(fill="x", side="bottom")
        keys = [("1", "", None), ("2", "Wrap", self.toggle_wrap), ("3", "Quit", self.close),
                ("4", "Mode", self.next_mode), ("5", "", None), ("6", "", None),
                ("7", "Search", self.search), ("8", "Charset", self.toggle_charset), ("9", "", None),
                ("10", "Quit", self.close)]
        for i, (n, label, fn) in enumerate(keys):
            bar.columnconfigure(i, weight=1, uniform="k")
            cell = tk.Frame(bar, bg=BLACK)
            cell.grid(row=0, column=i, sticky="ew")
            a = tk.Label(cell, text=n, bg=BLACK, fg=WHITE, font=f)
            b = tk.Label(cell, text=label, bg=DCYAN, fg=BLACK, font=f, anchor="w")
            a.pack(side="left")
            b.pack(side="left", fill="x", expand=True, padx=(0, 4))
            if fn:
                for w in (cell, a, b):
                    w.bind("<Button-1>", lambda e, fn=fn: fn())
        self.text.bind("<Key>", self.on_key)
        self.text.bind("<Button-1>", lambda e: self.text.focus_set())
        self.place(x=0, y=0, relwidth=1, relheight=1)
        self.lift()
        self.render()
        self.text.focus_force()

    # -------------------------------------------------------------- content
    def c64_mode(self):
        return self.mode in ("BASIC", "PETSCII") and bool(self.app.c64_font)

    def c64_base(self):
        return C64_PUA_LOWER if self.lower else C64_PUA_UPPER

    def content(self):
        note = ""
        if len(self.data) < self.total:
            note = f"[Showing the first {len(self.data):,} of {self.total:,} bytes]\n\n"
        if self.c64_mode():                      # real C64 characters (runs of text, reversed?)
            if self.mode == "BASIC":
                return note, m65cbm.basic_v2_screen_runs(self.data, self.c64_base())
            return note, m65cbm.petscii_screen_runs(self.data, self.c64_base())
        if self.mode == "BASIC":
            return note, m65cbm.detokenize_basic_v2(self.data, lower=self.lower)
        if self.mode == "PETSCII":
            return note, m65cbm.petscii_text(self.data, lower=self.lower)
        if self.mode == "Hex":
            if len(self.data) > HEX_LIMIT:
                note = f"[Hex view shows the first {HEX_LIMIT:,} of {self.total:,} bytes]\n\n"
            return note, hex_dump(self.data)
        txt, self.encoding = decode_text(self.data)
        return note, txt

    def render(self):
        self.encoding = ""
        note, body = self.content()
        t = self.text
        t.configure(state="normal", wrap="char" if self.wrap else "none")
        if self.c64_mode():
            t.configure(font=self.app.c64_font, bg=C64_BLUE, fg=C64_LIGHT_BLUE,
                        selectbackground=C64_LIGHT_BLUE, selectforeground=C64_BLUE,
                        spacing1=3, pady=6, padx=8)      # C64 glyphs fill the cell: a little air
        else:
            t.configure(font=self.app.font, bg=NAVY, fg=CYAN,
                        selectbackground=DCYAN, selectforeground=BLACK, spacing1=0, pady=0, padx=4)
        t.delete("1.0", "end")
        if note:
            t.insert("end", note, "note")
        if isinstance(body, list):
            args = []
            for chunk, rev in body:
                args += [chunk, ("rev",) if rev else ()]
            for i in range(0, len(args), 2000):  # insert in batches
                t.insert("end", *args[i:i + 2000])
        else:
            t.insert("end", body)
        t.configure(state="disabled")
        t.yview_moveto(0)
        self.update_title()

    def update_title(self):
        extra = ""
        if self.mode in ("BASIC", "PETSCII"):
            extra = "  lower/upper case" if self.lower else "  upper case/graphics"
            if not self.app.c64_font:
                extra += f"  (no C64 font: put {C64_FONT_FILE} next to the program)"
        elif self.mode == "Text" and self.encoding:
            extra = f"  {self.encoding}"
        lines = int(self.text.index("end-1c").split(".")[0])
        others = "/".join(m for m in self.modes if m != self.mode)
        self.titlebar.configure(text=f" {self.name}   {self.total:,} bytes   [{self.mode}{extra}]"
                                     f"   {lines:,} lines   (F4: {others})")

    # -------------------------------------------------------------- actions
    def toggle_wrap(self):
        self.wrap = not self.wrap
        self.text.configure(wrap="char" if self.wrap else "none")

    def next_mode(self):
        i = self.modes.index(self.mode)
        self.mode = self.modes[(i + 1) % len(self.modes)]
        self.render()

    def toggle_charset(self):
        if self.mode in ("BASIC", "PETSCII"):
            self.lower = not self.lower
            self.render()

    def search(self):
        term = self.app.ask_string("Search", "Search for", self.search_term)
        self.text.focus_force()
        if not term:
            return
        self.search_term = term
        t = self.text
        ranges = t.tag_ranges("hit")
        start = ranges[1] if ranges else t.index("@0,0")
        if self.c64_mode():
            pat = m65cbm.screen_search_pattern(term, self.c64_base())
            pos = t.search(pat, start, stopindex="end", regexp=True) or \
                t.search(pat, "1.0", stopindex="end", regexp=True)
        else:
            pos = t.search(term, start, stopindex="end", nocase=True) or \
                t.search(term, "1.0", stopindex="end", nocase=True)
        t.tag_remove("hit", "1.0", "end")
        if not pos:
            self.app.message("Search", f'"{term}" was not found.')
            self.text.focus_force()
            return
        end = f"{pos}+{len(term)}c"
        t.tag_add("hit", pos, end)
        t.see(pos)

    def close(self):
        if self.app.viewer is self:
            self.app.viewer = None
        self.destroy()
        self.app.restore_focus()

    def on_key(self, e):
        k = e.keysym
        t = self.text
        if k in ("Escape", "F3", "F10", "q", "Q"):
            self.close()
        elif k == "F2":
            self.toggle_wrap()
        elif k == "F4":
            self.next_mode()
        elif k == "F7":
            self.search()
        elif k == "F8":
            self.toggle_charset()
        elif k == "Up":
            t.yview_scroll(-1, "units")
        elif k == "Down":
            t.yview_scroll(1, "units")
        elif k == "Prior":
            t.yview_scroll(-1, "pages")
        elif k == "Next":
            t.yview_scroll(1, "pages")
        elif k == "Home":
            t.yview_moveto(0)
            t.xview_moveto(0)
        elif k == "End":
            t.yview_moveto(1)
        elif k == "Left":
            t.xview_scroll(-4, "units")
        elif k == "Right":
            t.xview_scroll(4, "units")
        elif (e.state & 0x4) and k.lower() in ("c", "a", "insert"):
            return None                        # let Ctrl+C / Ctrl+A copy and select
        return "break"


# ======================================================================
#  File panel
# ======================================================================
class Panel(tk.Frame):
    kind = KIND_PC

    def __init__(self, app, parent, remote):
        super().__init__(parent, bg=NAVY)
        self.app = app
        self.remote = remote              # True for MEGA65 and EXP board panels
        self.path = "/" if self.kind == KIND_MEGA65 else ""
        self.error_msg = ""
        self.entries = []
        self.marked = set()
        self.active = False
        self.loading = False
        f = app.font

        frame = tk.Frame(self, bg=CYAN, padx=1, pady=1)   # outer line
        frame.pack(fill="both", expand=True, padx=2, pady=(4, 2))
        mid = tk.Frame(frame, bg=NAVY, padx=2, pady=2)
        mid.pack(fill="both", expand=True)
        inner = tk.Frame(mid, bg=CYAN, padx=1, pady=1)    # inner line -> double border
        inner.pack(fill="both", expand=True)
        box = tk.Frame(inner, bg=NAVY)
        box.pack(fill="both", expand=True)

        trow = tk.Frame(box, bg=NAVY)
        trow.pack(fill="x")
        self.title = tk.Label(trow, text="", bg=NAVY, fg=CYAN, font=f)
        self.title.pack()
        trow.bind("<Button-1>", lambda e: self.app.set_active(self))
        self.title.bind("<Button-1>", lambda e: self.app.panel_menu(self))
        self.title.configure(cursor="hand2")
        self.colhdr = tk.Label(box, text="", bg=NAVY, fg=YELLOW, font=f, anchor="w")
        self.colhdr.pack(fill="x")
        self.lb = tk.Listbox(box, font=f, bg=NAVY, fg=CYAN, bd=0, highlightthickness=0,
                             activestyle="none", selectmode="browse", exportselection=False,
                             selectbackground=NAVY, selectforeground=CYAN)
        self.lb.pack(fill="both", expand=True)
        tk.Frame(box, bg=CYAN, height=1).pack(fill="x")
        self.status = tk.Label(box, text="", bg=NAVY, fg=CYAN, font=f, anchor="w")
        self.status.pack(fill="x")


        self.lb.bind("<<ListboxSelect>>", lambda e: self.update_status())
        self.lb.bind("<Double-Button-1>", lambda e: self.app.enter())
        self.lb.bind("<Button-1>", self._click)
        self.lb.bind("<Button-3>", self._rclick)
        self.lb.bind("<Configure>", lambda e: self.render())
        for w in (self.colhdr, self.status, box):
            w.bind("<Button-1>", lambda e: self.app.set_active(self))

    # ---------------------------------------------------------- helpers
    def chars_wide(self):
        w = self.lb.winfo_width()
        cw = (self.app.c64_font_obj.measure("0") if self.c64_view() else self.app.font_obj.measure("0")) or 8
        return max(30, w // cw - 1)

    def current(self):
        sel = self.lb.curselection()
        if not sel or not self.entries:
            return None
        i = sel[0]
        return self.entries[i] if i < len(self.entries) else None

    def selected_items(self):
        if self.marked:
            return [e for e in self.entries if e.name in self.marked and not e.up]
        cur = self.current()
        return [cur] if cur and not cur.up else []

    def set_cursor(self, idx):
        if not self.entries:
            return
        idx = max(0, min(idx, len(self.entries) - 1))
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(idx)
        self.lb.activate(idx)
        self.lb.see(idx)
        self.update_status()

    def cursor(self):
        sel = self.lb.curselection()
        return sel[0] if sel else 0

    def page(self):
        try:
            lh = self.app.font_obj.metrics("linespace") or 16
            return max(1, self.lb.winfo_height() // lh - 1)
        except Exception:
            return 10

    def _click(self, e):
        self.app.set_active(self)
        idx = self.lb.nearest(e.y)
        self.set_cursor(idx)
        return "break"

    def _rclick(self, e):
        self.app.set_active(self)
        idx = self.lb.nearest(e.y)
        self.set_cursor(idx)
        self.toggle_mark(advance=False)
        return "break"

    # ---------------------------------------------------------- display
    def set_active(self, on):
        self.active = on
        if on:
            self.lb.configure(selectbackground=DCYAN, selectforeground=BLACK)
            self.title.configure(bg=DCYAN, fg=BLACK)
        else:
            self.lb.configure(selectbackground=NAVY, selectforeground=CYAN)
            self.title.configure(bg=NAVY, fg=CYAN)
        self.render(keep=True)

    def c64_view(self):
        """Disk image directories are drawn in the C64 font when it's available."""
        return bool(getattr(self, "disk", None)) and bool(self.app.c64_font)

    def c64_base(self):
        return C64_PUA_LOWER if getattr(self, "disk_lower", False) else C64_PUA_UPPER

    def _apply_fonts(self):
        c64 = self.c64_view()
        if getattr(self, "_c64_fonts", False) != c64:
            self._c64_fonts = c64
            want = self.app.c64_font if c64 else self.app.font
            for w in (self.lb, self.colhdr, self.status):
                w.configure(font=want)

    def render(self, keep=True):
        cur = self.cursor() if keep else 0
        self._apply_fonts()
        if self.active:
            self.app.update_keybar()
        if self.c64_view():
            return self._render_c64(cur)
        W = self.chars_wide()
        namew = W - 11 - 17 - 2
        disk = getattr(self, "disk", None)
        if disk:
            self.colhdr.configure(text=f"{'Name'.center(namew)}│{'Blocks'.center(11)}│{'Type'.center(16)}")
        else:
            self.colhdr.configure(text=f"{'Name'.center(namew)}│{'Size'.center(11)}│{'Date'.center(16)}")
        shown = self.path or "Drives"
        origin = getattr(self, "origin", None)
        if self.kind == KIND_MEGA65:
            shown = "MEGA65:" + (self.path or "/")
        elif self.kind == KIND_BOARD:
            shown = "EXP:/" + self.path
        elif getattr(self, "zip_path", None):
            base = origin["label"] if origin else self.zip_path
            shown = base + (os.sep + self.zip_inner.replace("/", os.sep) if self.zip_inner else "")
        elif disk:
            base = origin["label"] if origin else self.disk_path
            shown = base + "".join(f"/{part.name}" for part in self.disk_stack) + \
                f"   {self.disk_free} blocks free"
        maxt = W - 4
        if len(shown) > maxt:
            shown = "…" + shown[-(maxt - 1):]
        self.title.configure(text=f" {shown} ")
        self.lb.delete(0, "end")
        msg = ""
        if not self.entries and self.kind == KIND_MEGA65 and not self.app.ftp_connected():
            msg = "Not connected - press F4 to connect"
        elif not self.entries and self.kind == KIND_BOARD and self.error_msg:
            msg = self.error_msg
        if msg:
            self.lb.insert("end", "")
            self.lb.insert("end", msg.center(W))
            self.lb.itemconfig(1, fg=YELLOW, selectforeground=YELLOW)
            self.status.configure(text="")
            return
        for i, e in enumerate(self.entries):
            name = e.name
            if e.ref is not None and getattr(self, "disk_lower", False):
                name = m65cbm.petscii_name(e.ref.raw_name, lower=True) + e.name[len(e.ref.name):]
            if len(name) > namew:
                name = name[:namew - 1] + "»"
            if e.up:
                size = "►UP--DIR◄"
            elif e.blocks is not None:
                size = str(e.blocks)
            elif e.is_dir:
                size = "►SUB-DIR◄"
            else:
                size = fmt_size(e.size)
            date = e.ftype if e.blocks is not None else (e.date[:16] if e.date else "")
            self.lb.insert("end", f"{name:<{namew}}│{size:>11}│{date:<16}")
            if e.name in self.marked:
                self.lb.itemconfig(i, fg=YELLOW, selectforeground=YELLOW)
            elif e.is_dir:
                self.lb.itemconfig(i, fg=WHITE, selectforeground=WHITE if not self.active else BLACK)
        if self.entries:
            self.set_cursor(cur)
        else:
            self.status.configure(text="")

    def _render_c64(self, cur):
        """The directory of a disk image in real C64 characters (like LOAD"$",8)."""
        base = self.c64_base()
        W = self.chars_wide()
        bw, tw = 6, 5
        namew = max(8, W - bw - tw - 2)
        bar = chr(base + 0x5D)

        def a(t):
            return m65cbm.ascii_screen_string(t, base)

        self.colhdr.configure(text=a("NAME".center(namew)) + bar + a("BLOCKS".center(bw)) + bar + a("TYPE".center(tw)))
        origin = getattr(self, "origin", None)
        shown = (origin["label"] if origin else self.disk_path) + "".join(f"/{p.name}" for p in self.disk_stack)
        free = f"   {self.disk_free} blocks free"
        maxt = max(10, int(self.lb.winfo_width() / (self.app.font_obj.measure("0") or 8)) - 4 - len(free))
        if len(shown) > maxt:
            shown = "…" + shown[-(maxt - 1):]
        self.title.configure(text=f" {shown}{free} ")
        self.lb.delete(0, "end")
        for i, e in enumerate(self.entries):
            if e.up:
                row = a("..".ljust(namew)) + bar + a("UP".rjust(bw)) + bar + a("".ljust(tw))
            else:
                name = m65cbm.screen_string(e.ref.raw_name, base)
                suffix = e.name[len(e.ref.name):]           # " #2" for a repeated name
                name = (name + a(suffix))[:namew]
                row = name.ljust(namew) + bar + a(str(e.blocks).rjust(bw)) + bar + a(e.ftype.ljust(tw)[:tw])
            self.lb.insert("end", row)
            if e.name in self.marked:
                self.lb.itemconfig(i, fg=YELLOW, selectforeground=YELLOW)
            elif e.is_dir:
                self.lb.itemconfig(i, fg=WHITE, selectforeground=WHITE if not self.active else BLACK)
        if self.entries:
            self.set_cursor(cur)

    def update_status(self):
        if self.c64_view():
            return self._status_c64()
        W = self.chars_wide()
        if self.marked:
            n = len(self.marked)
            tot = sum(e.size for e in self.entries if e.name in self.marked)
            txt = f" {tot:,} bytes in {n} selected item{'s' if n != 1 else ''}"
            self.status.configure(text=txt[:W], fg=YELLOW)
            return
        e = self.current()
        if not e:
            self.status.configure(text="")
            return
        disk = getattr(self, "disk", None)
        if disk and (e.up or not self.entries[1:]):
            free = self.disk_free
            self.status.configure(text=f" {disk.label}   {free} BLOCKS FREE."[:W], fg=CYAN)
            return
        txt = " " + e.name
        if e.blocks is not None:
            txt += f"  ({e.blocks} blocks, {e.ftype})"
        elif not e.up and not e.is_dir:
            txt += f"  ({e.size:,} bytes)"
        if e.title:
            txt += f"  {e.title}"
        self.status.configure(text=txt[:W], fg=CYAN)

    def _status_c64(self):
        base = self.c64_base()
        W = self.chars_wide()

        def a(t):
            return m65cbm.ascii_screen_string(t, base)
        if self.marked:
            n = len(self.marked)
            blocks = sum(e.blocks or 0 for e in self.entries if e.name in self.marked)
            self.status.configure(text=a(f" {blocks} BLOCKS IN {n} FILE{'S' if n != 1 else ''}")[:W], fg=YELLOW)
            return
        e = self.current()
        if not e:
            self.status.configure(text="")
            return
        d = self.disk
        if e.up or not self.entries[1:]:
            label = (a(' "') + m65cbm.screen_string(d.raw_disk_name, base) + a('" ') +
                     m65cbm.screen_string(d.raw_disk_id, base) + a(" ") +
                     m65cbm.screen_string(d.raw_dos_type, base))
            self.status.configure(text=label[:W], fg=CYAN)
            return
        txt = a(' "') + m65cbm.screen_string(e.ref.raw_name, base) + a(f'"  {e.blocks} {e.ftype}')
        self.status.configure(text=txt[:W], fg=CYAN)

    def toggle_charset(self):
        """F8 in a disk image: upper case/graphics <-> lower/upper case."""
        self.disk_lower = not getattr(self, "disk_lower", False)
        self.render()

    def toggle_mark(self, advance=True):
        e = self.current()
        if e and not e.up:
            self.marked.symmetric_difference_update({e.name})
        i = self.cursor()
        self.render()
        self.set_cursor(i + 1 if advance else i)

    def mark_all(self, on=True):
        self.marked = {e.name for e in self.entries if not e.up} if on else set()
        self.render()

    def goto_name(self, name):
        for i, e in enumerate(self.entries):
            if e.name.lower() == (name or "").lower():
                self.set_cursor(i)
                return
        self.set_cursor(0)

    def set_entries(self, path, entries, focus_name=None, keep_order=False):
        old_path = self.path
        self.path = path
        if not keep_order:
            entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        if self.has_parent():
            entries.insert(0, Entry("..", True, up=True))
        self.entries = entries
        if old_path != path:
            self.marked.clear()
        else:
            names = {e.name for e in entries}
            self.marked &= names
        self.render(keep=False)
        if focus_name:
            self.goto_name(focus_name)

    # ------------------------------------------------- to be overridden
    def has_parent(self):
        raise NotImplementedError

    def load(self, path, focus_name=None):
        raise NotImplementedError

    def refresh(self):
        cur = self.current()
        self.load(self.path, cur.name if cur else None)


class LocalPanel(Panel):
    def __init__(self, app, parent):
        super().__init__(app, parent, remote=False)
        self.zip_path = None      # set while showing the inside of a .zip
        self.zip_inner = ""       # folder inside the zip ("" = its root)
        self.disk = None          # DiskImage while showing the inside of a .d64/.d71/.d81
        self.disk_path = None
        self.disk_stack = []      # 1581 partitions entered (DirEntry list)
        self.disk_free = 0
        self.origin = None        # set when the container was fetched from the MEGA65/EXP board

    @property
    def in_container(self):
        return bool(self.zip_path or self.disk_path)

    def load_disk(self, path, stack=None, focus_name=None):
        stack = list(stack or [])
        try:
            img = self.disk if (self.disk and self.disk_path == path) else DiskImage(path)
            dents = img.list(stack[-1] if stack else None)
        except (DiskImageError, OSError) as e:
            self.app.show_error(f"Cannot open the disk image:\n{path}\n\n{e}")
            return False
        ents, seen = [], {}
        for d in dents:
            name = d.name or "(no name)"
            n = seen.get(name.lower(), 0) + 1        # C64 directories can repeat names
            seen[name.lower()] = n
            if n > 1:
                name = f"{name} #{n}"
            is_dir = img.partition_dir_start(d) is not None
            ents.append(Entry(name, is_dir, d.blocks * 254, "", ref=d, blocks=d.blocks, ftype=d.type_name))
        if (self.disk_path, [x.slot for x in self.disk_stack]) != (path, [x.slot for x in stack]):
            self.marked.clear()
        self.zip_path, self.zip_inner = None, ""
        self.disk, self.disk_path, self.disk_stack = img, path, stack
        self.disk_free = img.blocks_free()
        self.set_entries(os.path.dirname(path), ents, focus_name, keep_order=True)
        return True

    def leave_container(self):
        """Back to the folder that holds the zip / disk image (or to the MEGA65 / EXP
        panel it was fetched from). Returns the panel that is now showing."""
        name = os.path.basename(self.zip_path or self.disk_path or "")
        origin = self.origin
        self.origin = None
        self.zip_path, self.zip_inner = None, ""
        self.disk, self.disk_path, self.disk_stack = None, None, []
        if origin:
            shutil.rmtree(origin["tmpdir"], ignore_errors=True)
            self.path = origin["pc_path"]
            self.entries = []
            return origin
        self.load(self.path, name)
        return None

    def load_zip(self, zip_path, inner="", focus_name=None):
        try:
            ents = list_zip(zip_path, inner)
        except (zipfile.BadZipFile, OSError, RuntimeError) as e:
            self.app.show_error(f"Cannot open the ZIP file:\n{zip_path}\n\n{e}")
            return False
        if (self.zip_path, self.zip_inner) != (zip_path, inner):
            self.marked.clear()
        self.disk, self.disk_path, self.disk_stack = None, None, []
        self.zip_path, self.zip_inner = zip_path, inner
        self.set_entries(os.path.dirname(zip_path), ents, focus_name)
        return True

    def zip_member(self, name):
        return (self.zip_inner + "/" + name) if self.zip_inner else name

    def refresh(self):
        if self.zip_path:
            cur = self.current()
            self.load_zip(self.zip_path, self.zip_inner, cur.name if cur else None)
        elif self.disk_path:
            cur = self.current()
            self.disk = None              # re-read the image from disk
            self.load_disk(self.disk_path, self.disk_stack, cur.name if cur else None)
        else:
            super().refresh()

    def has_parent(self):
        if self.zip_path or self.disk_path:
            return True
        if not self.path:
            return False
        if IS_WINDOWS:
            return True   # parent of a drive root = drive list
        return self.path != "/"

    def load(self, path, focus_name=None):
        if self.origin:                       # a fetched container is being abandoned
            shutil.rmtree(self.origin["tmpdir"], ignore_errors=True)
            self.origin = None
        self.zip_path, self.zip_inner = None, ""
        self.disk, self.disk_path, self.disk_stack = None, None, []
        if path == "" and IS_WINDOWS:
            drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
            self.set_entries("", [Entry(d, True) for d in drives], focus_name)
            return True
        try:
            path = os.path.abspath(path)
            ents = []
            with os.scandir(path) as it:
                for de in it:
                    try:
                        isdir = de.is_dir()
                        st = de.stat()
                        date = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                        ents.append(Entry(de.name, isdir, 0 if isdir else st.st_size, date))
                    except OSError:
                        continue
        except OSError as e:
            self.app.show_error(f"Cannot read folder:\n{path}\n\n{e.strerror or e}")
            return False
        self.set_entries(path, ents, focus_name)
        return True

    def parent_path(self):
        if IS_WINDOWS and os.path.dirname(self.path) == self.path:
            return ""   # drive list
        return os.path.dirname(self.path)

    def child_path(self, name):
        return name if self.path == "" else os.path.join(self.path, name)


class RemotePanel(Panel):
    kind = KIND_MEGA65

    def __init__(self, app, parent):
        super().__init__(app, parent, remote=True)

    def has_parent(self):
        return self.path not in ("", "/")

    def load(self, path, focus_name=None, on_fail=None):
        if not self.app.ftp_connected():
            self.entries = []
            self.render()
            return
        self.loading = True
        self.status.configure(text=" Reading MEGA65 SD card...", fg=YELLOW)

        def job():
            return self.app.ftp.listdir(path)

        def done(ents):
            self.loading = False
            self.set_entries(path, [Entry(e.name, e.is_dir, e.size, e.date) for e in ents], focus_name)

        def err(e):
            self.loading = False
            self.update_status()
            self.app.show_error(e)
            if on_fail:
                on_fail()

        self.app.worker.submit(job, done, err)

    def parent_path(self):
        return remote_parent(self.path)

    def child_path(self, name):
        return remote_join(self.path, name)


class BoardPanel(Panel):
    """The Expansion Board Integrated JTAG's own SD card, over its web interface."""
    kind = KIND_BOARD

    def __init__(self, app, parent):
        super().__init__(app, parent, remote=True)
        self.path = ""

    def has_parent(self):
        return self.path != ""

    def load(self, path, focus_name=None, on_fail=None):
        try:
            board = self.app.board()
        except BoardError as e:
            self.error_msg = "Expansion board not set up - see Setup (F9)"
            self.entries = []
            self.render()
            self.app.show_error(e)
            return
        self.loading = True
        self.status.configure(text=" Reading EXP board SD card...", fg=YELLOW)

        def done(ents):
            self.loading = False
            self.error_msg = ""
            self.set_entries(path, [Entry(e.name, e.is_dir, e.size, e.date, title=e.title) for e in ents],
                             focus_name)
            if board.last_list_incomplete:
                self.app.message("EXP board", "The expansion board only sent part of this folder's listing,\n"
                                              "so some entries may be missing. (A known firmware limit:\n"
                                              "listing pages over about 11.5 KB get cut short.)")

        def err(e):
            self.loading = False
            if not self.entries:
                self.error_msg = "EXP board not reachable - press F2 to retry"
                self.render()
            self.update_status()
            self.app.show_error(e)
            if on_fail:
                on_fail()

        self.app.worker.submit(lambda: board.list(path), done, err)

    def parent_path(self):
        return board_parent(self.path)

    def child_path(self, name):
        return board_join(self.path, name)


# ======================================================================
#  Main application
# ======================================================================
class App:
    def __init__(self):
        self.cfg = load_config()
        c64_font_path = load_c64_font()
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.configure(bg=NAVY)
        self.root.geometry("1200x720")
        self.root.minsize(800, 400)
        self.font = pick_font(int(self.cfg.get("font_size", 11)))
        self.font_obj = tkfont.Font(family=self.font[0], size=self.font[1])
        self.c64_font = None
        if c64_font_path and C64_FONT_FAMILY in tkfont.families():
            px = 16 if int(self.cfg.get("font_size", 11)) < 14 else 24   # multiples of 8 stay crisp
            self.c64_font = (C64_FONT_FAMILY, -px)
        self.c64_font_obj = tkfont.Font(font=self.c64_font) if self.c64_font else self.font_obj
        self.log = collections.deque(maxlen=3000)
        self._ui_q = queue.Queue()
        self.ftp = None
        self.worker = Worker(self)
        self.busy = False
        self.dialogs = []
        self.viewer = None
        if "exp_board" not in self.cfg:          # first run: switch it on if a board is configured
            self.cfg["exp_board"] = bool(self.board_info()[0])

        # top bar
        self.topbar = tk.Label(self.root, text="", bg=DCYAN, fg=BLACK, font=self.font, anchor="w")
        self.topbar.pack(fill="x")

        panes = tk.Frame(self.root, bg=NAVY)
        panes.pack(fill="both", expand=True)
        panes.columnconfigure(0, weight=1, uniform="p")
        panes.columnconfigure(1, weight=1, uniform="p")
        panes.rowconfigure(0, weight=1)
        self.panes = panes
        self.slots = [{}, {}]            # per side: kind -> panel (made when first needed)
        self.left = self.panel_of(0, KIND_PC)
        self.right = self.panel_of(1, KIND_MEGA65)
        self.left.grid(row=0, column=0, sticky="nsew")
        self.right.grid(row=0, column=1, sticky="nsew")

        self._build_keybar()
        self._bind_keys()

        self.active = self.left
        self.root.after(50, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.report_callback_exception = self._crash
        # a borderless box can end up behind the main window (e.g. after the Windows
        # file picker) while still holding the keyboard - bring it back to the front
        self.root.bind("<FocusIn>", self._on_root_focus, add="+")
        self.root.bind("<Button-1>", self._on_root_focus, add="+")
        self._logfile("---- start " + time.strftime("%Y-%m-%d %H:%M:%S") + f" v{APP_VERSION}")

        start = self.cfg.get("local_path") or os.path.expanduser("~")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        self.left.load(start)
        self.right.render()
        for side in (0, 1):
            mode = self.cfg.get("left_mode" if side == 0 else "right_mode") or (KIND_PC if side == 0 else KIND_MEGA65)
            if mode == KIND_PC and side == 1:
                rp = self.cfg.get("right_local_path") or start
                self.show_pc(1, rp if os.path.isdir(rp) else start, activate=False)
            elif mode == KIND_MEGA65 and side == 0:
                self.show_kind(0, KIND_MEGA65, activate=False, autoconnect=False)
            elif mode == KIND_BOARD and self.exp_enabled():
                self.show_kind(side, KIND_BOARD, activate=False)
            elif mode == KIND_BOARD and side == 1:
                rp = self.cfg.get("right_local_path") or start
                self.show_pc(1, rp if os.path.isdir(rp) else start, activate=False)
        self.set_active(self.left)
        self.update_topbar()

        if not self.cfg.get("exe_path"):
            self.root.after(300, self.first_run)
        elif self.cfg.get("connect_on_start"):
            self.root.after(300, self.connect)

    # ----------------------------------------------------- plumbing
    def post(self, fn, *args):
        self._ui_q.put((fn, args))

    def _poll(self):
        try:
            while True:
                fn, args = self._ui_q.get_nowait()
                try:
                    fn(*args)
                except Exception as e:  # noqa: BLE001
                    self.log_text(f"[ui error] {e}\n")
        except queue.Empty:
            pass
        self._poll_n = getattr(self, "_poll_n", 0) + 1
        if self.dialogs and self._poll_n % 10 == 0:      # ~3 times a second while a box is open
            self._keep_dialog_on_top()
        self.root.after(30, self._poll)

    def _keep_dialog_on_top(self):
        """A message box must never end up hidden behind the main window (e.g. after the
        Windows Firewall prompt, or a click on the main window) - it still holds the
        keyboard and mouse, so the program would look frozen.

        Rule: while a box is open, the keyboard focus belongs in that box. If the focus
        is somewhere else in this program, the box has been covered: bring it back.
        If the focus is in another program (focus_get() is None) leave everything alone.
        (Tk's window stacking list can't be used for this: on Windows it doesn't include
        borderless boxes.)"""
        if getattr(self, "native_dialog_open", False):
            return
        top = self.dialogs[-1]
        try:
            if not top.winfo_exists():
                return
            if WIN32.ok:
                # On Windows, Tk can still believe the keyboard is in the box while
                # Windows shows (and types into) the main window, so ask Windows.
                why = WIN32.box_hidden(top, self.root)
                if why:
                    n = getattr(self, "_raise_logged", 0)
                    if n < 5:
                        self._raise_logged = n + 1
                        self._logfile(f"[dialog] brought box back to the front ({why})")
                    self.raise_dialog()
                return
            try:
                f = self.root.focus_get()
            except KeyError:          # focus in a native window, e.g. the Windows file picker
                return
            if f is None:
                return
            if str(f) == str(top) or str(f).startswith(str(top) + "."):
                return
            self.raise_dialog()
        except tk.TclError:
            pass

    def log_text(self, text):
        for line in text.replace("\r", "\n").splitlines():
            if line.strip():
                self.log.append(line)
                self._logfile(line)

    @staticmethod
    def _logfile(line):
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _crash(self, exc, val, tb):
        import traceback
        text = "".join(traceback.format_exception(exc, val, tb))
        self._logfile("[PROGRAM ERROR]\n" + text)
        self.busy = False
        try:
            self.message("Program error", "Something went wrong (details saved in m65sd.log):\n\n" +
                         "\n".join(text.strip().splitlines()[-8:]), style="red")
        except Exception:
            pass

    def ftp_connected(self):
        return self.ftp is not None and self.ftp.connected

    def raise_dialog(self):
        if not self.dialogs:
            return
        top = self.dialogs[-1]
        try:
            top.lift()
            top.attributes("-topmost", True)
            self.root.after(300, lambda: top.winfo_exists() and top.attributes("-topmost", False))
            top.grab_set()
            WIN32.set_owner(top, self.root)
            WIN32.bring_up(top)
            w = getattr(top, "focus_widget", top)
            (w if w.winfo_exists() else top).focus_force()
        except tk.TclError:
            pass

    def _on_root_focus(self, e=None):
        if self.dialogs and e is not None:
            try:
                if e.widget.winfo_toplevel() is self.root:
                    self.root.after(20, lambda: self.dialogs and self._keep_dialog_on_top())
            except (tk.TclError, AttributeError):
                pass

    def restore_focus(self):
        if self.dialogs:
            return
        try:
            self.root.focus_force()
            self.active.lb.focus_force()
        except tk.TclError:
            pass

    def focus_active(self):
        try:
            self.active.lb.focus_set()
        except Exception:
            pass

    def in_disk_view(self, p=None):
        p = p or self.active
        return p.kind == KIND_PC and bool(getattr(p, "disk_path", None))

    def f8(self):
        """F8 = Delete, except inside a disk image (read only) where it switches the
        C64 character set, like the viewer's F8."""
        if self.in_disk_view():
            self.active.toggle_charset()
        else:
            self.delete()

    def update_keybar(self):
        lbl = getattr(self, "f8_label", None)
        if lbl is not None:
            lbl.full_text = "Charset" if self.in_disk_view() else "Del"
            lbl.configure(text=lbl.full_text)
            self.fit_keybar()

    def set_active(self, panel):
        self.active = panel
        other = self.right if panel is self.left else self.left
        other.set_active(False)
        panel.set_active(True)
        self.update_keybar()
        self.focus_active()

    def update_topbar(self):
        if self.ftp_connected():
            how = ("image/device " + os.path.basename(self.cfg["device"])) if self.cfg.get("device") else \
                  (self.cfg["ip_iface"] if self.cfg.get("ip_iface") else "Ethernet")
            st = f"● MEGA65 connected ({how})"
        else:
            st = "○ MEGA65 not connected"
        left = f"  {APP_NAME}"
        right = f"{st}   Ctrl+H Help  "
        W = max(40, self.root.winfo_width() // (self.font_obj.measure("0") or 8))
        self.topbar.configure(text=left + " " * max(1, W - len(left) - len(right) - 1) + right)

    # ----------------------------------------------------- key bar
    def _build_keybar(self):
        bar = tk.Frame(self.root, bg=BLACK)
        bar.pack(fill="x", side="bottom")
        keys = [("F1", "Drive", self.drive_menu), ("F2", "Refresh", self.refresh), ("F3", "View", self.view),
                ("F4", "Connect", self.toggle_link), ("F5", "Copy", self.copy), ("F6", "Ren", self.rename),
                ("F7", "Mkdir", self.mkdir), ("F8", "Del", self.f8), ("F9", "Setup", self.setup),
                ("F10", "Quit", self.quit)]
        self.link_label = None
        self.key_labels = []
        bar.bind("<Configure>", lambda e: self.fit_keybar())
        for i, (n, label, fn) in enumerate(keys):
            bar.columnconfigure(i, weight=1, uniform="k")
            cell = tk.Frame(bar, bg=BLACK)
            cell.grid(row=0, column=i, sticky="ew")
            a = tk.Label(cell, text=n, bg=BLACK, fg=WHITE, font=self.font)
            b = tk.Label(cell, text=label, bg=DCYAN, fg=BLACK, font=self.font, anchor="w")
            a.pack(side="left")
            b.pack(side="left", fill="x", expand=True, padx=(0, 4))
            for w in (cell, a, b):
                w.bind("<Button-1>", lambda e, fn=fn: fn())
            self.key_labels.append(b)
            if label == "Connect":
                self.link_label = b
            if n == "F8":
                self.f8_label = b

    # short forms for a narrow window
    KEY_SHORT = {"Drive": "Drv", "Refresh": "Rfsh", "View": "View", "Connect": "Conn",
                 "Copy": "Copy", "Ren": "Ren", "Mkdir": "MkD", "Del": "Del", "Charset": "Chr",
                 "Setup": "Set", "Quit": "Quit", "Help": "Help"}

    def fit_keybar(self):
        """Use the short key names when the window is too narrow for the full ones."""
        for b in getattr(self, "key_labels", []):
            full = getattr(b, "full_text", None) or b.cget("text")
            b.full_text = full
            room = b.master.winfo_width() - b.master.winfo_children()[0].winfo_width() - 4
            want = full if room <= 1 or self.font_obj.measure(full) <= room else self.KEY_SHORT.get(full, full)
            if b.cget("text") != want:
                b.configure(text=want)

    def _bind_keys(self):
        r = self.root
        r.bind("<Configure>", lambda e: self.update_topbar() if e.widget is r else None)

    def on_key(self, e):
        k = e.keysym
        ctrl = bool(e.state & 0x4)
        alt = bool(e.state & 0x20000) or bool(e.state & 0x8) and not IS_WINDOWS
        if IS_MAC and e.state & 0x10:            # the Option key on a Mac
            alt = True
        p = self.active
        handlers = {
            "F1": self.drive_menu, "F2": self.refresh, "F3": self.view, "F4": self.toggle_link,
            "F5": self.copy, "F6": self.rename, "F7": self.mkdir, "F8": self.f8,
            "Delete": self.delete, "F9": self.setup, "F10": self.quit,
        }
        if alt and k == "F1":
            self.panel_menu(self.left)
            return "break"
        if alt and k == "F2":
            self.panel_menu(self.right)
            return "break"
        if k in handlers:
            handlers[k]()
            return "break"
        if ctrl and k.lower() == "r":
            self.refresh()
            return "break"
        if ctrl and k.lower() == "l":
            self.show_log()
            return "break"
        if (ctrl and k.lower() == "h") or (k == "question" and not ctrl):
            self.help()
            return "break"
        if ctrl and k.lower() == "a":
            p.mark_all(True)
            return "break"
        if k == "Tab" or k == "ISO_Left_Tab":
            self.set_active(self.right if p is self.left else self.left)
            return "break"
        if k in ("Return", "KP_Enter"):
            self.enter()
            return "break"
        if k == "BackSpace":
            self.go_up()
            return "break"
        if k in ("Insert", "space"):
            p.toggle_mark()
            return "break"
        if k in ("plus", "KP_Add"):
            p.mark_all(True)
            return "break"
        if k in ("minus", "KP_Subtract"):
            p.mark_all(False)
            return "break"
        if k == "Up":
            p.set_cursor(p.cursor() - 1)
            return "break"
        if k == "Down":
            p.set_cursor(p.cursor() + 1)
            return "break"
        if k == "Prior":
            p.set_cursor(p.cursor() - p.page())
            return "break"
        if k == "Next":
            p.set_cursor(p.cursor() + p.page())
            return "break"
        if k == "Home":
            p.set_cursor(0)
            return "break"
        if k == "End":
            p.set_cursor(len(p.entries) - 1)
            return "break"
        if k in ("Left", "Right"):
            self.set_active(self.left if k == "Left" else self.right)
            return "break"
        if k in ("x", "X") and not ctrl:
            cur = p.current()
            if cur and not cur.is_dir and cur.name.lower().endswith(".zip") and \
                    not (p.kind == KIND_PC and p.in_container):
                self.extract_all(p, cur)
                return "break"
        # quick search: jump to first entry starting with typed letter
        if e.char and e.char.isprintable() and not ctrl:
            ch = e.char.lower()
            n = len(p.entries)
            start = p.cursor() + 1
            for j in range(n):
                i = (start + j) % n
                if p.entries[i].name.lower().startswith(ch):
                    p.set_cursor(i)
                    break
            return "break"
        return None

    # ----------------------------------------------------- dialogs
    def message(self, title, text, buttons=("OK",), style="grey", default=0):
        d = NCDialog(self, title, style)
        d.label(text, justify="left")
        d.add_buttons(list(buttons), default)
        return d.run()

    def ask_string(self, title, prompt, value=""):
        d = NCDialog(self, title)
        d.label(prompt, anchor="w", justify="left")
        var, ent = d.entry(value)
        d.add_buttons(["OK", "Cancel"])
        ent.bind("<Return>", lambda e: d.close(0))
        ent.select_range(0, "end")
        ent.icursor("end")
        res = d.run(focus=ent)
        return var.get().strip() if res == 0 else None

    def show_error(self, e):
        if isinstance(e, NotConnected):
            self.on_disconnected()
        text = str(e).strip() or e.__class__.__name__
        self.message("Error", wrap_lines(text, max_lines=18), style="red")

    def need_idle(self):
        if self.busy or self.left.loading or self.right.loading:
            return False
        return True

    # ----------------------------------------------------- navigation
    def enter(self):
        p = self.active
        e = p.current()
        if not e or p.loading:
            return
        if e.up:
            self.go_up()
            return
        in_zip = p.kind == KIND_PC and p.zip_path
        if in_zip:
            if e.is_dir:
                p.load_zip(p.zip_path, p.zip_member(e.name))
            elif e.name.lower().endswith(LAUNCH_EXTS):
                self.launch(p, e)
            else:
                self.view()
            return
        if p.kind == KIND_PC and p.disk_path:
            if e.is_dir:                       # a 1581 partition with its own directory
                p.load_disk(p.disk_path, p.disk_stack + [e.ref])
            else:
                self.view()
            return
        if not e.is_dir:
            low = e.name.lower()
            if low.endswith(LAUNCH_EXTS):
                self.launch(p, e)
            elif low.endswith(".zip") or is_disk_image(low):
                if p.kind == KIND_PC:
                    path = os.path.join(p.path, e.name)
                    if low.endswith(".zip"):
                        p.load_zip(path, "")
                    else:
                        p.load_disk(path)
                else:
                    self.open_remote_container(p, e)
            return
        p.load(p.child_path(e.name))

    def go_up(self):
        p = self.active
        if not p.has_parent() or p.loading:
            return
        if p.kind == KIND_PC and p.zip_path and p.zip_inner:
            parts = zip_inner_parts(p.zip_inner)
            p.load_zip(p.zip_path, "/".join(parts[:-1]), parts[-1])
            return
        if p.kind == KIND_PC and p.disk_path and p.disk_stack:
            part = p.disk_stack[-1]
            p.load_disk(p.disk_path, p.disk_stack[:-1], part.name)
            return
        if p.kind == KIND_PC and p.in_container:
            name = os.path.basename(p.zip_path or p.disk_path)
            origin = p.leave_container()
            if origin:                           # back to the MEGA65 / EXP board panel
                self._show(p.side, origin["panel"])
                self.set_active(origin["panel"])
                origin["panel"].goto_name(origin["name"])
            else:
                p.goto_name(name)
            return
        here = p.path
        parent = p.parent_path()
        if p.remote:
            p.load(parent, here.rstrip("/").rsplit("/", 1)[-1])
        else:
            name = os.path.basename(here.rstrip("\\/")) or here
            p.load(parent, name)

    def refresh(self):
        if self.need_idle():
            self.active.refresh()

    def view(self):
        """F3: view the file under the cursor (text, PETSCII, C64 BASIC or hex)."""
        if not self.need_idle() or self.viewer:
            return
        p = self.active
        e = p.current()
        if not e or e.up or e.is_dir:
            return
        try:
            if p.kind == KIND_PC and p.disk_path:
                data = p.disk.read_file(e.ref, limit=VIEW_LIMIT)
                total = max(len(data), e.size if e.ref.ftype == 5 else len(data))
                self.viewer = Viewer(self, disk_out_name(e), data[:VIEW_LIMIT], total, cbm=True)
                return
            if p.kind == KIND_PC and p.zip_path:
                with zipfile.ZipFile(p.zip_path) as z:
                    info = z.getinfo(p.zip_member(e.name))
                    with z.open(info) as f:
                        data = f.read(VIEW_LIMIT)
                self.viewer = Viewer(self, e.name, data, info.file_size)
                return
            if p.kind == KIND_PC:
                path = os.path.join(p.path, e.name)
                with open(path, "rb") as f:
                    data = f.read(VIEW_LIMIT)
                self.viewer = Viewer(self, e.name, data, os.path.getsize(path))
                return
        except (OSError, KeyError, zipfile.BadZipFile, RuntimeError, DiskImageError) as ex:
            return self.show_error(f"Can't view {e.name}:\n{ex}")
        # on the MEGA65 or the EXP board: fetch it to a temp file first
        if p.kind == KIND_MEGA65 and not self._require_remote():
            return
        try:
            board = self.board() if p.kind == KIND_BOARD else None
        except BoardError as ex:
            return self.show_error(ex)
        if e.size > 64 * 1024 * 1024 and self.message(
                "View", f'"{e.name}" is {e.size / 1024 ** 2:.0f} MB. It has to be copied to the PC\n'
                        "before it can be viewed. Go ahead?", ("Yes", "No"), default=1) != 0:
            return
        ftp, folder, name, size = self.ftp, p.path, e.name, e.size
        self.busy = True
        prog = ProgressDialog(self, "View", f"Fetching {name}...", cancellable=False)

        def job():
            tmpdir = tempfile.mkdtemp(prefix="m65sd_view_")
            try:
                local = os.path.join(tmpdir, "file.bin")
                if p.kind == KIND_MEGA65:
                    ftp.get(folder, name, local)
                else:
                    board.download(board_join(folder, name), local,
                                   lambda d, t: self.post(prog.update_progress, None, name, d / t if t else None))
                with open(local, "rb") as f:
                    return f.read(VIEW_LIMIT), os.path.getsize(local)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        def done(res):
            self.busy = False
            prog.finish()
            data, total = res
            self.viewer = Viewer(self, name, data, total or size)

        def err(ex):
            self.busy = False
            prog.finish()
            self.show_error(ex)

        self.worker.submit(job, done, err)

    def open_remote_container(self, p, e):
        """Enter on a .zip / disk image on the MEGA65 or EXP board: copy it to a temp
        folder on the PC and open it in this panel. Backspace comes back here."""
        if not self.need_idle():
            return
        if p.kind == KIND_MEGA65 and not self._require_remote():
            return
        try:
            board = self.board() if p.kind == KIND_BOARD else None
        except BoardError as ex:
            return self.show_error(ex)
        ftp, folder, name = self.ftp, p.path, e.name
        tmpdir = tempfile.mkdtemp(prefix="m65sd_open_")
        local = os.path.join(tmpdir, name)
        self.busy = True
        prog = ProgressDialog(self, "Open", f"Fetching {name}...", cancellable=False)

        def job():
            if p.kind == KIND_MEGA65:
                ftp.get(folder, name, local)
            else:
                board.download(board_join(folder, name), local,
                               lambda d, t: self.post(prog.update_progress, None, name, d / t if t else None))

        def done(_):
            self.busy = False
            prog.finish()
            pc = self.panel_of(p.side, KIND_PC)
            label = ("MEGA65:" + remote_join(folder, name)) if p.kind == KIND_MEGA65 else \
                    ("EXP:/" + board_join(folder, name))
            origin = {"panel": p, "label": label, "tmpdir": tmpdir, "pc_path": pc.path, "name": name}
            self._show(p.side, pc)
            ok = pc.load_zip(local, "") if name.lower().endswith(".zip") else pc.load_disk(local)
            if not ok:
                shutil.rmtree(tmpdir, ignore_errors=True)
                pc.path = origin["pc_path"]
                self._show(p.side, p)
                self.set_active(p)
                return
            pc.origin = origin
            pc.render()
            self.set_active(pc)

        def err(ex):
            self.busy = False
            prog.finish()
            shutil.rmtree(tmpdir, ignore_errors=True)
            self.show_error(ex)

        self.worker.submit(job, done, err)

    def drive_menu(self):
        self.panel_menu(self.left)

    def choose(self, title, options, current=0):
        """NC-style pick list. Returns the chosen index or None."""
        d = NCDialog(self, title)
        h = min(len(options), 16)
        lb = tk.Listbox(d.body, font=self.font, height=h, width=max(24, max(len(o) for o in options) + 2),
                        bg=GREY, fg=BLACK, selectbackground=DCYAN, selectforeground=BLACK,
                        bd=0, highlightthickness=0, activestyle="none", exportselection=False)
        for o in options:
            lb.insert("end", " " + o)
        lb.pack(pady=(2, 6))
        cur = max(0, min(current, len(options) - 1))
        lb.selection_set(cur)
        lb.activate(cur)
        lb.see(cur)

        def pick(e=None):
            sel = lb.curselection()
            return d.close(sel[0] if sel else None)

        def move(delta):
            sel = lb.curselection()
            i = max(0, min(len(options) - 1, (sel[0] if sel else 0) + delta))
            lb.selection_clear(0, "end")
            lb.selection_set(i)
            lb.activate(i)
            lb.see(i)
            return "break"

        lb.bind("<Return>", pick)
        lb.bind("<KP_Enter>", pick)
        lb.bind("<Double-Button-1>", pick)
        lb.bind("<Up>", lambda e: move(-1))
        lb.bind("<Down>", lambda e: move(1))
        lb.bind("<Home>", lambda e: move(-len(options)))
        lb.bind("<End>", lambda e: move(len(options)))

        def letter(e):     # a letter jumps to the next match; picks it if it's the only one
            if e.char and e.char.isalpha():
                hits = [i for i, o in enumerate(options) if o.lstrip().upper().startswith(e.char.upper())]
                if not hits:
                    return "break"
                sel = lb.curselection()
                cur = sel[0] if sel else -1
                nxt = next((i for i in hits if i > cur), hits[0])
                lb.selection_clear(0, "end")
                lb.selection_set(nxt)
                lb.activate(nxt)
                lb.see(nxt)
                if len(hits) == 1:
                    return pick()
                return "break"
            return None

        lb.bind("<Key>", letter, add="+")
        d.label("Enter = choose   Esc = cancel", fg=DGREY)
        return d.run(focus=lb)

    def pc_locations(self):
        """List of (label, path) for PC drives / places."""
        locs = []
        if IS_WINDOWS:
            for dl in string.ascii_uppercase:
                if os.path.exists(f"{dl}:\\"):
                    locs.append((f"{dl}:  drive", f"{dl}:\\"))
        else:
            locs.append(("/   root", "/"))
        locs.append(("Home folder", os.path.expanduser("~")))
        return locs

    # ----------------------------------------------------- panel sources
    def panel_of(self, side, kind):
        """The panel object for a side (0 left, 1 right) and source kind."""
        p = self.slots[side].get(kind)
        if p is None:
            cls = {KIND_PC: LocalPanel, KIND_MEGA65: RemotePanel, KIND_BOARD: BoardPanel}[kind]
            p = cls(self, self.panes)
            p.side = side
            p.lb.bind("<Key>", self.on_key)
            self.slots[side][kind] = p
        return p

    def side_panel(self, side):
        return self.left if side == 0 else self.right

    def visible(self, kind):
        return [p for p in (self.left, self.right) if p.kind == kind]

    def all_panels(self, kind):
        return [p for slot in self.slots for p in slot.values() if p.kind == kind]

    def _show(self, side, panel):
        old = self.side_panel(side)
        if old is panel:
            return
        self._remember(old)
        old.grid_remove()
        if side == 0:
            self.left = panel
        else:
            self.right = panel
        panel.grid(row=0, column=side, sticky="nsew")
        self.cfg["left_mode" if side == 0 else "right_mode"] = panel.kind

    def _remember(self, p):
        """Store a panel's folder so it comes back next time."""
        if p.kind == KIND_PC and p.path:
            self.cfg["local_path" if p.side == 0 else "right_local_path"] = p.path
        elif p.kind == KIND_MEGA65 and p.entries:
            self.cfg["remote_path"] = p.path
        elif p.kind == KIND_BOARD and p.entries:
            self.cfg["board_path"] = p.path

    def show_pc(self, side, path, activate=True, same_drive_keeps_folder=False):
        p = self.panel_of(side, KIND_PC)
        self._show(side, p)
        if same_drive_keeps_folder:     # picking the drive you're already on keeps your folder
            cur = p.path
            if cur and os.path.isdir(cur) and cur.upper()[:2] == (path or "").upper()[:2] \
                    and path != os.path.expanduser("~"):
                path = cur
        p.load(path)
        if activate:
            self.set_active(p)
        else:
            p.set_active(False)
        self.update_topbar()

    def show_kind(self, side, kind, activate=True, autoconnect=True):
        """Show the MEGA65 SD card or the EXP board SD card on a side."""
        p = self.panel_of(side, kind)
        self._show(side, p)
        if activate:
            self.set_active(p)
        else:
            p.set_active(False)
        if kind == KIND_MEGA65:
            if self.ftp_connected():
                p.load(p.path if p.entries else (self.cfg.get("remote_path") or "/"),
                       on_fail=lambda: p.load("/"))
            else:
                p.render()
                if autoconnect:
                    self.connect()
        else:
            p.load(p.path or self.cfg.get("board_path") or "", on_fail=None)
        self.update_topbar()

    # kept for older calls
    def show_right_pc(self, path, activate=True):
        self.show_pc(1, path, activate)

    def show_right_mega65(self):
        self.show_kind(1, KIND_MEGA65)

    def drive_menu(self):
        """F1 (Drive): choose what the active panel shows."""
        self.panel_menu(self.active)

    def panel_menu(self, panel):
        """Choose what a panel shows: F1 for the active panel, a click on a panel's
        title, or Alt+F1 (left) / Alt+F2 (right) where the system doesn't use those."""
        if not self.need_idle():
            return
        side = 0 if panel is self.left else 1
        locs = self.pc_locations()
        sources = [KIND_MEGA65] + ([KIND_BOARD] if self.exp_enabled() else [])
        options = [KIND_LABEL[k] for k in sources] + [l for l, _ in locs]
        current = 0
        if panel.kind in sources:
            current = sources.index(panel.kind)
        elif panel.kind == KIND_PC:
            current = len(sources)
            for i, (_, path) in enumerate(locs):
                if panel.path and panel.path.upper().startswith(path.upper()[:2]):
                    current = i + len(sources)
                    break
        idx = self.choose("Drive - left panel" if side == 0 else "Drive - right panel", options, current)
        if idx is None:
            return
        if idx < len(sources):
            self.show_kind(side, sources[idx])
        else:
            self.show_pc(side, locs[idx - len(sources)][1], same_drive_keeps_folder=True)

    # ----------------------------------------------------- connection
    def toggle_link(self):
        if not self.need_idle():
            return
        if self.ftp_connected():
            self.disconnect()
        elif not self.visible(KIND_MEGA65):
            self.show_kind(1, KIND_MEGA65)
        else:
            self.connect()

    def connect(self):
        if self.ftp_connected() or self.busy:
            return
        c = self.cfg
        if not c.get("exe_path") or not os.path.isfile(c["exe_path"]):
            self.message("Setup", "mega65_ftp has not been found.\nPlease set its location in Setup (F9).")
            self.setup()
            return
        self.ftp = Mega65FTP(c["exe_path"], c.get("ip_iface", ""), c.get("device", ""), c.get("extra_args", ""),
                             log_cb=lambda t: self.post(self.log_text, t))
        self.busy = True
        prog = ProgressDialog(self, "Connect", "Looking for the MEGA65 on the network...", cancellable=False)

        def done(_):
            self.busy = False
            prog.finish()
            self.update_topbar()
            shown = self.visible(KIND_MEGA65)
            for p in shown:
                p.load(c.get("remote_path") or "/", on_fail=lambda p=p: p.load("/"))
            if shown:
                self.set_active(shown[-1])

        def err(e):
            self.busy = False
            prog.finish()
            self.update_topbar()
            if IS_MAC:
                fw_q = ("\n\nIf macOS just asked whether mega65_ftp may accept incoming network\n"
                        "connections, click Allow, then press F4 again.\n")
                fw_c = " - macOS firewall (if on) allows mega65_ftp (UDP port 4510)"
            elif IS_WINDOWS:
                fw_q = ("\n\nIf Windows just asked whether to let mega65_ftp through the firewall,\n"
                        "allow it, then press F4 again - it gives up while that question is open.\n")
                fw_c = " - Windows Firewall allows mega65_ftp (UDP port 4510)"
            else:
                fw_q = "\n"
                fw_c = " - The firewall allows mega65_ftp (UDP port 4510)"
            hint = (fw_q +
                    "\nChecklist:\n"
                    " - Remote control is on: SHIFT + £ at the READY prompt (LED blinks green/yellow)\n"
                    " - MEGA65 is on, at the READY prompt, Ethernet cable plugged in\n"
                    " - ETHLOAD.M65 is in the root folder of the SD card\n"
                    " - PC and MEGA65 are on the same network, IPv6 enabled\n"
                    + fw_c)
            self.show_error(FTPError(str(e) + ("" if c.get("device") else hint)))

        self.worker.submit(self.ftp.connect, done, err)

    def disconnect(self, then=None):
        ftp = self.ftp
        if not ftp:
            if then:
                then()
            return
        self.busy = True

        def done(_):
            self.busy = False
            self.on_disconnected()
            if then:
                then()

        self.worker.submit(ftp.close, done, lambda e: done(None))

    def on_disconnected(self):
        if self.ftp and not self.ftp.connected:
            pass
        if not self.ftp_connected():
            for p in self.all_panels(KIND_MEGA65):
                if p.entries:
                    self.cfg["remote_path"] = p.path
                p.entries = []
                p.marked.clear()
                p.render()
        self.update_topbar()

    # ----------------------------------------------------- file ops
    def _require_remote(self):
        if not self.ftp_connected():
            self.message("MEGA65", "Not connected to the MEGA65.\nPress F4 to connect first.")
            return False
        return True

    # ----------------------------------------------------- copying (any source -> any destination)
    WHERE = {KIND_PC: "the PC folder", KIND_MEGA65: "the MEGA65 SD card", KIND_BOARD: "the EXP board SD card"}

    def board(self):
        url, user, pw, _src = self.board_info()
        return Board(url, user, pw)      # raises BoardError when no address is known

    @staticmethod
    def _join(kind, d, name):
        if kind == KIND_PC:
            return os.path.join(d, name)
        if kind == KIND_MEGA65:
            return remote_join(d, name)
        return board_join(d, name)

    def copy(self):
        if not self.need_idle():
            return
        src = self.active
        dst = self.right if src is self.left else self.left
        if dst.kind == KIND_PC and dst.in_container:
            self.message("Copy", "Can't copy into a ZIP file or disk image (read-only for now).\n"
                                 "Leave it first (Backspace in that panel).")
            return
        from_zip = src.kind == KIND_PC and bool(src.zip_path)
        from_disk = src.kind == KIND_PC and bool(src.disk_path)
        if src.kind == dst.kind and src.kind != KIND_PC:
            self.message("Copy", f"Both panels show {self.WHERE[src.kind]}.\n"
                                 "Copying within the same card isn't supported.")
            return
        if KIND_MEGA65 in (src.kind, dst.kind) and not self._require_remote():
            return
        if (src.kind == KIND_PC and src.path == "") or (dst.kind == KIND_PC and dst.path == ""):
            self.message("Copy", "Please open a folder first (not the drive list).")
            return
        if KIND_BOARD in (src.kind, dst.kind):
            try:
                self.board()
            except BoardError as e:
                self.show_error(e)
                return
        items = src.selected_items()
        if not items:
            return
        note = ""
        if dst.kind == KIND_BOARD and from_disk:
            self.message("Copy", "The expansion board only stores .bit, .cor and .m65j files,\n"
                                 "so files from a disk image can't go there.")
            return
        if dst.kind == KIND_BOARD and from_zip:
            note = "\n(only .bit/.cor/.m65j files are extracted - the board can't hold folders)"
        elif dst.kind == KIND_BOARD:
            ok = [i for i in items if not i.is_dir and i.name.lower().endswith(UPLOAD_EXTS)]
            if not ok:
                self.message("Copy", "The expansion board only accepts .bit, .cor and .m65j files\n"
                                     "(and it can't hold folders).")
                return
            if len(ok) < len(items):
                note = f"\n({len(items) - len(ok)} item(s) skipped - not .bit/.cor/.m65j or a folder)"
            items = ok
        if dst.kind == KIND_MEGA65:
            big = [i for i in items if not i.is_dir and i.size > MAX_FILE_SIZE]
            if big:
                items = [i for i in items if i not in big]
                txt = too_big_text(big[0].name, big[0].size) if len(big) == 1 else (
                    f"{len(big)} files are 2 GB or larger. mega65_ftp can only transfer\n"
                    "files smaller than 2 GB (a limit inside mega65_ftp itself).")
                if not items:
                    self.message("Copy", txt, style="red")
                    return
                note += f"\n({len(big)} file(s) of 2 GB or more skipped - mega65_ftp limit)"
        if src.kind == KIND_PC and dst.kind == KIND_PC and not from_zip and not from_disk:
            if os.path.normcase(os.path.abspath(src.path)) == os.path.normcase(os.path.abspath(dst.path)):
                self.message("Copy", "Both panels show the same folder.")
                return
            dnorm = os.path.normcase(os.path.abspath(dst.path)) + os.sep
            for it in items:
                if it.is_dir and dnorm.startswith(os.path.normcase(os.path.join(src.path, it.name)) + os.sep):
                    self.message("Copy", f'Cannot copy "{it.name}" into itself.', style="red")
                    return
        what = f'"{items[0].name}"' if len(items) == 1 else f"{len(items)} items"
        via = "\n(it goes through a temp folder on the PC)" if KIND_PC not in (src.kind, dst.kind) else ""
        shown_path = dst.path if dst.kind != KIND_BOARD else "/" + dst.path
        if from_zip or from_disk:
            via = ""
            what += f" from {os.path.basename(src.zip_path or (src.origin or {}).get('name') or src.disk_path)}"
        verb = "Extract" if (from_zip or from_disk) else "Copy"
        res = self.message(verb, f"{verb} {what} to {self.WHERE[dst.kind]}\n{shown_path}{via}{note}",
                           (verb, "Cancel"))
        if res != 0:
            return

        def dname(n):
            return safe_remote_name(n) if dst.kind == KIND_MEGA65 else n

        def out_name(i):                       # name a file gets when it leaves a disk image
            return disk_out_name(i) if from_disk else i.name

        existing = {e.name.lower() for e in dst.entries if not e.up}
        conflict = [i for i in items if dname(out_name(i)).lower() in existing]
        mode = "overwrite"
        if conflict:
            msg = (f'"{conflict[0].name}" already exists.' if len(conflict) == 1
                   else f"{len(conflict)} items already exist.")
            r = self.message("Copy", msg + "\nOverwrite (folders are merged)?",
                             ("Overwrite", "Skip existing", "Cancel"), style="red")
            if r is None or r == 2:
                return
            mode = "overwrite" if r == 0 else "skip"
        zsrc = {"path": src.zip_path, "inner": src.zip_inner, "fetch": None} if from_zip else None
        if from_disk:
            zsrc = {"disk": src.disk}
        self._transfer(src, dst, items, mode, dname, existing, zsrc)

    def extract_all(self, p, e):
        """X on a .zip: extract all of it into the folder shown in the other panel."""
        if not self.need_idle():
            return
        dst = self.right if p is self.left else self.left
        if dst.kind == KIND_PC and dst.in_container:
            self.message("Extract", "The other panel is showing a ZIP file or disk image.\n"
                                    "Leave it first (Backspace in that panel).")
            return
        if dst.kind == KIND_PC and dst.path == "":
            self.message("Extract", "Please open a folder in the other panel first.")
            return
        if KIND_MEGA65 in (p.kind, dst.kind) and not self._require_remote():
            return
        if KIND_BOARD in (p.kind, dst.kind):
            try:
                self.board()
            except BoardError as ex:
                return self.show_error(ex)
        if p.kind == KIND_PC:
            zpath, fetch = os.path.join(p.path, e.name), None
            try:
                top = list_zip(zpath, "")
            except (zipfile.BadZipFile, OSError, RuntimeError) as ex:
                return self.show_error(f"Cannot open the ZIP file:\n{zpath}\n\n{ex}")
        else:
            zpath, fetch, top = None, (p.kind, p.path, e.name), None

        def dname(n):
            return safe_remote_name(n) if dst.kind == KIND_MEGA65 else n

        # extract into a folder named after the ZIP (the board can't make folders)
        subdir = None
        if dst.kind != KIND_BOARD:
            subdir = os.path.splitext(e.name)[0].strip().rstrip(".") or "extracted"
        shown_path = dst.path if dst.kind != KIND_BOARD else "/" + dst.path
        note = "\n(only .bit/.cor/.m65j files - the board can't hold folders)" if dst.kind == KIND_BOARD else ""
        fetched = "\n(the ZIP is copied to a temp folder on the PC first)" if fetch else ""
        into = f'into the folder "{dname(subdir)}" in\n' if subdir else "to "
        existing = {x.name.lower() for x in dst.entries if not x.up}
        if top is not None:
            if dst.kind == KIND_BOARD:
                clash = False        # checked per file during the extract
            else:
                clash = dname(subdir).lower() in existing
            r = self.message("Extract", f'Extract everything in "{e.name}"\n{into}{self.WHERE[dst.kind]}\n'
                                        f"{shown_path}{note}", ("Extract", "Cancel"))
            if r != 0:
                return
            mode = "overwrite"
            if clash:
                r = self.message("Extract", f'A folder called "{dname(subdir)}" already exists there.\n'
                                            "Overwrite files in it (folders are merged)?",
                                 ("Overwrite", "Skip existing", "Cancel"), style="red")
                if r is None or r == 2:
                    return
                mode = "overwrite" if r == 0 else "skip"
        else:
            r = self.message("Extract", f'Extract everything in "{e.name}"\n{into}{self.WHERE[dst.kind]}\n'
                                        f"{shown_path}{note}{fetched}\n\nIf files already exist there:",
                             ("Overwrite", "Skip existing", "Cancel"))
            if r is None or r == 2:
                return
            mode = "overwrite" if r == 0 else "skip"
        zsrc = {"path": zpath, "inner": "", "fetch": fetch, "subdir": subdir}
        self._transfer(p, dst, None, mode, dname, existing, zsrc)

    def _transfer(self, src, dst, items, mode, dname, top_existing, zsrc=None):
        """Generic copy. Tasks: ("mkdir", parent, name) / ("file", src_ref, dst_dir, dst_name, size).
        zsrc: extract from a ZIP instead - {"path", "inner", "fetch": None | (kind, dir, name)};
        items=None means the whole ZIP."""
        ftp = self.ftp
        board = self.board() if KIND_BOARD in (src.kind, dst.kind) else None
        sk, dk = (("disk" if zsrc.get("disk") else "zip") if zsrc else src.kind), dst.kind
        src_dir, dst_dir = src.path, dst.path
        zhold = {"zf": None, "tmp": None}

        def plan_zip(tasks, add_file):
            if zsrc["fetch"]:                     # ZIP lives on the MEGA65 / EXP board: fetch it
                kind, d, name = zsrc["fetch"]
                zhold["tmp"] = tempfile.mkdtemp(prefix="m65sd_zip_")
                zsrc["path"] = os.path.join(zhold["tmp"], "archive.zip")
                if kind == KIND_MEGA65:
                    ftp.get(d, name, zsrc["path"])
                else:
                    board.download(board_join(d, name), zsrc["path"])
            zf = zipfile.ZipFile(zsrc["path"])
            zhold["zf"] = zf
            ip = zip_inner_parts(zsrc["inner"])
            wanted = None if items is None else {i.name for i in items}
            made = set()
            prefix = []
            sub = zsrc.get("subdir")
            if sub and dk != KIND_BOARD:
                prefix = [sub]
                safe = [(zip_parts(i.filename), i.is_dir()) for i in zf.infolist()]
                safe = [(pp, isd) for pp, isd in safe if pp]
                # zip already holds one top folder with that name -> don't nest it twice
                if safe and all(pp[0].lower() == sub.lower() and (len(pp) > 1 or isd) for pp, isd in safe):
                    prefix = []

            def ensure_dirs(segs):
                parent = dst_dir
                for seg in segs:
                    key = (parent, dname(seg))
                    if key not in made:
                        made.add(key)
                        tasks.append(("mkdir", parent, dname(seg)))
                    parent = self._join(dk, parent, dname(seg))
                return parent

            for info in zf.infolist():
                parts = zip_parts(info.filename)
                if not parts or parts[:len(ip)] != ip:
                    continue
                rest = parts[len(ip):]
                if not rest or (wanted is not None and rest[0] not in wanted):
                    continue
                if info.is_dir():
                    if dk != KIND_BOARD:
                        ensure_dirs(prefix + rest)
                    continue
                if dk == KIND_BOARD:              # board: core files only, no folders
                    if rest[-1].lower().endswith(UPLOAD_EXTS):
                        add_file(info.filename, dst_dir, rest[-1], info.file_size)
                    continue
                add_file(info.filename, ensure_dirs(prefix + rest[:-1]), rest[-1], info.file_size)
            if prefix:
                ensure_dirs(prefix)               # an empty ZIP still gets its folder
            if dk == KIND_BOARD and not any(t[0] == "file" for t in tasks):
                raise FTPError("There are no .bit, .cor or .m65j files in that part of the ZIP,\n"
                               "and the expansion board can only store those.")

        def plan():
            tasks, total = [], 0

            def add_file(ref, ddir, name, size):
                nonlocal total
                total += size
                tasks.append(("file", ref, ddir, dname(name), size))

            def walk(sdir, ddir_parent, name):
                ddir = self._join(dk, ddir_parent, dname(name))
                tasks.append(("mkdir", ddir_parent, dname(name)))
                if sk == KIND_PC:
                    try:
                        with os.scandir(sdir) as it:
                            ents = sorted(it, key=lambda d: d.name.lower())
                    except OSError:
                        return
                    for de in ents:
                        if de.is_dir(follow_symlinks=False):
                            walk(de.path, ddir, de.name)
                        else:
                            try:
                                sz = de.stat().st_size
                            except OSError:
                                sz = 0
                            add_file(de.path, ddir, de.name, sz)
                elif sk == KIND_MEGA65:
                    for e in sorted(ftp.listdir(sdir), key=lambda x: x.name.lower()):
                        if e.is_dir:
                            walk(remote_join(sdir, e.name), ddir, e.name)
                        else:
                            add_file((sdir, e.name), ddir, e.name, e.size)
                else:
                    for e in sorted(board.list(sdir), key=lambda x: x.name.lower()):
                        if e.is_dir:
                            walk(e.path, ddir, e.name)
                        elif e.kind not in ("PARTIAL", "THEME"):
                            add_file(e.path, ddir, e.name, e.size)

            if sk == "disk":
                img = zsrc["disk"]

                def walk_part(entry_list, ddir):
                    for it in entry_list:
                        if it.is_dir:                  # 1581 partition -> folder
                            sub = dname(safe_pc_name(it.ref.name or "partition"))
                            tasks.append(("mkdir", ddir, sub))
                            inner = [Entry(d.name or "(no name)", False, d.blocks * 254, ref=d, blocks=d.blocks)
                                     for d in img.list(it.ref)]
                            walk_part(inner, self._join(dk, ddir, sub))
                        else:
                            add_file(it.ref, ddir, disk_out_name(it), it.size)
                walk_part(items, dst_dir)
                return tasks, total
            if zsrc:
                plan_zip(tasks, add_file)
                return tasks, total

            for it in items:
                sref = self._join(sk, src_dir, it.name)
                if it.is_dir:
                    walk(sref, dst_dir, it.name)
                else:
                    add_file(sref if sk != KIND_MEGA65 else (src_dir, it.name), dst_dir, it.name, it.size)
            return tasks, total

        created = set()
        listing_cache = {dst_dir: top_existing}

        def exists_at_dst(ddir, name):
            if ddir in created:
                return False
            if dk == KIND_PC:
                return os.path.exists(os.path.join(ddir, name))
            if ddir not in listing_cache:
                ents = ftp.listdir(ddir) if dk == KIND_MEGA65 else board.list(ddir)
                listing_cache[ddir] = {e.name.lower() for e in ents}
            return name.lower() in listing_cache[ddir]

        def execute(t, progress):
            if t[0] == "mkdir":
                _, parent, name = t
                path = self._join(dk, parent, name)
                if dk == KIND_PC:
                    if not os.path.isdir(path):
                        os.makedirs(path)
                        created.add(path)
                elif dk == KIND_MEGA65:
                    try:
                        ftp.mkdir(parent, name)
                        created.add(path)
                    except FTPError as e:
                        if "already exists" not in str(e):
                            raise
                return
            _, ref, ddir, name, _size = t
            if mode == "skip" and exists_at_dst(ddir, name):
                return
            tmpdir = None
            try:
                if sk == KIND_PC:
                    local = ref
                elif sk == "disk":
                    tmpdir = tempfile.mkdtemp(prefix="m65sd_copy_")
                    local = os.path.join(tmpdir, "file.bin")
                    with open(local, "wb") as out:
                        out.write(zsrc["disk"].read_file(ref))
                elif sk == "zip":
                    tmpdir = tempfile.mkdtemp(prefix="m65sd_copy_")
                    local = os.path.join(tmpdir, "file.bin")
                    info = zhold["zf"].getinfo(ref)
                    done = 0
                    with zhold["zf"].open(info) as zin, open(local, "wb") as out:
                        while True:
                            chunk = zin.read(262144)
                            if not chunk:
                                break
                            out.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, info.file_size)
                    if dk == KIND_PC:
                        y, mo, d, h, mi, sec = info.date_time
                        try:
                            t = time.mktime((y, mo, d, h, mi, sec, 0, 0, -1))
                            os.utime(local, (t, t))
                        except (OverflowError, ValueError, OSError):
                            pass
                else:
                    tmpdir = tempfile.mkdtemp(prefix="m65sd_copy_")
                    local = os.path.join(tmpdir, "file.bin")
                    if sk == KIND_MEGA65:
                        ftp.get(ref[0], ref[1], local)
                    else:
                        board.download(ref, local, progress)
                if dk == KIND_PC:
                    dp = os.path.join(ddir, name)
                    if sk == KIND_PC:
                        shutil.copy2(local, dp)
                    else:
                        shutil.move(local, dp)
                elif dk == KIND_MEGA65:
                    ftp.put(local, ddir, name)
                else:
                    board.upload(local, board_join(ddir, name), progress)
            finally:
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)

        title = {KIND_PC: "Copy to PC", KIND_MEGA65: "Copy to MEGA65", KIND_BOARD: "Copy to EXP board"}[dk]
        if zsrc:
            title = title.replace("Copy", "Extract")
        if sk == "disk":
            items_names = {id(i): disk_out_name(i) for i in items}

        def after():
            if zhold["zf"] is not None:
                zhold["zf"].close()
            if zhold["tmp"]:
                shutil.rmtree(zhold["tmp"], ignore_errors=True)
            if sk == "disk":
                focus = dname(items_names[id(items[0])]) if items else None
            else:
                focus = dname(items[0].name) if items else (dname(zsrc["subdir"]) if zsrc and zsrc.get("subdir") else None)
            dst.load(dst.path, focus)

        self._run_transfer(title, plan, execute, after)

    def _run_transfer(self, title, plan_fn, exec_fn, refresh_panel):
        self.busy = True
        prog = ProgressDialog(self, title, "Preparing...")
        errors = []

        def job():
            tasks, total = plan_fn()
            done_bytes = 0
            nfiles = sum(1 for t in tasks if t[0] == "file")
            n = 0
            t0 = time.time()
            for t in tasks:
                if prog.cancelled:
                    break
                size = t[-1] if t[0] == "file" else 0
                name = t[3] if t[0] == "file" else t[2]
                cb = None
                if t[0] == "file":
                    n += 1
                    rate = done_bytes / max(0.5, time.time() - t0)
                    rtxt = f"  {rate / 1024:.1f} KB/s" if done_bytes else ""
                    label = f"File {n} of {nfiles}{rtxt}"
                    self.post(prog.update_progress, label, name,
                              done_bytes / total if total else n / max(1, nfiles))
                    cb = self._progress_cb(prog, label, name, done_bytes, size, total)
                try:
                    exec_fn(t, cb)
                except NotConnected:
                    raise
                except (FTPError, BoardError, OSError, zipfile.BadZipFile, RuntimeError, KeyError) as e:
                    last = str(e).splitlines()[-1] if str(e) else str(e)
                    errors.append(last if f'"{name}"' in last else f"{name}: {last}")
                    if len(errors) > 50:
                        break
                done_bytes += size
            return prog.cancelled

        def finish(cancelled):
            self.busy = False
            prog.finish()
            refresh_panel()
            if errors:
                self.message("Copy", "Some items could not be copied:\n\n" + "\n".join(errors[:12]), style="red")
            elif cancelled:
                self.message("Copy", "Copy cancelled.")

        def fail(e):
            self.busy = False
            prog.finish()
            self.show_error(e)
            refresh_panel()

        self.worker.submit(job, finish, fail)

    def _progress_cb(self, prog, label, name, base, size, total):
        """Byte-level progress for one file (used by EXP board transfers)."""
        last = [0.0]

        def cb(d, _tot):
            if total and (d - last[0]) > max(32768, total / 200):
                last[0] = d
                self.post(prog.update_progress, label, name, (base + min(d, size)) / total)
        return cb

    def exp_enabled(self):
        """Is the JTAG expansion board option switched on (Setup)?"""
        return bool(self.cfg.get("exp_board"))

    def apply_exp_setting(self):
        """After the option was switched off: no panel may keep showing the EXP board."""
        if self.exp_enabled():
            return
        for side in (0, 1):
            if self.side_panel(side).kind == KIND_BOARD:
                home = self.cfg.get("local_path") if side == 0 else self.cfg.get("right_local_path")
                self.show_pc(side, home if home and os.path.isdir(home) else os.path.expanduser("~"),
                             activate=False)
        self.set_active(self.active if self.active in (self.left, self.right) else self.left)

    def board_info(self):
        here = os.path.dirname(os.path.abspath(__file__))
        c = self.cfg
        return find_board(here, c.get("board_url", ""), c.get("board_user", ""), c.get("board_password", ""))

    def launch(self, p, e):
        """Enter on a .bit/.cor/.m65j: launch it on the MEGA65 via the expansion board."""
        if not self.need_idle():
            return
        if not self.exp_enabled():
            self.message("Launch core",
                         "Launching cores needs the MEGA65 JTAG expansion board.\n\n"
                         "If you have one, switch it on in Setup (F9).")
            return
        url, user, pw, src = self.board_info()
        if not url:
            self.message("Launch core",
                         "I couldn't find the expansion board's address.\n\n"
                         "Add url=http://<board-ip> to .m65j.config,\n"
                         "or enter it in Setup (F9).", style="red")
            return
        kind = p.kind
        if kind == KIND_MEGA65 and not self._require_remote():
            return
        where = {KIND_PC: "from this PC", KIND_MEGA65: "from the MEGA65 SD card",
                 KIND_BOARD: "from the EXP board SD card"}[kind]
        extra = "\nIt is copied to the PC first, then sent to the expansion board." if kind == KIND_MEGA65 else ""
        conn = "\nThe MEGA65 connection (F4) will be closed." if self.ftp_connected() else ""
        r = self.message("Launch core",
                         f'Launch "{e.name}" on the MEGA65 ({where})?{extra}\n\n'
                         f"Whatever is running now will be replaced.{conn}",
                         ("Launch", "Cancel"), style="red")
        if r != 0:
            return
        self.busy = True
        prog = ProgressDialog(self, "Launch core", "Preparing...", cancellable=False)
        ftp = self.ftp
        folder = p.path
        name = e.name
        zip_path = p.zip_path if kind == KIND_PC else None
        member = p.zip_member(name) if zip_path else None

        def progress(text):
            def cb(done, total):
                self.post(prog.update_progress, text, name, done / total if total else None)
            return cb

        def job():
            tmpdir = None
            try:
                path = None
                if kind == KIND_MEGA65:
                    self.post(prog.update_progress, "Copying from the MEGA65 SD card...", name, None)
                    tmpdir = tempfile.mkdtemp(prefix="m65sd_launch_")
                    path = os.path.join(tmpdir, name)
                    ftp.get(folder, name, path)
                elif kind == KIND_PC and zip_path:
                    tmpdir = tempfile.mkdtemp(prefix="m65sd_launch_")
                    path = os.path.join(tmpdir, name)
                    with zipfile.ZipFile(zip_path) as z, z.open(member) as zin, open(path, "wb") as out:
                        shutil.copyfileobj(zin, out)
                elif kind == KIND_PC:
                    path = os.path.join(folder, name)
                if ftp is not None and ftp.connected:
                    self.post(prog.update_progress, "Closing the MEGA65 connection...", name, None)
                    ftp.close()
                board = Board(url, user, pw)
                if kind == KIND_BOARD:
                    self.post(prog.update_progress, "Asking the expansion board to load it...", name, None)
                    return board.launch_board_sd(board_join(folder, name))
                return board.launch_file(path, name, progress(f"Sending to the expansion board ({url})"))
            finally:
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)

        def done(reply):
            self.busy = False
            prog.finish()
            self.on_disconnected()
            msg = f'Launched "{name}" on the MEGA65.'
            if reply and len(reply) < 200:
                msg += f"\n\nBoard says: {reply}"
            self.message("Launch core", msg)

        def err(ex):
            self.busy = False
            prog.finish()
            self.on_disconnected()
            if isinstance(ex, BoardError):
                self.message("Launch core", "The expansion board refused or could not be reached:\n\n" +
                             wrap_lines(ex), style="red")
            else:
                self.show_error(ex)

        self.worker.submit(job, done, err)

    def _board_cannot(self, what):
        self.message(what, f"The expansion board firmware can't {what.lower()} on its SD card.\n"
                           "Do that with the card in your PC.")

    def mkdir(self):
        if not self.need_idle():
            return
        p = self.active
        if p.kind == KIND_PC and p.in_container:
            self.message("Read only", "Can't make folders inside a ZIP file or disk image.\n"
                                      "Press Backspace to leave it.")
            return
        if p.kind == KIND_BOARD:
            return self._board_cannot("Make folders")
        if p.remote and not self._require_remote():
            return
        if not p.remote and p.path == "":
            return
        name = self.ask_string("Make directory", "Create the directory")
        if not name:
            return
        if p.remote:
            rname = safe_remote_name(name)
            self.busy = True

            def done(_):
                self.busy = False
                p.load(p.path, rname)

            def err(e):
                self.busy = False
                self.show_error(e)

            self.worker.submit(lambda: self.ftp.mkdir(p.path, rname), done, err)
        else:
            try:
                os.mkdir(os.path.join(p.path, name))
            except OSError as e:
                self.show_error(f"Cannot create folder:\n{e.strerror or e}")
            p.load(p.path, name)

    def rename(self):
        if not self.need_idle():
            return
        p = self.active
        if p.kind == KIND_PC and p.in_container:
            self.message("Read only", "Can't rename inside a ZIP file or disk image.\n"
                                      "Press Backspace to leave it.")
            return
        e = p.current()
        if not e or e.up or (not p.remote and p.path == ""):
            return
        if p.kind == KIND_BOARD:
            return self._board_cannot("Rename files")
        if p.remote and not self._require_remote():
            return
        new = self.ask_string("Rename", f'Rename "{e.name}" to', e.name)
        if not new or new == e.name:
            return
        if p.remote:
            new = safe_remote_name(new)
            self.busy = True

            def done(_):
                self.busy = False
                p.load(p.path, new)

            def err(ex):
                self.busy = False
                self.show_error(ex)
                p.refresh()

            self.worker.submit(lambda: self.ftp.rename(p.path, e.name, new), done, err)
        else:
            try:
                os.rename(os.path.join(p.path, e.name), os.path.join(p.path, new))
            except OSError as ex:
                self.show_error(f"Cannot rename:\n{ex.strerror or ex}")
            p.load(p.path, new)

    def delete(self):
        if not self.need_idle():
            return
        p = self.active
        if p.kind == KIND_PC and p.in_container:
            self.message("Read only", "Can't delete inside a ZIP file or disk image.\n"
                                      "Press Backspace to leave it.")
            return
        items = p.selected_items()
        if not items or (not p.remote and p.path == ""):
            return
        if p.kind == KIND_BOARD:
            if any(i.is_dir for i in items):
                self.message("Delete", "The expansion board firmware can't delete folders on its SD card.\n"
                                       "Unmark the folders, or do that with the card in your PC.")
                return
            try:
                self.board()
            except BoardError as ex:
                return self.show_error(ex)
        elif p.remote and not self._require_remote():
            return
        ndirs = sum(1 for i in items if i.is_dir)
        what = f'"{items[0].name}"' if len(items) == 1 else f"{len(items)} items"
        where = {KIND_MEGA65: "from the MEGA65 SD card", KIND_BOARD: "from the EXP board SD card"}.get(
            p.kind, "(to the Recycle Bin)" if send2trash else "PERMANENTLY from your PC")
        extra = "\nFolders are deleted with everything inside them." if ndirs else ""
        r = self.message("Delete", f"Do you wish to delete {what}\n{where}?{extra}", ("Delete", "Cancel"),
                         style="red")
        if r != 0:
            return
        gone = {i.name for i in items}
        cur = p.cursor()
        after = [e.name for e in p.entries[cur:] if e.name not in gone] or \
                [e.name for e in p.entries[:cur] if e.name not in gone][-1:]
        land = after[0] if after else None
        if p.remote:
            board = self.board() if p.kind == KIND_BOARD else None
            self.busy = True
            prog = ProgressDialog(self, "Delete", "Deleting...")
            errors = []

            def job():
                for n, it in enumerate(items, 1):
                    if prog.cancelled:
                        break
                    self.post(prog.update_progress, f"Item {n} of {len(items)}", it.name, (n - 1) / len(items))
                    try:
                        if p.kind == KIND_BOARD:
                            board.delete(board_join(p.path, it.name))
                        else:
                            self.ftp.delete(p.path, it.name, it.is_dir)
                    except NotConnected:
                        raise
                    except (FTPError, BoardError) as e:
                        errors.append(f"{it.name}: {str(e).splitlines()[-1]}")

            def done(_):
                self.busy = False
                prog.finish()
                p.marked.clear()
                p.load(p.path, land)
                if errors:
                    self.message("Delete", "Some items could not be deleted:\n\n" + "\n".join(errors[:12]),
                                 style="red")

            def err(e):
                self.busy = False
                prog.finish()
                self.show_error(e)
                p.refresh()

            self.worker.submit(job, done, err)
        else:
            errors = []
            for it in items:
                path = os.path.join(p.path, it.name)
                try:
                    if send2trash:
                        send2trash(path)
                    elif it.is_dir:
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{it.name}: {e}")
            p.marked.clear()
            p.load(p.path, land)
            if errors:
                self.message("Delete", "Some items could not be deleted:\n\n" + "\n".join(errors[:12]), style="red")

    # ----------------------------------------------------- setup / help / log
    def first_run(self):
        self.message("Welcome",
                     "Welcome to MEGA65 SD Commander!\n\n"
                     f"First, tell me where {FTP_NAME} is.\n"
                     "(Get it from mega65-tools on files.mega65.org)")
        self.setup()

    def setup(self):
        if self.busy:
            return
        c = self.cfg
        d = NCDialog(self, "Setup")
        d.label("mega65_ftp program:", anchor="w")
        exe_var, exe_ent = d.entry(c.get("exe_path", ""), width=60)
        browse = tk.Label(d.body, text="[ Browse... ]", bg=GREY, fg=BLACK, font=self.font, cursor="hand2")
        browse.pack(anchor="e")

        def do_browse(e=None):
            types = [("mega65_ftp", "mega65_ftp*.exe"), ("All files", "*.*")] if IS_WINDOWS else [("All", "*")]
            d.grab_release()
            self.native_dialog_open = True           # pause the "keep boxes on top" check
            # On Linux the borderless box sits above every normal window, including Tk's
            # file picker, so the picker would open hidden behind it: hide the box meanwhile.
            # (Windows and macOS show their own picker in front, so nothing changes there.)
            hide = not IS_WINDOWS and not IS_MAC
            if hide:
                d.withdraw()
            try:
                p = filedialog.askopenfilename(parent=self.root, title="Find mega65_ftp", filetypes=types)
            finally:
                self.native_dialog_open = False
                if hide:
                    try:
                        d.deiconify()
                        d.wait_visibility()
                    except tk.TclError:
                        pass
            self.raise_dialog()
            if p:
                exe_var.set(os.path.normpath(p))

        browse.bind("<Button-1>", do_browse)
        d.label("MEGA65 IPv6 address%interface (blank = find automatically):", anchor="w")
        ip_var, _ = d.entry(c.get("ip_iface", ""), width=60)
        d.label("Advanced - SD image / device instead of Ethernet (blank = Ethernet):", anchor="w")
        dev_var, _ = d.entry(c.get("device", ""), width=60)
        d.label("Advanced - extra mega65_ftp arguments:", anchor="w")
        extra_var, _ = d.entry(c.get("extra_args", ""), width=60)
        exp_var = tk.BooleanVar(value=self.exp_enabled())
        exp_lbl = tk.Label(d.body, font=self.font, bg=GREY, fg=BLACK, cursor="hand2", anchor="w")
        exp_lbl.pack(fill="x", pady=(6, 0))

        def paint_exp():
            exp_lbl.configure(text=f"[{'X' if exp_var.get() else ' '}] I have the MEGA65 JTAG expansion board"
                                   f" (EXP Board SD card, launching cores)")

        def flip_exp(e=None):
            exp_var.set(not exp_var.get())
            paint_exp()

        exp_lbl.bind("<Button-1>", flip_exp)
        paint_exp()
        burl, _u, _p, bsrc = self.board_info()
        found = f"{burl}  (from {bsrc})" if burl and bsrc and bsrc != "Setup" else "not found in .m65j.config"
        d.label(f"Expansion board address - blank = .m65j.config:\n  {found}",
                anchor="w", justify="left")
        brow = tk.Frame(d.body, bg=GREY)
        brow.pack(fill="x")
        tk.Label(brow, text="URL", font=self.font, bg=GREY, fg=BLACK).pack(side="left")
        burl_var = tk.StringVar(value=c.get("board_url", ""))
        tk.Entry(brow, textvariable=burl_var, width=26, font=self.font, bg=DCYAN, fg=BLACK, relief="flat",
                 highlightthickness=0).pack(side="left", padx=(4, 10))
        tk.Label(brow, text="User", font=self.font, bg=GREY, fg=BLACK).pack(side="left")
        buser_var = tk.StringVar(value=c.get("board_user", ""))
        tk.Entry(brow, textvariable=buser_var, width=10, font=self.font, bg=DCYAN, fg=BLACK, relief="flat",
                 highlightthickness=0).pack(side="left", padx=(4, 10))
        tk.Label(brow, text="Password", font=self.font, bg=GREY, fg=BLACK).pack(side="left")
        bpw_var = tk.StringVar(value=c.get("board_password", ""))
        tk.Entry(brow, textvariable=bpw_var, width=10, show="*", font=self.font, bg=DCYAN, fg=BLACK,
                 relief="flat", highlightthickness=0).pack(side="left", padx=(4, 0))
        row = tk.Frame(d.body, bg=GREY)
        row.pack(fill="x", pady=4)
        auto_var = tk.BooleanVar(value=bool(c.get("connect_on_start")))
        auto_lbl = tk.Label(row, font=self.font, bg=GREY, fg=BLACK, cursor="hand2")
        auto_lbl.pack(side="left")

        def paint_auto():
            auto_lbl.configure(text=f"[{'X' if auto_var.get() else ' '}] Connect when the program starts")

        def flip(e=None):
            auto_var.set(not auto_var.get())
            paint_auto()

        auto_lbl.bind("<Button-1>", flip)
        paint_auto()
        tk.Label(row, text="   Font size:", font=self.font, bg=GREY, fg=BLACK).pack(side="left")
        fs_var = tk.StringVar(value=str(c.get("font_size", 11)))
        tk.Entry(row, textvariable=fs_var, width=4, font=self.font, bg=DCYAN, fg=BLACK, relief="flat",
                 highlightthickness=0).pack(side="left")
        d.add_buttons(["Save", "Cancel"])
        exe_ent.bind("<Return>", lambda e: d.close(0))
        res = d.run(focus=exe_ent)
        if res != 0:
            return
        conn_changed = (exe_var.get().strip() != c.get("exe_path") or ip_var.get().strip() != c.get("ip_iface")
                        or dev_var.get().strip() != c.get("device") or extra_var.get().strip() != c.get("extra_args"))
        exe = exe_var.get().strip().strip('"')
        if exe and os.path.isdir(exe):          # a folder was given - look inside it
            for cand in FTP_NAMES:
                if os.path.isfile(os.path.join(exe, cand)):
                    exe = os.path.join(exe, cand)
                    break
        if exe and not os.path.isfile(exe):
            self.message("Setup", f"mega65_ftp was not found at:\n{exe}\n\nPlease check the location.",
                         style="red")
        c["exe_path"] = exe
        c["ip_iface"] = ip_var.get().strip()
        c["device"] = dev_var.get().strip().strip('"')
        c["extra_args"] = extra_var.get().strip()
        c["board_url"] = burl_var.get().strip()
        c["board_user"] = buser_var.get().strip()
        c["board_password"] = bpw_var.get()
        c["exp_board"] = exp_var.get()
        c["connect_on_start"] = auto_var.get()
        try:
            fs = max(7, min(28, int(fs_var.get())))
        except ValueError:
            fs = c.get("font_size", 11)
        if fs != c.get("font_size"):
            c["font_size"] = fs
            self.message("Setup", "The new font size will be used next time you start the program.")
        save_config(c)
        self.apply_exp_setting()
        if conn_changed and self.ftp_connected():
            self.message("Setup", "Connection settings changed.\nPress F4 twice to reconnect with them.")

    def help(self):
        txt = (
            "Tab / ← →     Switch panel          Enter      Open folder\n"
            "Backspace     Parent folder         F1         Drive: choose what the\n"
            "Click title   Drive menu                       active panel shows\n"
            "Space / Ins   Mark / unmark         + / -      Mark all / none\n"
            "Right click   Mark / unmark         Ctrl+A     Mark all\n"
            "Letter key    Jump to name          Ctrl+R/F2  Refresh folder\n"
            "\n"
            "Enter on a .zip   Open it like a folder - F5 extracts marked items\n"
            "Enter on a .d64/.d71/.d81  Open the disk image (read only) - F5\n"
            "                  extracts files as NAME.prg / NAME.seq ...\n"
            "X on a .zip       Extract all of it into a new folder with the\n"
            "                  ZIP's name, in the other panel\n"
            "Enter on a .bit/.cor/.m65j  Launch it on the MEGA65 (via the\n"
            "                            JTAG expansion board)\n"
            "F3  View the file: text, PETSCII, C64 BASIC or hex\n"
            "    (Enter on a file inside a ZIP or disk image views it too)\n"
            "F4  Connect / disconnect the MEGA65   (Ctrl+L: mega65_ftp log)\n"
            "F5  Copy marked items to the other panel (folders included)\n"
            "F6  Rename           F7  Make folder\n"
            "F8  Delete (Del) - in a disk image: switch C64 character set\n"
            "F9  Setup            F10  Quit       Ctrl+H or ?  This help\n"
            "\n"
            "Either panel can show a PC drive, the MEGA65 SD card or the\n"
            "EXP Board SD card (F1 Drive). F5 copies between any two.\n"
            "EXP board: core files only (.bit/.cor/.m65j); no folders/rename.\n"
            "While connected, the MEGA65 is busy running the transfer helper;\n"
            "disconnecting (F4 or quitting) hands it back to you."
        )
        self.message(f"{APP_NAME} {APP_VERSION} - Help", txt)

    def show_log(self):
        d = NCDialog(self, "mega65_ftp log")
        t = tk.Text(d.body, width=100, height=24, font=self.font, bg=NAVY, fg=CYAN, relief="flat",
                    highlightthickness=0, wrap="none")
        t.pack(fill="both", expand=True)
        t.insert("end", "\n".join(self.log) or "(nothing yet)")
        t.see("end")
        t.configure(state="disabled")
        d.add_buttons(["Close"])
        d.run()

    def quit(self):
        if self.busy:
            if self.message("Quit", "A job is still running.\nQuit anyway?", ("Yes", "No"), default=1) != 0:
                return
        elif self.message("Quit", f"Do you want to quit {APP_NAME}?", ("Yes", "No")) != 0:
            return
        for p in (self.left, self.right):
            self._remember(p)
        self.cfg["left_mode"], self.cfg["right_mode"] = self.left.kind, self.right.kind
        save_config(self.cfg)
        if self.ftp_connected() and not self.busy:
            self.disconnect(then=self.root.destroy)
            self.root.after(10000, self.root.destroy)   # safety net
        else:
            if self.ftp:
                self.ftp._kill()
            self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
