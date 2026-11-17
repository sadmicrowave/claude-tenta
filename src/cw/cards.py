"""Every message the tool prints to a person, drawn the way the wizard draws its questions.

A message is a card. Ordinary news (done, queued, ok, a note) has a coloured rail down its left edge, a filled chip naming the kind of news, a plain subject, muted
context, and labelled facts whose labels are coloured capitals. A warning or anything critical (an error, a removal, something that runs as root) is drawn inside
a coloured outline instead, like the wizard's protection cards. Commands to copy are printed beneath the card, each alone on a line, so selecting one never
picks up a frame. Colours are roles from cw.theme; nothing here names a colour. Lengths are always counted on the visible text, never on colour codes.
"""
import re
import shutil
import textwrap

from . import theme

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]")
FRAMED = {"bad", "warn", "root"}                    # these roles get the outline; everything else gets the rail
CHIP = {"ok": "ok_chip", "bad": "bad_chip", "warn": "warn_chip", "root": "root_chip"}
ROLE_OF = {"info": "accent", "accent": "accent", "ok": "ok", "bad": "bad", "warn": "warn", "root": "root"}


class Heading(str):
    """A heading inside a card ("Output", "Measured"): a coloured capital line with a blank line above it."""


class Block(list):
    """Lines that are shown as they are (a job's output): not re-flowed, only cut at the card's edge."""


def vlen(text):
    return len(ANSI.sub("", text))


def width(stream=None, cols=None):
    cols = cols or shutil.get_terminal_size((80, 24)).columns
    return max(50, min(cols, 100))


def role_for(color):
    """The role a message's colour stands for; an unknown one reads as plain information."""
    return ROLE_OF.get(color, "accent")


def wrap(text, room):
    return textwrap.wrap(str(text), max(10, room), break_long_words=True, break_on_hyphens=False) or [""]


def card(title, details, *, tag, color, steps=None, steps_label="Next", cols=None, framed=None):
    """The lines of one message card (a list of strings, colour codes included)."""
    role = role_for(color)
    p = lambda text, *codes: theme.sgr(text, *codes)                    # noqa: E731
    w = width(cols=cols)
    rows = [d for d in details if d]
    labelled = [d for d in rows if isinstance(d, tuple)]
    lw = max([len(d[0]) for d in labelled] or [0])
    body = []                                                           # (kind, payload) so the two frames can place them
    for d in rows:
        if isinstance(d, tuple):
            body.append(("fact", d))
        elif isinstance(d, Heading):
            body += [("blank", None), ("head", str(d))]
        elif isinstance(d, Block):
            body += [("raw", line) for line in d]
        else:
            body.append(("blank", None) if d == "\n" else ("note", str(d)))
    if steps:
        body += [("blank", None), ("fact", (steps_label, None))] + [("step", s) for s in steps]
    framed = (role in FRAMED) if framed is None else framed             # a result's output box is framed in green too, not only for warnings
    inner = w - 4 if framed else w - 2

    def label(text):
        return p(text.upper().ljust(lw if lw else 0), role if framed else "label")

    out = []
    if framed:
        edge = lambda t: p(t, role)                                       # noqa: E731
        top_tag = tag.upper()
        out.append(edge("╭─ ") + p(title, role, "strong") + edge(" " + "─" * max(1, inner - vlen(title) - len(top_tag) - 4) + " ") + p(top_tag, role) + edge(" ─╮"))

        def row(text=""):
            return edge("│") + " " + text + " " * max(0, inner - vlen(text)) + " " + edge("│")
        if body:
            out.append(row())
    else:
        rail = lambda text="": p("┃ ", role) + text                      # noqa: E731
        row = rail
        out += [rail(p(f" {tag.upper()} ", CHIP.get(role, "accent_chip"))), rail(), rail(p(title, "strong"))]
        if body and body[0][0] != "note":                              # context hugs its subject; facts and sections are set apart from it
            out.append(rail())
    fact_room = inner - (lw + 2 if lw else 0)
    for kind, item in body:
        if kind == "blank":
            out.append(row())
        elif kind == "note":
            out += [row(p(t, "muted")) for t in wrap(item, inner)]
        elif kind == "head":
            out.append(row(p(item.upper(), role if framed else "label")))
        elif kind == "raw":
            out += [row(item[i:i + inner]) for i in range(0, max(len(item), 1), inner)]
        elif kind == "fact" and item[1] is None:                        # a heading inside the card ("NEXT")
            out.append(row(p(item[0].upper(), role if framed else "label")))
        elif kind == "fact":
            first, *rest = wrap(item[1], fact_room)
            out.append(row(label(item[0]) + "  " + first))
            out += [row(" " * (lw + 2) + line) for line in rest]
        elif kind == "step":
            first, *rest = wrap(item, inner - 4)
            out += [row(p("  - ", "muted") + first)] + [row("    " + line) for line in rest]
    if framed:
        out += ([row()] if body else []) + [edge("╰" + "─" * (inner + 2) + "╯")]
    return out


def meta(sections, cols=None):
    """Facts ABOUT something (how long it took, what it used), drawn quietly beneath it: muted capitals and muted values, no rail and no frame, so they never compete with
    the content above. sections is [(heading, [(label, value), ...])]."""
    w = width(cols=cols)
    p = lambda text: theme.sgr(text, "muted")                           # noqa: E731
    out = []
    for heading, rows in sections:
        if not rows:
            continue
        lw = max(len(k) for k, _ in rows)
        out += ["", "  " + p(heading.upper())]
        for k, v in rows:
            first, *rest = wrap(v, w - lw - 6)
            out.append("  " + p(k.upper().ljust(lw)) + "  " + p(first))
            out += ["  " + " " * (lw + 2) + p(line) for line in rest]
    return out


def commands(run, label="Run this"):
    """The commands to copy: a label, then each command alone on its own line with nothing before or after it."""
    cmds = [run] if isinstance(run, str) else list(run)
    return ["", "", theme.sgr(f" {label.upper()} ", "accent_chip"), ""] + [theme.sgr(c, "ok", "strong") for c in cmds]       # its own breakout, set apart from the card above
