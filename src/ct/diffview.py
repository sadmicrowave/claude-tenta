"""Colours for a diff, the way GitHub draws one: an added line on a soft green band, a removed line on a soft red band, a hunk header or a changed-file note on a soft
blue-grey band, each with text in a stronger shade of the same colour. The bands run the full width of the card, and a long line that wraps keeps its colour on every
row. The shades follow the terminal's background (light or dark; see theme.background).

The pieces are exact RGB colours chosen for contrast, so they do not depend on the 16-colour palette of a terminal or on the active theme."""
from . import cards, theme

# kind -> (background, text), for a light terminal and for a dark one
SHADES = {
    "light": {"add": ((218, 251, 225), (17, 99, 41)), "del": ((255, 235, 233), (130, 6, 29)), "neutral": ((221, 244, 255), (9, 80, 160)), "meta": (None, (87, 96, 106))},
    "dark": {"add": ((18, 58, 32), (126, 231, 135)), "del": ((72, 24, 30), (255, 123, 114)), "neutral": ((24, 44, 72), (121, 192, 255)), "meta": (None, (139, 148, 158))},
}
STATUS_KIND = {"A": "add", "D": "del"}                  # everything else a file can be (modified, renamed, copied, type changed) is neutral


def _shade(kind, mode=None):
    return SHADES[mode or theme.background()][kind]


def code(kind, mode=None):
    """The terminal colour codes (as theme.sgr takes them) for a kind of line."""
    bg, fg = _shade(kind, mode)
    return (f"48;2;{bg[0]};{bg[1]};{bg[2]};" if bg else "") + f"38;2;{fg[0]};{fg[1]};{fg[2]}"


def status_kind(status):
    return STATUS_KIND.get((status or "M")[:1], "neutral")


def status_cell(text, status, mode=None):
    """A table cell (text, role) for a changed file's status or name, on the band for its kind."""
    return (text, code(status_kind(status), mode))


def counts(path, added, removed, mode=None):
    """A changed file with its line counts as a card fact: the name, then +N on the green band and -M on the red one (git prints '-' for a binary file: shown as neutral)."""
    binary = added == "-" or removed == "-"
    pieces = [(f"{path}  ", None)]
    pieces += [("binary", code("neutral", mode))] if binary else [(f"+{added}", code("add", mode)), (" ", None), (f"-{removed}", code("del", mode))]
    return cards.Styled(pieces + [("", None)])


def line_kind(line):
    if line.startswith("+++") or line.startswith("---") or line.startswith(("diff ", "index ", "new file", "deleted file", "similarity", "rename ", "old mode", "new mode", "Binary")):
        return "meta"
    if line.startswith("@@"):
        return "neutral"
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "del"
    return None                                          # context: left plain


def lines(text, room, mode=None):
    """The diff text as rows exactly `room` characters wide, each coloured by its kind (a long line wraps; every piece keeps the colour)."""
    out = []
    for raw in str(text).rstrip("\n").expandtabs(4).split("\n"):
        kind = line_kind(raw)
        pieces = [raw[i:i + room] for i in range(0, max(len(raw), 1), room)] or [""]
        for piece in pieces:
            out.append(theme.sgr(piece.ljust(room), code(kind, mode)) if kind else piece)
    return out


def room(framed=True):
    """How many characters wide a card's inside is, so a band can reach both edges."""
    return cards.width() - (4 if framed else 2)
