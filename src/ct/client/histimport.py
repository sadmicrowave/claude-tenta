"""Reading this computer's own past shell commands ONCE, so the command log is not empty on the first day: `ct-run offload import`.

The first release assumes no AI model use: only shell history is read. Sources (each is asked about separately; nothing is saved until the person says yes to it):
  zsh    `~/.zsh_history` (or `$ZDOTDIR/.zsh_history`) when it was written with EXTENDED_HISTORY (`: <epoch>:<seconds>;<command>`): a real date, and a whole-second duration. 0 means
         "under a second", which is not a measurement, so it is untimed. Plain lines have no date and are not imported (they are counted and said).
  bash   `~/.bash_history` only for commands that have a `#<epoch>` line before them (HISTTIMEFORMAT); bash never records a duration, so they are untimed.
  fish   `~/.local/share/fish/fish_history` (or under `$XDG_DATA_HOME`): every entry has a date, none has a duration (untimed); fish's `\\n` and `\\\\` escapes are undone.

What is recorded is what the live hook records (cmdlog.CommandLog.hook): the command line, with the same secret screening (a line that looks like it carries a secret is dropped and only
counted) and the same length cap; shapes are what any screen prints. Each row carries `src` (where it came from) and `id` (so a second run adds nothing). The files read are
never modified, and nothing leaves this computer. Files over a size cap and files that cannot be read are skipped and named, with the system's own error text.

Not read (backlog): PowerShell's history (no dates), other shells, a custom HISTFILE. The coding assistant's own transcripts are a later feature (branch feat/assistant-hooks-and-transcripts).
"""
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .cmdlog import MAX_LINE, SECRET

DAY = 86400
SOURCES = {"zsh": "zsh history", "bash": "bash history", "fish": "fish history"}
SRC_MARK = {"zsh": "zsh", "bash": "bash", "fish": "fish"}          # the `src` of a row
SOURCE_OF = {v: k for k, v in SRC_MARK.items()}
ZSH = re.compile(r"^: (\d+):(\d+);(.*)$", re.S)
BASH_STAMP = re.compile(r"^#(\d{9,})$")
FISH_UNESCAPE = re.compile(r"\\([n\\])")


@dataclass
class Report:
    source: str
    label: str
    paths: list = field(default_factory=list)       # what would be (or was) read, for the screen
    files: int = 0
    commands: int = 0
    timed: int = 0
    untimed: int = 0
    undated: int = 0                                # lines with no date: counted, never imported
    screened: int = 0                               # lines dropped because they look like they carry a secret
    older: int = 0                                  # outside the window
    skipped: list = field(default_factory=list)     # (path, why)
    first: int = 0
    last: int = 0


@dataclass
class Scan:
    reports: list
    rows: list

    def report(self, source):
        return next(r for r in self.reports if r.source == source)


def row_id(src, ts, cmd, cwd):
    return hashlib.sha1(f"{src}\0{ts}\0{cmd}\0{cwd or ''}".encode("utf-8", "replace")).hexdigest()[:16]


class _Collect:
    def __init__(self, rep, src, now, days):
        self.rep, self.src, self.cut, self.rows = rep, src, now - days * DAY, []

    def add(self, ts, cmd, secs, cwd=None):
        rep = self.rep
        cmd = (cmd or "").strip("\n")
        if not cmd.strip():
            return
        if SECRET.search(cmd):
            rep.screened += 1
            return
        if ts < self.cut:
            rep.older += 1
            return
        ts = int(ts)
        rep.commands += 1
        rep.timed += secs is not None
        rep.untimed += secs is None
        rep.first = min(rep.first or ts, ts)
        rep.last = max(rep.last, ts)
        cmd = cmd[:MAX_LINE]
        self.rows.append({"ts": ts, "cmd": cmd, "secs": secs, "cwd": cwd, "src": self.src, "id": row_id(self.src, ts, cmd, cwd)})


def _open_error(e):
    return str(e) or e.__class__.__name__


def _read_text(path, rep, max_bytes):
    try:
        if path.stat().st_size > max_bytes:
            rep.skipped.append((str(path), f"larger than {max_bytes // (1024 * 1024) or '<1'} MB, not read"))
            return None
        rep.files += 1
        return path.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as e:
        rep.skipped.append((str(path), _open_error(e)))
        return None


def scan_zsh(path, now, days, max_bytes):
    rep = Report("zsh", SOURCES["zsh"], [str(path)])
    col = _Collect(rep, SRC_MARK["zsh"], now, days)
    text = _read_text(Path(path), rep, max_bytes)
    cur = None                                              # [ts, secs, command text] of the entry still being read (a trailing backslash continues it)

    def flush():
        if cur:
            col.add(cur[0], cur[2], None if cur[1] < 1 else float(cur[1]))
    for line in (text or "").split("\n"):
        m = ZSH.match(line)
        if m:
            flush()
            cur = [int(m.group(1)), int(m.group(2)), m.group(3)]
        elif cur is not None and cur[2].endswith("\\"):
            cur[2] += "\n" + line
        elif line.strip():
            rep.undated += 1
    flush()
    return rep, col.rows


def scan_bash(path, now, days, max_bytes):
    rep = Report("bash", SOURCES["bash"], [str(path)])
    col = _Collect(rep, SRC_MARK["bash"], now, days)
    text = _read_text(Path(path), rep, max_bytes)
    ts = None
    for line in (text or "").split("\n"):
        m = BASH_STAMP.match(line)
        if m:
            ts = int(m.group(1))
        elif line.strip():
            if ts is None:
                rep.undated += 1
            else:
                col.add(ts, line, None)
            ts = None
    return rep, col.rows


def scan_fish(path, now, days, max_bytes):
    rep = Report("fish", SOURCES["fish"], [str(path)])
    col = _Collect(rep, SRC_MARK["fish"], now, days)
    text = _read_text(Path(path), rep, max_bytes)
    cmd = None
    for line in (text or "").split("\n"):
        if line.startswith("- cmd: "):
            if cmd is not None:                              # the previous entry never had a date
                rep.undated += 1
            cmd = FISH_UNESCAPE.sub(lambda m: "\n" if m.group(1) == "n" else "\\", line[7:])
        elif line.startswith("  when: ") and cmd is not None:
            try:
                col.add(int(line[8:]), cmd, None)
            except (ValueError, OverflowError, OSError):
                rep.undated += 1
            cmd = None
    if cmd is not None:
        rep.undated += 1
    return rep, col.rows


def scan(home, sources=tuple(SOURCES), now=None, days=90, max_bytes=200 * 1024 * 1024, env=None):
    """Read the chosen sources under `home` (nothing is written anywhere). Returns Scan(reports, rows). A source that is not there is an empty report."""
    now = time.time() if now is None else now
    env = env or {}
    home = Path(home)
    zdir = Path(env["ZDOTDIR"]).expanduser() if env.get("ZDOTDIR") else home
    fish = (Path(env["XDG_DATA_HOME"]).expanduser() if env.get("XDG_DATA_HOME") else home / ".local" / "share") / "fish" / "fish_history"
    reports, rows = [], []
    for s in sources:
        rep, r = {"zsh": lambda: scan_zsh(zdir / ".zsh_history", now, days, max_bytes), "bash": lambda: scan_bash(home / ".bash_history", now, days, max_bytes),
                  "fish": lambda: scan_fish(fish, now, days, max_bytes)}[s]()
        reports.append(rep)
        rows += r
    return Scan(reports, rows)
