"""Worker aliases: names you give your workers for the `ct-` commands, kept on THIS computer only.

An alias is a mask over the worker's real install name (its identity in the configuration, the units on the machine and the GitHub repositories). Nothing is sent to a worker and no hostname
changes, so it costs nothing to change. The file is `~/.ct/aliases.toml`:

    [aliases]
    tenta3 = "lab-vm"          # real install name = what you call it here

    [settings]
    show_real = true           # show "lab-vm  (tenta3)"; false shows only "lab-vm"

Typing: anywhere a worker is named (`--worker`, `ct-run use`, ...), the alias or the real name works and means the same worker. Files such as `.tenta.toml` keep the REAL name, so renaming an
alias never breaks them. Showing: every card shows a worker through `show()`, one function, so the alias appears everywhere or nowhere. Not on a worker's own screens: a worker draws its own `status`.
"""
import os
import re
import tomllib
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def path():
    return Path(os.path.expanduser("~")) / ".ct" / "aliases.toml"


def load():
    """({real name: alias}, show_real). A missing or unreadable file means no aliases (the commands then show real names, as they always did)."""
    try:
        raw = tomllib.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, True
    aliases = {k: v for k, v in (raw.get("aliases") or {}).items() if isinstance(v, str) and NAME.match(v)}
    return aliases, bool((raw.get("settings") or {}).get("show_real", True))


def save(aliases, show_real=True):
    """Write the file; with nothing to say (no aliases, defaults) it is removed."""
    p = path()
    if not aliases and show_real:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# claude-tenta: names for your workers, on this computer only (`ct-admin workers rename`). Edit freely; nothing here reaches a worker.\n", "[aliases]\n"]
    lines += [f'{k} = "{v}"\n' for k, v in sorted(aliases.items())]
    lines += ["\n[settings]\n", f"show_real = {'true' if show_real else 'false'}\n"]
    p.write_text("".join(lines), encoding="utf-8")


def resolve(typed, real_names=None):
    """The real name for what was typed: a real name stands for itself, an alias means its worker. Anything else comes back unchanged (the caller says it is not a worker)."""
    if typed is None:
        return None
    aliases, _ = load()
    if real_names is not None and typed in real_names:
        return typed
    for real, alias in aliases.items():
        if alias == typed:
            return real
    return typed


def show(real):
    """How a worker is named on screen: its alias with the real name beside it, only the alias when the file says to mask, or the real name when it has no alias."""
    aliases, show_real = load()
    alias = aliases.get(real)
    if not alias:
        return real
    return f"{alias}  ({real})" if show_real else alias


def check(alias, real, real_names):
    """None when `alias` can be given to `real`, otherwise what is wrong, in a sentence."""
    aliases, _ = load()
    if not NAME.match(alias or ""):
        return "A name is letters, digits, dot, dash and underscore, starting with a letter or digit."
    if alias == real:
        return "That is already its own name; use - (or --clear) to have no alias."
    if alias in real_names:
        return "That is the real name of another worker."
    taken = [r for r, a in aliases.items() if a == alias and r != real]
    if taken:
        return "Another worker already has that name."
    return None
