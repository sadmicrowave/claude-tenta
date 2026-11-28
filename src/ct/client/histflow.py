"""The screens and questions of `ct-run offload import`, and the offer at the end of `ct-setup install` (design: docs/design/WORKSPACE-JOBS.md, "The first-day data").

Rules every screen here keeps:
  - Nothing is recorded without its own explicit yes (anything but y or yes is no). Each source is its own question.
  - Without a terminal nothing is read and nothing is recorded: the exact command is printed instead.
  - A file that cannot be read is named with the system's own words; the rest of the setup goes on.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

from .. import cards
from .. import ui as _ui
from ..ui import CYAN, GREEN, RED, YELLOW
from ..words import plural
from . import histimport
from . import refuse
from .repo import ClientError

IMPORT = "ct-run offload import"


def emit(*a, **k):
    k.setdefault("stream", sys.stderr)
    _ui.say(*a, **k)


def user_home(ctx):
    return Path(getattr(ctx, "user_home", None) or Path.home())


def yes(ask, question):
    try:
        return ask(_ui.confirm_prompt(question)).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def day(ts):
    d = datetime.fromtimestamp(ts)
    return f"{d:%b} {d.day}"


# ---- import ----
def settings_of(ctx):
    return (int(getattr(ctx, "import_days", 90) or 90), int(getattr(ctx, "import_max_mb", 200) or 200) * 1024 * 1024)


def show_scan(sc, stream, days=90):
    """What would be read and what is in it. Nothing has been saved yet."""
    rows = [r for r in sc.reports]
    emit("Past commands tenta could read once", *[("Read from", p) for r in rows if r.files or r.skipped for p in r.paths], cards.Why(
        "Counting them reads those files here, on this computer, and nothing is saved or sent. Only the command lines are kept (never anything that looks like a secret), in your private command log. The files are never changed."),
        tag="IMPORT", color=CYAN, stream=stream)
    _ui.table("What is in them", ["source", "commands", "dates", "timing"],
              [[r.label, plural(r.commands, "command"), (day(r.first) if day(r.first) == day(r.last) else f"{day(r.first)} to {day(r.last)}") if r.commands else "-",
                f"{r.timed} timed, {r.untimed} untimed" if r.commands else "-"] for r in rows], tag="FOUND", stream=stream,
              notes=tuple(n for r in rows for n in note_lines(r, days)))
    left = [(p, why) for r in rows for p, why in r.skipped]
    if left:
        emit("Some files were not read", *[("Not read", f"{p}: {why}") for p, why in left[:5]], *([f"and {len(left) - 5} more"] if len(left) > 5 else []), tag="NOTE", color=YELLOW, stream=stream)


def note_lines(r, days=90):
    out = []
    if r.undated:
        out.append(f"{r.label}: {plural(r.undated, 'line')} with no date, so not imported.")
    if r.screened:
        out.append(f"{r.label}: {plural(r.screened, 'command')} left out because they look like they carry a secret.")
    if r.older:
        out.append(f"{r.label}: {plural(r.older, 'older command')} left out (only the last {plural(days, 'day')} are read).")
    return out


def ask_sources(sc, log, ask, stream):
    """One question per source that has commands the log does not hold yet; a source whose commands are all there already is not asked about (it says so)."""
    have = {r.get("id") for r in log.read() if r.get("id")}
    chosen = []
    for r in sc.reports:
        new = sum(1 for x in sc.rows if histimport.SOURCE_OF[x["src"]] == r.source and x["id"] not in have)
        if not r.commands:
            continue
        if not new:
            _ui.say("Those commands are already in your log", ("Source", f"{r.label}: nothing new"), tag="NOTE", color=YELLOW, stream=stream)
            continue
        if yes(ask, f"Import {r.label} ({plural(new, 'new command')}), kept only on this computer?"):
            chosen.append(r.source)
    return chosen


def cmd_import(ctx, a):
    log = command_log(ctx)
    try:
        return import_command(ctx, a, log)
    except OSError as e:                                   # the log could not be read (wrong owner or mode): the same refusal the summary gives
        from . import cli as client_cli
        raise client_cli.unreadable_log(log, e)


def import_command(ctx, a, log):
    if a.clear:
        n = log.clear_imported()
        emit("Imported commands removed from the log", ("Cleared", plural(n, "imported command")), "Commands recorded live are untouched.", tag="DONE", color=GREEN, run_label="Import again", run=IMPORT)
        return 0
    if not ctx.is_tty():
        emit("Nothing was read", "Importing asks a question for each source, so it needs a terminal. Nothing was read and nothing was recorded.", tag="NOTE", color=YELLOW,
             run_label="Run this at a terminal", run=IMPORT)
        return 0
    return run_import(ctx, log, a.source or list(histimport.SOURCES), ctx.ask, sys.stderr)


def command_log(ctx):
    from .cmdlog import CommandLog
    return CommandLog(ctx.home, own_tools=ctx.own_tools)


def run_import(ctx, log, sources, ask, stream, now=None):
    """Count, show, ask once per source, record what was agreed. Returns 0; a log that cannot be written raises ClientError. `declined` sources are returned by the caller's need only through the printed card."""
    days, max_bytes = settings_of(ctx)
    sc = histimport.scan(user_home(ctx), sources, now=ctx.clock() if now is None else now, days=days, max_bytes=max_bytes, env=os.environ)
    if not any(r.commands for r in sc.reports):
        show_scan(sc, stream, days)
        emit("There is nothing to import", "None of those files holds a command from the last " + plural(days, "day") + ".", tag="NOTE", color=YELLOW, stream=stream)
        return 0
    show_scan(sc, stream, days)
    chosen = ask_sources(sc, log, ask, stream)
    if not chosen:
        emit("Nothing was imported", "Nothing was added to your log.", tag="NOTE", color=YELLOW, stream=stream, run_label="Do it later", run=IMPORT)
        return 0
    rows = [x for x in sc.rows if histimport.SOURCE_OF[x["src"]] in chosen]
    try:
        added, dup = log.import_rows(rows)
    except OSError as e:
        from ..packages import card_safe
        raise refuse.error("import.log.unwritable", ("Log file", str(log.path)), example="ct-run offload log show", reason=card_safe(str(e)))
    untimed = sum(1 for x in rows if x["secs"] is None)
    if not added:
        emit("Those commands were already in your log", ("Already there", plural(dup, "command")), "Nothing was added.", tag="NOTE", color=YELLOW, stream=stream, run="ct-run offload review")
        return 0
    emit("Your past commands were added", ("Added", plural(added, "command")), *([("Already there", plural(dup, "command"))] if dup else []),
         f"{untimed} of the commands are untimed: they count as runs and add no time.",
         cards.Why("A time is only known where your shell wrote one (zsh does, whole seconds; bash and fish do not). A command with no time counts as a run and adds no time."),
         tag="DONE", color=GREEN, stream=stream, run_label="See what a worker could take", run="ct-run offload review")
    return 0


# ---- the offer at the end of ct-setup install ----
def setup_offers(log, ask, stream, user_home_dir, now, days=90, max_bytes=200 * 1024 * 1024, env=None):
    """While the command log is too empty to show value: offer the one-time import (one question per source). Returns True when the log changed. A failure is a note, never a failed install."""
    env = os.environ if env is None else env
    try:
        sc = histimport.scan(user_home_dir, tuple(histimport.SOURCES), now=now, days=days, max_bytes=max_bytes, env=env)
        if not any(r.commands for r in sc.reports):
            return False                                      # nothing to offer: the summary that follows says the log is empty and names the command
        show_scan(sc, stream, days)
        chosen = ask_sources(sc, log, ask, stream)
        if not chosen:
            _ui.say("You can do this later", "Nothing was recorded.", tag="NEXT", color=CYAN, stream=stream, run=IMPORT)
            return False
        added, dup = log.import_rows([x for x in sc.rows if histimport.SOURCE_OF[x["src"]] in chosen])
        _ui.say("Your past commands were added", ("Added", plural(added, "command")), tag="DONE", color=GREEN, stream=stream)
        return added > 0
    except OSError as e:
        from ..packages import card_safe
        _ui.say("Your past commands could not be added", cards.Why(card_safe(str(e))), tag="NOTE", color=YELLOW, stream=stream, run=IMPORT)
        return False
