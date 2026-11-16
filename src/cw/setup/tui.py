"""How the wizard talks to a person at a terminal.

Questions use a colored rail down the left edge, a chip naming the section, and a progress bar on top; answered questions collapse to one line each above
the active one. The keys are always listed under the input: Enter or the down arrow goes on, the up arrow goes back to the previous question (with your
earlier answer ready to change), Tab completes a choice, Ctrl-C stops. A protection that could lock you out gets an amber frame with Apply and Skip buttons.
Everything is drawn in place: when you move on, the block is erased and the next one drawn, so the screen never fills up.

Typing is handled here, not by the terminal's line editor, so a hidden answer shows dots (you can see it was accepted), pasting works even when the terminal wraps
the paste in escape sequences, and the arrow keys can mean "previous question".

Only a real terminal gets this. Anything else (a pipe, NO_COLOR, --yes, the tests) gets the plain text the wizard always printed.
The terminal is drawn on standard output with plain ANSI sequences; nothing here needs a library.
"""
import re
import shutil
import sys
import textwrap

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]")
BLUE, BLUE_CHIP, AMBER, AMBER_CHIP = "38;5;75", "48;5;27;38;5;255;1", "38;5;178", "48;5;178;38;5;16;1"
GREEN, GREEN_BTN, RED, DIM, BOLD = "38;5;41", "48;5;28;38;5;255;1", "38;5;203", "2", "1"
WINDOW = 4                           # answered questions kept on screen above the active one; the rest are summarised
BAR = 24
DOT = "•"
BRAND = "@cw-ballast"

SECTIONS = {"identity": "INSTALL", "server": "SERVER", "github": "GITHUB", "updates": "UPDATES", "hardware": "MACHINE", "network": "NETWORK",
            "access": "ACCESS", "alerts": "ALERTS", "hardening": "PROTECTIONS", "registry": "CLASSES", "policy": "RULES", "isolation": "JOBS", "schedule": "TIMERS", "paths": "FOLDERS"}


def strip(text):
    return ANSI.sub("", text)


def vlen(text):
    return len(strip(text))


def clean_paste(text):
    """Pasted text with the terminal's own wrapping (bracketed-paste markers and any other escape sequence), line breaks and control characters removed."""
    return re.sub(r"[\x00-\x1f\x7f]", "", ANSI.sub("", text)).strip()


class Editor:
    """One line of text being edited. Pure logic: feed it key events, read `.text`. Events are the names Keyboard produces."""

    def __init__(self, text="", secret=False, options=None):
        self.buf, self.secret, self.options = list(text), secret, list(options or [])
        self.pos = len(self.buf)

    @property
    def text(self):
        return "".join(self.buf)

    def shown(self):
        return DOT * len(self.buf) if self.secret else self.text

    def insert(self, s):
        self.buf[self.pos:self.pos] = list(s)
        self.pos += len(s)

    def complete(self):
        word = self.text.strip().lower()
        hits = [o for o in self.options if o.lower().startswith(word)] if word else []
        if len(hits) == 1:
            self.buf, self.pos = list(hits[0]), len(hits[0])
        elif hits:
            common = hits[0]
            for h in hits[1:]:
                while not h.lower().startswith(common.lower()):
                    common = common[:-1]
            self.buf, self.pos = list(common), len(common)

    def feed(self, ev):
        """None to keep editing, or ('answer', text) / ('back',)."""
        if isinstance(ev, tuple) and ev[0] == "paste":
            self.insert(clean_paste(ev[1]))
        elif ev in ("enter", "down"):
            return ("answer", self.text)
        elif ev in ("up", "shift-tab"):
            return ("back", self.text)               # what was typed so far goes with it, so it is still there when the person comes back
        elif ev == "ctrl-c":
            raise KeyboardInterrupt
        elif ev == "backspace":
            if self.pos:
                del self.buf[self.pos - 1]
                self.pos -= 1
        elif ev == "delete":
            if self.pos < len(self.buf):
                del self.buf[self.pos]
        elif ev == "left":
            self.pos = max(0, self.pos - 1)
        elif ev == "right":
            self.pos = min(len(self.buf), self.pos + 1)
        elif ev in ("home", "ctrl-a"):
            self.pos = 0
        elif ev in ("end", "ctrl-e"):
            self.pos = len(self.buf)
        elif ev == "ctrl-u":
            self.buf, self.pos = [], 0
        elif ev == "ctrl-w":
            end = self.pos
            while self.pos and self.buf[self.pos - 1] == " ":
                self.pos -= 1
            while self.pos and self.buf[self.pos - 1] != " ":
                self.pos -= 1
            del self.buf[self.pos:end]
        elif ev == "tab":
            self.complete()
        elif isinstance(ev, str) and len(ev) == 1 and ev.isprintable():
            self.insert(ev)
        return None


class Keyboard:
    """Reads key events from a real terminal: single characters, and the names 'enter', 'backspace', 'delete', 'tab', 'shift-tab', 'up', 'down', 'left', 'right',
    'home', 'end', 'esc', 'ctrl-a/e/u/w/c', and ('paste', text) for a bracketed paste. Holds the terminal in cbreak mode (no echo, no line buffering, Ctrl-C still works)."""

    def __init__(self, fd=None):
        self.fd, self.old = fd, None

    def __enter__(self):
        import termios
        import tty
        self.fd = self.fd if self.fd is not None else sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        import termios
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def flush(self):
        import termios
        termios.tcflush(self.fd, termios.TCIFLUSH)

    def _wait(self, t):
        import select
        return bool(select.select([self.fd], [], [], t)[0])

    def _byte(self):
        import os
        b = os.read(self.fd, 1)
        if not b:
            raise EOFError("the terminal was closed")
        return b

    def _char(self, first):
        n = 0 if first[0] < 0xC0 else 1 if first[0] < 0xE0 else 2 if first[0] < 0xF0 else 3
        rest = b"".join(self._byte() for _ in range(n))
        return (first + rest).decode("utf-8", "replace")

    def read(self):
        b = self._byte()
        if b == b"\x1b":
            return self._escape()
        simple = {b"\r": "enter", b"\n": "enter", b"\t": "tab", b"\x7f": "backspace", b"\x08": "backspace", b"\x03": "ctrl-c", b"\x01": "ctrl-a", b"\x05": "ctrl-e",
                  b"\x15": "ctrl-u", b"\x17": "ctrl-w"}
        if b in simple:
            return simple[b]
        return self._char(b)

    def _escape(self):
        if not self._wait(0.05):
            return "esc"
        b = self._byte()
        if b == b"O":
            return {b"A": "up", b"B": "down", b"C": "right", b"D": "left", b"H": "home", b"F": "end"}.get(self._byte(), "esc")
        if b != b"[":
            return "esc"
        params = b""
        while True:
            c = self._byte()
            if 0x40 <= c[0] <= 0x7E:
                break
            params += c
        key = params.decode() + c.decode()
        if key == "200~":
            return ("paste", self._paste())
        return {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "shift-tab", "3~": "delete", "1~": "home", "7~": "home", "4~": "end", "8~": "end"}.get(key, "esc")

    def _paste(self):
        data = b""
        while not data.endswith(b"\x1b[201~") and self._wait(2.0):
            data += self._byte()
        return data[:-6].decode("utf-8", "replace")


class TuiPrompter:
    def __init__(self, out=None, width=None, rows=None, keys=None, color=True):
        self.out, self._width, self._rows, self._keys, self.color = out or sys.stdout, width, rows, keys, color
        self.title, self.review, self.rawtext, self.total_hint = "setup", None, {}, 0
        self.row = 0                           # lines from the top of the block being drawn to the cursor
        self._kb = None

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

    def heading(self, text, step=None):
        """A phase heading in the wizard's style, printed once into the scrollback: a blue rule with the title, optionally the step (`2 of 4`)."""
        tag = f" {step} " if step else ""
        rule = "━" * max(2, self.width - vlen(text) - vlen(tag) - 6)
        self.write("\n" + self.sgr("━━ ", BLUE) + self.sgr(text, BLUE, BOLD) + (self.sgr(" ·" + tag, DIM) if tag else " ") + self.sgr(rule, DIM) + "\n")

    def sub(self, text):
        self.write("\n" + self.sgr("  ▸ ", BLUE) + self.sgr(text, BOLD) + "\n")

    def box(self, title, tag, lines, code=BLUE):
        """A static framed card in the scrollback (the protection card's sibling, for things that are not a choice)."""
        inner = self.width - 4
        a = lambda t: self.sgr(t, code)                                       # noqa: E731

        def row(text=""):
            return a("│") + " " + text + " " * max(0, inner - vlen(text)) + " " + a("│")
        out = [a("╭─ ") + self.sgr(title, code, BOLD) + a(" " + "─" * max(1, inner - vlen(title) - vlen(tag) - 4) + " ") + self.sgr(tag, code) + a(" ─╮"), row()]
        for line in lines:
            for t in (textwrap.wrap(line, inner - 2) or [""]):
                out.append(row(t))
        out += [row(), a("╰" + "─" * (inner + 2) + "╯")]
        self.write("\n".join(out) + "\n")

    def sudo_card(self, user, host):
        """Shown just before the server's password prompt, so it is clear what is being asked for and why nothing appears while typing."""
        self.box("Administrator password", "asked by the server, not by this tool",
                 [self.sgr(f"Type the sudo password of {user} on {host}.", BOLD),
                  self.sgr("Nothing is shown as you type, not even dots. That is normal; press Enter when done.", DIM),
                  self.sgr("Only the server sees it. It is used once, to install the worker and its protections.", DIM)], code=AMBER)

    def sudo_prompt(self, user, host):
        return self.sgr(f"  ❯ password for {user}@{host}: ", AMBER, BOLD)

    def banner(self, subtitle):
        """The splash header, drawn once at the start (not part of the redrawn block)."""
        from . import brand
        cols = self._width or shutil.get_terminal_size((80, 24)).columns
        self.write("\n".join(brand.banner(cols, brand.spaced(subtitle), color=self.color)) + "\n")

    def begin(self, total=0, title="setup"):
        self.title = title

    # ---- keys ----
    def event(self):
        if self._keys is not None:
            return next(self._keys)
        return self._kb.read()

    def session(self):
        """For the whole run: keep the terminal from echoing keys that are typed while nothing is waiting for them (a connection check, an install step), which would
        scribble over the screen. A no-op when keys are injected or there is no terminal."""
        outer = self

        class Quiet:
            def __enter__(self):
                self.fd = None
                if outer._keys is None:
                    import termios
                    try:
                        self.fd = sys.stdin.fileno()
                        self.old = termios.tcgetattr(self.fd)
                        new = termios.tcgetattr(self.fd)
                        new[3] &= ~termios.ECHO
                        termios.tcsetattr(self.fd, termios.TCSADRAIN, new)
                    except (termios.error, OSError, ValueError):
                        self.fd = None
                return self

            def __exit__(self, *exc):
                if self.fd is not None:
                    import termios
                    termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
        return Quiet()

    def listening(self):
        """Hold the terminal for key events (and ask it to mark pastes) while a screen is up. A no-op when keys are injected."""
        outer = self

        class Ctx:
            def __enter__(self):
                if outer._keys is None:
                    outer._kb = Keyboard().__enter__()
                    outer._kb.flush()                       # anything typed before the screen appeared is ignored, so an accidental Enter never answers it
                    outer.write("\x1b[?2004h")
                return self

            def __exit__(self, *exc):
                if outer._keys is None and outer._kb is not None:
                    outer.write("\x1b[?2004l")
                    outer._kb.__exit__(*exc)
                    outer._kb = None
        return Ctx()

    # ---- the progress bar and the answered questions ----
    def progress(self, pos, total):
        n = round(BAR * pos / total) if total else 0
        bar = (self.sgr("━" * (n - 1) + "╸", BLUE) if n else "") + self.sgr("─" * (BAR - n), DIM)
        left = " " + self.sgr(BRAND, BLUE, BOLD) + self.sgr("  " + self.title, DIM)
        right = bar + self.sgr(f"  {min(pos, total)} of {total} done", DIM)
        return left + " " * max(2, self.width - vlen(left) - vlen(right)) + right

    def answered_lines(self, items, everything=False, limit=None):
        shown = items if everything else items[-WINDOW:]
        if limit is not None and len(shown) > limit:
            shown = shown[-limit:]
        out = []
        hidden = len(items) - len(shown)
        if hidden:
            out.append(self.rail(self.sgr(f"… {hidden} earlier answer{'s' if hidden != 1 else ''}", DIM)))
        for label, value in shown:
            room = max(10, self.width - 30)
            out.append(self.rail(self.sgr("✓ ", GREEN) + self.sgr(label.ljust(24), DIM) + (value if len(value) <= room else value[:room - 1] + "…")))
        return out

    def labelled(self, label, code, text, hang=10):
        """A row with a coloured label on the left and wrapped text beside it: RISK, CHOICES, EXAMPLES."""
        lines = textwrap.wrap(text, max(20, self.width - 4 - hang)) or [""]
        return [self.rail(self.sgr(label.ljust(hang), code, BOLD) + lines[0])] + [self.rail(" " * hang + line) for line in lines[1:]]

    def controls(self, can_back, options):
        parts = ([("↑", "back")] if can_back else []) + [("Enter", "next")] + ([("Tab", "completes")] if options else []) + [("Ctrl-C", "stop")]
        return self.rail(self.sgr(" · ", DIM).join(self.sgr(k, BLUE, BOLD) + self.sgr(" " + v, DIM) for k, v in parts))

    def choice_lines(self, q, current):
        rows, width = q["choices"], self.width - 4
        name_w = max(len(v) for v, _ in rows)
        out = []
        for n, (value, desc) in enumerate(rows, 1):
            on = value == current
            lead = "  " + self.sgr("▸ " if on else "  ", GREEN) + self.sgr(f"{n} ", DIM) + (self.sgr(value.ljust(name_w), BOLD) if on else value.ljust(name_w)) + "  "
            lines = textwrap.wrap(desc, max(20, width - name_w - 10)) or [""]
            out.append(self.rail(lead + self.sgr(lines[0], DIM)))
            out += [self.rail(" " * (name_w + 8) + self.sgr(line, DIM)) for line in lines[1:]]
        return out

    # ---- a question ----
    def ask(self, q, default, options, problem=None, shown="", view=None):
        """Show one question and read its answer. Returns ('answer', text) or ('back', text typed so far). `view` carries the answered items, the position, and the text to prefill."""
        view = view or {}
        items, pos, total = view.get("items", []), view.get("pos", 0), view.get("total", 1)
        can_back, prefill = view.get("can_back", False), view.get("prefill") or ""
        self.erase()
        w = self.width - 4
        section = SECTIONS.get(q["key"].partition(".")[0], "SETUP")
        head = [self.progress(pos, total), ""]
        head += self.answered_lines(items) + ([self.rail()] if items else [])
        head += [self.rail(self.sgr(f" {section} ", BLUE_CHIP) + self.sgr(f"  {pos + 1} of {total}", DIM)), self.rail(), self.rail(self.sgr(q["title"], BOLD))]
        head += [self.rail(self.sgr(line, DIM)) for line in textwrap.wrap(q["why"], w)]
        if q.get("risk"):
            head += [self.rail()] + self.labelled("RISK", AMBER, q["risk"])
        if q.get("choices"):
            head += [self.rail()] + self.labelled("CHOICES", BLUE, "type a name or its number, or press Tab") + self.choice_lines(q, (prefill or shown or "").strip())
        elif q.get("examples"):
            head += [self.rail()] + self.labelled("EXAMPLES", BLUE, "  ·  ".join(q["examples"]))
        if problem:
            head += [self.rail(), self.rail(self.sgr("That did not work: " + problem, RED))]
        head.append(self.rail())
        hint = ("Enter keeps: " + shown) if shown else ("this one needs an answer" if q.get("required") else "Enter leaves it empty")
        tail = [self.rail(self.sgr("─" * min(44, w), DIM)), self.rail(self.sgr(hint, DIM)), self.controls(can_back, options), ]
        self.emit(head + [self.rail()] + tail)             # the empty rail line is where the answer is typed; the underline, hint and keys sit below it
        self.write(f"\x1b[{len(tail) + 1}A\r")
        self.row -= len(tail) + 1
        editor = Editor(prefill, secret=bool(q.get("secret")), options=options)
        with self.listening():
            self.draw_input(editor, shown if not q.get("secret") else "")
            while True:
                result = editor.feed(self.event())
                self.draw_input(editor, shown if not q.get("secret") else "")
                if result is None:
                    continue
                if result[0] == "back" and not can_back:
                    continue
                return result

    def draw_input(self, editor, placeholder):
        avail = self.width - 5
        text, pos = editor.shown(), editor.pos
        start = max(0, pos - avail + 1) if len(text) > avail else 0
        window = text[start:start + avail]
        body = self.sgr(window, BOLD) if window else self.sgr(placeholder[:avail], DIM)
        line = self.sgr("┃ ", BLUE) + self.sgr("❯ ", BLUE, BOLD) + body
        col = 4 + (pos - start)
        self.write("\r\x1b[2K" + line + "\r" + (f"\x1b[{col}C" if col else ""))

    def confirm(self, items, total, message, hidden=0):
        """The last screen before anything happens: every answer, then Enter to go on or the up arrow to change one. If `hidden` advanced settings are using their defaults,
        A reviews them. Returns 'go', 'back' or 'advanced'."""
        self.erase()
        room = max(3, self.rows - 11)                      # the block must fit on the screen, or it could not be redrawn
        pairs = [("Enter", "start"), ("↑", "change an answer")] + ([("A", f"review the {hidden} advanced settings")] if hidden else []) + [("Ctrl-C", "stop")]
        keys = self.rail(self.sgr(" · ", DIM).join(self.sgr(k, BLUE, BOLD) + self.sgr(" " + v, DIM) for k, v in pairs))
        note = [self.rail(self.sgr(f"+ {hidden} advanced settings use their defaults", DIM))] if hidden else []
        lines = [self.progress(total, total), ""] + self.answered_lines(items, everything=True, limit=room) + note + [self.rail(), self.rail(self.sgr(message, BOLD)), self.rail(), keys]
        with self.listening():
            self.emit(lines)
            while True:
                k = self.event()
                if k == "ctrl-c":
                    self.erase()
                    raise KeyboardInterrupt
                if k == "enter":
                    self.erase()
                    return "go"
                if k in ("up", "shift-tab"):
                    self.erase()
                    return "back"
                if hidden and isinstance(k, str) and k.lower() == "a":
                    self.erase()
                    return "advanced"

    def finish(self):
        """Leave the progress bar and every answer on screen as a short review (the last set of answers a question flow produced)."""
        self.erase()
        if self.review:
            items, total = self.review
            self.emit([self.progress(total, total), ""] + self.answered_lines(items, everything=True) + [""])
        self.review, self.row = None, 0

    # ---- a protection: the amber frame ----
    def protection(self, info):
        """Show the amber card and let the person choose Apply or Skip. Returns True to apply. Left/right choose, Enter confirms, y/n answer at once, s toggles the steps."""
        self.erase()
        choice, full = 0, False
        with self.listening():
            while True:
                self.emit(self.card(info, choice, full))
                k = self.event()
                self.erase()
                if k == "ctrl-c":
                    raise KeyboardInterrupt
                key = k.lower() if isinstance(k, str) else k
                if key == "y":
                    return True
                if key == "n":
                    return False
                if key == "enter":
                    return choice == 0
                if key in ("left", "right", "tab", "shift-tab"):
                    choice = 1 - choice
                elif key == "s":
                    full = not full

    def card(self, info, choice, full):
        inner = self.width - 4
        a = lambda t: self.sgr(t, AMBER)                                      # noqa: E731
        title = f"Protection {info['index']} of {info['total']}" if info.get("total") else "Protection"
        tag = "could lock you out" if info.get("lockout") else "recommended"
        top = a("╭─ ") + self.sgr(title, AMBER, BOLD) + a(" " + "─" * max(1, inner - len(title) - len(tag) - 4) + " ") + self.sgr(tag, AMBER) + a(" ─╮")

        def row(text=""):
            return a("│") + " " + text + " " * max(0, inner - vlen(text)) + " " + a("│")
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


def make(interactive=True, stream=None):
    """The terminal screens on a real terminal with colour; None everywhere else (the wizard then prints the plain text it always did)."""
    from .. import ui
    stream = stream or sys.stdout
    if interactive and getattr(stream, "isatty", lambda: False)() and ui._color_on(stream) and sys.stdin.isatty():
        return TuiPrompter(out=stream)
    return None
