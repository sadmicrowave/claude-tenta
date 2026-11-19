"""Every message the tool prints to a person, drawn the way the wizard draws its questions.

A message is a card. Ordinary news (done, queued, ok, a note) has a coloured rail down its left edge, a filled chip naming the kind of news, a plain subject, muted
context, and labelled facts whose labels are coloured capitals. A warning or anything critical (an error, a removal, something that runs as root) is drawn inside
a coloured outline instead, like the wizard's protection cards. Commands to copy are printed beneath the card, each alone on a line, so selecting one never
picks up a frame. Colours are roles from ct.theme; nothing here names a colour. Lengths are always counted on the visible text, never on colour codes.
"""
import re
import shutil
import textwrap

from . import theme

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]")
FRAMED = {"bad", "warn", "root"}                    # these roles get the outline; everything else gets the rail
CHIP = {"ok": "ok_chip", "bad": "bad_chip", "warn": "warn_chip", "root": "root_chip", "primary": "primary_chip", "accent": "accent_chip"}
ROLE_OF = {"info": "primary", "primary": "primary", "accent": "accent", "ok": "ok", "bad": "bad", "warn": "warn", "root": "root"}


class Heading(str):
    """A heading inside a card ("Output", "Measured"): a coloured capital line with a blank line above it."""


class Styled(list):
    """A fact's value made of differently coloured pieces: [(text, role or None), ...]. Kept on one line (cut with a mark at the card's edge)."""

    def plain(self):
        return "".join(t for t, _ in self)

    def lines(self, room):
        """The value as one or more lines: the pieces before the last stay on the first line, and the last (the long explanation) wraps under itself, so nothing is cut off."""
        *head, (last, last_role) = list(self)
        lead = "".join(t for t, _ in head)
        text = [theme.sgr(t, r) if r and t else t for t, r in head]
        width = max(10, room - len(lead))
        pieces = wrap(last.lstrip(), width - 2 if lead else width) if last else [""]
        first = "".join(text) + ("  " if lead and pieces and pieces[0] else "") + (theme.sgr(pieces[0], last_role) if last_role and pieces[0] else pieces[0])
        return [first] + [(" " * (len(lead) + 2)) + (theme.sgr(p, last_role) if last_role else p) for p in pieces[1:]]


class Cmd(str):
    """A fact's label that is a command (or any name written exactly as typed): shown as it is, in the command colour, not turned into capitals."""


class Block(list):
    """Lines that are shown as they are (a job's output): not re-flowed, only cut at the card's edge."""


def vlen(text):
    return len(ANSI.sub("", text))


def width(stream=None, cols=None):
    cols = cols or shutil.get_terminal_size((80, 24)).columns
    return max(50, min(cols, 100))


PROGRAMS = ("ct-setup", "ct-admin", "ct-run", "ct-worker", "ct-update")
NESTED = {"add", "remove", "list", "pre", "post", "profile", "log", "hook", "direct", "github", "help"}       # words that follow a command word: ct-admin action add, ct-run offload log hook pre, ct-setup transport direct
_SUBS = []


def subcommands():
    """Every command word the three tools have, read from their own parsers once (so a new command is a command here too, with nothing to keep in step)."""
    if not _SUBS:
        found = set(NESTED)
        for mod in ("client", "admin", "setup"):
            try:
                import importlib
                parsed = importlib.import_module(f"ct.{mod}.cli").build_parser()
                sp = parsed[1] if isinstance(parsed, tuple) else next(a for a in parsed._actions if hasattr(a, "choices") and a.choices)
                found |= set(getattr(sp, "choices", None) or {})
            except Exception:                                                  # a card must always be drawable
                pass
        _SUBS.append(found)
    return _SUBS[0]


_ARG = r"(?:--[\w-]+|<[^>\s]+>|\{\}|[A-Z]{3,}|[\w@:/=-]+(?:\.[\w@:/=-]+)+|[\w.-]*[@/:=][\w@:/=.-]*)"          # a flag, <placeholder>, {}, NAME, box.example, user@host, a path
_BARE = re.compile(r"(?<![`\w/.-])((?:" + "|".join(PROGRAMS) + r")(?P<rest>(?: " + _ARG + r"| [a-z][\w-]*)*)|ssh-copy-id(?: -p \S+)? [\w@.:{}-]+|ssh-keygen -R [\w@.:{}-]+|sudo systemctl \w+ \w+)")


def code_spans(text):
    """A command written in a sentence, as typed (ct-admin check, ssh-copy-id user@host), becomes a `code` span, so it is a code chip wherever a card is drawn and nobody has to remember the
    backticks. Spans already in backticks are left alone. Only real command words follow the program name, so 'ct-run jobs shows them' chips 'ct-run jobs'."""
    text = str(text)
    if not any(t in text for t in PROGRAMS + ("ssh-copy-id", "ssh-keygen", "sudo systemctl")):
        return text
    subs = subcommands()

    def one(m):
        if m.group(1).startswith(PROGRAMS):
            keep, words = [], m.group("rest").split(" ")[1:] if m.group("rest") else []
            for w in words:
                if (w[:1].isalpha() and w.islower() and w not in subs and not re.fullmatch(_ARG, w)) or (w in ("again", "and", "to", "on", "for")):
                    break
                keep.append(w)
            cmd = " ".join([m.group(1).split(" ")[0]] + keep)
        else:
            cmd = m.group(1)
        return f"`{cmd}`" + code_spans(m.string[m.start() + len(cmd):m.end()])               # what was left over may hold a second command
    parts = re.split(r"(`[^`]*`)", text)
    return "".join(p if p.startswith("`") else _BARE.sub(one, p) for p in parts)


def inline(lines, base=None):
    """Lines of one paragraph with every `code` span shown in the code role (a filled background) and the rest in `base`. A span may run over a line break. The backticks stay, inside the
    coloured span, so the same text still reads as code where colour is off."""
    out, in_code = [], False
    for line in lines:
        res, buf = [], ""

        def flush(text, role):
            if text:
                res.append(theme.sgr(text, role) if role else text)
        for ch in line:
            if ch == "`" and not in_code:
                flush(buf, base)
                buf, in_code = "`", True
            elif ch == "`":
                flush(buf + "`", "code")
                buf, in_code = "", False
            else:
                buf += ch
        flush(buf, "code" if in_code else base)
        out.append("".join(res))
    return out


def role_for(color):
    """The role a message's colour stands for; an unknown one reads as plain information."""
    return ROLE_OF.get(color, "primary")


def wrap(text, room):
    return textwrap.wrap(str(text), max(10, room), break_long_words=True, break_on_hyphens=False) or [""]


def card(title, details, *, tag, color, steps=None, steps_label="Next", cols=None, framed=None):
    """The lines of one message card (a list of strings, colour codes included)."""
    role = role_for(color)
    p = lambda text, *codes: theme.sgr(text, *codes)                    # noqa: E731
    w = width(cols=cols)
    rows = [d for d in details if d]
    labelled = [d for d in rows if isinstance(d, tuple)]
    lw = max([len(d[0]) + (2 if isinstance(d[0], Cmd) else 0) for d in labelled] or [0])
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
        if isinstance(text, Cmd):
            return chip(str(text)) + " " * max(0, lw - len(text) - 2)                       # the chip's two padding spaces count
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
        out += [rail(p(f" {tag.upper()} ", CHIP.get(role, "primary_chip"))), rail(), rail(p(title, "strong"))]
        if body and body[0][0] != "note":                              # context hugs its subject; facts and sections are set apart from it
            out.append(rail())
    fact_room = inner - (lw + 2 if lw else 0)
    for kind, item in body:
        if kind == "blank":
            out.append(row())
        elif kind == "note":
            out += [row(t) for t in inline(wrap(code_spans(item), inner), "muted")]
        elif kind == "head":
            out.append(row(p(item.upper(), role if framed else "label")))
        elif kind == "raw":
            out += [row(item)] if vlen(item) <= inner else [row(item[i:i + inner]) for i in range(0, max(len(item), 1), inner)]
        elif kind == "fact" and item[1] is None:                        # a heading inside the card ("NEXT")
            out.append(row(p(item[0].upper(), role if framed else "label")))
        elif kind == "fact" and isinstance(item[1], Styled):
            first, *rest = item[1].lines(fact_room)
            out.append(row(label(item[0]) + "  " + first))
            out += [row(" " * (lw + 2) + line) for line in rest]
        elif kind == "fact":
            first, *rest = inline(wrap(code_spans(item[1]), fact_room))
            out.append(row(label(item[0]) + "  " + first))
            out += [row(" " * (lw + 2) + line) for line in rest]
        elif kind == "step":
            first, *rest = inline(wrap(code_spans(item), inner - 4))
            out += [row(p("  - ", "muted") + first)] + [row("    " + line) for line in rest]
    if framed:
        out += ([row()] if body else []) + [edge("╰" + "─" * (inner + 2) + "╯")]
    return out


def table_lines(header, rows, room, color=True):
    """A table as lines for a card's body: coloured capital column labels, then the rows, columns lined up. A cell is text or (text, role). The widest column gives way
    when the table is wider than `room`; a cut cell ends with a mark."""
    plain = lambda c: c[0] if isinstance(c, tuple) else str(c)           # noqa: E731
    n = len(header)
    widths = [max([len(header[i])] + [len(plain(r[i])) for r in rows if i < len(r)]) for i in range(n)]
    over = sum(widths) + 2 * (n - 1) - room
    if over > 0:
        widest = max(range(n), key=lambda i: widths[i])
        widths[widest] = max(8, widths[widest] - over)
    def cell(c, i):
        text, role = (c if isinstance(c, tuple) else (c, None))
        text = str(text)
        text = text if len(text) <= widths[i] else text[:max(1, widths[i] - 1)] + "…"
        pad = " " * (widths[i] - len(text))
        return (theme.sgr(text, role) if role and color else text) + pad
    head = "  ".join(h.upper().ljust(widths[i]) for i, h in enumerate(header)).rstrip()
    out = [theme.sgr(head, "label") if color else head]
    out += ["  ".join(cell(r[i] if i < len(r) else "", i) for i in range(n)).rstrip() for r in rows]
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


def chip(text):
    """A command or name as it is typed, as a code chip: the one way a command to copy is drawn anywhere (callers check whether colour is on)."""
    return theme.sgr(f" {text} ", "code")


def commands(run, label="Run this"):
    """The commands to copy: a label, then each command alone on its own line as a code chip (nothing before or after it), so it is selected and copied the same way everywhere."""
    cmds = [run] if isinstance(run, str) else list(run)
    return ["", "", theme.sgr(f" {label.upper()} ", "primary_chip"), ""] + [chip(c) for c in cmds]       # its own breakout, set apart from the card above; every command is a code chip, the same as one written in `backticks` in a sentence
