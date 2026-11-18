"""The header for the wizard: a picture of a tentacled creature over a caption line naming this tool, its job and its version, like a CLI splash screen.

The picture is drawn from the table below (rows of coloured runs, colours as RGB), so it needs no data file and no escape codes in the source. It is artwork, so its
colours belong to it and do not follow the theme; with colour off it is the same shapes in plain characters. A terminal too narrow for it gets one line instead.
"""
from .. import __version__, theme

NAME = "claude-tenta"
HANDLE = "@claude-tenta"

PALETTE = [
    (150, 160, 192),
    (92, 99, 122),
    (146, 46, 118),
    (128, 136, 160),
    (94, 28, 84),
    (236, 152, 180),
    (20, 152, 168),
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


# One palette for light and dark terminals. The cloud (index 0) and the letters (index 6) used to be near-white and switched to dark ink on a light terminal, which depended on guessing the
# terminal's background correctly: a wrong guess made the name dark on dark (seen on a real terminal). They are mid-tones now, readable on both (the letters 3:1 or better against white
# and against a dark terminal's background, a test checks it), so nothing here depends on the guess.
LIGHT = {}
SKY = [[]]                                       # a row of sky above the picture, so the picture keeps its height with one row less water
WATER_ROWS = 2                                   # the last rows of ART are water; the picture is drawn with these


def paint(text, colour, on=True):
    return theme.rgb(text, colour) if on and colour else text


def _cells(row):
    return [(i, ch) for i, text in row for ch in text]


CLOUD = (0, 1, 3)                                # the colours of the cloud: its pieces are placed on their own


def scene(width, light=False):
    """[[(colour index or None, character)]] rows exactly `width` wide: the creature and its name a little left of centre, the cloud further left, the water carried
    on to both edges (the wave pattern repeats)."""
    rows = SKY + ART[:len(ART) - 1]                       # the solid bottom row of water is dropped
    free = max(0, width - WIDTH)
    pad = int(free * 0.35)                                # left of centre, not centred
    cloud_at = pad - int(free * 0.2)                      # and the cloud nearer the edge, to use the empty space on its side
    out = []
    for n, row in enumerate(rows):
        cells = _cells(row)
        cells += [(None, " ")] * (WIDTH - len(cells))
        if n >= len(rows) - WATER_ROWS:                  # water: repeat the pattern across the whole width
            out.append([cells[(x - pad) % WIDTH] for x in range(width)])
            continue
        line = [(None, " ")] * width
        for x, cell in enumerate(cells):                 # the cloud first, so the creature is drawn over it where they meet
            if cell[0] in CLOUD:
                line[cloud_at + x] = cell
        for x, cell in enumerate(cells):
            if cell[0] is not None and cell[0] not in CLOUD:
                line[pad + x] = cell
        out.append(line)
    return out


def colour_of(index, light=False):
    return (LIGHT.get(index) if light else None) or PALETTE[index]


def art_rows(color=True, width=WIDTH, light=False):
    """The picture as lines of text, `width` wide."""
    lines = []
    for line in scene(width, light):
        text, run, cur = "", "", "start"
        for i, ch in line:
            if i != cur:
                text += paint(run, colour_of(cur, light) if cur is not None and cur != "start" else None, color) if run else ""
                run, cur = "", i
            run += ch
        text += paint(run, colour_of(cur, light) if cur is not None and cur != "start" else None, color)
        lines.append(text)
    return lines


def banner(cols, subtitle="S E T U P", color=True, light=None):
    """The header as a list of lines for a terminal `cols` wide: the picture in a frame across the whole width, in the same outline as every card."""
    if cols < WIDTH + 2:
        return ["", " " + theme.sgr(HANDLE, "primary") if color else " " + HANDLE, "   " + (theme.sgr(subtitle + f"   v{__version__}", "quiet") if color else f"{subtitle}   v{__version__}"), ""]
    light = (theme.background() == "light") if light is None else light
    edge = (lambda t: theme.sgr(t, "primary")) if color else (lambda t: t)
    inner = cols - 2
    title, tag = f"{NAME.upper()}  {subtitle}", f"v{__version__}"
    head = (edge("╭─ ") + (theme.sgr(title, "strong") if color else title) + edge(" " + "─" * max(1, inner - len(title) - len(tag) - 6) + " ") + (theme.sgr(tag, "primary") if color else tag) + edge(" ─╮"))
    body = [edge("│") + row + edge("│") for row in art_rows(color, inner, light)]
    return [""] + [head] + body + [edge("╰" + "─" * inner + "╯"), ""]
