"""The header for the wizard: a short text caption naming this tool, its job and its version, like a CLI splash screen.

There is no picture for now (it will be redesigned with the armada name in mind); the caption is the same at every terminal width."""
from .. import __version__, theme

NAME = "armada"
HANDLE = "@armada"


def spaced(text):
    """'install' -> 'I N S T A L L' (words kept apart), the way the subtitle is written."""
    return "   ".join(" ".join(w.upper()) for w in text.split())


def banner(cols, subtitle="S E T U P", color=True, light=None):
    """The header as a list of lines: the handle, then the job and the version, with a blank line above and below. `cols` and `light` are kept so callers need not change."""
    sub = f"{subtitle}   v{__version__}"
    return ["", " " + (theme.sgr(HANDLE, "primary") if color else HANDLE), "   " + (theme.sgr(sub, "quiet") if color else sub), ""]
