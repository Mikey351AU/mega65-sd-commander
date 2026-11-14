# Installing MEGA65 SD Commander

MEGA65 SD Commander is a Norton Commander-style file manager for your MEGA65's SD card. It
talks to the MEGA65 over **Ethernet**, so you can copy, rename and delete files and folders
on the SD card from your PC, look inside ZIP files and `.D64`/`.D71`/`.D81` disk images,
and view text, PETSCII and C64 BASIC files.

This guide gets you running with just a MEGA65, a network cable and a PC. If you also have
the **MEGA65 JTAG Expansion Board**, follow the main steps first, then the extra section at
the end.

> These steps are for **Windows 10/11**. SD Commander also runs on **macOS** and **Linux**:
> follow the same steps with the changes in [macOS](#macos) or [Linux](#linux).

---

## What you need

| | |
|---|---|
| **MEGA65** | Running release core **0.96 or newer** (2024 R6 machines already have one) |
| **Network** | The MEGA65 and your PC on the same network: both plugged into your router or switch, or a cable straight between them |
| **Python 3.8 or newer** | Free, from python.org - [step 1](#1-install-python) |
| **mega65_ftp** | Part of the free MEGA65 tools - [step 2](#2-get-mega65_ftp) |
| **MEGA65 SD Commander** | This project - [step 3](#3-install-mega65-sd-commander) |

---

## 1. Install Python

1. Download the latest **Python 3** for Windows from <https://www.python.org/downloads/>.
2. Run the installer and, on the first screen, **tick "Add python.exe to PATH"**.
3. Click **Install Now**. The standard install includes **tcl/tk (tkinter)**, which SD
   Commander needs for its window. If you choose *Customize installation*, make sure
   *tcl/tk and IDLE* stays ticked.

Python from the Microsoft Store works too.

**Optional:** to make deletes on your PC go to the Recycle Bin instead of being permanent,
open a Command Prompt and run:

```
pip install send2trash
```

## 2. Get mega65_ftp

SD Commander uses **mega65_ftp** from the official MEGA65 tools to do the actual transfers.

1. Download the **mega65-tools** package for Windows (development branch) from Filehost:
   <https://files.mega65.org?id=658322fd-e586-4b4f-a991-89470b269b4a>
2. Unzip it and find **`mega65_ftp.exe`**.
3. Either copy `mega65_ftp.exe` into the SD Commander folder (step 3), where it's found
   automatically, or remember where it is: SD Commander asks for it the first time it starts.

Use a recent build. Older copies, like the ones bundled with some other tools, may be missing
options that SD Commander needs.

## 3. Install MEGA65 SD Commander

1. Download the latest release ZIP from this repository's **Releases** page.
2. Unzip it into a folder you can write to, for example `C:\MEGA65\SD Commander`.
   Avoid `C:\Program Files`: SD Commander saves its settings next to the program.
3. The folder should contain:

   ```
   m65_sd_commander.py          the program
   m65ftp.py, m65cbm.py,        parts of the program
   m65board.py
   fonts\                       the C64 font (see "Credits" below)
   Start MEGA65 SD Commander.bat
   Start with console (debug).bat
   Start MEGA65 SD Commander.command   (macOS)
   README.md, INSTALL.md,       documentation
   screenshot.png
   ```

## 4. Prepare the MEGA65

Most of this is done once. The last step is needed **every time the MEGA65 is switched on
or reset**.

1. **DIP switch 2 must be ON** on the MEGA65 mainboard. Machines from the 2024 batch 3
   onwards already have it on. The switch block is mounted upside down, so check which way
   is "ON".
2. **Copy `ETHLOAD.M65` to the root of the SD card.** Take it from the archive of the core
   you installed on the MEGA65. If you update the core later, copy the new `ETHLOAD.M65` too.
3. **Give the MEGA65 a MAC address** if it doesn't have one: press **ALT + 1** at power-up
   to open the Configuration Tool, and in the **NETWORK** section set a MAC address that
   isn't `00:00:00:00:00:00`. Save and exit.
4. **Turn on remote control:** at the READY prompt, press **SHIFT + £** (pound). The power
   LED starts **blinking green/yellow**. This needs doing again after every reset or
   power-off.

> Only turn on remote control on your own home network. While it's on, any computer on
> the network can talk to the MEGA65.

## 5. First start

1. Double-click **`Start MEGA65 SD Commander.bat`**.
2. If you copied `mega65_ftp.exe` into the SD Commander folder, it's found automatically and
   you go straight to the two panels. If not, a welcome box asks where it is: click
   **Browse...**, pick it (or the folder that holds it), then **Save**.
3. The **"I have the MEGA65 JTAG expansion board"** option starts switched off, unless an
   expansion board set-up is found (see the last section). To check it, press **F9** (Setup).
4. Press **F4** to connect. **The first time**, Windows Firewall asks whether
   `mega65_ftp.exe` may use the network. Allow it on **private networks**.
   mega65_ftp gives up while that question is open, so this first attempt usually ends with
   a "Could not connect" message. Click **OK** and press **F4** again.
5. The MEGA65 shows the green **"MEGA65 ETHERNET FILE TRANSFER HELPER"** screen while it's
   connected, and the right-hand panel shows its SD card.

When you've finished, press **F4** again (or quit) to disconnect, and the MEGA65 returns to
BASIC.

**Basic keys:** Tab switches panels, Enter opens, Backspace goes up, F5 copies, F6 renames,
F7 makes a folder, F8 deletes, F3 views a file, **F1 (Drive)** chooses what the active panel
shows (PC drive, MEGA65 SD card ...), and **Ctrl+H** shows all keys.

## Troubleshooting

| Problem | What to check |
|---|---|
| **F4 says it can't find the MEGA65** | Remote control on (LED blinking green/yellow, SHIFT + £)? `ETHLOAD.M65` in the SD card's root? Both on the same network? |
| **Still not found** | Windows Firewall must allow `mega65_ftp.exe` (UDP port **4510**, IPv6). IPv6 must be enabled on your PC's network adapter (it is by default). Don't change your router for this. |
| **You clicked Cancel on the firewall question, or it never appeared** | Open **Windows Security > Firewall & network protection > Allow an app through firewall > Change settings**, then tick **Private** for every `mega65_ftp` entry. To get the question back instead, **Remove** the `mega65_ftp` entries; Windows asks again the next time you press F4. |
| **The MEGA65 hangs when you connect** | Its `ETHLOAD.M65` is probably older than its core. Copy the one from your current core's archive. |
| **"mega65_ftp rejected the options"** | Your `mega65_ftp.exe` is too old. Get the current one (step 2). |
| **Nothing happens when you start it** | Run **`Start with console (debug).bat`** to see error messages. If it says Python wasn't found, reinstall Python with *"Add python.exe to PATH"* ticked. |
| **Something else went wrong** | Press **Ctrl+L** for the mega65_ftp log. Errors are also saved in `m65sd.log` in the SD Commander folder; include it when reporting a problem. |

**Good to know**

- Files must be **under 2 GB** to go to or from the MEGA65 SD card. This is a limit inside
  mega65_ftp.
- Disk images (`.D64`, `.D71`, `.D81`) can be opened and files copied out of them. They are
  read-only for now.

## macOS

Follow the steps above, with these changes:

- **Python:** install it with the macOS installer from <https://www.python.org/downloads/>,
  which includes tkinter. The `python3` that comes with Apple's developer tools has an old
  tkinter that doesn't display everything properly.
- **mega65_ftp:** download the **macOS** build of mega65-tools from Filehost. The program is
  called **`mega65_ftp.osx`**. Put it in the SD Commander folder, where it's found
  automatically, or point SD Commander at it in the welcome box or Setup (F9).
  If macOS refuses to run it because it's from an unidentified developer, open Terminal in
  that folder and run `xattr -d com.apple.quarantine mega65_ftp.osx`, or allow it under
  **System Settings > Privacy & Security**.
- **Start it:** double-click **`Start MEGA65 SD Commander.command`**. The first time,
  macOS may block it: right-click it, choose **Open**, then **Open** again. (Or run
  `python3 m65_sd_commander.py` in Terminal, in the SD Commander folder.)
- **Firewall:** if the macOS firewall is on and asks whether `mega65_ftp.osx` may accept
  incoming network connections, click **Allow**, then press F4 again.
- **Keys:** on a Mac keyboard, hold **fn** for the F-keys (fn + F5 copies), or turn on
  *Use F1, F2, etc. keys as standard function keys* in **System Settings > Keyboard**.

## Linux

- **Python:** Linux usually has Python 3. Install tkinter too if it's missing
  (Debian/Ubuntu: `sudo apt install python3-tk`).
- **mega65_ftp:** use the Linux build of mega65-tools from Filehost.
- **Start it:** run `python3 m65_sd_commander.py` in the SD Commander folder.
- **C64 font:** the viewer only shows real C64 characters if the font is installed for your
  user (copy `fonts/C64_Pro_Mono-STYLE.ttf` to `~/.local/share/fonts/`, or `~/.fonts/` on
  older systems). Otherwise it falls back to plain text.

---

## For MEGA65 JTAG Expansion Board users

If your MEGA65 has the **MEGA65 Expansion Board Integrated JTAG** (the Pico W-based core
loader), SD Commander can also:

- show and manage the **expansion board's own SD card** ("EXP Board SD card" in the
  F1 Drive menu), and copy between it, your PC and the MEGA65's SD card
- **launch cores**: Enter on a `.bit`, `.cor` or `.m65j` file loads it into the MEGA65,
  whether it's on your PC, the MEGA65's SD card, the board's SD card or inside a ZIP

This uses the board's **WiFi web interface**, the same one used by `m65j.py`.

### On the board

The board's own settings live in `mega65-jtag.cfg` on **its** SD card (see the
[board's repository](https://github.com/MEGA65/mega65-expansion-board-integrated-jtag)).
For SD Commander you need:

- **WiFi on:** `ssid=`, `psk=` and `http=1`.
- **A fixed IP address:** the board doesn't announce a network name, so SD Commander can
  only reach it by IP address. Give it one that doesn't change, either with a DHCP
  reservation in your router or with `dhcp=0` and `ip=`.
- **An `allow=` rule for your PC's network:** `files=1` lets SD Commander list, copy and
  delete files; **`bitstreams=1` is needed to launch cores**. For example:
  `allow=192.168.1.0/24 files=1 bitstreams=1`
- **Login (optional):** if you set `http_user=` and `http_password=` there, put the same
  user and password in `.m65j.config` (below).
- **Write grant off:** set `require_write_grant=0`. With the default (`1`), files can only be
  copied *to* the board's SD card while write access is turned on with the board's
  write-enable button. Otherwise copies from SD Commander fail with "HTTP 423".

### On your PC

SD Commander finds the board the same way as `m65j.py`: from a **`.m65j.config`** file.
If you already use `m65j.py`, there's nothing more to do.
Otherwise, create a text file called `.m65j.config` in **your home folder**
(`C:\Users\<you>\.m65j.config`) or in the **SD Commander folder**:

```ini
url=http://192.168.1.65
user=mega65
password=programmer
```

(`ip=192.168.1.65` works instead of `url=`. Leave out `user=` and `password=` if the board
doesn't use a login.)

You can also type the address, user and password in **Setup (F9)**, which takes priority
over `.m65j.config`.

### Turn it on

In **Setup (F9)**, tick **"I have the MEGA65 JTAG expansion board"**. It's ticked
automatically on first start if a `.m65j.config` with a board address was found. Setup
shows which config file was used.

### Good to know

- The board's firmware only stores **`.bit`, `.cor` and `.m65j`** files, and can't make
  folders, rename files or delete folders. SD Commander tells you when something isn't
  possible.
- Launching a core replaces whatever the MEGA65 is running, so SD Commander closes its
  Ethernet connection first. Press **F4** to reconnect once you're back in the normal
  MEGA65 core (and turn remote control on again with SHIFT + £).
- **Big folders on the board:** current board firmware cuts folder listings off at about
  11.5 KB (around 15-20 cores, depending on name lengths). The web interface and other
  tools lose the same entries. SD Commander shows what arrives and warns if entries are
  missing. Until a firmware fix is released, keep each board folder to around 15 cores.

| Problem | What to check |
|---|---|
| **"HTTP 401"** | Wrong or missing `user=`/`password=` in `.m65j.config` (or Setup). |
| **"HTTP 403"** | Your PC isn't covered by an `allow=` rule, or the rule lacks `bitstreams=1` (needed to launch cores). |
| **"HTTP 423"** | `require_write_grant=0` isn't set in `mega65-jtag.cfg`, or the board is busy fetching cores. |
| **"Could not reach the expansion board"** | Board off WiFi or at a different address. Check the address in `.m65j.config` / Setup. |

---

## Credits

- **mega65_ftp** and the Ethernet tools: the MEGA65 team,
  <https://github.com/MEGA65/mega65-tools>
- **C64 Pro Mono** font, (c) Style, <https://style64.org/c64-truetype>. Included unmodified
  under its licence (`fonts/license.txt`), which allows this only for software that is
  given away free.
- MEGA65 Ethernet setup details: the MEGA65 wiki,
  [Ethernet tools (mega65_ftp and etherload)](https://mega65.atlassian.net/wiki/spaces/MEGA65/pages/29327361/Ethernet+tools+mega65_ftp+and+etherload)
