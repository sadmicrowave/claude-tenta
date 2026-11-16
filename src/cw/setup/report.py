"""How the setup prints what it is doing, on a terminal and everywhere else.

On a terminal: a STATUS column in capitals and one colour (green for done, yellow for waiting, red for a problem, dim for "nothing to do"), the action
aligned beside it, details dim after it, headings bold and coloured, and the raw commands left out unless --verbose asks for them. Anywhere else (a pipe, a
log, a test, --dry-run) it is the same plain text it always was, so nothing that reads it changes.
"""
import sys

from .. import ui as _ui
from ..ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW

BLUE = "38;5;75"            # the wizard's own blue

GOOD = {"ok", "installed", "created", "written", "linked", "stored", "updated", "removed", "satisfied", "applied", "running", "done", "seeded", "cloned", "present",
        "added", "sent", "set", "captured"}
QUIET = {"skipped", "exists", "planned", "unchanged", "already cloned", "no change", "off"}
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

    def note(self, status, step, detail=""):
        if not self.styled:
            return f"  {status.upper():<9} {step}" + (f"  ({detail})" if detail else "")
        return "  " + self.p(status.upper().ljust(self.WIDTH), BOLD, status_color(status)) + step + (self.p(f"  {detail}", DIM) if detail else "")

    def cmd(self, text):
        """A raw command the setup runs: shown in a plain log, hidden on a terminal unless --verbose (the cards and --dry-run already show what will happen)."""
        if not self.styled:
            return "    $ " + text
        return self.p("      $ " + text, DIM) if self.verbose else None

    def info(self, text):
        return "    " + text if not self.styled else "      " + self.p(text, DIM)

    def kv(self, label, value):
        return f"  {label:<22} {value}" if not self.styled else "  " + self.p(label.ljust(22), DIM) + str(value)
