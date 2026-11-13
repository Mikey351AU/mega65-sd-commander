"""
m65ftp.py - Python wrapper around the MEGA65 'mega65_ftp' tool.

Keeps ONE mega65_ftp process running (so the helper routine is only pushed to
the MEGA65 once per session) and talks to it over stdin/stdout, using the
"MEGA65 SD-Card:<dir>> " prompt to know when each command has finished.

Designed to be usable on its own (no GUI code in here).

    ftp = Mega65FTP(exe_path="mega65_ftp.exe")       # ethernet auto-discover
    ftp.connect()
    for e in ftp.listdir("/"):
        print(e)
    ftp.put(r"C:\\games\\elite.d81", "/", "ELITE.D81")
    ftp.close()
"""

import locale
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

PROMPT_RE = re.compile(r"MEGA65 SD-Card:(.*?)> $")
YESNO_RE = re.compile(r"\(y/n\)\??\s*$", re.IGNORECASE)
DIR_LINE_RE = re.compile(
    r"^\s*(<DIR>|\d+)\s+\|\s+(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*\|\s(.{1,12}?)\s*\|\s?(.*)$"
)
ERROR_HINTS = (
    "ERROR", " ERRO ", " CRIT ", "does not exist", "Could not", "could not",
    "unknown command", "No response", "Unable to",
)

IS_WINDOWS = sys.platform.startswith("win")

# mega65_ftp keeps file sizes in 32-bit signed ints: a file of 2 GiB or more is
# "uploaded" as an empty entry (and hidden/unreadable on Windows builds).
MAX_FILE_SIZE = 2 ** 31 - 1
UPLOADED_RE = re.compile(r"Uploaded (\d+) bytes in")


def too_big_text(name, size):
    return (f'"{name}" is {size / 1024 ** 3:.2f} GB - too big: mega65_ftp can only transfer '
            f"files under 2 GB (a limit inside mega65_ftp itself)")


class FTPError(Exception):
    pass


class NotConnected(FTPError):
    pass


@dataclass
class RemoteEntry:
    name: str          # long name if present, else 8.3 name
    short_name: str    # 8.3 name
    is_dir: bool
    size: int
    date: str          # "YYYY-MM-DD HH:MM:SS"


def _quote(s):
    if '"' in s:
        raise FTPError('Names containing " cannot be sent to mega65_ftp')
    return f'"{s}"'


def _is_ascii(s):
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def safe_remote_name(name):
    """FAT/mega65_ftp friendly name: ASCII only, no illegal characters."""
    bad = '"*/:<>?\\|'
    out = "".join(c if (32 <= ord(c) < 127 and c not in bad) else "_" for c in name)
    out = out.strip().rstrip(".")
    return out or "_"


def remote_join(d, name):
    return ("/" + name) if d in ("", "/") else (d.rstrip("/") + "/" + name)


def remote_parent(d):
    if d in ("", "/"):
        return "/"
    p = d.rstrip("/").rsplit("/", 1)[0]
    return p or "/"


class Mega65FTP:
    """Thread-safe (serialised) wrapper; call from a worker thread, not the GUI thread."""

    def __init__(self, exe_path, ip_iface="", device="", extra_args="",
                 startup_timeout=60.0, log_cb=None):
        self.exe_path = exe_path
        self.ip_iface = ip_iface.strip()
        self.device = device.strip()          # -d <image/device> (testing / card reader)
        self.extra_args = extra_args.strip()
        self.startup_timeout = startup_timeout
        self.log_cb = log_cb                  # called with each raw output chunk
        self.proc = None
        self.cwd = "/"
        self._q = queue.Queue()
        self._lock = threading.RLock()
        self._enc = locale.getpreferredencoding(False) or "utf-8"
        self.help_text = ""
        self.version = ""
        self.options = set()

    # ------------------------------------------------------------- probing
    def probe(self):
        """Run 'mega65_ftp -h' to find its version and which options it knows.
        Older builds don't have -y, and very old ones have no Ethernet (-e)."""
        flags = 0x08000000 if IS_WINDOWS else 0
        try:
            r = subprocess.run([self.exe_path, "-h"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=15, creationflags=flags)
            text = r.stdout.decode(self._enc, errors="replace")
        except (OSError, subprocess.TimeoutExpired) as e:
            raise FTPError(f"Could not run mega65_ftp:\n{e}")
        self.help_text = text
        m = re.search(r"version:\s*(.+)", text)
        self.version = m.group(1).strip() if m else "(unknown - old build)"
        # option lines look like "  -y - don't ask ..." ; the usage line like "[-e]"
        self.options = set(re.findall(r"^\s*-(\w)\s+-", text, re.MULTILINE))
        self.options |= set(re.findall(r"\[-(\w)[\] ]", text))
        self._log(f"[mega65_ftp version {self.version}; options: {''.join(sorted(self.options))}]\n")
        return self.version

    # ------------------------------------------------------------- process
    @property
    def connected(self):
        return self.proc is not None and self.proc.poll() is None

    def build_args(self):
        args = [self.exe_path]
        if "y" in self.options:
            args.append("-y")
        if self.device:
            args += ["-d", os.path.abspath(self.device)]
        elif self.ip_iface:
            args += ["-i", self.ip_iface]
        else:
            args += ["-e"]
        if self.extra_args:
            args += shlex.split(self.extra_args, posix=not IS_WINDOWS)
        return args

    def connect(self):
        with self._lock:
            if self.connected:
                return
            if not self.exe_path or not os.path.isfile(self.exe_path):
                raise FTPError(f"mega65_ftp not found:\n{self.exe_path or '(not set)'}")
            self.probe()
            if not self.device and not ({"e", "i"} & self.options):
                raise FTPError(
                    "This mega65_ftp.exe does not support Ethernet (no -e option).\n"
                    f"Version: {self.version}\n\n"
                    "Please download the latest mega65-tools Windows build\n"
                    "from files.mega65.org and point Setup (F9) at it.")
            self._q = queue.Queue()
            flags = 0x08000000 if IS_WINDOWS else 0   # CREATE_NO_WINDOW
            self._log("[starting] " + subprocess.list2cmdline(self.build_args()) + "\n")
            try:
                self.proc = subprocess.Popen(
                    self.build_args(),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, bufsize=0,
                    cwd=os.path.dirname(os.path.abspath(self.exe_path)),
                    creationflags=flags,
                )
            except OSError as e:
                self.proc = None
                raise FTPError(f"Could not start mega65_ftp:\n{e}")
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
            try:
                out = self._wait_prompt(self.startup_timeout)
            except FTPError as e:
                self._kill()
                msg = str(e)
                if "Usage: mega65_ftp" in msg or "Mode options" in msg:
                    msg = ("mega65_ftp rejected the options it was given:\n"
                           f"  {' '.join(self.build_args()[1:])}\n"
                           f"Version: {self.version}\n\n"
                           "Check 'extra arguments' in Setup (F9), or update mega65_ftp.")
                raise FTPError("Could not connect to the MEGA65.\n\n" + msg +
                               "\n\nCommand: " + subprocess.list2cmdline(self.build_args()) +
                               f"\nmega65_ftp version: {self.version}")
            self._log(f"[connected] {out.strip()[-200:]}\n")

    def close(self):
        """Send 'exit' (lets the MEGA65 continue) and stop the process."""
        with self._lock:
            if not self.connected:
                self.proc = None
                return
            try:
                self._send("exit")
                self.proc.wait(timeout=8)
            except Exception:
                pass
            self._kill()

    def _kill(self):
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def _reader(self, proc):
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                self._q.put(None)
                return
            self._q.put(chunk.decode(self._enc, errors="replace"))

    def _log(self, text):
        if self.log_cb:
            try:
                self.log_cb(text)
            except Exception:
                pass

    def _send(self, line):
        self._log(f"> {line}\n")
        self.proc.stdin.write((line + "\n").encode(self._enc, errors="replace"))
        self.proc.stdin.flush()

    def _wait_prompt(self, timeout=None):
        buf = ""
        deadline = time.time() + timeout if timeout else None
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.time())
            if deadline is not None and remaining == 0:
                raise FTPError("Timed out waiting for mega65_ftp.\n" + buf[-600:])
            try:
                chunk = self._q.get(timeout=remaining if remaining is not None else 0.5)
            except queue.Empty:
                if self.proc is None or self.proc.poll() is not None:
                    chunk = None
                else:
                    continue
            if chunk is None:
                self.proc = None
                raise NotConnected("mega65_ftp stopped.\n" + buf[-800:].strip())
            self._log(chunk)
            buf += chunk
            if YESNO_RE.search(buf):
                # older builds without -y ask before deleting folders / wildcards;
                # the GUI has already asked the user, so answer yes.
                self._send("y")
                buf += "\n"
                continue
            m = PROMPT_RE.search(buf)
            if m:
                self.cwd = m.group(1) or "/"
                return buf[:m.start()]

    def command(self, line, check=True):
        """Run one mega65_ftp command; returns its output text."""
        with self._lock:
            if not self.connected:
                raise NotConnected("Not connected to the MEGA65.")
            if len(line) > 1000:
                raise FTPError("Command too long for mega65_ftp (path too long?)")
            self._send(line)
            out = self._wait_prompt()
            # non-Windows readline builds echo the command - strip it
            lines = out.splitlines()
            if lines and lines[0].strip() == line.strip():
                out = "\n".join(lines[1:])
            out = out.replace("\r", "\n")
            if check and any(h in out for h in ERROR_HINTS):
                raise FTPError(self._clean(out) or f"'{line}' failed")
            return out

    @staticmethod
    def _clean(out):
        keep = [l.strip() for l in out.splitlines()
                if l.strip() and not l.startswith(("Uploaded", "Downloaded", "- Using DOS"))]
        return "\n".join(keep[-8:])

    # -------------------------------------------------------------- file ops
    def cd(self, path):
        with self._lock:
            if self.cwd != path:
                self.command(f"cd {_quote(path)}")
                if self.cwd != path and path != "/":
                    raise FTPError(f"Could not open folder {path}")

    def listdir(self, path=None):
        with self._lock:
            if path:
                self.cd(path)
            out = self.command("dir", check=False)
            entries = []
            for line in out.splitlines():
                m = DIR_LINE_RE.match(line)
                if not m:
                    continue
                kind, date, short, longn = m.groups()
                short = short.strip()
                longn = longn.strip()
                name = longn or short
                if name in (".", ".."):
                    continue
                isdir = kind == "<DIR>"
                entries.append(RemoteEntry(name, short, isdir, 0 if isdir else int(kind), date))
            if not entries and ("ERROR" in out or "Could not" in out):
                raise FTPError(self._clean(out))
            return entries

    def mkdir(self, remote_dir, name):
        with self._lock:
            self.cd(remote_dir)
            self.command(f"mkdir {_quote(name)}")

    def delete(self, remote_dir, name, is_dir=None):
        """Deletes a file, or a folder and everything in it."""
        with self._lock:
            if "y" not in self.options:
                # Older mega65_ftp asks "(y/n)?" before deleting a non-empty folder and
                # that question never reaches us through the pipe -> empty it ourselves.
                if is_dir is None:
                    is_dir = any(e.is_dir and e.name.lower() == name.lower()
                                 for e in self.listdir(remote_dir))
                if is_dir:
                    sub = remote_join(remote_dir, name)
                    for e in self.listdir(sub):
                        self.delete(sub, e.name, e.is_dir)
            self.cd(remote_dir)
            out = self.command(f"del {_quote(name)}")
            if "successfully deleted" not in out:
                raise FTPError(self._clean(out) or f"Could not delete {name}")

    def rename(self, remote_dir, old, new):
        with self._lock:
            self.cd(remote_dir)
            self.command(f"rename {_quote(old)} {_quote(new)}")

    def put(self, local_path, remote_dir, remote_name=None):
        remote_name = safe_remote_name(remote_name or os.path.basename(local_path))
        size = os.path.getsize(local_path)
        if size > MAX_FILE_SIZE:
            raise FTPError(too_big_text(os.path.basename(local_path), size))
        with self._lock:
            self.cd(remote_dir)
            tmpdir = None
            src = os.path.abspath(local_path)
            if not _is_ascii(src) or '"' in src:
                tmpdir = tempfile.mkdtemp(prefix="m65sd_")
                tmp = os.path.join(tmpdir, "upload.bin")
                shutil.copyfile(src, tmp)
                src = tmp
            try:
                out = self.command(f"put {_quote(src)} {_quote(remote_name)}")
            finally:
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            if "Uploaded" not in out:
                raise FTPError(self._clean(out) or f"Upload of {remote_name} failed")
            # never trust "Uploaded" on its own - check the byte count
            m = UPLOADED_RE.findall(out)
            if m and int(m[-1]) != size:
                raise FTPError(f"Upload of {remote_name} incomplete: mega65_ftp reported "
                               f"{int(m[-1]):,} of {size:,} bytes")

    def get(self, remote_dir, remote_name, local_path):
        with self._lock:
            self.cd(remote_dir)
            dest = os.path.abspath(local_path)
            tmpdir = None
            if not _is_ascii(dest) or '"' in dest:
                tmpdir = tempfile.mkdtemp(prefix="m65sd_")
                dest = os.path.join(tmpdir, "download.bin")
            try:
                out = self.command(f"get {_quote(remote_name)} {_quote(dest)}")
                if "Downloaded" not in out or not os.path.exists(dest):
                    raise FTPError(self._clean(out) or f"Download of {remote_name} failed")
                if tmpdir:
                    shutil.move(dest, local_path)
            finally:
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)

    def walk(self, remote_dir):
        """Yield (dir, entries) for remote_dir and every folder below it."""
        stack = [remote_dir]
        while stack:
            d = stack.pop()
            ents = self.listdir(d)
            yield d, ents
            for e in ents:
                if e.is_dir:
                    stack.append(remote_join(d, e.name))
