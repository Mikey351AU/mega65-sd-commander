"""
m65board.py - launch cores on the MEGA65 through the Expansion Board
Integrated JTAG (Pico W) web interface.

  PUT  /jtag?name=<name>   body = core file  -> board spools it to DOWNLOADS/
                                                and programs the MEGA65 via JTAG
  GET  /jtag?file=<path>                      -> program a core already on the
                                                board's own SD card

Board address / login come from .m65j.config (same file m65j.py uses:
url= / ip= [port=], user=, password=), unless overridden in Setup.
No GUI code in here.
"""

import base64
import html
import http.client
import os
import re
import urllib.error
import urllib.parse
import urllib.request

LAUNCH_EXTS = (".bit", ".cor", ".m65j")
UPLOAD_EXTS = LAUNCH_EXTS          # the board firmware only stores core files
LIST_TIMEOUT = 20
HTTP_TIMEOUT = 180

HTTP_HINTS = {
    401: "wrong or missing user/password in .m65j.config",
    403: "this PC's IP is not covered by an allow= rule in mega65-jtag.cfg (launching cores also "
         "needs bitstreams=1 on that rule), or the file type is not allowed",
    423: "write grant not active (press the write-enable button, or set require_write_grant=0) "
         "or the board is busy fetching cores",
}


class BoardError(Exception):
    pass


def config_candidates(app_dir):
    home = os.path.expanduser("~")
    return [
        os.path.join(app_dir, ".m65j.config"),
        os.path.join(home, ".m65j.config"),
        os.path.join(home, "mega65-expansion-board-integrated-jtag", "tools", ".m65j.config"),
        os.path.join(home, "mega65-expansion-board-integrated-jtag", ".m65j.config"),
    ]


def read_m65j_config(path):
    cfg = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw in f.read().splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip().lower()] = v.strip()
    return cfg


def normalize_url(device):
    if not device:
        return ""
    device = device.strip().rstrip("/")
    if not device.startswith(("http://", "https://")):
        device = "http://" + device
    return device


def board_from_m65j(cfg):
    url = cfg.get("url") or cfg.get("web_url") or cfg.get("http_url") or cfg.get("base_url")
    dev = cfg.get("device", "")
    if not url and dev and not dev.upper().startswith(("COM", "/DEV/")):
        url = dev
    if not url:
        ip = cfg.get("ip") or cfg.get("host")
        if ip:
            url = f"{ip}:{cfg['port']}" if cfg.get("port") else ip
    return normalize_url(url), (cfg.get("user") or cfg.get("username")), cfg.get("password")


def find_board(app_dir, url_override="", user_override="", password_override=""):
    """Returns (url, user, password, source_description)."""
    url, user, pw, src = "", None, None, ""
    for p in config_candidates(app_dir):
        if os.path.isfile(p):
            try:
                url, user, pw = board_from_m65j(read_m65j_config(p))
            except OSError:
                continue
            if url:
                src = p
                break
    if url_override.strip():
        url, src = normalize_url(url_override), "Setup"
    if user_override.strip():
        user = user_override.strip()
    if password_override:
        pw = password_override
    return url, user, pw, src


class _ProgressReader:
    def __init__(self, f, total, cb):
        self.f, self.total, self.cb, self.done = f, total, cb, 0

    def read(self, n=-1):
        chunk = self.f.read(65536 if n is None or n < 0 else n)
        self.done += len(chunk)
        if self.cb:
            self.cb(self.done, self.total)
        return chunk


class BoardEntry:
    __slots__ = ("name", "path", "is_dir", "size", "date", "title", "kind")

    def __init__(self, name, path, is_dir, size, date, title, kind):
        self.name, self.path, self.is_dir, self.size = name, path, is_dir, size
        self.date, self.title, self.kind = date, title, kind

    def __repr__(self):
        return f"BoardEntry({self.path!r}, dir={self.is_dir}, size={self.size})"


def board_join(d, name):
    return f"{d}/{name}" if d else name


def board_parent(d):
    return d.rsplit("/", 1)[0] if "/" in d else ""


class Board:
    ROW_RE = re.compile(r'<tr class="entry-row"[^>]*?data-kind="([^"]*)"[^>]*>(.*?)</tr>', re.S | re.I)
    NAME_RE = re.compile(r'<a class="core-name" href="([^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
    CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)

    def __init__(self, url, user=None, password=None):
        self.last_list_incomplete = False
        if not url:
            raise BoardError("No expansion board address found.\n"
                             "Add url=http://<board-ip> to .m65j.config, or set it in Setup (F9).")
        self.base = normalize_url(url)
        self.user, self.password = user, password

    def _auth(self, req):
        if self.user is not None or self.password is not None:
            tok = base64.b64encode(f"{self.user or ''}:{self.password or ''}".encode()).decode()
            req.add_header("Authorization", f"Basic {tok}")

    @staticmethod
    def _http_text(e):
        body = e.read(240).decode("utf-8", "replace").strip()
        msg = f"HTTP {e.code} {e.reason}" + (f": {body}" if body else "")
        if e.code in HTTP_HINTS:
            msg += f"\n\nLikely cause: {HTTP_HINTS[e.code]}"
        return msg

    def _open(self, req, timeout=HTTP_TIMEOUT, allow_partial=False):
        self._auth(req)
        self.last_missing = 0
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                try:
                    data = r.read()
                except http.client.IncompleteRead as e:
                    # Board firmware bug: pages bigger than its TCP send buffer (~11.5 KB)
                    # are cut short. Keep what arrived when the caller can cope with that.
                    if not allow_partial:
                        raise
                    data = e.partial
                    self.last_missing = e.expected or 0
                return data.decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as e:
            raise BoardError(self._http_text(e))
        except http.client.HTTPException as e:
            raise BoardError(f"The expansion board sent a broken reply ({e.__class__.__name__}: {e})")
        except (urllib.error.URLError, OSError) as e:
            raise BoardError(f"Could not reach the expansion board at {self.base}\n{e}")

    @staticmethod
    def _strip(s):
        return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()

    def list(self, path):
        """List a folder on the board's SD card. path: "" = root, "CORES/R6" etc.
        """
        q = urllib.parse.quote(path if path else "/", safe="")
        req = urllib.request.Request(self.base + f"/index.html?path={q}")
        page = self._open(req, timeout=LIST_TIMEOUT, allow_partial=True)
        missing = self.last_missing
        if "busy fetching" in page.lower() or "<h1>Fetching cores</h1>" in page:
            raise BoardError("The board is busy fetching cores. Try again when it has finished.")
        entries, row_lens = [], []
        last_end = 0
        for m in self.ROW_RE.finditer(page):
            row_lens.append(m.end() - m.start())
            last_end = m.end()
            e = self._parse_row(m.group(1), m.group(2), path)
            if e:
                entries.append(e)
        table_end = "</tbody>" in page.lower() or "</table>" in page.lower()
        self.last_list_incomplete = False
        if missing and not table_end:
            # The page was cut inside the rows. Rescue a half-received last row
            # (its name, date and size arrive before its download/delete buttons).
            tail = page[last_end:]
            t = re.search(r'<tr class="entry-row"[^>]*?data-kind="([^"]*)"[^>]*>(.*)$', tail, re.S | re.I)
            if t:
                e = self._parse_row(t.group(1), t.group(2), path)
                if e:
                    entries.append(e)
            # Missing bytes = rest of that row + the page footer (>= 182 bytes, the
            # firmware's built-in one) + any rows that never arrived. If fewer bytes
            # are missing than the footer plus one whole row, no row can be lost.
            shortest_row = min(row_lens) if row_lens else 200
            self.last_list_incomplete = (missing - 182) >= shortest_row
        if "SD list failed" in page:
            raise BoardError("The board reported: SD list failed")
        return entries

    def _parse_row(self, kind, body, path):
        kind = kind.upper()
        m = self.NAME_RE.search(body)
        if not m:
            return None
        href = html.unescape(m.group(1))
        label = self._strip(m.group(2))
        cells = self.CELL_RE.findall(body)
        modified = self._strip(cells[1]) if len(cells) > 1 else ""
        size_txt = self._strip(cells[2]) if len(cells) > 2 else ""
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        if kind == "DIR" and "path" in qs:
            full = qs["path"][0]
        elif "file" in qs:
            full = qs["file"][0]
        elif "theme" in qs:
            full = board_join(path, qs["theme"][0])
        elif kind == "PARTIAL":
            full = board_join(path, label + ".partial")
        else:
            full = board_join(path, label)
        full = full.strip("/")
        name = full.rsplit("/", 1)[-1]
        title = label if label != name and kind != "PARTIAL" else ""
        return BoardEntry(name, full, kind == "DIR",
                          int(size_txt) if size_txt.isdigit() else 0,
                          modified if modified != "-" else "", title, kind)

    def upload(self, local_path, remote_path, progress=None):
        """Store a core file on the board's SD card (PUT /files/<path>)."""
        if not remote_path.lower().endswith(UPLOAD_EXTS):
            raise BoardError("The expansion board only accepts .bit, .cor and .m65j files.")
        total = os.path.getsize(local_path)
        if total == 0:
            raise BoardError("empty files can't be uploaded")
        url = self.base + "/files/" + urllib.parse.quote(remote_path.lstrip("/"), safe="/")
        with open(local_path, "rb") as f:
            req = urllib.request.Request(url, data=_ProgressReader(f, total, progress), method="PUT")
            req.add_header("Content-Length", str(total))
            req.add_header("Content-Type", "application/octet-stream")
            return self._open(req)

    def download(self, remote_path, local_path, progress=None):
        url = self.base + "/files/" + urllib.parse.quote(remote_path.lstrip("/"), safe="/")
        req = urllib.request.Request(url)
        self._auth(req)
        tmp = local_path + ".part"
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r, open(tmp, "wb") as out:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            os.replace(tmp, local_path)
        except urllib.error.HTTPError as e:
            raise BoardError(self._http_text(e))
        except (urllib.error.URLError, OSError) as e:
            raise BoardError(f"Download failed: {e}")
        except http.client.HTTPException as e:
            raise BoardError(f"Download failed - the board sent a broken reply ({e.__class__.__name__}: {e})")
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def delete(self, remote_path):
        q = urllib.parse.quote(remote_path, safe="")
        req = urllib.request.Request(self.base + f"/delete?file={q}", data=b"", method="PUT")
        req.add_header("Content-Length", "0")
        return self._open(req)

    def launch_file(self, local_path, name=None, progress=None):
        """Send a core from this PC; the board programs the MEGA65 with it."""
        name = name or os.path.basename(local_path)
        total = os.path.getsize(local_path)
        if total == 0:
            raise BoardError("The file is empty.")
        url = self.base + "/jtag?name=" + urllib.parse.quote(name, safe="")
        with open(local_path, "rb") as f:
            req = urllib.request.Request(url, data=_ProgressReader(f, total, progress), method="PUT")
            req.add_header("Content-Length", str(total))
            req.add_header("Content-Type", "application/octet-stream")
            return self._open(req)

    def launch_board_sd(self, remote_path):
        """Program a core that is already on the expansion board's own SD card."""
        q = urllib.parse.quote(remote_path, safe="")
        return self._open(urllib.request.Request(self.base + f"/jtag?file={q}"))
