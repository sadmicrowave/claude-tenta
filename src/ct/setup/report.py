"""How the setup prints what it is doing, on a terminal and everywhere else.

On a terminal: a STATUS column in capitals and one colour (green for done, yellow for waiting, red for a problem, dim for "nothing to do"), the action
aligned beside it, details dim after it, headings bold and coloured, and the raw commands left out unless --verbose asks for them. Anywhere else (a pipe, a
log, a test, --dry-run) it is the same plain text it always was, so nothing that reads it changes.
"""
import shutil
import sys
import textwrap

from .. import ui as _ui
from ..ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW

BLUE = "primary"               # a role: the main colour unless the theme says otherwise

GOOD = {"ok", "installed", "created", "written", "linked", "stored", "updated", "removed", "satisfied", "applied", "running", "done", "seeded", "cloned", "present",
        "added", "sent", "set", "captured"}
QUIET = {"skipped", "exists", "planned", "unchanged", "no change", "off"}
BAD = {"failed", "refused", "stopped", "error", "missing"}


def status_color(status):
    s = status.lower()
    if s in BAD:
        return RED
    if s in GOOD:
        return GREEN
    if s in QUIET:
        return DIM
    return YELLOW                          # waiting, partial, manual, anything that wants a look


class Styler:
    """The lines of the setup's output. On a terminal every line sits on the main-colour rail of a card that opens with its section's chip and is never closed (the output
    is a stream), so the whole setup reads as cards like every other screen; anywhere else the same lines are the plain text they always were."""
    WIDTH = 11

    def __init__(self, styled=False, verbose=False, stream=None):
        self.styled, self.verbose, self.stream = styled, verbose, stream or sys.stdout

    def p(self, text, *codes):
        return _ui.paint(text, *codes, stream=self.stream) if self.styled else text

    def rail(self, text=""):
        return (self.p("┃ ", BLUE) + text) if self.styled else text

    def columns(self):
        """How wide a line may be. Only a real terminal is measured; a log or a pipe is never wrapped."""
        try:
            return max(20, min(shutil.get_terminal_size((100, 24)).columns, 160)) - 1 if self.stream.isatty() else 0
        except (AttributeError, ValueError, OSError):
            return 0

    def wrapped(self, lead, text, hang, dim=True, code=None):
        """`lead` (already coloured, `hang` visible columns wide) then `text`; a long text continues on following lines under the first word, never past the right edge, and
        every line carries the rail. Lengths are counted on the visible text, never on colour codes."""
        cols = self.columns()
        found = []
        room = max(20, cols - hang - 2)
        from .. import cards
        if self.styled and text:                                                       # commands in the text are code chips, however the line is cut
            found = []
            pieces = cards.wrap_text(cards.lift(text, room, found), room, break_long_words=True, break_on_hyphens=False) if cols else [cards.code_spans(text)]
            role = {DIM: "muted", BOLD: "strong", RED: "bad", GREEN: "ok", YELLOW: "warn", CYAN: "info"}.get((code or (DIM,))[0]) if (dim or code) else None
            pieces = cards.inline(pieces, role)
        else:
            pieces = textwrap.wrap(text, room, break_long_words=True, break_on_hyphens=False) if cols and text else [text]       # nochip: plain text, no colour to draw a chip in
            pieces = pieces or [""]
            paint = (lambda t: self.p(t, *(code or (DIM,)))) if (dim or code) else (lambda t: t)
            pieces = [paint(x) for x in pieces]
        lines = [self.rail(lead + pieces[0])] + [self.rail(" " * hang + x) for x in pieces[1:]]
        if self.styled and text and found:                                      # a command too long for one line: alone below, no rail beside it, so selecting it copies only the command
            lines += [""] + [self.command(c) for c in found]
        return lines

    def head(self, text):
        if not self.styled:
            return "\n" + text
        return "\n" + self.rail(self.p(f" {text.upper()} ", "primary_chip")) + "\n" + self.rail()

    def note(self, status, step, detail=""):
        if not self.styled:
            return f"  {status.upper():<9} {step}" + (f"  ({detail})" if detail else "")
        width = max(self.WIDTH, len(status) + 1)                      # a label as long as the column ("BOOTSTRAPPED") still gets its space before the step
        lead = self.p(status.upper().ljust(width), BOLD, status_color(status))
        if not detail:
            return "\n".join(self.wrapped(lead, step, width, dim=False))
        cols = self.columns()
        if cols and cols - (width + len(step) + 2) - 2 < 24:                  # no room beside the step: the detail goes on its own lines under it, indented two
            return "\n".join(self.wrapped(lead, step, width, dim=False) + self.wrapped("  ", detail, 2))
        return "\n".join(self.wrapped(lead + step + "  ", detail, width + len(step) + 2))       # the detail starts right after the step, and wraps under itself

    def cmd(self, text):
        """A raw command the setup runs: shown in a plain log, hidden on a terminal unless --verbose (the cards and --dry-run already show what will happen)."""
        if not self.styled:
            return "    $ " + text
        return self.rail(self.p("$ " + text, DIM)) if self.verbose else None

    def info(self, text):
        if not self.styled:
            return "    " + text
        return "\n".join(self.wrapped("", text, 0))

    def kv(self, label, value):
        if not self.styled:
            return f"  {label:<22} {value}"
        return "\n".join(self.wrapped(self.p(label.upper().ljust(22), "label"), str(value), 22, dim=False))

    def warn(self, text):
        """Something to read before going on, in the caution colour."""
        if not self.styled:
            return "\n  " + text
        return "\n".join(self.wrapped(self.p("NOTE  ", BOLD, YELLOW), text, 6, code=(YELLOW,)))

    def error(self, text):
        if not self.styled:
            return "  " + text
        return "\n".join(self.wrapped(self.p("ERROR  ", BOLD, RED), text, 7, code=(RED,)))

    def block(self, lines):
        """Lines shown as they are (a configuration file, a plan), muted, each on the rail."""
        if not self.styled:
            return "\n".join("    " + line for line in lines)
        return "\n".join(self.rail(self.p(line, DIM)) for line in lines)

    def command(self, text):
        """A command to copy: alone on its line, no rail beside it, so selecting it picks up nothing else."""
        if not self.styled:
            return "    " + text
        from .. import cards
        return cards.chip(text)

    def line(self, text):
        """Any other line a part of the setup hands over as plain text, put on the rail in the style its first word asks for (NOTE, ERROR, OK, SKIP, a `$ command`, an
        indented continuation, a plain statement). A line that is already styled passes through untouched. Plain mode: the text, unchanged."""
        if not self.styled or "┃" in _ui_strip(text) or text.lstrip("\n").startswith("\x1b["):
            return text
        out = []
        while text.startswith("\n"):
            out.append(self.rail())
            text = text[1:]
        for raw in text.split("\n"):
            body = raw.strip()
            if not body:
                out.append(self.rail())
                continue
            first, _, rest = body.partition(" ")
            word = first.rstrip(":").upper()
            if word in ("NOTE", "WARNING", "WARN"):
                out.append(self.warn(rest.strip() or body).lstrip("\n"))
            elif word in ("ERROR", "FAIL", "FAILED"):
                out.append(self.error(rest.strip() or body))
            elif word in ("OK", "SKIP", "UPDATED", "REMOVED", "INSTALLED", "WAITING", "WRITTEN", "PRESENT", "ADDED"):
                out.append(self.note(word, rest.strip()))
            elif body.startswith("$ "):
                out.append(self.rail(self.p(body, DIM)))
            elif raw.startswith(" ") or raw.startswith("-"):
                out.append("\n".join(self.wrapped("", body, 0)))                                  # an indented continuation: quiet
            else:
                out.append("\n".join(self.wrapped("", body, 0, dim=False, code=(BOLD,))))        # a plain statement
        return "\n".join(out)


def _ui_strip(text):
    import re
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z~]", "", text)


def sink(stream=None, verbose=False):
    """The function every part of the setup prints through: `say(text)`. On a terminal each line is styled onto the rail (see Styler.line); anywhere else the text is
    printed as it is. Nothing in the setup prints on its own."""
    stream = stream or sys.stderr
    styler = Styler(styled=_ui._color_on(stream), verbose=verbose, stream=stream)

    def say(text=""):
        print(styler.line(str(text)), file=stream)                                                 # output: the setup's one printer
    return say
