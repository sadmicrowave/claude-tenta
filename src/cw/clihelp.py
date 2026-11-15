"""Styled `--help` for the cw commands: `prog`, `prog help`, `prog -h`, `prog help CMD`, `prog CMD -h`."""
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
