"""The header for the wizard: a small mark beside the name in large block letters, and a spaced-out subtitle line with the version, like a CLI splash screen.

Wide terminals get the mark and the big name; narrower ones get the big name alone; a very narrow one gets a single line. It is drawn once at the start of a
setup, and the progress line keeps the name on screen after that. Everything is plain text and ANSI colour, generated here from a small glyph table.
"""
from .. import __version__

NAME = "cw-ballast"
HANDLE = "@cw-ballast"
TEAL, TEAL_DIM, GREY = "38;5;80", "38;5;37", "38;5;245"

# Block letters, 6 rows high. The solid body is drawn bright and the box-drawing shadow darker, so the letters look raised.
GLYPHS = {
    "c": [" ██████╗", "██╔════╝", "██║     ", "██║     ", "╚██████╗", " ╚═════╝"],
    "w": ["██╗    ██╗", "██║    ██║", "██║ █╗ ██║", "██║███╗██║", "╚███╔███╔╝", " ╚══╝╚══╝ "],
    "-": ["       ", "       ", "█████╗ ", "╚════╝ ", "       ", "       "],
    "b": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██████╔╝", "╚═════╝ "],
    "a": [" █████╗ ", "██╔══██╗", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    "l": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "s": ["███████╗", "██╔════╝", "███████╗", "╚════██║", "███████║", "╚══════╝"],
    "t": ["████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ██║   ", "   ╚═╝   "],
}
SHADOW = set("╔╗╚╝║═")


def word(text, gap=1):
    """The rows of `text` in block letters."""
    rows = [""] * 6
    for ch in text.lower():
        g = GLYPHS[ch]
        width = max(len(r) for r in g)
        for i in range(6):
            rows[i] += g[i].ljust(width) + " " * gap
    return [r.rstrip() for r in rows]


def mark(radius=5):
    """A small ballast weight: a ring on top of a round body, drawn with @ and # (light from the upper left), in the spirit of a CLI logo."""
    rows = []
    for y in range(-6, radius + 3):
        row = ""
        for x in range(-2 * radius - 2, 2 * radius + 3):
            cx = x / 2
            body = cx ** 2 + (y - 2) ** 2 <= (radius + 0.3) ** 2
            ring = y < -0.5 and abs((cx / 2.6) ** 2 + ((y + 3.6) / 2.2) ** 2 - 1) < 0.32
            row += ("@" if cx + (y - 2) < 0.8 else "#") if body else "@" if ring else " "
        rows.append(row.rstrip())
    left = min(len(r) - len(r.lstrip()) for r in rows if r.strip())
    return [r[left:] for r in rows if r.strip() or True][: len(rows)]


def paint(text, code, on=True):
    return f"\x1b[{code}m{text}\x1b[0m" if on and code else text


def colour_letters(row, on=True):
    out, cur, buf = [], None, ""
    for ch in row:
        kind = "shadow" if ch in SHADOW else "body" if ch == "█" else "space"
        if kind != cur and buf:
            out.append(paint(buf, TEAL if cur == "body" else TEAL_DIM if cur == "shadow" else None, on))
            buf = ""
        cur, buf = kind, buf + ch
    if buf:
        out.append(paint(buf, TEAL if cur == "body" else TEAL_DIM if cur == "shadow" else None, on))
    return "".join(out)


def spaced(text):
    """'install' -> 'I N S T A L L' (words kept apart), the way the subtitle line is written."""
    return "   ".join(" ".join(w.upper()) for w in text.split())


def banner(cols, subtitle="S E T U P", color=True):
    """The header as a list of lines for a terminal `cols` wide."""
    letters = word(NAME)
    text_w = max(len(r) for r in letters)
    sub = f"{HANDLE}   {subtitle}   v{__version__}"
    if cols < text_w + 4:
        return ["", " " + paint(HANDLE, TEAL, color) + paint("   " + subtitle + f"   v{__version__}", GREY, color), ""]
    art = mark()
    with_mark = cols >= text_w + max(len(r) for r in art) + 6
    lines = [""]
    if with_mark:
        m_w = max(len(r) for r in art) + 3
        top = max(0, (len(art) - 6) // 2)
        for i in range(len(art)):
            left = paint(art[i].ljust(m_w - 3), TEAL, color) + "   "
            j = i - top
            right = colour_letters(letters[j], color) if 0 <= j < 6 else ""
            lines.append(" " + left + right)
        lines.append(" " + " " * m_w + paint(sub, GREY, color))
    else:
        lines += [" " + colour_letters(r, color) for r in letters]
        lines.append(" " + paint(sub, GREY, color))
    return lines + [""]
