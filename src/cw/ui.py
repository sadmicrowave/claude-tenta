"""Terminal output used by every cw command: one shape for messages a person reads.

A coloured TAG and a bold title, indented detail lines, optional bullet steps, then commands to copy, each alone on its own line.
Colour is only used when a person is watching a terminal: plain text when piped or captured, when NO_COLOR is set, or CW_COLOR=never.
"""
import os
import sys

from . import cards, theme

# Roles, not colours: what each looks like is decided in cw.theme (and the user's theme.toml). The older names stay so every caller keeps working.
BOLD, DIM, ITALIC, RED, GREEN, YELLOW, CYAN = "strong", "muted", "italic", "bad", "ok", "warn", "info"
ACCENT, ROOT = "accent", "root"


def _color_on(stream):
    mode = os.environ.get("CW_COLOR", "auto")
    if mode == "always":
        return True
    return mode != "never" and "NO_COLOR" not in os.environ and stream.isatty()


def paint(text, *codes, stream=None):
    """Colour (by role, see cw.theme) for a person reading a terminal; plain text when piped, captured by Claude, or NO_COLOR is set."""
    if not codes or not _color_on(stream or sys.stderr):
        return text
    return theme.sgr(text, *codes)


def say(title, *details, tag="DONE", color=GREEN, steps=None, steps_label="Next", run=None, run_label="Run this", stream=None):
    """One shape for every message a person reads: a coloured TAG and a bold title, indented detail lines, optional bullet steps,
    then commands to copy, each alone on its own line (nothing before or after it)."""
    stream = stream or sys.stderr
    if _color_on(stream):                                      # a person at a terminal: the wizard's card (see cw.cards)
        out = [""] + cards.card(title, details, tag=tag, color=color, steps=steps, steps_label=steps_label)
        if run:
            out += cards.commands(run, run_label)
        print("\n".join(out) + "\n", file=stream)
        return
    out = ["", f"{paint(tag, BOLD, color, stream=stream)}  {paint(title, BOLD, stream=stream)}"]
    lines = [d for d in details if d]          # a ("Label", "value") pair becomes an aligned bold-label line; "\n" is a blank line
    width = max([len(d[0]) for d in lines if isinstance(d, tuple)] or [0])
    if lines:
        out.append("")
        for d in lines:
            if isinstance(d, tuple):
                out.append(f"  {paint(d[0].ljust(width), BOLD, stream=stream)}  {d[1]}")
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
        out += [paint(c, BOLD, GREEN, stream=stream) for c in ([run] if isinstance(run, str) else run)]
    print("\n".join(out) + "\n", file=stream)


def fail(title, why=None, steps=None, example=None, code=2):
    """An error a person can act on: what went wrong, why, how to fix it, and a command to copy. Exits."""
    say(title, why, tag="ERROR", color=RED, steps=steps, steps_label="How to fix it", run=example)
    sys.exit(code)


def die(msg, code=2):
    """Anything unexpected (git output and the like): the first line is the title, the rest is the detail."""
    head, _, rest = str(msg).partition("\n")
    fail(head, rest.strip() or None, code=code)


def related(*pairs, title="From here", stream=None, force=False):
    """The commands that usually come next after what was just shown, in the one shape every command uses: a bold line saying what it does, the command to copy on
    its own line beneath it. Printed to stderr and only for a person at a terminal, so a pipe, a script or `--json` never sees it (CW_HINTS=never turns it off).
    pairs are (what it does, command); a pair whose command is None is left out, so callers can offer things conditionally."""
    stream = stream or sys.stderr
    pairs = [p for p in pairs if p and p[1]]
    watching = force or (sys.stdout.isatty() and stream.isatty() and os.environ.get("CW_HINTS") != "never")
    if not pairs or not watching:
        return False
    out = ["", paint(title, BOLD, ACCENT, stream=stream)]
    for what, command in pairs:
        out += ["", paint(what, BOLD, stream=stream), paint(command, BOLD, GREEN, stream=stream)]
    print("\n".join(out) + "\n", file=stream)
    return True


def confirm_prompt(question, stream=None):
    """The text of a yes/no question, for `input()`. Always the `prompt` colour, wrapped before the terminal can cut a word, with the answer hint last.
    Plain text when nobody is watching a terminal."""
    stream = stream or sys.stdout
    if not _color_on(stream):
        return f"\n  {question} [y/N] "
    cols = max(40, min(__import__("shutil").get_terminal_size((100, 24)).columns, 100))
    text = __import__("textwrap").fill(question, cols - 8, subsequent_indent="    ", break_long_words=False, break_on_hyphens=False)
    return "\n" + theme.sgr("  ❯ " + text + "  [y/N] ", "prompt", "strong")
