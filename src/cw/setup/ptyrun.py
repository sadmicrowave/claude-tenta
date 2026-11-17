"""Run a command that wants a real terminal (a browser sign-in with prompts) on the person's own terminal, and keep a copy of what it printed.

`claude setup-token` prints a long token for the person to copy. Running it here lets the installer read the token itself instead of asking for it to be pasted
back. Everything the program prints still appears on the person's terminal, and their keys still reach it.
"""
import fcntl
import os
import pty
import re
import select
import shutil
import struct
import sys
import termios
import tty

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>]")
TOKEN_LINE = re.compile(r"\s*(sk-ant-[A-Za-z0-9_-]+)\s*")
WORD_LINE = re.compile(r"\s*([A-Za-z0-9_-]+)\s*")


def extract_token(text):
    """The sign-in token in what a program printed, or None. The token is one long word that the terminal may have wrapped over several lines, so the lines
    that follow it are joined while each is a single word; the blank line after it ends it. When it was printed more than once (a redrawn screen) the last one wins."""
    lines = ANSI.sub("", text).replace("\r", "").split("\n")
    found = []
    for i, line in enumerate(lines):
        m = TOKEN_LINE.fullmatch(line)
        if not m:
            continue
        token = m.group(1)
        for following in lines[i + 1:]:
            w = WORD_LINE.fullmatch(following)
            if not w:
                break
            token += w.group(1)
        found.append(token)
    return found[-1] if found else None


def keep_newline_translation(fd):
    """Make "\\n" start a new line at the left edge again. `ssh -t` and raw mode switch the terminal's output processing off, and then anything this program prints
    meanwhile (a bare newline) only moves down, so the next line starts wherever the last one ended: a staircase. Input stays raw."""
    try:
        attrs = termios.tcgetattr(fd)
        attrs[1] |= termios.OPOST | termios.ONLCR
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except (termios.error, OSError, ValueError):
        pass


def run_capture(argv, env=None, out=None):
    """Returns (exit code, everything printed). The child gets a terminal of its own with the size of the person's."""
    out = out or sys.stdout.buffer
    stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    size = shutil.get_terminal_size((100, 30))
    pid, master = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", size.lines, size.columns, 0, 0))
            if env:
                os.environ.update(env)
            os.execvp(argv[0], list(argv))
        except OSError:
            pass
        os._exit(127)
    old = termios.tcgetattr(stdin_fd) if stdin_fd is not None else None
    if old is not None:
        tty.setraw(stdin_fd)
        keep_newline_translation(stdin_fd)
    captured = bytearray()
    try:
        while True:
            fds = [master] + ([stdin_fd] if stdin_fd is not None else [])
            ready, _, _ = select.select(fds, [], [], 0.5)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                captured += data
                out.write(data)
                out.flush()
            if stdin_fd is not None and stdin_fd in ready:
                data = os.read(stdin_fd, 4096)
                if data:
                    os.write(master, data)
    finally:
        if old is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old)
        os.close(master)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), captured.decode("utf-8", "replace")
