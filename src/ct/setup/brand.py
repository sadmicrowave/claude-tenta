"""The header for the wizard: a picture of a tentacled creature over a caption line naming this tool, its job and its version, like a CLI splash screen.

The picture is drawn from the table below (rows of coloured runs, colours as RGB), so it needs no data file and no escape codes in the source. It is artwork, so its
colours belong to it and do not follow the theme; with colour off it is the same shapes in plain characters. A terminal too narrow for it gets one line instead.
"""
from .. import __version__, theme

NAME = "claude-tenta"
HANDLE = "@claude-tenta"

PALETTE = [
    (240, 242, 248),
    (92, 99, 122),
    (146, 46, 118),
    (128, 136, 160),
    (94, 28, 84),
    (236, 152, 180),
    (245, 245, 250),
    (104, 170, 235),
    (47, 111, 192),
    (214, 234, 255),
    (29, 78, 146),
]
ART = [
    [(None, '       '), (0, '▒▓▒')],
    [(None, '     '), (0, '▒▓████████'), (1, '░'), (None, ' '), (0, '▒'), (None, '         '), (2, '▒▒▓▓████▓▓▓▒')],
    [(None, '   '), (0, '▓████████████████'), (2, '▒▒▓██████████████████▓▒')],
    [(None, '   '), (3, '▒▒▒▒▒▒▒▒▒▒▒▒▒▒'), (2, '▓██████████████████████████▒')],
    [(None, '    '), (1, '░░░░░░░░░░░░'), (2, '▓████████'), (4, '█'), (5, '█'), (4, '███████████'), (2, '███████▓')],
    [(None, '    '), (2, '▓████▓▒'), (None, '   '), (2, '▓█████'), (5, '██'), (4, '████▓▓▒▒'), (None, '     '), (4, '▒▓██'), (5, '█'), (2, '███████')],
    [(None, '  '), (2, '▒███'), (4, '███'), (2, '█'), (5, '█▒'), (None, ' '), (2, '██████'), (4, '███▒'), (None, '               '), (4, '▓███'), (2, '███'), (4, '███')],
    [(None, '  '), (2, '█'), (5, '█'), (2, '█'), (4, '█▓'), (None, '  '), (4, '▒'), (None, '  '), (2, '█████'), (4, '██▓▒'), (None, '                  '), (4, '▓'), (2, '█████'), (4, '███▓')],
    [(None, '  '), (2, '███'), (6, '██████'), (2, '▓'), (6, '██▀▀▀'), (4, '█'), (6, '██▄'), (None, ' '), (6, '██'), (None, ' '), (6, '██████'), (None, ' '), (6, '▄█▀▀█▄'), (None, '  '), (2, '██████'), (4, '███')],
    [(None, '  '), (2, '▓'), (4, '█'), (2, '███'), (6, '██'), (2, '██▒'), (6, '██▀▀'), (4, '█▒'), (6, '██▀███'), (None, '   '), (6, '██'), (None, '   '), (6, '██▄▄██'), (None, '  '), (2, '▒█████'), (4, '███▒')],
    [(None, '   '), (4, '▓██'), (2, '▓'), (6, '██'), (2, '███'), (6, '██▄▄▄'), (None, ' '), (6, '██'), (None, '  '), (6, '██'), (None, '   '), (6, '██'), (None, '   '), (6, '██'), (None, '  '), (6, '██'), (None, '   '), (2, '█████'), (4, '███▓')],
    [(7, '▓▓███▓'), (8, '███'), (7, '▓▓████'), (8, '██'), (7, '█▓██▓▒█'), (8, '██'), (7, '█'), (8, '███'), (7, '▓▓████'), (8, '██'), (7, '█▓▓███'), (8, '███'), (7, '██'), (9, '█')],
    [(10, '██████'), (8, '█████'), (10, '██████'), (8, '█████'), (10, '██████'), (8, '█████'), (10, '██████'), (8, '█████'), (10, '██████')],
    [(10, '██████████████████████████████████████████████████')],
]
WIDTH = 50                                    # the widest row of the picture


def spaced(text):
    """'install' -> 'I N S T A L L' (words kept apart), the way the subtitle is written."""
    return "   ".join(" ".join(w.upper()) for w in text.split())


def paint(text, colour, on=True):
    return theme.rgb(text, colour) if on and colour else text


def art_rows(color=True):
    """The picture as lines of text."""
    return ["".join(paint(text, PALETTE[i] if i is not None else None, color) for i, text in row).rstrip() for row in ART]


def banner(cols, subtitle="S E T U P", color=True):
    """The header as a list of lines for a terminal `cols` wide."""
    caption = f"  {paint(NAME.upper(), (240, 242, 248), color)}  {paint(subtitle, (150, 154, 170), color)}   {paint('v' + __version__, (214, 218, 234), color)}"
    if cols < WIDTH + 2:
        return ["", " " + theme.sgr(HANDLE, "primary") if color else " " + HANDLE, "   " + (theme.sgr(subtitle + f"   v{__version__}", "quiet") if color else f"{subtitle}   v{__version__}"), ""]
    return [""] + art_rows(color) + [caption, ""]
