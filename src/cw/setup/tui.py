"""How the wizard talks to a person at a terminal.

Questions use a colored rail down the left edge, a chip naming the section, and a progress bar on top; answered questions collapse to one line each above
the active one. A protection that could lock you out gets an amber frame with Apply and Skip buttons you choose with the arrow keys. Everything is drawn in place:
when you answer, the block is erased and the next one drawn, so the screen never fills up.

Only a real terminal gets this. Anything else (a pipe, NO_COLOR, --yes, the tests) uses `PlainPrompter`, the plain text the wizard always printed.
The terminal is drawn on standard output with plain ANSI sequences; nothing here needs a library.
"""
import getpass
import re
import shutil
import sys
import textwrap

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
BLUE, BLUE_CHIP, AMBER, AMBER_CHIP = "38;5;75", "48;5;27;38;5;255;1", "38;5;178", "48;5;178;38;5;16;1"
GREEN, GREEN_BTN, RED, DIM, BOLD = "38;5;41", "48;5;28;38;5;255;1", "38;5;203", "2", "1"
WINDOW = 4                           # answered questions kept on screen above the active one; the rest are summarised
BAR = 24

SECTIONS = {"identity": "INSTALL", "server": "SERVER", "github": "GITHUB", "updates": "UPDATES", "hardware": "MACHINE", "network": "NETWORK",
            "access": "ACCESS", "alerts": "ALERTS", "hardening": "PROTECTIONS", "registry": "CLASSES", "policy": "RULES", "isolation": "JOBS", "schedule": "TIMERS", "paths": "FOLDERS"}


def strip(text):
    return ANSI.sub("", text)


def vlen(text):
    return len(strip(text))


class PlainPrompter:
    """The plain-text way: what the wizard printed before the terminal screens existed. `ask(prompt) -> text` is injected so tests can answer."""

    def __init__(self, ask=input, say=print):
        self._ask, self.say = ask, say

    def begin(self, total, title=""):
        pass

    def finish(self):
        pass

    def ask(self, q, default, options, problem=None, shown=""):
        self.say(f"\n{q['title']}")
        self.say(f"  {q['why']}")
        if options:
            self.say("  Choices: " + ", ".join(options))
        if problem:
            self.say(f"  That did not work: {problem}")
        return self._ask(f"  [{shown}] " if shown else "  ").strip()

    def answered(self, q, shown):
        pass

    def protection(self, info):
        self.say(f"\n{info['title']}\n  {info['why']}\n  Risk: {info['risk']}\n  Now: {info['now']}\n  Steps:")
        for s in info["steps"]:
            self.say("    " + s.replace("\n", "\n    "))
        return self._ask(f"\n  Apply {info['id']}? [y/N] ").strip().lower() == "y"


class TuiPrompter:
    def __init__(self, ask=input, secret=getpass.getpass, out=None, width=None, rows=None, keys=None, color=True):
        self._ask, self._secret, self.out = ask, secret, out or sys.stdout
        self._width, self._rows, self._keys, self.color = width, rows, keys, color
        self.total, self.title, self.done, self.items = 0, "", 0, []
        self.row = 0                           # lines from the top of the block being drawn to the cursor
        self.messages = []

    # ---- drawing helpers ----
    def sgr(self, text, *codes):
        return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if self.color and codes else text

    @property
    def width(self):
        cols = self._width or shutil.get_terminal_size((80, 24)).columns
        return max(50, min(cols, 100))

    @property
    def rows(self):
        return self._rows or shutil.get_terminal_size((80, 24)).lines

    def write(self, text):
        self.out.write(text)
        self.out.flush()

    def emit(self, lines):
        for line in lines:
            self.write(line + "\n")
        self.row += len(lines)

    def erase(self):
        """Erase everything drawn since the block began, leaving the cursor where it started."""
        if self.row and self.color:
            self.write(f"\x1b[{self.row}A\r\x1b[J")
        self.row = 0

    def rail(self, text="", code=BLUE):
        return self.sgr("┃ ", code) + text

    # ---- the progress bar and the answered questions ----
    def begin(self, total, title="cw-setup"):
        self.total, self.title, self.done, self.items = total, title, 0, []

    def progress(self):
        n = round(BAR * self.done / self.total) if self.total else 0
        bar = (self.sgr("━" * (n - 1) + "╸", BLUE) if n else "") + self.sgr("─" * (BAR - n), DIM)
        left = " " + self.sgr(self.title, BOLD)
        right = bar + self.sgr(f"  {min(self.done, self.total)} of {self.total}", DIM)
        return left + " " * max(2, self.width - vlen(left) - vlen(right)) + right

    def answered_lines(self, everything=False):
        shown = self.items if everything else self.items[-WINDOW:]
        out = []
        hidden = len(self.items) - len(shown)
        if hidden:
            out.append(self.rail(self.sgr(f"… {hidden} earlier answer{'s' if hidden != 1 else ''}", DIM)))
        for label, value in shown:
            room = max(10, self.width - 30)
            out.append(self.rail(self.sgr("✓ ", GREEN) + self.sgr(label.ljust(24), DIM) + (value if len(value) <= room else value[:room - 1] + "…")))
        return out

    def answered(self, q, shown):
        self.done += 1
        self.items.append((q.get("label", q["key"]), "set" if q.get("secret") else (shown or "(none)")))

    # ---- a question ----
    def ask(self, q, default, options, problem=None, shown=""):
        self.erase()
        w = self.width - 4
        section = SECTIONS.get(q["key"].partition(".")[0], "SETUP")
        head = [self.progress(), ""] if self.total else []
        head += self.answered_lines() + ([self.rail()] if self.items else [])
        head += [self.rail(self.sgr(f" {section} ", BLUE_CHIP) + self.sgr(f"  {min(self.done + 1, self.total)} of {self.total}", DIM) if self.total else self.sgr(f" {section} ", BLUE_CHIP)),
                 self.rail(), self.rail(self.sgr(q["title"], BOLD))]
        head += [self.rail(self.sgr(line, DIM)) for line in textwrap.wrap(q["why"], w)]
        if options:
            head.append(self.rail(self.sgr("Choices: " + ", ".join(options), DIM)))
        if problem:
            head.append(self.rail(self.sgr("That did not work: " + problem, RED)))
        head.append(self.rail())
        hint = ("Enter keeps: " + shown) if shown else ("needs an answer" if q.get("required") else "Enter to skip")
        tail = [self.rail(self.sgr("─" * min(44, w), DIM)), self.rail(self.sgr(hint, DIM))]
        self.emit(head + [self.rail()] + tail)             # the empty rail line is where the answer is typed; the underline and hint sit below it
        self.write("\x1b[3A\r")
        self.row -= 3
        secret = bool(q.get("secret"))
        text = (self._secret if secret else self._ask)(self.prompt(wrap=not secret))
        self.row += 1
        return (text or "").strip()

    def prompt(self, wrap):
        """The input prompt. readline needs the colour codes wrapped in \\x01 and \\x02 so it can tell how wide the prompt is; getpass must not get them."""
        o, c = ("\x01", "\x02") if wrap else ("", "")
        return f"{o}\x1b[{BLUE}m{c}┃ {o}\x1b[0m{c}{o}\x1b[{BOLD};{BLUE}m{c}❯ {o}\x1b[0m{c}"

    def finish(self):
        """Leave the progress bar and every answer on screen as a short review."""
        self.erase()
        if self.total or self.items:
            self.emit(([self.progress(), ""] if self.total else []) + self.answered_lines(everything=True))
            self.emit([""])
        self.row = 0

    # ---- a protection: the amber frame ----
    def key(self):
        if self._keys:
            return next(self._keys)
        return read_key()

    def protection(self, info):
        """Show the amber card and let the person choose Apply or Skip. Returns True to apply. Left/right choose, Enter confirms, y/n answer at once, s toggles the steps."""
        self.erase()
        choice, full = 0, False
        while True:
            self.emit(self.card(info, choice, full))
            k = self.key()
            if k == "ctrl-c":
                self.erase()
                raise KeyboardInterrupt
            self.erase()
            if k in ("y",):
                return True
            if k in ("n",):
                return False
            if k == "enter":
                return choice == 0
            if k in ("left", "right", "tab"):
                choice = 1 - choice
            elif k == "s":
                full = not full

    def card(self, info, choice, full):
        inner = self.width - 4
        a = lambda t: self.sgr(t, AMBER)                                      # noqa: E731
        title = f"Protection {info['index']} of {info['total']}" if info.get("total") else "Protection"
        tag = "could lock you out" if info.get("lockout") else "recommended"
        top = a("╭─ ") + self.sgr(title, AMBER, BOLD) + a(" " + "─" * max(1, inner - len(title) - len(tag) - 4) + " ") + self.sgr(tag, AMBER) + a(" ─╮")

        def row(text=""):
            return a("│") + " " + text + " " * max(0, inner - vlen(text) - 0) + " " + a("│")
        lines = [top, row()]
        lines.append(row(self.sgr(info["title"], BOLD)))
        for t in textwrap.wrap(info["why"], inner - 2):
            lines.append(row(self.sgr(t, DIM)))
        lines.append(row())
        for label, code, text in (("Risk  ", AMBER, info["risk"]), ("Now   ", DIM, info["now"])):
            wrapped = textwrap.wrap(text, inner - 8) or [""]
            for k, part in enumerate(wrapped):
                lines.append(row(self.sgr(label if k == 0 else "      ", code) + (part if label.startswith("Risk") else self.sgr(part, DIM))))
        lines.append(row())
        steps = info["steps"]
        for s in steps if full else steps[:4]:
            parts = s.splitlines()
            for k, part in enumerate(parts if full else parts[:3]):
                lines.append(row(self.sgr(("  $ " if k == 0 else "      ") + part.strip("\n")[:inner - 6], DIM)))
            if not full and len(parts) > 3:
                lines.append(row(self.sgr("      …", DIM)))
        if not full and len(steps) > 4:
            lines.append(row(self.sgr(f"  … and {len(steps) - 4} more steps (press s)", DIM)))
        lines.append(row())
        apply_b = self.sgr("  Apply  ", GREEN_BTN) if choice == 0 else self.sgr("  Apply  ", DIM)
        skip_b = self.sgr("  Skip  ", BOLD, "48;5;240") if choice == 1 else self.sgr("  Skip  ", DIM)
        lines.append(row("    " + apply_b + "     " + skip_b))
        lines.append(row())
        foot = "←/→ choose · Enter confirms · y/n · s shows the steps"
        lines.append(a("╰─ ") + self.sgr(foot, AMBER) + a(" " + "─" * max(1, inner - len(foot) - 2) + "─╯"))
        return lines


def read_key():
    """One key press from a real terminal: 'left', 'right', 'enter', 'tab', 'ctrl-c' or the character itself."""
    import os
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1).decode("utf-8", "replace")
        if ch == "\x1b":
            rest = os.read(fd, 2).decode("utf-8", "replace") if select.select([fd], [], [], 0.05)[0] else ""
            return {"[C": "right", "[D": "left"}.get(rest, "esc")
        return {"\r": "enter", "\n": "enter", "\t": "tab", "\x03": "ctrl-c"}.get(ch, ch.lower())
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def make(ask=input, interactive=True, stream=None):
    """The terminal screens on a real terminal with colour; None everywhere else (the wizard then prints the plain text it always did)."""
    from .. import ui
    stream = stream or sys.stdout
    if interactive and getattr(stream, "isatty", lambda: False)() and ui._color_on(stream) and sys.stdin.isatty():
        return TuiPrompter(ask=ask, out=stream)
    return None
