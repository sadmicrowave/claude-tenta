"""The folder's own settings: `.tenta.toml`, the file `ct-run use` writes in a working folder (the way a CLAUDE.md holds what Claude should know about a folder).

It says which worker this folder's jobs go to, and may name single commands (job classes) that go to a different worker. It is plain TOML you can open and edit by hand; `ct-run use` is the
one command that reads and writes it, so a person, a script or Claude can set it again later in one line. The nearest file at or above the folder you are in wins (as with `.git`), so a
project's file covers its sub-folders. An explicit `--worker` on the command line always wins over the file.

`~/.tenta.toml` is the same file for the whole computer (like `~/.claude/CLAUDE.md` for folders): `ct-admin workers use WORKER --command CLASS` writes it. Where a job goes, most specific first:
this folder's [commands], then the computer's [commands] (a command dedicated to one worker everywhere), then this folder's `worker`, then the computer's `worker`. `reserved` (computer file
only) lists workers that take nothing except the commands dedicated to them.
"""
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .repo import ClientError

FILE = ".tenta.toml"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HEADER = ("# claude-tenta: which worker jobs go to. Written by `ct-run use` (a folder) or `ct-admin workers use` (this computer, ~/.tenta.toml); edit it freely.\n"
          "# worker = the worker every job from here goes to. [commands] = single job classes that go to a particular worker.\n"
          "# reserved = workers that take nothing except the commands dedicated to them (the computer's file only).\n")


@dataclass
class Pins:
    path: Path
    worker: str = None
    commands: dict = field(default_factory=dict)             # job class -> worker
    reserved: list = field(default_factory=list)             # the computer's file only: workers that take nothing but the commands dedicated to them

    def command_for(self, cls):
        if cls in self.commands:
            return self.commands[cls], f"{cls} is set to this worker in {self.path}"
        return None, None

    def worker_for(self):
        return (self.worker, f"{self.path} sends everything from here to this worker") if self.worker else (None, None)

    def for_class(self, cls):
        """(worker, what pinned it) for a job of this class from this one file, or (None, None)."""
        return self.command_for(cls) if cls in self.commands else self.worker_for()

    def empty(self):
        return not self.worker and not self.commands and not self.reserved


def global_path():
    """The computer's own file, ~/.tenta.toml."""
    return Path(os.path.expanduser("~")) / FILE


def find(start=None):
    """The nearest folder .tenta.toml at or above `start` (the folder you are in), or None. The computer's file in the home folder is the computer layer, never a folder's."""
    here = Path(start or Path.cwd()).resolve()
    own = global_path().resolve()
    for d in (here, *here.parents):
        p = d / FILE
        if p.is_file() and p != own:
            return p
    return None


def global_pins():
    """The computer's Pins (an empty one when the file does not exist)."""
    p = global_path()
    return read(p) if p.is_file() else Pins(p)


def read(path):
    """The pins in a file. A file that cannot be read says so with the tool's own words and how to fix it."""
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ClientError(f"{path} cannot be read", str(e), [f"Open it and fix the line named above, or run ct-run use --clear to start again."])
    cmds = raw.get("commands") or {}
    worker = raw.get("worker")
    bad = [k for k, v in {"worker": worker, **cmds}.items() if v is not None and not (isinstance(v, str) and NAME.match(v))]
    if bad or not isinstance(cmds, dict):
        raise ClientError(f"{path} has a value that is not a worker name", f"Check: {', '.join(bad) or 'commands'}. A worker name is letters, digits, dot, dash or underscore.", ["Open the file and fix it, or run ct-run use --clear to start again."])
    reserved = raw.get("reserved") or []
    if not isinstance(reserved, list) or not all(isinstance(r, str) and NAME.match(r) for r in reserved):
        raise ClientError(f"{path} has a reserved list that is not worker names", "It is a list of worker names, for example reserved = [\"pentest\"].", ["Open the file and fix it."])
    return Pins(Path(path), worker, dict(cmds), list(reserved))


def pins_for(start=None):
    """The Pins that apply to the folder `start`, or None when no file is found."""
    p = find(start)
    return read(p) if p else None


def pin_for(cls, start=None):
    """(worker, why) a job of class `cls` run from `start` is pinned to, or (None, None). Most specific first: the folder's command, the computer's command, the folder's worker, the computer's worker."""
    folder, mine = pins_for(start), global_pins()
    for pins, ask in ((folder, "command_for"), (mine, "command_for")):
        w, why = getattr(pins, ask)(cls) if pins else (None, None)
        if w:
            return w, why + (" (for every folder on this computer)" if pins is mine else "")
    for pins in (folder, mine):
        w, why = pins.worker_for() if pins else (None, None)
        if w:
            return w, why
    return None, None


def prefs():
    """What the computer's file says to the scheduler: a reserved worker takes only the commands dedicated to it (sched.Prefs.dedicated)."""
    from .. import sched
    mine = global_pins()
    return sched.Prefs(dedicated={w: sorted(c for c, ww in mine.commands.items() if ww == w) for w in mine.reserved})


def _q(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(pins):
    """Write the file (header, the worker, then the commands). Nothing left to say removes the file."""
    if pins.empty():
        Path(pins.path).unlink(missing_ok=True)
        return
    lines = [HEADER]
    if pins.worker:
        lines.append(f"worker = {_q(pins.worker)}\n")
    if pins.reserved:
        lines.append("reserved = [" + ", ".join(_q(w) for w in sorted(pins.reserved)) + "]\n")
    if pins.commands:
        lines.append("\n[commands]\n")
        lines += [f"{k} = {_q(v)}\n" if NAME.match(k) else f"{_q(k)} = {_q(v)}\n" for k, v in sorted(pins.commands.items())]
    Path(pins.path).write_text("".join(lines), encoding="utf-8")


def check_name(kind, value):
    if not NAME.match(value or ""):
        raise ClientError(f"{value!r} is not a valid {kind} name", "Letters, digits, dot, dash and underscore only.", [])
