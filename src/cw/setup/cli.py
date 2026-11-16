"""cw-setup client | server | audit

  client    set up your own computer: your config, SSH key login, the two private repositories, the registry
  server    turn a clean Ubuntu or Debian machine into a worker (run as root on it)
  audit     read-only: check a server against its configuration and report anything that drifted

Options:  --dry-run (show everything, change nothing; the default for server unless --apply)   --yes (take every default, ask nothing)
          --answers FILE (a TOML file of answers)   --set section.key=value (repeatable)
"""
import argparse
import sys
import tomllib
from pathlib import Path

from .. import config
from .. import ui as _ui
from ..ui import BOLD, GREEN, RED, YELLOW, paint
from . import audit as audit_mod
from . import questions
from .client import ClientSetup
from .host import Host
from .server import ServerSetup, SetupError

ROOT = Path(__file__).resolve().parents[3]


def emit(*a, **k):
    k.setdefault("stream", sys.stderr)
    _ui.say(*a, **k)


def load_answers(path, sets):
    """A TOML answers file (nested tables or dotted keys) plus --set pairs -> {'section.key': value}."""
    given = {}
    if path:
        try:
            data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise questions.WizardError([f"cannot read the answers file {path}: {e}"])
        for section, vals in data.items():
            if not isinstance(vals, dict):
                raise questions.WizardError([f"{section}: answers belong in tables like [server]"])
            for k, v in vals.items():
                given[f"{section}.{k}"] = v
    given.update(questions.parse_set(sets))
    return given


def build_parser():
    ap = argparse.ArgumentParser(prog="cw-setup", add_help=False)
    sp = ap.add_subparsers(dest="cmd")
    for n in ("client", "server", "audit"):
        s = sp.add_parser(n, add_help=False)
        s.add_argument("--dry-run", action="store_true")
        s.add_argument("--apply", action="store_true")
        s.add_argument("--yes", action="store_true")
        s.add_argument("--answers")
        s.add_argument("--set", action="append", default=[])
        s.add_argument("--source", default=str(ROOT))
        s.add_argument("--rev")
        s.add_argument("--queue-key")
        s.add_argument("--results-key")
    return ap


def main(argv=None, host=None, ask=input, repos_factory=None, **overrides):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    try:
        a = build_parser().parse_args(argv)
    except SystemExit:
        emit("That is not a command cw-setup understands", "Run cw-setup help.", tag="ERROR", color=RED)
        return 2
    if not a.cmd:
        emit("No command given", "Run cw-setup help.", tag="ERROR", color=RED)
        return 2
    host = host or Host()
    say = lambda text="": print(text, file=sys.stderr)       # noqa: E731
    try:
        if a.cmd == "audit":
            cfg = config.load()
            rows, bad = audit_mod.run(host, cfg, say=say)
            return 1 if bad else 0
        given = load_answers(a.answers, a.set)
        interactive = not a.yes and sys.stdin.isatty()
        dry = not a.apply if a.cmd == "server" else a.dry_run
        if a.cmd == "server":
            setup = ServerSetup(host, a.source, ask=ask if interactive else None, say=say, dry_run=dry, given=given, use_defaults=not interactive, rev=a.rev,
                                confirm=(lambda p: ask(paint(f"\n  {p} [y/N] ", BOLD, YELLOW)).strip().lower() == "y") if interactive else (lambda p: True), **overrides)
        else:
            if repos_factory is None:
                from ..client.repo import ClientRepos
                repos_factory = lambda cfg: ClientRepos(cfg.path("client_queue"), cfg.path("client_results"), cfg.queue_url, cfg.results_url, jobs_branch=cfg.github.jobs_branch)  # noqa: E731
            setup = ClientSetup(host, Path(host.env.get("HOME", "~")).expanduser() / ".cw", ask=ask if interactive else None,
                                say=say, dry_run=dry, given=given, use_defaults=not interactive, repos_factory=repos_factory, queue_key=a.queue_key, results_key=a.results_key)
        report = setup.run_all()
    except questions.WizardError as e:
        emit("Those answers cannot be used", *[f"- {p}" for p in e.problems], tag="ERROR", color=RED)
        return 2
    except SetupError as e:
        emit(e.title, e.why, tag="STOPPED", color=RED, steps=e.steps or None, steps_label="What to do")
        return 1
    except config.ConfigError as e:
        emit("The configuration has problems", *[f"- {p}" for p in e.problems], tag="ERROR", color=RED)
        return 1
    waiting = [r for r in report if r[1] == "waiting"]
    if dry:
        emit("Nothing was changed", "That was a preview.", tag="PREVIEW", color=YELLOW, run_label="Do it for real", run=f"cw-setup {a.cmd}" + (" --apply" if a.cmd == "server" else ""))
    elif waiting:
        emit("Almost there", *[f"{s}: {d}" for s, _, d in waiting], tag="WAITING", color=YELLOW)
    else:
        emit("Done", tag="DONE", color=GREEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
