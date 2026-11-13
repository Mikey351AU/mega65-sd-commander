"""
m65cbm.py - read-only access to Commodore disk images (.D64 / .D71 / .D81),
plus PETSCII and C64 BASIC V2 helpers for viewing files.

No GUI code in here.

    img = DiskImage("games.d81")
    print(img.disk_name, img.disk_id, img.blocks_free())
    for e in img.list():                # root directory
        print(e.blocks, e.name, e.type_name)
    data = img.read_file(e)             # bytes of a file
    for e in img.list(part):            # inside a 1581 partition (part = an entry)
        ...
"""

import os

TYPE_NAMES = {0: "DEL", 1: "SEQ", 2: "PRG", 3: "USR", 4: "REL", 5: "CBM"}
IMAGE_EXTS = (".d64", ".d71", ".d81")

# (size, kind, tracks, has_error_bytes)
_SIZES = {
    174848: ("d64", 35, False), 175531: ("d64", 35, True),
    196608: ("d64", 40, False), 197376: ("d64", 40, True),
    205312: ("d64", 42, False), 206114: ("d64", 42, True),
    349696: ("d71", 70, False), 351062: ("d71", 70, True),
    819200: ("d81", 80, False), 822400: ("d81", 80, True),
}


class DiskImageError(Exception):
    pass


def is_disk_image(name):
    return name.lower().endswith(IMAGE_EXTS)


def _d64_spt(track):
    """Sectors per track on a 1541 (tracks above 35 continue the last zone)."""
    if track <= 17:
        return 21
    if track <= 24:
        return 19
    if track <= 30:
        return 18
    return 17


# ---------------------------------------------------------------- PETSCII
def petscii_char(b, lower=False):
    """One PETSCII byte as a readable character (None for control codes)."""
    if b == 0x5C:
        return "£"
    if b == 0x5E:
        return "↑"
    if b == 0x5F:
        return "←"
    if b == 0xFF or b == 0xDE:
        return "π"
    if 0x20 <= b <= 0x40 or 0x5B <= b <= 0x5D:
        return chr(b)
    if 0x41 <= b <= 0x5A:                  # unshifted letters
        return chr(b + 32) if lower else chr(b)
    if 0x61 <= b <= 0x7A or 0xC1 <= b <= 0xDA:   # shifted letters
        base = (b - 0x61) if b < 0x80 else (b - 0xC1)
        return chr(0x41 + base) if lower else "▒"
    if b == 0xA0:
        return " "                          # shifted space
    if 0x60 <= b <= 0x7F or 0xA1 <= b <= 0xBF or 0xDB <= b <= 0xFE:
        return "▒"                          # graphics character
    return None


def petscii_name(raw, lower=False):
    """A directory name (padded with $A0) as text."""
    raw = bytes(raw).rstrip(b"\xa0")
    out = []
    for b in raw:
        c = petscii_char(b, lower)
        out.append(c if c is not None else "?")
    return "".join(out)


CONTROL_NAMES = {
    0x05: "wht", 0x0D: "cr", 0x0E: "swlc", 0x11: "down", 0x12: "rvon", 0x13: "home",
    0x14: "del", 0x1C: "red", 0x1D: "rght", 0x1E: "grn", 0x1F: "blu", 0x81: "orng",
    0x85: "f1", 0x86: "f3", 0x87: "f5", 0x88: "f7", 0x89: "f2", 0x8A: "f4", 0x8B: "f6",
    0x8C: "f8", 0x8D: "sret", 0x8E: "swuc", 0x90: "blk", 0x91: "up", 0x92: "rvof",
    0x93: "clr", 0x94: "inst", 0x95: "brn", 0x96: "lred", 0x97: "gry1", 0x98: "gry2",
    0x99: "lgrn", 0x9A: "lblu", 0x9B: "gry3", 0x9C: "pur", 0x9D: "left", 0x9E: "yel",
    0x9F: "cyn",
}


def petscii_text(data, lower=True):
    """A PETSCII text file (e.g. a SEQ file) as readable text. Control codes are
    dropped, except carriage return ($0D) which becomes a new line."""
    out = []
    for b in data:
        if b == 0x0D or b == 0x8D:
            out.append("\n")
            continue
        if b == 0x0A:
            continue
        c = petscii_char(b, lower)
        if c is not None:
            out.append(c)
    return "".join(out)


# ---------------------------------------------------------------- BASIC V2
BASIC_V2_TOKENS = (
    "END FOR NEXT DATA INPUT# INPUT DIM READ LET GOTO RUN IF RESTORE GOSUB RETURN REM "
    "STOP ON WAIT LOAD SAVE VERIFY DEF POKE PRINT# PRINT CONT LIST CLR CMD SYS OPEN "
    "CLOSE GET NEW TAB( TO FN SPC( THEN NOT STEP + - * / ^ AND OR > = < SGN INT ABS "
    "USR FRE POS SQR RND LOG EXP COS SIN TAN ATN PEEK LEN STR$ VAL ASC CHR$ LEFT$ "
    "RIGHT$ MID$ GO"
).split()          # $80 .. $CB

BASIC_V2_LOADS = (0x0801, 0x1001, 0x0401, 0x1201)   # C64, VIC-20 (+expanded), PET


def prg_load_address(data):
    return data[0] | (data[1] << 8) if len(data) >= 2 else None


def looks_like_basic_v2(data):
    """True for a PRG that loads where Commodore BASIC V2 programs live and whose
    first line links look sane."""
    load = prg_load_address(data)
    if load not in BASIC_V2_LOADS or len(data) < 6:
        return False
    link = data[2] | (data[3] << 8)
    return link == 0 or load < link < load + len(data) + 1


def detokenize_basic_v2(data, lower=False, max_lines=20000):
    """List a tokenised Commodore BASIC V2 program like the C64's LIST command.
    Inside quotes, control codes appear as {clr}, {red}, ... like petcat shows them."""
    load = prg_load_address(data)
    body = data[2:]
    pos = 0
    lines = []
    while pos + 4 <= len(body) and len(lines) < max_lines:
        link = body[pos] | (body[pos + 1] << 8)
        if link == 0:
            break
        number = body[pos + 2] | (body[pos + 3] << 8)
        pos += 4
        text = [f"{number} "]
        quote = False
        while pos < len(body) and body[pos] != 0:
            b = body[pos]
            pos += 1
            if b == 0x22:
                quote = not quote
                text.append('"')
            elif quote:
                c = petscii_char(b, lower)
                if c is not None:
                    text.append(c)
                else:
                    name = CONTROL_NAMES.get(b)
                    text.append("{" + (name or f"${b:02x}") + "}")
            elif 0x80 <= b <= 0xCB:
                tok = BASIC_V2_TOKENS[b - 0x80]
                text.append(tok.lower() if lower else tok)
            elif b == 0xFF:
                text.append("π")
            elif b >= 0x80:
                text.append(f"{{${b:02x}}}")
            else:
                c = petscii_char(b, lower)
                text.append(c if c is not None else f"{{${b:02x}}}")
        pos += 1          # skip the 0 that ends the line
        lines.append("".join(text))
        # lines are read in order (more robust than trusting each line's link address)
    del load
    return "\n".join(lines)


# ---------------------------------------------------------------- disk images
class DirEntry:
    __slots__ = ("name", "raw_name", "ftype", "closed", "locked", "track", "sector",
                 "blocks", "slot")

    def __init__(self, raw_name, ftype, closed, locked, track, sector, blocks, slot):
        self.raw_name = raw_name
        self.name = petscii_name(raw_name)
        self.ftype, self.closed, self.locked = ftype, closed, locked
        self.track, self.sector, self.blocks, self.slot = track, sector, blocks, slot

    @property
    def type_name(self):
        t = TYPE_NAMES.get(self.ftype, "???")
        return ("" if self.closed else "*") + t + ("<" if self.locked else "")

    @property
    def is_partition(self):
        return self.ftype == 5

    @property
    def extension(self):
        return TYPE_NAMES.get(self.ftype, "bin").lower()

    def __repr__(self):
        return f"DirEntry({self.name!r} {self.type_name} {self.blocks} blk @{self.track}/{self.sector})"


class DiskImage:
    """A .D64 / .D71 / .D81 image, read into memory."""

    def __init__(self, path):
        self.path = path
        size = os.path.getsize(path)
        if size not in _SIZES:
            raise DiskImageError(f"{os.path.basename(path)} doesn't look like a D64/D71/D81 image "
                                 f"({size:,} bytes is not a standard size)")
        self.kind, self.tracks, self.has_errors = _SIZES[size]
        with open(path, "rb") as f:
            self.data = f.read()
        self._offsets = self._track_offsets()
        if self.kind == "d81":
            hdr = self.sector(40, 0)
            self.raw_disk_name, self.raw_disk_id, self.raw_dos_type = hdr[0x04:0x14], hdr[0x16:0x18], hdr[0x19:0x1B]
            self.disk_name = petscii_name(self.raw_disk_name)
            self.disk_id = petscii_name(self.raw_disk_id)
            self.dos_type = petscii_name(self.raw_dos_type)
            self.dir_start = (hdr[0], hdr[1]) if hdr[0] else (40, 3)
        else:
            bam = self.sector(18, 0)
            self.raw_disk_name, self.raw_disk_id, self.raw_dos_type = bam[0x90:0xA0], bam[0xA2:0xA4], bam[0xA5:0xA7]
            self.disk_name = petscii_name(self.raw_disk_name)
            self.disk_id = petscii_name(self.raw_disk_id)
            self.dos_type = petscii_name(self.raw_dos_type)
            self.dir_start = (bam[0], bam[1]) if bam[0] else (18, 1)

    # -- geometry
    def _track_offsets(self):
        offs, pos = {}, 0
        for t in range(1, self.tracks + 1):
            offs[t] = pos
            pos += self.sectors_per_track(t) * 256
        return offs

    def sectors_per_track(self, track):
        if self.kind == "d81":
            return 40
        if self.kind == "d71" and track > 35:
            return _d64_spt(track - 35)
        return _d64_spt(track)

    def valid(self, track, sector):
        return 1 <= track <= self.tracks and 0 <= sector < self.sectors_per_track(track)

    def sector(self, track, sector):
        if not self.valid(track, sector):
            raise DiskImageError(f"bad track/sector {track}/{sector}")
        o = self._offsets[track] + sector * 256
        return self.data[o:o + 256]

    # -- directory
    def _dir_chain(self, start):
        t, s = start
        seen = set()
        while t and self.valid(t, s) and (t, s) not in seen and len(seen) < 400:
            seen.add((t, s))
            sec = self.sector(t, s)
            yield sec
            t, s = sec[0], sec[1]

    def list(self, partition=None, include_deleted=False):
        """Directory entries of the root, or of a 1581 partition (a CBM entry)."""
        start = self.dir_start
        if partition is not None:
            start = self.partition_dir_start(partition)
            if start is None:
                raise DiskImageError(f"{partition.name} is a partition without a directory")
        entries, slot = [], 0
        for sec in self._dir_chain(start):
            for i in range(8):
                e = sec[i * 32:(i + 1) * 32]
                ftype_byte = e[2]
                slot += 1
                if ftype_byte == 0 and not include_deleted:
                    continue          # empty / scratched slot
                ftype = ftype_byte & 0x07
                entries.append(DirEntry(e[5:21], ftype, bool(ftype_byte & 0x80), bool(ftype_byte & 0x40),
                                        e[3], e[4], e[30] | (e[31] << 8), slot))
        return entries

    def partition_dir_start(self, entry):
        """A 1581 partition that holds a sub-directory has a header at its first
        sector (format 'D'); returns where its directory starts, or None."""
        if self.kind != "d81" or not entry.is_partition or not self.valid(entry.track, entry.sector):
            return None
        hdr = self.sector(entry.track, entry.sector)
        if hdr[2] != 0x44 or not self.valid(hdr[0], hdr[1]):
            return None
        return hdr[0], hdr[1]

    def partition_name(self, entry):
        hdr = self.sector(entry.track, entry.sector)
        return petscii_name(hdr[0x04:0x14])

    # -- files
    def read_file(self, entry, limit=None):
        """The contents of a file (follows its sector chain). A partition (CBM)
        is returned as its raw blocks."""
        if entry.ftype == 5:
            out, t, s = bytearray(), entry.track, entry.sector
            for _ in range(entry.blocks):
                if not self.valid(t, s):
                    break
                out += self.sector(t, s)
                s += 1
                if s >= self.sectors_per_track(t):
                    t, s = t + 1, 0
            return bytes(out)
        out = bytearray()
        t, s = entry.track, entry.sector
        seen = set()
        while t and self.valid(t, s) and (t, s) not in seen:
            seen.add((t, s))
            sec = self.sector(t, s)
            nt, ns = sec[0], sec[1]
            if nt == 0:
                out += sec[2:max(2, ns) + 1]      # ns = index of the last byte used
                break
            out += sec[2:]
            if limit and len(out) >= limit:
                break
            t, s = nt, ns
        return bytes(out)

    # -- free space
    def blocks_free(self):
        if self.kind == "d81":
            free = 0
            for t in range(1, 81):
                if t == 40:
                    continue
                bam = self.sector(40, 1 if t <= 40 else 2)
                i = (t - 1) % 40
                free += bam[0x10 + i * 6]
            return free
        bam = self.sector(18, 0)
        free = sum(bam[4 + (t - 1) * 4] for t in range(1, 36) if t != 18)
        if self.kind == "d71":
            free += sum(bam[0xDD + (t - 36)] for t in range(36, 71) if t != 53)
        return free

    @property
    def label(self):
        return f'"{self.disk_name}" {self.disk_id} {self.dos_type}'.rstrip()


# ---------------------------------------------------------------- C64 screen codes
# For showing PETSCII with a real C64 font (e.g. Style's "C64 Pro Mono", whose glyphs
# sit in the Unicode Private Use Area by screen code). A "run" is (text, reversed).

def petscii_screen_code(b):
    """(screen code, reversed) for a PETSCII byte as the C64 shows it - control
    codes appear reversed, the way LIST shows them inside quotes."""
    if b < 0x20:
        return b, True                     # e.g. {wht} ($05) shows as a reversed E
    if b < 0x40:
        return b, False
    if b < 0x60:
        return b - 0x40, False
    if b < 0x80:
        return b - 0x20, False
    if b < 0xA0:
        return b - 0x40, True
    if b < 0xC0:
        return b - 0x40, False
    if b < 0xFF:
        return b - 0x80, False
    return 0x5E, False                     # pi


_ASCII_SC = {"@": 0, "[": 0x1B, "£": 0x1C, "]": 0x1D, "↑": 0x1E, "^": 0x1E, "←": 0x1F, "π": 0x5E}


def ascii_screen_code(ch):
    """Screen code for a plain character (line numbers, keywords, search text)."""
    if ch in _ASCII_SC:
        return _ASCII_SC[ch]
    o = ord(ch)
    if 0x20 <= o <= 0x3F:
        return o
    if "A" <= ch <= "Z":
        return o - 0x40
    if "a" <= ch <= "z":
        return o - 0x60
    return 0x3F                            # '?'


def _runs_add(runs, ch, rev):
    if runs and runs[-1][1] == rev:
        runs[-1][0].append(ch)
    else:
        runs.append(([ch], rev))


def _runs_done(runs):
    return [("".join(chars), rev) for chars, rev in runs]


def petscii_screen_runs(data, base):
    """A PETSCII text file as runs of C64-font characters. Handles RVS ON/OFF ($12/$92);
    a carriage return ends reverse mode like on the C64. base = the font's PUA base."""
    runs, rev = [], False
    for b in data:
        if b in (0x0D, 0x8D):
            _runs_add(runs, "\n", False)
            rev = False
            continue
        if b == 0x12:
            rev = True
            continue
        if b == 0x92:
            rev = False
            continue
        if b < 0x20 or 0x80 <= b < 0xA0:
            continue                       # colours, cursor movement, ... are not shown
        sc, _ = petscii_screen_code(b)
        _runs_add(runs, chr(base + sc), rev)
    return _runs_done(runs)


def basic_v2_screen_runs(data, base, max_lines=20000):
    """A C64 BASIC V2 program listed like LIST, as runs of C64-font characters
    (control codes inside quotes come out reversed, just like on the real machine)."""
    load = prg_load_address(data)
    body = data[2:]
    pos, lines, runs = 0, 0, []

    def text(t):
        for ch in t:
            _runs_add(runs, chr(base + ascii_screen_code(ch)), False)

    while pos + 4 <= len(body) and lines < max_lines:
        link = body[pos] | (body[pos + 1] << 8)
        if link == 0:
            break
        number = body[pos + 2] | (body[pos + 3] << 8)
        pos += 4
        if lines:
            _runs_add(runs, "\n", False)
        text(f"{number} ")
        quote = False
        while pos < len(body) and body[pos] != 0:
            b = body[pos]
            pos += 1
            if b == 0x22:
                quote = not quote
                text('"')
            elif quote or b < 0x80:
                sc, rev = petscii_screen_code(b)
                _runs_add(runs, chr(base + sc), rev)
            elif b <= 0xCB:
                text(BASIC_V2_TOKENS[b - 0x80])
            elif b == 0xFF:
                _runs_add(runs, chr(base + 0x5E), False)
            else:
                sc, rev = petscii_screen_code(b)
                _runs_add(runs, chr(base + sc), rev)
        pos += 1
        lines += 1
    del load
    return _runs_done(runs)


def screen_search_pattern(term, base):
    """A Tk text-search regular expression matching term in C64-font text,
    ignoring upper/lower case."""
    out = []
    for ch in term:
        sc = ascii_screen_code(ch)
        if ch.isalpha() and ch.isascii():
            n = ord(ch.upper()) - 0x40
            out.append("[" + chr(base + n) + chr(base + 0x40 + n) + "]")
        else:
            c = chr(base + sc)
            out.append("\\" + c if c in "\\^$.|?*+()[]{}" else c)
    return "".join(out)


def screen_string(raw, base):
    """PETSCII bytes (e.g. a directory name, without its $A0 padding) as one string in
    a C64 font. Reversed characters use the font's own reversed glyphs (screen code
    + $80), so this works where per-character colours can't be used."""
    out = []
    for b in bytes(raw).rstrip(b"\xa0"):
        sc, rev = petscii_screen_code(b)
        out.append(chr(base + sc + (0x80 if rev else 0)))
    return "".join(out)


def ascii_screen_string(text, base):
    """Plain text (numbers, file types, headings) in a C64 font."""
    return "".join(chr(base + ascii_screen_code(c)) if c != "\n" else c for c in text)
