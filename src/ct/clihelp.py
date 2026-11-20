"""Styled `--help` for the ct commands: `prog`, `prog help`, `prog -h`, `prog help CMD`, `prog CMD -h`."""
import argparse
import difflib
import re
import sys

from . import cards
from .ui import PRIMARY, RED, say


def _placeholder(act):
    if act.choices:
        return "{" + "|".join(map(str, act.choices)) + "}"
    if act.type in (int, float):
        return "<n>"
    if any(w in act.dest for w in ("file", "dir", "spec")):
        return "<path>"
    return "<value>"


def _subcommands_of(parser):
    """[(name, one line)] alphabetically, for a command that has commands of its own (ct-run offload log ...)."""
    out = []
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction):
            helps = {c.dest: c.help or "" for c in act._choices_actions}
            out += [(n, helps.get(n, "")) for n in sorted(act.choices)]
    return out


def _options_of(parser):
    rows = []
    for act in parser._actions:
        if act.dest == "help" or isinstance(act, argparse._SubParsersAction):
            continue
        if act.option_strings:
            name = ", ".join(act.option_strings) + (f" {_placeholder(act)}" if act.nargs != 0 else "")
        else:
            name = "-- <command>" if act.nargs == "..." else f"<{(act.metavar or act.dest).lower()}>"
        rows.append((name, (act.help or "").replace("%(default)s", str(act.default))))
    return rows


def _usage_of(prog, cmd, parser):
    """`ct-run profile [options] -- <command>`: short, instead of argparse's one very long line."""
    opts = [a for a in parser._actions if a.option_strings and a.dest != "help"]
    pos = [a for a in parser._actions if not a.option_strings]
    parts = [prog, cmd] + (["[options]"] if opts else [])
    for a in pos:
        parts.append("-- <command>" if a.nargs == "..." else (f"[{a.dest}]" if a.nargs == "?" else f"<{(a.metavar or a.dest).lower()}>"))
    return " ".join(parts)


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
        say("No such command", ("Command", want), tag="ERROR", color=RED, stream=out, run_label="See the commands", run=f"{prog} help")
        return True
    cmds = lambda rows: [(cards.Cmd(c), d) for c, d in rows]          # noqa: E731  (a command is shown as typed, in the command colour)
    if want:
        one, exs = details[want]
        rows = [one]
        parser = sub.choices.get(want) if sub is not None else None
        if parser is not None:
            rows += [cards.Heading("Usage"), cards.Block([_usage_of(prog, want, parser)])]
            subs = _subcommands_of(parser)
            if subs:
                rows += [cards.Heading("Commands")] + cmds([(f"{prog} {want} {n}", d) for n, d in subs])
            opts = _options_of(parser)
            if opts:
                rows += [cards.Heading("Options")] + cmds(opts)
        if exs:
            rows += [cards.Heading("Examples")] + [(cards.Cmd(cmd), desc) for desc, cmd in exs]
        say(f"{prog} {want}", *rows, tag="HELP", color=PRIMARY, stream=out)
        return True
    rows = [cards.Heading("Usage"), cards.Block([f"{prog} <command> [options]"])]
    if groups:                                  # [(title, [command, ...])]: the commands under headings, in the order given
        for title, names in groups:
            rows += [cards.Heading(title)] + cmds([(c, details[c][0]) for c in sorted(names)])                   # commands alphabetically within each group
    else:
        rows += [cards.Heading("Commands")] + cmds(sorted((c, d[0]) for c, d in details.items()))                    # alphabetically by command name
    if examples:
        rows += [cards.Heading("Examples")] + [(cards.Cmd(cmd), desc) for desc, cmd in examples]
    rows += [cards.Heading("More")] + cmds([(f"{prog} help <command>", "every option of one command, with examples")] + list(see or []))
    say(prog, summary, "\n", *rows, tag="HELP", color=PRIMARY, stream=out)
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

    def parse_args(self, args=None, namespace=None):
        """Leftover words are reported against the sub-command they followed (`ct-run use --bogus` is about `ct-run use`), not against the top-level tool."""
        ns, extra = self.parse_known_args(args, namespace)
        if extra:
            chosen = getattr(ns, "cmd", None)
            for action in self._actions:
                if isinstance(action, argparse._SubParsersAction) and chosen in action.choices:
                    raise UsageError(action.choices[chosen], f"unrecognized arguments: {' '.join(extra)}")
            raise UsageError(self, f"unrecognized arguments: {' '.join(extra)}")
        return ns


def _words(parser, prog):
    """'ct-admin action' for the parser of `ct-admin action` (the root parser is created with prog=, so this is exact)."""
    return parser.prog if parser.prog.startswith(prog) else prog


def usage_problem(err, prog, known=()):
    """(title, why, steps, example, facts) for a UsageError: a fixed title, what was wrong in plain words, the real choices, the closest one as a command to copy, and what the person typed as labelled rows."""
    parser, msg = err.parser, err.message
    here = _words(parser, prog)
    sub_cmd = here[len(prog):].strip()
    usage = _usage_of(prog, sub_cmd, parser)
    m = re.search(r"invalid choice: '([^']*)'.*?\(choose from (.*)\)", msg)
    if m:
        bad, choices = m.group(1), sorted(c.strip().strip("'") for c in m.group(2).split(","))                  # listed alphabetically, like the help
        close = difflib.get_close_matches(bad, choices, n=1, cutoff=0.7)                    # only a real near miss is offered as "did you mean"
        top = not sub_cmd
        arg = re.match(r"argument ([A-Za-z_-]+):", msg)
        what = "command" if top else (arg.group(1) if arg and arg.group(1) not in ("cmd",) else "option")             # a positional's own name (verb), else 'option'
        steps = [f"Choose one of: {', '.join(choices)}"] + ([f"Usage: {usage}", f"All of its options: {prog} help {sub_cmd}"] if not top else [f"Every command, with examples: {prog} help"])         # a syntax error always carries the command's own syntax and where its full help is
        return ("That is not a command" if top or what == "command" else "That is not one of the choices", f"The choices are: {', '.join(choices)}.", steps,
                f"{here} {close[0]}" if close and not top else f"{prog} {close[0]}" if close else None, [(what.capitalize(), bad), ("In", here)])
    m = re.search(r"required: (.*)", msg)
    if m:
        return ("A required argument is missing", "Something it must be told is missing.", [f"Usage: {usage}", f"All of its options: {prog} help {sub_cmd}"], None, [("Command", here), ("Needs", m.group(1))])
    m = re.search(r"unrecognized arguments: (.*)", msg)
    if m:
        return ("That command does not know that argument", "That option or word is not part of this command.", [f"Usage: {usage}", f"All of its options: {prog} help {sub_cmd}"], None, [("Command", here), ("Argument", m.group(1))])
    return ("A command was given something it cannot use", re.sub(r"^argument [^:]+: ", "", msg), [f"Usage: {usage}"], None, [("Command", here)])
