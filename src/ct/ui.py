"""Terminal output used by every ct command: one shape for messages a person reads.

A coloured TAG and a bold title, indented detail lines, optional bullet steps, then commands to copy, each alone on its own line.
Colour is only used when a person is watching a terminal: plain text when piped or captured, when NO_COLOR is set, or CT_COLOR=never.
"""
import base64
import os
import sys

from . import cards, diffview, theme

# Roles, not colours: what each looks like is decided in ct.theme (and the user's theme.toml). The older names stay so every caller keeps working.
BOLD, DIM, ITALIC, RED, GREEN, YELLOW, CYAN = "strong", "muted", "italic", "bad", "ok", "warn", "info"
ACCENT, PRIMARY, ROOT = "accent", "primary", "root"


def _color_on(stream):
    mode = os.environ.get("CT_COLOR", "auto")
    if mode == "always":
        return True
    return mode != "never" and "NO_COLOR" not in os.environ and stream.isatty()


def paint(text, *codes, stream=None):
    """Colour (by role, see ct.theme) for a person reading a terminal; plain text when piped, captured by Claude, or NO_COLOR is set."""
    if not codes or not _color_on(stream or sys.stderr):
        return text
    return theme.sgr(text, *codes)


def clipboard_tool(env=None, which=None):
    """The program that puts text on the clipboard of the machine this runs on, or None: pbcopy on a Mac, wl-copy / xclip / xsel on Linux with a desktop. Not used over SSH (the clipboard
    there is another computer's), where the terminal's own escape sequence is the only way."""
    import shutil
    env, which = os.environ if env is None else env, which or shutil.which
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return None
    for argv in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if (argv[0] in ("wl-copy",) and not env.get("WAYLAND_DISPLAY")) or (argv[0] in ("xclip", "xsel") and not env.get("DISPLAY")):
            continue
        if which(argv[0]):
            return argv
    return None


def _run_clipboard_tool(argv, text):
    import subprocess
    try:
        return subprocess.run(argv, input=text.encode(), timeout=3, capture_output=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def chip(text, stream=None):
    """A command to copy as a code chip when a person is watching a terminal, plain text otherwise."""
    return cards.chip(text) if _color_on(stream or sys.stderr) else text


def offer_copy(run, stream):
    """Put the first (recommended) command on the clipboard of a person watching a terminal and return the lines that say so. A clipboard program is used where there is one (a terminal
    often ignores the escape sequence for it: it did, and nothing was copied); the sequence (OSC 52) is sent as well, for a terminal that honours it, and is the only way over SSH.
    Nothing is typed or run for them: they paste it. Off with CT_COPY=never; never when output is piped, captured or plain."""
    first = (run if isinstance(run, str) else (list(run) or [""])[0]).strip()
    if not first or os.environ.get("CT_COPY", "").lower() == "never" or not _color_on(stream):
        return []
    tool = clipboard_tool()
    if tool:
        _run_clipboard_tool(tool, first)
    try:
        stream.write("\x1b]52;c;" + base64.b64encode(first.encode()).decode() + "\x07")
        stream.flush()
    except (OSError, ValueError):
        return []
    return ["", paint(f"copied to your clipboard: {first}", DIM, stream=stream), paint("paste it at the prompt", DIM, stream=stream), paint("(CT_COPY=never turns this off)", DIM, stream=stream)]


def say(title, *details, tag="DONE", color=GREEN, steps=None, steps_label="Next", run=None, run_label="Run this", stream=None, framed=None):
    """One shape for every message a person reads: a coloured TAG and a bold title, indented detail lines, optional bullet steps,
    then commands to copy, each alone on its own line (nothing before or after it)."""
    stream = stream or sys.stderr
    if _color_on(stream):                                      # a person at a terminal: the wizard's card (see ct.cards)
        lifted = []                                         # commands too long for a line of the card: drawn alone below it, with the ones to run
        out = [""] + cards.card(title, details, tag=tag, color=color, steps=steps, steps_label=steps_label, framed=framed, lifted=lifted)
        cmds = lifted + ([run] if isinstance(run, str) else list(run or []))
        if cmds:
            out += cards.commands(cmds, run_label if run else "Command") + offer_copy(cmds, stream)
        print("\n".join(out) + "\n", file=stream)
        return
    out = ["", f"{paint(tag, BOLD, color, stream=stream)}  {paint(title, BOLD, stream=stream)}"]
    lines = [d for d in details if d]          # a ("Label", "value") pair becomes an aligned bold-label line; "\n" is a blank line
    width = max([len(d[0]) for d in lines if isinstance(d, tuple)] or [0])
    if lines:
        out.append("")
        for d in lines:
            if isinstance(d, tuple):
                out.append(f"  {paint(d[0].ljust(width), BOLD, stream=stream)}  {d[1].plain() if isinstance(d[1], cards.Styled) else d[1]}")
            elif isinstance(d, cards.Block):
                out += [f"  {x}" for x in d]
            elif isinstance(d, cards.Heading):
                out += ["", f"  {paint(str(d).upper(), BOLD, stream=stream)}"]
            else:
                out.append("" if d == "\n" else f"  {d}")
    if steps:
        out += ["", paint(steps_label, BOLD, stream=stream)] + [f"  - {x}" for x in steps]
    if run:
        out += ["", paint(run_label, BOLD, stream=stream), ""]
        out += [c for c in ([run] if isinstance(run, str) else run)]       # plain text, not a terminal: exactly as typed, nothing to select around
    print("\n".join(out) + "\n", file=stream)


def fail(title, why=None, steps=None, example=None, code=2):
    """An error a person can act on: what went wrong, why, how to fix it, and a command to copy. Exits."""
    say(title, why, tag="ERROR", color=RED, steps=steps, steps_label="How to fix it", run=example)
    sys.exit(code)


def die(msg, code=2):
    """Anything unexpected (git output and the like): the first line is the title, the rest is the detail."""
    head, _, rest = str(msg).partition("\n")
    fail(head, rest.strip() or None, code=code)


def show(lines, stream=None):
    """Print lines that are already drawn (a card from `cw.cards`, a table of worker cards): the one place such text reaches the terminal."""
    print("\n".join(lines), file=stream or sys.stdout)                                                   # output: pre-drawn lines


def document(title, text, *, tag="SHOW", color=None, stream=None, notes=(), framed=True, as_diff=False):
    """The text of something (a job file, a kept output) as a card with the text inside it, for a person at a terminal. Piped, only the text is printed, exactly as it
    is, so a script can read it."""
    stream = stream or sys.stdout
    if _color_on(stream):
        lines = diffview.lines(text, diffview.room(framed)) if as_diff else (str(text).rstrip("\n").split("\n") or [""])
        if not str(text).strip():
            lines = [theme.sgr("(nothing to show)", "muted")]                          # never an empty box
        out = [""] + cards.card(title, [*notes, cards.Block(lines)], tag=tag, color=color or PRIMARY, framed=framed)
        print("\n".join(out) + "\n", file=stream)                                                       # output: a card
    else:
        print(text, file=stream)                                                                          # output: data, exactly as it is


def table(title, header, rows, *, tag="LIST", color=None, stream=None, notes=(), run=None, run_label="Next", empty=None):
    """A list of things as a card: the title, a short note or two, then a table with coloured capital column labels. Plain text (aligned columns) when nobody is watching
    a terminal. `rows` is [[cell, ...]] where a cell is text or (text, role); `empty` is said instead when there are no rows."""
    stream = stream or sys.stdout
    color = color or PRIMARY
    if not rows and empty:
        say(title, empty, tag="NOTE", color=YELLOW, stream=stream)
        return
    if _color_on(stream):
        w = cards.width()
        out = [""] + cards.card(title, [*notes, "\n", cards.Block(cards.table_lines(header, rows, w - 4))], tag=tag, color=color)
        if run:
            out += cards.commands(run, run_label) + offer_copy(run, stream)
        print("\n".join(out) + "\n", file=stream)                                                       # output: a card
        return
    lines = cards.table_lines(header, [[c[0] if isinstance(c, tuple) else c for c in r] for r in rows], 10 ** 6, color=False)
    tail = ["", run_label, *([run] if isinstance(run, str) else run)] if run else []
    print("\n".join([title, *[str(n) for n in notes], "", *lines, *tail]) + "\n", file=stream)       # output: plain text for a pipe


def related(*pairs, title="From here", stream=None, force=False):
    """The commands that usually come next after what was just shown, in the one shape every command uses: a bold line saying what it does, the command to copy on
    its own line beneath it. Printed to stderr and only for a person at a terminal, so a pipe, a script or `--json` never sees it (CT_HINTS=never turns it off).
    pairs are (what it does, command); a pair whose command is None is left out, so callers can offer things conditionally."""
    stream = stream or sys.stderr
    pairs = [p for p in pairs if p and p[1]]
    watching = force or (sys.stdout.isatty() and stream.isatty() and os.environ.get("CT_HINTS") != "never")
    if not pairs or not watching:
        return False
    out = ["", paint(title, BOLD, PRIMARY, stream=stream)]
    for what, command in pairs:
        out += ["", paint(what, BOLD, stream=stream), chip(command, stream)]
    print("\n".join(out) + "\n", file=stream)
    return True


def confirm_prompt(question, stream=None):
    """The text of a yes/no question, for `input()`. Always the `prompt` colour, wrapped before the terminal can cut a word, with the answer hint last.
    Plain text when nobody is watching a terminal."""
    stream = stream or sys.stdout
    if not _color_on(stream):
        return f"\n  {question} [y/N] "
    cols = max(40, min(__import__("shutil").get_terminal_size((100, 24)).columns, 100))
    from . import cards
    import textwrap
    lines = textwrap.wrap(cards.code_spans(question), cols - 8, subsequent_indent="    ", break_long_words=False, break_on_hyphens=False) or [""]     # a command in the question is a code chip
    lines[0], lines[-1] = "  ❯ " + lines[0], lines[-1] + "  [y/N] "
    return "\n" + "\n".join(cards.inline(lines, ("prompt", "strong")))
