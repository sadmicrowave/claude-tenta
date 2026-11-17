"""Styled `--help` for the cw commands: `prog`, `prog help`, `prog -h`, `prog help CMD`, `prog CMD -h`."""
import argparse
import difflib
import re
import sys

from .ui import BOLD, CYAN, DIM, GREEN, RED, paint, say


def _placeholder(act):
    if act.choices:
        return "{" + "|".join(map(str, act.choices)) + "}"
    if act.type in (int, float):
        return "<n>"
    if any(w in act.dest for w in ("file", "dir", "spec")):
        return "<path>"
    return "<value>"


def _options_of(parser):
    rows = []
    for act in parser._actions:
        if act.dest == "help":
            continue
        if act.option_strings:
            name = ", ".join(act.option_strings) + (f" {_placeholder(act)}" if act.nargs != 0 else "")
        else:
            name = "-- <command>" if act.nargs == "..." else f"<{(act.metavar or act.dest).lower()}>"
        rows.append((name, (act.help or "").replace("%(default)s", str(act.default))))
    return rows


def _usage_of(prog, cmd, parser):
    """`cw-run profile [options] -- <command>`: short, instead of argparse's one very long line."""
    opts = [a for a in parser._actions if a.option_strings and a.dest != "help"]
    pos = [a for a in parser._actions if not a.option_strings]
    parts = [prog, cmd] + (["[options]"] if opts else [])
    for a in pos:
        parts.append("-- <command>" if a.nargs == "..." else (f"[{a.dest}]" if a.nargs == "?" else f"<{(a.metavar or a.dest).lower()}>"))
    return " ".join(parts)


def _table(rows, stream, bold_first=True):
    """Two aligned columns; the right column wraps to the terminal and keeps its indent."""
    if not rows:
        return
    import shutil
    import textwrap
    w = max(len(r[0]) for r in rows)
    room = max(shutil.get_terminal_size((100, 24)).columns - w - 5, 30)
    for left, right in rows:
        lines = textwrap.wrap(right, room) or [""]
        print(f"  {paint(f'{left:<{w}}', *((BOLD,) if bold_first else ()), stream=stream)}  {lines[0]}")
        for extra in lines[1:]:
            print(" " * (w + 4) + extra)


def _examples(examples, stream):
    if not examples:
        return
    print("\n" + paint("EXAMPLES", BOLD, CYAN, stream=stream))
    for desc, cmd in examples:
        print(f"\n  {paint(desc, DIM, stream=stream)}\n  {paint(cmd, BOLD, GREEN, stream=stream)}")


def show_cli_help(argv, prog, summary, sub, details, examples, see=None, groups=None):
    """Styled help for `prog`: `prog`, `prog help`, `prog -h`, `prog help CMD`, `prog CMD -h`.
    `details` maps each command to (one line, [(what it shows, example command), ...]). Returns True when it printed help."""
    args = argv[1:]
    if not args or args[0] in ("help", "-h", "--help"):
        want = args[1] if len(args) > 1 and args[0] == "help" else ""
    elif len(args) > 1 and args[1] in ("-h", "--help") and args[0] in details:
        want = args[0]
    else:
        return False
    out = sys.stdout
    if want and want not in details:
        say(f"No command called {want}", tag="ERROR", color=RED, stream=out, run_label="See the commands", run=f"{prog} help")
        return True
    if want:
        one, exs = details[want]
        print(f"\n{paint(prog + ' ' + want, BOLD, GREEN, stream=out)}  {paint(one, BOLD, stream=out)}")
        parser = sub.choices.get(want) if sub is not None else None
        if parser is not None:
            print("\n" + paint("USAGE", BOLD, CYAN, stream=out) + "\n  " + _usage_of(prog, want, parser))
            opts = _options_of(parser)
            if opts:
                print("\n" + paint("OPTIONS", BOLD, CYAN, stream=out))
                _table(opts, out)
        _examples(exs, out)
        print()
        return True
    print(f"\n{paint(prog, BOLD, GREEN, stream=out)}  {paint(summary, BOLD, stream=out)}")
    print("\n" + paint("USAGE", BOLD, CYAN, stream=out) + f"\n  {prog} <command> [options]")
    if groups:                                  # [(title, [command, ...])]: the commands under headings, in the order given
        for title, names in groups:
            print("\n" + paint(title.upper(), BOLD, CYAN, stream=out))
            _table([(c, details[c][0]) for c in names], out)
    else:
        print("\n" + paint("COMMANDS", BOLD, CYAN, stream=out))
        _table([(c, d[0]) for c, d in details.items()], out)
    _examples(examples, out)
    print("\n" + paint("MORE", BOLD, CYAN, stream=out))
    _table([(f"{prog} help <command>", "every option of one command, with examples")] + list(see or []), out, bold_first=False)
    print()
    return True


class UsageError(Exception):
    """A command line argparse could not accept. Raised instead of letting argparse print its own usage text, so the person sees one message in our style."""

    def __init__(self, parser, message):
        super().__init__(message)
        self.parser, self.message = parser, message


class Parser(argparse.ArgumentParser):
    """argparse that never prints or exits: a bad command line becomes a UsageError for `usage_problem` to explain."""

    def error(self, message):
        raise UsageError(self, message)


def _words(parser, prog):
    """'cw-admin action' for the parser of `cw-admin action` (the root parser is created with prog=, so this is exact)."""
    return parser.prog if parser.prog.startswith(prog) else prog


def usage_problem(err, prog, known=()):
    """(title, why, steps, example) for a UsageError: what was wrong in plain words, the real choices, and the closest one as a command to copy."""
    parser, msg = err.parser, err.message
    here = _words(parser, prog)
    sub_cmd = here[len(prog):].strip()
    usage = _usage_of(prog, sub_cmd, parser)
    m = re.search(r"invalid choice: '([^']*)'.*?\(choose from (.*)\)", msg)
    if m:
        bad, choices = m.group(1), [c.strip().strip("'") for c in m.group(2).split(",")]
        close = difflib.get_close_matches(bad, choices, n=1, cutoff=0.5)
        top = not sub_cmd
        what = "command" if top else "option"
        steps = [f"Choose one of: {', '.join(choices)}"] + ([] if not top else [f"Every command, with examples: {prog} help"])
        return (f"{bad!r} is not a {what} of {here}", f"{here} accepts: {', '.join(choices)}.", steps,
                f"{here} {close[0]}" if close and not top else f"{prog} {close[0]}" if close else (None if top else usage))
    m = re.search(r"required: (.*)", msg)
    if m:
        return (f"{here} needs {m.group(1)}", "Something it must be told is missing.", [f"Usage: {usage}", f"All of its options: {prog} help {sub_cmd}"], None)
    m = re.search(r"unrecognized arguments: (.*)", msg)
    if m:
        return (f"{here} does not know {m.group(1)}", "That option or word is not part of this command.", [f"Usage: {usage}", f"All of its options: {prog} help {sub_cmd}"], None)
    return (f"{here} was given something it cannot use", re.sub(r"^argument [^:]+: ", "", msg), [f"Usage: {usage}"], None)
