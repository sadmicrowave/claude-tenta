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

BLUE = "accent"                # a role: the wizard's own blue unless the theme says otherwise

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
    WIDTH = 11

    def __init__(self, styled=False, verbose=False, stream=None):
        self.styled, self.verbose, self.stream = styled, verbose, stream or sys.stdout

    def p(self, text, *codes):
        return _ui.paint(text, *codes, stream=self.stream) if self.styled else text

    def head(self, text):
        return "\n" + (self.p(f"▸ {text}", BOLD, BLUE) if self.styled else text)

    def columns(self):
        """How wide a line may be. Only a real terminal is measured; a log or a pipe is never wrapped."""
        try:
            return max(50, min(shutil.get_terminal_size((100, 24)).columns, 160)) - 1 if self.stream.isatty() else 0
        except (AttributeError, ValueError, OSError):
            return 0

    def wrapped(self, lead, text, hang, dim=True):
        """`lead` (already coloured, `hang` visible columns wide) then `text`; a long text continues on following lines under the first word, never past the right edge,
        and no line ever starts with a space that came from the wrapping. Lengths are counted on the visible text, never on colour codes."""
        cols = self.columns()
        pieces = textwrap.wrap(text, max(20, cols - hang), break_long_words=True, break_on_hyphens=False) if cols and text else [text]
        pieces = pieces or [""]
        paint = (lambda t: self.p(t, DIM)) if dim else (lambda t: t)
        return [lead + paint(pieces[0])] + [" " * hang + paint(x) for x in pieces[1:]]

    def note(self, status, step, detail=""):
        if not self.styled:
            return f"  {status.upper():<9} {step}" + (f"  ({detail})" if detail else "")
        lead = "  " + self.p(status.upper().ljust(self.WIDTH), BOLD, status_color(status))
        if not detail:
            return "\n".join(self.wrapped(lead, step, 2 + self.WIDTH, dim=False))
        first = self.wrapped(lead + step + "  ", detail, 2 + self.WIDTH + len(step) + 2)       # the detail starts right after the step, and wraps under itself
        return "\n".join(first)

    def cmd(self, text):
        """A raw command the setup runs: shown in a plain log, hidden on a terminal unless --verbose (the cards and --dry-run already show what will happen)."""
        if not self.styled:
            return "    $ " + text
        return self.p("      $ " + text, DIM) if self.verbose else None

    def info(self, text):
        if not self.styled:
            return "    " + text
        return "\n".join(self.wrapped("      ", text, 6))

    def kv(self, label, value):
        if not self.styled:
            return f"  {label:<22} {value}"
        return "\n".join(self.wrapped("  " + self.p(label.ljust(22), DIM), str(value), 24, dim=False))
