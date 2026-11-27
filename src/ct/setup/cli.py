"""ct-setup add-worker | audit | client | demo | install | rename | rotate-key | server | transport | tripwire-test | uninstall

  add-worker      find another computer on your network, check it, and set it up as one more worker (your other installs are not touched)
  audit           read-only: check a server against its configuration and report anything that drifted
  client          set up your own computer: your config, SSH key login, the two private repositories, the registry
  demo            try the question and protection screens with sample questions; nothing is saved or changed
  install         everything, from your own computer, in one command. It starts by scanning your network and asking which computer: a new one, or a worker you already have to set up again (ct-setup install --worker NAME or ct-setup install ADDRESS skips the scan). Then it asks the questions, sets up both sides, sends the release to the server, runs the server half there, adds the deploy keys to GitHub for you, and sends a first job
  rename          give the install a new name everywhere: the server, GitHub and this computer (preview first; job history, secrets and keys are kept)
  rotate-key      replace a Direct worker's mailbox key (the only key that lets this computer queue jobs on it): a new key, the worker trusts only it, then the old one is deleted (preview first)
  server          turn a clean Ubuntu or Debian machine into a worker (run as root on it)
  transport       move an existing install between GitHub and Direct, either way (preview first; history is copied across, nothing is deleted)
  tripwire-test   on the worker, as root: read a decoy credential and prove the worker locks itself down, then unlock it (needs a full machine with a kernel audit)
  uninstall       remove an install, completely and on purpose (preview first): a Linux worker, a Windows worker (--worker NAME), or the ct commands on this computer (--client); your GitHub repositories stay

Which computer a command acts on:  install picks from the scan or --worker NAME;  uninstall, rename, transport: your first install, or --worker NAME;  add-worker: a new computer by address; client, server, audit, tripwire-test, demo: the computer they run on.

Options:  --advanced (also ask the settings that have safe defaults)   --dry-run (show everything, change nothing; the default for server unless --apply)   --yes (take every default, ask nothing) --answers FILE (a TOML file of answers)   --set section.key=value (repeatable)
"""
import argparse
import contextlib
import os
import sys
import tomllib
from pathlib import Path

from .. import cards, clihelp, config
from ..client.repo import ClientError
from .. import ui as _ui
from ..ui import BOLD, GREEN, RED, YELLOW, paint
from . import audit as audit_mod
from . import hardening, questions, tui
from . import report as report_mod
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


# Which computer each command acts on (the standing rule, checked in tests/test_setup_targets.py: a command added here must say which).
PICKS = ("install",)                                  # one worker, chosen from the network scan (existing ones too) or named with --worker or by address
PICK_COMMANDS = ("uninstall", "rename", "transport", "rotate-key")   # one worker: your first install, or the one named with --worker
NEW_COMMANDS = ("add-worker",)                        # a computer that is not a worker yet
LOCAL_COMMANDS = ("client", "server", "audit", "demo", "tripwire-test")     # the computer it runs on; no --worker


def build_parser():
    ap = clihelp.Parser(prog="ct-setup", add_help=False)
    sp = ap.add_subparsers(dest="cmd")
    for n in ("client", "server", "audit", "install", "uninstall", "demo", "tripwire-test", "rename", "transport", "add-worker", "rotate-key"):
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
        s.add_argument("--approver-file", help="the public key that may approve jobs from the administrator's computer (server only)")
        s.add_argument("--mailbox-key-file", help="the public key your computer uses for the Direct mailbox (server only)")
        s.add_argument("name", nargs="?", help="with rename: the new name")
        s.add_argument("--user", help="with add-worker: the account to log in as on the new computer (default: your admin account)")
        s.add_argument("--worker", metavar="NAME", help="install, uninstall, rename, transport: act on this worker (its install name, address or listed name); without it, install scans and asks, the others act on your first install")
        s.add_argument("--worker-name", help="with add-worker: the new worker's install name (default: made from its hostname)")
        s.add_argument("--port", type=int, default=22, help="with add-worker: its SSH port")
        s.add_argument("--to", help="with rename --here (on the server): the new name")
        s.add_argument("--json", action="store_true", help="with rename --here: print the plan as JSON (the client half draws it)")
        s.add_argument("--no-sandbox-test", action="store_true", help="rename only: skip the check of the services' sandbox (a test machine whose systemd cannot enforce one; never on a real worker)")
        s.add_argument("--no-test", action="store_true")
        s.add_argument("--advanced", action="store_true")
        s.add_argument("--no-banner", action="store_true")
        s.add_argument("--verbose", action="store_true", help="on a terminal, also show the raw commands the setup runs")
        s.add_argument("--here", action="store_true")
        s.add_argument("--keep-data", action="store_true", help="uninstall: keep the data (a worker's state folders, or this computer's configuration and keys with --client)")
        s.add_argument("--remove-account", action="store_true")
        s.add_argument("--client", action="store_true", help="uninstall: remove the ct commands and your configuration from THIS computer instead of a worker")
    return ap


def aim_at_worker(key):
    """--worker NAME for the commands that read the configuration: make the configuration that worker's own (the first install's needs nothing). Returns an exit code on a name that is nobody's."""
    from . import addworker
    cfg = config.load()
    if not cfg.source:
        emit("There is no install yet", "Nothing to point --worker at.", tag="ERROR", color=RED, run="ct-setup install")
        return 2
    found = addworker.find_worker(cfg, Path(cfg.source).parent / "workers.toml", key)
    if found is None:
        names, _ = addworker.known_workers(cfg, Path(cfg.source).parent / "workers.toml")
        emit("That is not one of your workers", ("Name", key), f"Your workers: {', '.join(sorted(names))}.", tag="ERROR", color=RED, run="ct-admin workers")
        return 2
    if found[0] is not addworker.MAIN:
        os.environ["CT_CONFIG"] = str(found[1][1])
    return 0


def default_repos(cfg):
    from ..client.repo import ClientRepos
    return ClientRepos.from_config(cfg)


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
    if a.client:
        return client_uninstall_command(a, ask, say)
    if a.here:
        cfg = load_config()
        interactive = not a.yes and sys.stdin.isatty()
        run = Uninstall(host, cfg, say=say, dry_run=not a.apply, keep_data=a.keep_data, remove_account=a.remove_account,
                        confirm=None if a.yes else (lambda p: ask(_ui.confirm_prompt(f"{p}")).strip().lower() == "y") if interactive else (lambda p: False))
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
    from ..admin.ssh import Ssh, family_for
    cfg = config.load()
    cfg.require("server.host", "server.admin_user")
    if str(cfg.paths.code)[1:3] == ":/":                                           # a Windows worker: removed from here over SSH with PowerShell
        return windows_uninstall_command(a, cfg, ask, say)
    ssh = (ssh_factory or (lambda h, u, p: Ssh(h, u, p, family=family_for(cfg))))(cfg.server.host, cfg.server.admin_user, cfg.server.ssh_port)
    # The code that does the removal comes with this checkout, not from the install being removed, so it works however old or broken that install is.
    d, rev, _ = deliver_source(ssh, a.source, say=say)
    try:
        base = f"env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={d}/src CT_CONFIG={cfg.paths.config}/ct.toml python3 -m ct.setup uninstall --here " + " ".join(flags)
        if not a.apply:
            r = ssh.capture(base)
            say((r.stdout or "") + (r.stderr or ""))
            emit("To remove it", tag="NEXT", color=GREEN, run_label="Run", run="ct-setup uninstall --apply" + "".join(" " + f for f in flags))
            return 0 if r.returncode == 0 else 1
        if not a.yes:
            if not sys.stdin.isatty() or ask(_ui.confirm_prompt(f"Remove the {cfg.identity.name} install from {cfg.server.host}? Run the preview first if unsure.")).strip().lower() != "y":
                emit("Not removed", "You answered no, or there was nobody to ask.", tag="STOPPED", color=YELLOW)
                return 1
        rc = ssh.interactive(f"sudo {base} --apply --yes")
    finally:
        remove_remote_dir(ssh, d)
    if rc:
        emit("The removal stopped", f"Exit {rc}. Read the messages above.", tag="ERROR", color=RED)
        return 1
    emit("Removed", f"The {cfg.identity.name} install is gone from {cfg.server.host}. Your repositories and your computer's configuration were not touched.", tag="DONE", color=GREEN,
         run_label="Set it up again", run="ct-setup install")
    return 0


def client_uninstall_command(a, ask, say, scripts_dir="auto", home=None):
    """ct-setup uninstall --client: the ct commands, the startup-file lines and (unless --keep-data) the configuration and keys on THIS computer. A preview by default; one question before --apply."""
    from ..admin import workers as workers_mod
    from .uninstall_client import ClientUninstall
    if a.worker or a.here or a.remove_account:
        emit("--client cannot be combined with those options", ("Option", "--client"), "It acts only on this computer; to remove a worker use --worker NAME on its own.", tag="ERROR", color=RED)
        return 2
    cfg = config.load()
    if not cfg.source:
        emit("There is nothing to remove on this computer", "No configuration was found, so no install was made here.", tag="STOPPED", color=YELLOW)
        return 0
    interactive = not a.yes and sys.stdin.isatty()
    run = ClientUninstall(cfg, home=home, scripts_dir=scripts_dir, say=say, dry_run=not a.apply, keep_data=a.keep_data,
                          confirm=None if a.yes else (lambda p: ask(_ui.confirm_prompt(f"{p}")).strip().lower() == "y") if interactive else (lambda p: False))
    listed = sorted(workers_mod.load_list(run.workers_file)) if run.workers_file else []
    still = [("Workers still set up", ", ".join(listed))] if listed else []
    if not a.apply:
        emit("What removing the ct commands from this computer does", *still, *run.rows(), tag="PREVIEW", color=YELLOW,
             run_label="Remove them", run="ct-setup uninstall --client --apply" + (" --keep-data" if a.keep_data else ""))
        return 0
    status = run.run_all()[-1][1]
    if status == "declined":
        emit("Not removed", "You answered no, or there was nobody to ask.", tag="STOPPED", color=YELLOW)
        return 1
    env = run.environment_command()
    emit("The ct commands are removed from this computer", *still, "Your GitHub repositories and any worker computer were not touched.", tag="DONE", color=GREEN,
         **({"run_label": "Remove the Python environment they lived in", "run": env} if env else {}))
    return 0


def rotate_command(a, host, ask, ssh_factory, say, verify=None, keygen=None):
    """ct-setup rotate-key. On the worker (--here, as root, Linux): write the new public key as the mailbox's only key. From this computer: preview by default; --apply makes the new key, has the worker trust only it,
    swaps the local key file and checks it (src/ct/setup/rotate.py explains the order that keeps you from being locked out)."""
    from . import rotate
    if a.here:
        cfg = load_config()
        pub = Path(a.mailbox_key_file).read_text() if a.mailbox_key_file else ""
        if not pub.strip().startswith("ssh-"):
            emit("No public key was given", "The worker half needs the new public key: --mailbox-key-file FILE.", tag="ERROR", color=RED)
            return 2
        setup = ServerSetup(host, a.source, say=say, dry_run=False, rev=a.rev, mailbox_pubkey=pub)
        setup.cfg = cfg
        setup.mailbox()
        emit("The worker trusts only the new key", "The old key no longer works.", tag="DONE", color=GREEN)
        return 0
    cfg = config.load()
    cfg.require("server.host", "server.admin_user")
    if str(cfg.paths.code)[1:3] == ":/":                                               # a Windows worker: through its mailbox script over SSH
        from . import winuninstall, wintools
        run = wintools.ssh_runner(cfg.server.admin_user, cfg.server.host, int(cfg.server.ssh_port or 22))
        install = lambda pub: rotate.windows_install(run, winuninstall.windows_root(cfg), cfg.identity.name, cfg.server.worker_user, pub)      # noqa: E731
    else:
        from ..admin.ssh import Ssh, family_for
        ssh = (ssh_factory or (lambda h, u, p: Ssh(h, u, p, family=family_for(cfg))))(cfg.server.host, cfg.server.admin_user, cfg.server.ssh_port)
        install = rotate.linux_install(ssh, cfg, a.source, deliver_source, remove_remote_dir)
    interactive = not a.yes and sys.stdin.isatty()
    r = rotate.Rotation(cfg, install=install, verify=verify or rotate.git_verify(cfg), say=say, dry_run=not a.apply, keygen=keygen or rotate.keygen,
                        confirm=None if a.yes else (lambda p: ask(_ui.confirm_prompt(f"{p}")).strip().lower() == "y") if interactive else (lambda p: False))
    facts = [("Worker", cfg.identity.name), ("Computer", cfg.server.host)]
    r.check()
    if not a.apply:
        emit("What replacing the mailbox key does", *facts, *r.plan_rows(), tag="PREVIEW", color=YELLOW, run_label="Replace it", run=f"ct-setup rotate-key --worker {cfg.identity.name} --apply")
        return 0
    status = r.run_all()[-1][1]
    if status == "declined":
        emit("Not replaced", "You answered no, or there was nobody to ask.", tag="STOPPED", color=YELLOW)
        return 1
    emit("The mailbox key was replaced", *facts, "The worker trusts only the new key, and it works. The old key is deleted.", tag="DONE", color=GREEN, run_label="See the worker", run="ct-admin check")
    return 0


def windows_uninstall_command(a, cfg, ask, say, runner=None):
    """ct-setup uninstall --worker NAME for a Windows worker: preview by default, one question before --apply removes anything."""
    from .winuninstall import WindowsUninstall
    if a.remove_account:
        emit("That option is for Linux workers", ("Option", "--remove-account"), "On Windows the worker account is always removed with its folders.", tag="ERROR", color=RED)
        return 2
    interactive = not a.yes and sys.stdin.isatty()
    run = WindowsUninstall(cfg, Path(cfg.source).parent / "workers.toml", run=runner, say=say, dry_run=not a.apply, keep_data=a.keep_data,
                           confirm=None if a.yes else (lambda p: ask(_ui.confirm_prompt(f"{p}")).strip().lower() == "y") if interactive else (lambda p: False))
    facts = [("Worker", cfg.identity.name), ("Computer", cfg.server.host)]
    if not a.apply:
        emit("What removing this Windows worker does", *facts, *run.rows(), tag="PREVIEW", color=YELLOW,
             run_label="Remove it", run="ct-setup uninstall --worker " + cfg.identity.name + " --apply" + (" --keep-data" if a.keep_data else ""))
        return 0
    status = run.run_all()[-1][1]
    if status == "declined":
        emit("Not removed", "You answered no, or there was nobody to ask.", tag="STOPPED", color=YELLOW)
        return 1
    if status == "partial":
        emit("The Windows worker is removed except its profile folder", *facts, "Windows keeps the worker account's profile in use until the computer restarts, so that one folder is still there. Nothing on this computer was forgotten yet, so the same command finishes the job.",
             tag="WARNING", color=YELLOW, steps=["Restart the Windows computer.", "Run the command again."], steps_label="What to do", run_label="Finish the removal", run="ct-setup uninstall --worker " + cfg.identity.name + " --apply" + (" --keep-data" if a.keep_data else ""))
        return 1
    emit("The Windows worker is removed", *facts, "Your GitHub repositories, Windows itself and the administrator account were not touched.", tag="DONE", color=GREEN, run_label="Set it up again", run="ct-setup add-worker " + cfg.server.host)
    return 0


def finish(a, report, dry):
    if dry:
        emit("Nothing was changed on the server", "That was a preview.", tag="PREVIEW", color=YELLOW, run_label="Do it for real", run="ct-setup install")
    elif any(r[1] == "failed" for r in report):
        emit("The setup finished but the first job did not come back", "Run ct-admin check and ct-admin status to see why.", tag="WARNING", color=YELLOW)
        return 1
    else:
        emit("The worker is set up", *([("First job", "went through the whole loop: sent, picked up, run and its result came back")] if any(r[0] == "a job went through the whole loop" and r[1] == "ok" for r in report) else []),
             tag="DONE", color=GREEN, run_label="Try it", run=["ct-run classes", "ct-run submit sys-status --wait", "ct-admin status"])
    return 0


def main(argv=None, host=None, ask=input, repos_factory=None, ssh_factory=None, verify=None, screen=None, **overrides):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        _ui.document("ct-setup", __doc__ or "", tag="HELP", stream=sys.stdout, reflow=True)
        return 0
    try:
        a = build_parser().parse_args(argv)
    except (clihelp.UsageError, SystemExit) as e:
        title, why, steps, example, facts = clihelp.usage_problem(e, "ct-setup") if isinstance(e, clihelp.UsageError) else ("That is not a command ct-setup understands", "Run ct-setup help.", [], None, [])
        emit(title, *facts, cards.Why(why or ""), tag="ERROR", color=RED, steps=steps or None, steps_label="Try", run=example, run_label="Did you mean")
        return 2
    if not a.cmd:
        emit("No command given", "Run ct-setup help.", tag="ERROR", color=RED)
        return 2
    host = host or Host()
    say = report_mod.sink(sys.stderr)
    if a.worker and a.cmd in LOCAL_COMMANDS + NEW_COMMANDS:
        emit("That command has no --worker", ("Command", f"ct-setup {a.cmd}"), "It acts on the computer it runs on." if a.cmd in LOCAL_COMMANDS else "It sets up a computer that is not a worker yet; give its address instead.", tag="ERROR", color=RED)
        return 2
    if a.worker and a.cmd in PICK_COMMANDS:
        rc = aim_at_worker(a.worker)
        if rc:
            return rc
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
                                    confirm=None if a.yes else (lambda p: ask(_ui.confirm_prompt(f"{p}")).strip().lower() == "y") if interactive else (lambda p: False))
                if a.json:
                    run.check()
                    import json as _json
                    print(_json.dumps({"old": run.old, "new": run.new, "steps": [{"title": t, "lines": ls} for t, ls in run.plan_lines()]}))      # output: data (the client half draws it)
                    return 0
                status = run.run_all()[-1][1]
                if status == "declined":
                    return 1
                if status == "done":
                    emit("Renamed on this machine", f"{run.old} is now {run.new}.", tag="DONE", color=GREEN)
                return 0
            return rename.rename_command(a, host, ask, ssh_factory, say)
        if a.cmd == "transport":
            from . import migrate
            if a.here:                                           # on the server, as root: the half the client half runs over ssh
                cfg = load_config()
                interactive = not a.yes and sys.stdin.isatty()
                run = migrate.Migrate(host, cfg, a.to, source=a.source, rev=a.rev, say=say, dry_run=not a.apply, skip_sandbox_test=a.no_sandbox_test,
                                      mailbox_pubkey=(Path(a.mailbox_key_file).read_text() if a.mailbox_key_file else None),
                                      confirm=None if a.yes else (lambda p: ask(_ui.confirm_prompt(f"{p}")).strip().lower() == "y") if interactive else (lambda p: False))
                if a.json:
                    run.check()
                    import json as _json
                    print(_json.dumps({"old": run.old, "new": run.to, "steps": [{"title": t, "lines": ls} for t, ls in run.plan_lines()]}))      # output: data (the client half draws it)
                    return 0
                status = run.run_all()[-1][1]
                if status == "declined":
                    return 1
                if status == "done":
                    emit("Moved on this machine", f"The worker now uses {run.to}.", tag="DONE", color=GREEN)
                return 0
            return migrate.transport_command(a, host, ask, ssh_factory, say, verify=verify)
        def worker_flow(command):
            from . import addworker
            from ..admin.ssh import Ssh, family_for
            interactive = not a.yes and sys.stdin.isatty()
            screen_ = tui.make(interactive=interactive)

            from .install import flat as flat_answers
            main = config.load()                                          # read NOW: once the install starts CT_CONFIG names the new worker's file, which does not exist yet
            answered = tomllib.loads(Path(main.source).read_text()) if main.source else {}                          # only what you answered for the first worker, as defaults for this one
            flat_main = {k: v for k, v in flat_answers(answered).items() if k not in ("identity.name", "server.host", "server.admin_user", "paths.client_home")}

            def installer_factory(conf, home, name, server_host, user, suggest):
                return Installer(a.source, ssh_factory=ssh_factory or (lambda h, u, p: Ssh(h, u, p)), local=host, say=say, ask=ask if interactive else None,
                                 given={**load_answers(a.answers, a.set), **suggest}, ui=screen_, advanced=False, use_defaults=not interactive, dry_run=False,
                                 repos_factory=repos_factory or default_repos, wait_for_keys=900, home=home, config_path=conf, targeted=True, suggest={**flat_main, "transport.kind": main.transport.kind},
                                 verify=verify or (lambda cfg: default_verify(cfg, say=say)))
            return addworker.run(a, host, ask, ssh_factory, say, ui=screen_, installer_factory=installer_factory, interactive=interactive, command=command)
        if a.cmd == "add-worker":
            return worker_flow("add-worker")
        if a.cmd == "tripwire-test":
            from . import tripwire
            cfg = config.load()
            ok, why = tripwire.run(host, cfg, say=say, confirm=(lambda p: True) if a.yes else (lambda p: ask(_ui.confirm_prompt(f"{p}")).strip().lower() == "y"))
            emit("The tripwire works" if ok else "The tripwire test did not pass", why if ok else cards.Why(why), tag="OK" if ok else "FAILED", color=GREEN if ok else RED)
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
        if a.cmd == "rotate-key":
            return rotate_command(a, host, ask, ssh_factory, say, verify=verify)
        if a.cmd == "uninstall":
            return uninstall_command(a, host, ask, ssh_factory, say)
        given = load_answers(a.answers, a.set) if a.cmd != "audit" else {}
        if a.cmd == "install" and not a.here and "server.host" not in given and (a.worker or a.name or (not a.yes and sys.stdin.isatty())):
            from . import addworker                                     # install does not assume a computer: it scans, and you pick one of yours (set up again) or a new one; --worker NAME or an address skips the scan
            cfg0 = config.load()
            if cfg0.source:
                rc = worker_flow("install")
                if rc is not addworker.MAIN:
                    return rc
                given["server.host"] = cfg0.server.host
            else:                                                       # the very first install: nothing is configured, so the only question is which computer
                target = a.name
                if not target:
                    ui0 = tui.make(interactive=True)
                    target, code = addworker.scan_for(set(), ui0, ask, say, lambda *x, **k: _ui.say(*x, **k), True, command="install", banner="install")
                    if target is None:
                        return code
                given["server.host"] = target
        if a.cmd == "install":
            from ..admin.ssh import Ssh, family_for
            interactive = not a.yes and sys.stdin.isatty()
            screen_ = tui.make(interactive=interactive)
            live.append(screen_)
            if screen_ is not None and not a.no_banner:
                screen_.banner("install preview" if a.dry_run else "install", unless_shown=True)
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
                                approver_pubkey=(Path(a.approver_file).read_text() if a.approver_file else None),
                                mailbox_pubkey=(Path(a.mailbox_key_file).read_text() if a.mailbox_key_file else None), verbose=a.verbose, ui=screen_, advanced=a.advanced,
                                confirm=(lambda p: ask(_ui.confirm_prompt(f"{p}")).strip().lower() == "y") if interactive else (lambda p: True), **overrides)
        else:
            if repos_factory is None:
                from ..client.repo import ClientRepos
                repos_factory = ClientRepos.from_config
            setup = ClientSetup(host, Path(host.env.get("HOME", "~")).expanduser() / ".ct", ask=ask if interactive else None,
                                say=say, dry_run=dry, given=given, use_defaults=not interactive, repos_factory=repos_factory, queue_key=a.queue_key, results_key=a.results_key,
                                ui=screen_, advanced=a.advanced, verbose=a.verbose,
                                confirm_card=(screen_.protection if screen_ is not None else
                                              ((lambda info: ask(_ui.confirm_prompt(f"{info['title']}?")).strip().lower() == "y") if interactive else None)),
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
        emit(e.title, *e.facts, cards.Why(e.why or ""), tag="STOPPED", color=RED, steps=e.steps or None, steps_label="What to do")
        return 1
    except ClientError as e:                    # a failed SSH connection or git step on this side: said in plain words, never a traceback
        for _s in live:
            _s is not None and _s.erase()
        emit(e.title, cards.Why(e.why or ""), tag="ERROR", color=RED, steps=e.steps or None, steps_label="How to fix it", run=e.example.split("\n") if e.example else None)
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
        emit("Nothing was changed", "That was a preview.", tag="PREVIEW", color=YELLOW, run_label="Do it for real", run=f"ct-setup {a.cmd}" + (" --apply" if a.cmd == "server" else ""))
    elif waiting:
        emit("Almost there", *[f"{s}: {d}" for s, _, d in waiting], tag="WAITING", color=YELLOW)
        return EXIT_WAITING if a.cmd == "server" else 0
    elif a.cmd == "server" and a.no_banner:
        pass                                        # run by the installer, which prints its own "server setup" row; a second DONE here would be a stray
    else:
        emit("Done", tag="DONE", color=GREEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
