"""cw-setup install | uninstall | rename | demo | client | server | audit | tripwire-test

  client    set up your own computer: your config, SSH key login, the two private repositories, the registry
  server    turn a clean Ubuntu or Debian machine into a worker (run as root on it)
  rename    give the install a new name everywhere: the server, GitHub and this computer (preview first; job history, secrets and keys are kept)
  audit     read-only: check a server against its configuration and report anything that drifted
  tripwire-test  on the worker, as root: read a decoy credential and prove the worker locks itself down, then unlock it (needs a full machine with a kernel audit)
  uninstall remove an install from the server, completely and on purpose (preview first); your repositories and your computer's own configuration stay
  demo      try the question and protection screens with sample questions; nothing is saved or changed
  install   everything, from your own computer, in one command: it asks the questions, sets up both sides, sends the release to the server, runs the server
            half there, adds the deploy keys to GitHub for you, and sends a first job

Options:  --advanced (also ask the settings that have safe defaults)   --dry-run (show everything, change nothing; the default for server unless --apply)   --yes (take every default, ask nothing)
          --answers FILE (a TOML file of answers)   --set section.key=value (repeatable)
"""
import argparse
import contextlib
import sys
import tomllib
from pathlib import Path

from .. import config
from ..client.repo import ClientError
from .. import ui as _ui
from ..ui import BOLD, GREEN, RED, YELLOW, paint
from . import audit as audit_mod
from . import hardening, questions, tui
from .client import ClientSetup
from .host import Host
from .install import Installer, default_verify, deliver_source, remove_remote_dir
from .uninstall import Uninstall, load_config
from .server import EXIT_WAITING, ServerSetup, SetupError

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
    for n in ("client", "server", "audit", "install", "uninstall", "demo", "tripwire-test", "rename"):
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
        s.add_argument("--wait-for-keys", type=int, default=0, metavar="SECONDS")
        s.add_argument("--secrets-file", help="a KEY=value file of sign-in secrets from the installer (server only)")
        s.add_argument("--approver-file", help="the public key that may approve jobs from the administrator's computer (server only)")
        s.add_argument("name", nargs="?", help="with rename: the new name")
        s.add_argument("--to", help="with rename --here (on the server): the new name")
        s.add_argument("--no-sandbox-test", action="store_true", help="rename only: skip the check of the services' sandbox (a test machine whose systemd cannot enforce one; never on a real worker)")
        s.add_argument("--no-test", action="store_true")
        s.add_argument("--advanced", action="store_true")
        s.add_argument("--no-banner", action="store_true")
        s.add_argument("--verbose", action="store_true", help="on a terminal, also show the raw commands the setup runs")
        s.add_argument("--here", action="store_true")
        s.add_argument("--keep-data", action="store_true")
        s.add_argument("--remove-account", action="store_true")
    return ap


def default_repos(cfg):
    from ..client.repo import ClientRepos
    return ClientRepos(cfg.path("client_queue"), cfg.path("client_results"), cfg.queue_url, cfg.results_url, jobs_branch=cfg.github.jobs_branch)


def demo(ui, say):
    """The question screens (with the same keys as the real thing: Enter, the up arrow to go back, Tab) and both kinds of protection frame, on sample content."""
    ui.begin(title="demo")
    asked = {"hardware.profile", "network.ipv6", "alerts.kind", "alerts.url", "updates.mode"}
    questions.collect(("client", "server"), ui=ui, only=asked, advanced=True, confirm="That was the question screens. Continue to the protection screens?")
    ui.finish()
    samples = [("ssh-keys-only", ["write /etc/ssh/sshd_config.d/10-example.conf (mode 644):\n    PasswordAuthentication no\n    KbdInteractiveAuthentication no\n    PermitRootLogin no", "sshd -t", "systemctl reload ssh"],
                "password logins are on"),
               ("unattended-upgrades", ["apt-get install -y unattended-upgrades", "write /etc/apt/apt.conf.d/20auto-upgrades (mode 644):\n    APT::Periodic::Update-Package-Lists \"1\";"], "not installed")]
    for i, (mid, steps, now) in enumerate(samples, 1):
        mod = hardening.MODULES[mid]
        chose = ui.protection({"id": mod.id, "title": mod.title, "why": mod.why, "risk": mod.risk, "now": now, "steps": steps, "lockout": mod.lockout, "index": i, "total": len(samples)})
        say(f"  You chose {'Apply' if chose else 'Skip'} for: {mod.title}")
    say("\n  That is everything the wizard shows. Nothing was saved or changed.\n")


def uninstall_command(a, host, ask, ssh_factory, say):
    """On the server itself (--here, run as root): remove it. From your computer: confirm once, then run exactly that on the server over ssh -t."""
    flags = (["--keep-data"] if a.keep_data else []) + (["--remove-account"] if a.remove_account else [])
    if a.here:
        cfg = load_config()
        interactive = not a.yes and sys.stdin.isatty()
        run = Uninstall(host, cfg, say=say, dry_run=not a.apply, keep_data=a.keep_data, remove_account=a.remove_account,
                        confirm=None if a.yes else (lambda p: ask(paint(f"\n  {p} [y/N] ", BOLD, YELLOW)).strip().lower() == "y") if interactive else (lambda p: False))
        report = run.run_all()
        status = report[-1][1] if report else ""
        if status == "planned":
            emit("Nothing was removed", "That was a preview.", tag="PREVIEW", color=YELLOW)
        elif status == "removed":
            emit("Removed", f"The {cfg.identity.name} install is gone from this machine.", tag="DONE", color=GREEN)
        elif status == "declined":
            emit("Not removed", "You answered no.", tag="STOPPED", color=YELLOW)
            return 1
        return 0
    from ..admin.ssh import Ssh
    cfg = config.load()
    cfg.require("server.host", "server.admin_user")
    ssh = (ssh_factory or (lambda h, u, p: Ssh(h, u, p)))(cfg.server.host, cfg.server.admin_user, cfg.server.ssh_port)
    # The code that does the removal comes with this checkout, not from the install being removed, so it works however old or broken that install is.
    d, rev, _ = deliver_source(ssh, a.source, say=say)
    try:
        base = f"env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={d}/src CW_CONFIG={cfg.paths.config}/cw.toml python3 -m cw.setup uninstall --here " + " ".join(flags)
        if not a.apply:
            r = ssh.capture(base)
            say((r.stdout or "") + (r.stderr or ""))
            emit("To remove it", tag="NEXT", color=GREEN, run_label="Run", run="cw-setup uninstall --apply" + "".join(" " + f for f in flags))
            return 0 if r.returncode == 0 else 1
        if not a.yes:
            if not sys.stdin.isatty() or ask(paint(f"\n  Remove the {cfg.identity.name} install from {cfg.server.host}? Run the preview first if unsure. [y/N] ", BOLD, YELLOW)).strip().lower() != "y":
                emit("Not removed", "You answered no, or there was nobody to ask.", tag="STOPPED", color=YELLOW)
                return 1
        rc = ssh.interactive(f"sudo {base} --apply --yes")
    finally:
        remove_remote_dir(ssh, d)
    if rc:
        emit("The removal stopped", f"Exit {rc}. Read the messages above.", tag="ERROR", color=RED)
        return 1
    emit("Removed", f"The {cfg.identity.name} install is gone from {cfg.server.host}. Your repositories and your computer's configuration were not touched.", tag="DONE", color=GREEN,
         run_label="Set it up again", run="cw-setup install")
    return 0


def finish(a, report, dry):
    if dry:
        emit("Nothing was changed on the server", "That was a preview.", tag="PREVIEW", color=YELLOW, run_label="Do it for real", run="cw-setup install")
    elif any(r[1] == "failed" for r in report):
        emit("The setup finished but the first job did not come back", "Run cw-admin check and cw-admin status to see why.", tag="WARNING", color=YELLOW)
        return 1
    else:
        emit("The worker is set up", tag="DONE", color=GREEN, run_label="Try it", run=["cw-run classes", "cw-run submit sys-status --wait", "cw-admin status"])
    return 0


def main(argv=None, host=None, ask=input, repos_factory=None, ssh_factory=None, verify=None, screen=None, **overrides):
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
    live = []                                                  # the terminal screen in use, if any, so an error can clear it before it is printed
    try:
        if a.cmd == "audit":
            cfg = config.load()
            rows, bad = audit_mod.run(host, cfg, say=say)
            return 1 if bad else 0
        if a.cmd == "rename":
            from . import rename
            if a.here:                                           # on the server, as root: the half the client half runs over ssh
                cfg = load_config()
                interactive = not a.yes and sys.stdin.isatty()
                run = rename.Rename(host, cfg, a.to, source=a.source, rev=a.rev, say=say, dry_run=not a.apply,
                                    skip_sandbox_test=a.no_sandbox_test,
                                    confirm=None if a.yes else (lambda p: ask(paint(f"\n  {p} [y/N] ", BOLD, YELLOW)).strip().lower() == "y") if interactive else (lambda p: False))
                status = run.run_all()[-1][1]
                if status == "declined":
                    return 1
                if status == "done":
                    emit("Renamed on this machine", f"{run.old} is now {run.new}.", tag="DONE", color=GREEN)
                return 0
            return rename.rename_command(a, host, ask, ssh_factory, say)
        if a.cmd == "tripwire-test":
            from . import tripwire
            cfg = config.load()
            ok, why = tripwire.run(host, cfg, say=say, confirm=(lambda p: True) if a.yes else (lambda p: ask(paint(f"\n  {p} [y/N] ", BOLD, YELLOW)).strip().lower() == "y"))
            emit("The tripwire works" if ok else "The tripwire test did not pass", why, tag="OK" if ok else "FAILED", color=GREEN if ok else RED)
            return 0 if ok else 1
        if a.cmd == "demo":
            ui = screen or tui.make(interactive=True)
            if ui is None:
                emit("The demo needs a real terminal", "It draws screens and reads keys, so it cannot run in a pipe or with NO_COLOR set.", tag="ERROR", color=RED)
                return 2
            ui.banner("demo")
            with ui.session():
                demo(ui, say)
            return 0
        if a.cmd == "uninstall":
            return uninstall_command(a, host, ask, ssh_factory, say)
        given = load_answers(a.answers, a.set) if a.cmd != "audit" else {}
        if a.cmd == "install":
            from ..admin.ssh import Ssh
            interactive = not a.yes and sys.stdin.isatty()
            screen_ = tui.make(interactive=interactive)
            live.append(screen_)
            if screen_ is not None and not a.no_banner:
                screen_.banner("install preview" if a.dry_run else "install")
            inst = Installer(a.source, ssh_factory=ssh_factory or (lambda h, u, p: Ssh(h, u, p)), local=host, say=say, ask=ask if interactive else None, given=given,
                             ui=screen_, advanced=a.advanced, verbose=a.verbose,
                             use_defaults=not interactive, dry_run=a.dry_run, repos_factory=repos_factory or default_repos, wait_for_keys=a.wait_for_keys or 900,
                             verify=None if (a.no_test or a.dry_run) else (verify or (lambda cfg: default_verify(cfg, say=say))))
            with (screen_.session() if screen_ is not None else contextlib.nullcontext()):
                report = inst.run_all()
            dry = a.dry_run
            return finish(a, report, dry)
        interactive = not a.yes and sys.stdin.isatty()
        dry = not a.apply if a.cmd == "server" else a.dry_run
        screen_ = tui.make(interactive=interactive)
        live.append(screen_)
        if screen_ is not None and not a.no_banner:
            screen_.banner(a.cmd)
        if a.cmd == "server":
            setup = ServerSetup(host, a.source, ask=ask if interactive else None, say=say, dry_run=dry, given=given, use_defaults=not interactive, rev=a.rev, wait_for_keys=a.wait_for_keys,
                                claude_secrets=config.read_env_file(a.secrets_file) if a.secrets_file else None,
                                approver_pubkey=(Path(a.approver_file).read_text() if a.approver_file else None), verbose=a.verbose, ui=screen_, advanced=a.advanced,
                                confirm=(lambda p: ask(paint(f"\n  {p} [y/N] ", BOLD, YELLOW)).strip().lower() == "y") if interactive else (lambda p: True), **overrides)
        else:
            if repos_factory is None:
                from ..client.repo import ClientRepos
                repos_factory = lambda cfg: ClientRepos(cfg.path("client_queue"), cfg.path("client_results"), cfg.queue_url, cfg.results_url, jobs_branch=cfg.github.jobs_branch)  # noqa: E731
            setup = ClientSetup(host, Path(host.env.get("HOME", "~")).expanduser() / ".cw", ask=ask if interactive else None,
                                say=say, dry_run=dry, given=given, use_defaults=not interactive, repos_factory=repos_factory, queue_key=a.queue_key, results_key=a.results_key,
                                ui=screen_, advanced=a.advanced, verbose=a.verbose,
                                confirm_card=(screen_.protection if screen_ is not None else
                                              ((lambda info: ask(f"  {info['title']}? [y/N] ").strip().lower() == "y") if interactive else None)),
                                **({"scripts_dir": overrides.pop("scripts_dir")} if "scripts_dir" in overrides else {}))
        with (screen_.session() if screen_ is not None else contextlib.nullcontext()):
            report = setup.run_all()
    except questions.WizardError as e:
        for _s in live:
            _s is not None and _s.erase()
        emit("Those answers cannot be used", *[f"- {p}" for p in e.problems], tag="ERROR", color=RED)
        return 2
    except SetupError as e:
        for _s in live:
            _s is not None and _s.erase()
        emit(e.title, e.why, tag="STOPPED", color=RED, steps=e.steps or None, steps_label="What to do")
        return 1
    except ClientError as e:                    # a failed SSH connection or git step on this side: said in plain words, never a traceback
        for _s in live:
            _s is not None and _s.erase()
        emit(e.title, e.why, tag="ERROR", color=RED, steps=e.steps or None, steps_label="How to fix it", run=e.example.split("\n") if e.example else None)
        return 2
    except config.ConfigError as e:
        for _s in live:
            _s is not None and _s.erase()
        emit("The configuration has problems", *[f"- {p}" for p in e.problems], tag="ERROR", color=RED)
        return 1
    except KeyboardInterrupt:
        for _s in live:
            _s is not None and _s.erase()
        emit("Stopped", "Nothing is half-done: running the same command again continues from here.", tag="STOPPED", color=YELLOW)
        return 130
    waiting = [r for r in report if r[1] == "waiting"]
    if dry:
        emit("Nothing was changed", "That was a preview.", tag="PREVIEW", color=YELLOW, run_label="Do it for real", run=f"cw-setup {a.cmd}" + (" --apply" if a.cmd == "server" else ""))
    elif waiting:
        emit("Almost there", *[f"{s}: {d}" for s, _, d in waiting], tag="WAITING", color=YELLOW)
        return EXIT_WAITING if a.cmd == "server" else 0
    else:
        emit("Done", tag="DONE", color=GREEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
