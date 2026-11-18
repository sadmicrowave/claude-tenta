"""ct-setup transport: move an existing install from GitHub to Direct, with a preview first.

Nothing is reinstalled. The worker keeps its job history, secrets, approver key and registry (approvals stay valid: they are signed over the job file, not over where
the repositories live). What changes: the configuration says `[transport] kind = "direct"`, the worker gets its mailbox (account, two bare repositories, your
computer's key), the repositories' history is copied into the mailbox with `git push --mirror`, the worker's clones and yours are pointed at the mailbox, and the
files that depend on the connection (the mailbox shell) are written by the ordinary release installer. The GitHub repositories and their deploy keys are NOT touched:
they are your way back, and the finish card says how to remove them once Direct has proved itself.

The server half (`Migrate`, run as root on the worker) does the worker's part; the client half (`transport_command`) runs it over SSH, then rewrites your own configuration and clones.
Going back to GitHub is not done by this command yet: it says so and names the way (ct-setup install, answering GitHub to the connection question).
"""
import json
import shlex
import sys
import tomllib
from pathlib import Path

from .. import config, render, transport
from . import hardening, report as report_mod, toml_out
from .rename import render_plan
from .report import sink as _sink
from .server import ServerSetup, SetupError

STYLER = report_mod.Styler(styled=sys.stderr.isatty(), stream=sys.stderr)


def with_transport(text, kind):
    """The configuration text with `[transport] kind` set (everything else as it was)."""
    raw = tomllib.loads(text or "")
    raw.setdefault("transport", {})["kind"] = kind
    return toml_out.dump(raw)


def load_new_config(text, kind):
    clean, problems = config.validate(tomllib.loads(with_transport(text, kind)), require_alert_url=False)
    if problems:
        raise SetupError("The new connection does not make a valid configuration", "\n".join(problems[:5]))
    return config.Config(clean)


class Call:
    """A step that is code, not a command (shown as one line in the plan)."""
    kind = "call"

    def __init__(self, label, fn):
        self.label, self.fn = label, fn

    def show(self):
        return self.label


class Migrate:
    def __init__(self, host, cfg, to, *, source, rev, say=None, dry_run=True, confirm=None, mailbox_pubkey=None, skip_sandbox_test=False):
        self.host, self.cfg, self.to, self.source, self.rev = host, cfg, to, source, rev
        self.say, self.dry_run, self.confirm = say or _sink(), dry_run, confirm
        self.mailbox_pubkey, self.skip_sandbox_test = mailbox_pubkey, skip_sandbox_test
        self.old = transport.for_config(cfg).kind
        self.report = []
        source_file = getattr(cfg, "source", None) or cfg.path("config_file")
        self.conf_path = str(source_file)
        self.text = host.read(self.conf_path) or ""
        self.cfg2 = None

    # ---- checks, before anything is touched ----
    def check(self):
        if self.to not in transport.TRANSPORTS:
            raise SetupError(f"{self.to} is not a connection method", f"Choose one of: {', '.join(transport.TRANSPORTS)}.")
        if self.to == self.old:
            raise SetupError(f"The install already uses {self.old}", "There is nothing to move.")
        if self.to != "direct":
            raise SetupError("Moving back to GitHub is not done by this command yet", "Nothing was changed. Your GitHub repositories were never touched, so they still hold the history up to the move.",
                             ["Run ct-setup install and answer GitHub to the connection question: it sets the worker up for GitHub again."])
        w = self.cfg.server.worker_user
        if not self.host.run(["id", w]).ok:
            raise SetupError(f"The worker account {w} does not exist on this machine", "Nothing was changed.",
                             ["Find the account the worker runs as: ls -ld /var/lib/*/queue shows its owner.", f"Then add it to {self.conf_path} under [server]: worker_user = \"<that account>\", and run this again."])
        for which in ("queue", "results"):
            if self.dry_run:
                break                       # the preview runs as the administrator, who cannot see the worker's private folders (found on a real machine); the real run is root and checks
            if not self.host.exists(self.cfg.path(f"{which}_clone") + "/.git"):
                raise SetupError(f"The worker has no {which} clone", f"{self.cfg.path(which + '_clone')} is missing, so there is nothing to copy into the mailbox.",
                                 ["Run ct-setup install again; it makes the clones."])
        running = self.host.run(["sh", "-c", f"ls {self.cfg.path('state')}/running 2>/dev/null | head -1"]).out.strip()
        if running:
            raise SetupError("A job is running", f"{running} is still running. Wait for it, or stop it (ct-admin kill), then try again; nothing was changed.")
        if not self.host.exists(f"{self.source}/src/ct/__init__.py"):
            raise SetupError("The release to install is missing", f"{self.source} is not a release checkout.")
        if not self.dry_run and not (self.mailbox_pubkey or "").strip().startswith("ssh-") and not self.host.exists(f"{self.cfg.paths.config}/mailbox-client.pub"):
            raise SetupError("Your computer's mailbox key did not arrive", "Without it nothing could use the mailbox, so nothing was changed.", ["Run ct-setup transport direct from your computer; it sends the key."])
        self.cfg2 = load_new_config(self.text, self.to)

    # ---- the plan ----
    def steps(self):
        c, c2, h, w = self.cfg, self.cfg2, self.host, self.cfg.server.worker_user
        A, t2 = hardening.Action, transport.for_config(self.cfg2)
        mail = t2.mail_user(c2)
        timers = [p for p in render.UNITS if p.endswith(".timer") and p.split(".")[0] in ("fetch", "step") and h.exists(f"/etc/systemd/system/{c.unit(p)}")]
        out = [("Pause the job loop", [A("run", argv=["systemctl", "stop", c.unit(p)]) for p in timers])]
        out.append(("Write the new connection into the configuration", [A("write", path=self.conf_path, text=with_transport(self.text, self.to), mode=0o644)]))
        out.append(("Make the mailbox (as a fresh install would)", [Call(f"create the account {mail}, the two repositories under {c2.path('mailbox')} and the key",
                                                                         self.make_mailbox)]))
        copy = []
        for which, owner in (("queue", mail), ("results", w)):
            clone, bare = c.path(f"{which}_clone"), t2.repo_path(c2, which)
            copy += [A("run", argv=argv, why=f"copies the {which} history into the mailbox, so nothing is lost") for argv in transport.copy_history_commands(clone, bare)]
            copy += [A("run", argv=["chown", "-R", f"{owner}:{mail}", bare]), A("run", argv=["chmod", "-R", "u+rwX,g+rX,o-rwx", bare])]
        out.append(("Copy the history into the mailbox", copy))
        out.append(("Point the worker's clones at the mailbox", [A("run", argv=["runuser", "-u", w, "--", "git", "-C", c.path(f"{which}_clone"), "remote", "set-url", "origin", t2.worker_url(c2, which)])
                                                                 for which in ("queue", "results")]))
        out.append(("Write the files that depend on the connection (the same installer an update uses)", [
            A("run", argv=["env", "PYTHONDONTWRITEBYTECODE=1", f"PYTHONPATH={self.source}/src", f"CT_CONFIG={self.conf_path}", "python3", "-m", "ct.update"]
              + (["--no-sandbox-test"] if self.skip_sandbox_test else []) + ["--local", self.source, self.rev], why="writes the mailbox shell and re-renders the services")]))
        out.append(("Start the job loop again", [A("run", argv=["systemctl", "start", c2.unit(p)]) for p in timers]))
        return out

    def make_mailbox(self):
        setup = ServerSetup(self.host, self.source, say=self.say, dry_run=False, rev=self.rev, mailbox_pubkey=self.mailbox_pubkey)
        setup.cfg = self.cfg2
        setup.mailbox()
        return setup.report

    def plan_lines(self):
        out = []
        for title, actions in self.steps():
            lines = []
            for a in actions:
                if a.kind == "call":
                    lines.append(a.label)
                elif a.kind == "run":
                    lines.append(shlex.join(a.argv))
                elif a.kind == "write":
                    lines.append(f"write {a.path}")
                else:
                    lines.append(f"remove {a.path}")
            out.append((title, lines))
        return out

    def run_all(self):
        self.check()
        steps = self.steps()
        render_plan(self.plan_lines(), self.old, self.to, where="on this machine")
        if self.dry_run:
            self.report.append(("transport", "planned", f"{sum(len(a) for _, a in steps)} steps"))
            return self.report
        if self.confirm is not None and not self.confirm(f"Move this install from {self.old} to {self.to} on this machine?"):
            self.report.append(("transport", "declined", ""))
            return self.report
        for title, actions in steps:
            for a in actions:
                if a.kind == "call":
                    a.fn()
                    continue
                r = a.do(self.host)
                if not r.ok and not (a.argv and a.argv[0] == "systemctl" and a.argv[1] in ("stop", "start")):
                    raise SetupError(f"{title} failed", f"`{a.show().splitlines()[0][:160]}` said: {r.out.strip()[:240]}",
                                     ["Run the failing command by hand to see its full error and clear the cause, then run the same ct-setup transport command again: it carries on and skips what is already done.",
                                      "The job loop may be paused: the same command starts it again when it finishes, or run `systemctl start " + self.cfg.unit("fetch.timer") + " " + self.cfg.unit("step.timer") + "` to start it by hand."])
            self.say(STYLER.note("done", title))
        self.report.append(("transport", "done", f"{self.old} -> {self.to}"))
        return self.report


# ---- the client half: on your own computer ---------------------------------------------------------------------------------------------------------
def ensure_key(host, cfg2):
    """The key this computer uses for the mailbox (made here if missing). Returns the public key text."""
    key = transport.for_config(cfg2).client_key(cfg2)
    if not (host.exists(str(key)) and host.exists(str(key) + ".pub")):
        Path(host.path(key)).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        r = host.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ct-mailbox", "-f", str(host.path(key))])
        if not r.ok:
            raise SetupError("The mailbox key could not be made", r.out.strip()[-200:] or "ssh-keygen did not make it.", [f"Run: ssh-keygen -t ed25519 -N '' -f {key}", "Then run ct-setup transport direct again."])
    return (host.read(str(key) + ".pub") or "").strip()


def rewrite_local(host, cfg, cfg2, kind):
    """Your computer's configuration says the new connection, and the two clones fetch from and push to the mailbox. Returns [(step, status, detail)]."""
    notes = []
    host.write(cfg.source, with_transport(host.read(cfg.source) or "", kind), 0o600)
    notes.append(("configuration", "written", cfg.source))
    from ..client.repo import ClientRepos
    repos = ClientRepos.from_config(cfg2)
    for which, clone, url in (("queue", cfg2.path("client_queue"), cfg2.queue_url), ("results", cfg2.path("client_results"), cfg2.results_url)):
        if host.exists(clone + "/.git") or host.exists(clone + "/HEAD"):
            r = host.run(["git", "-C", clone, "remote", "set-url", "origin", url])
            notes.append((f"{which} clone", "pointed" if r.ok else "failed", url if r.ok else r.out.strip()[-160:]))
    return notes, repos


def transport_command(a, host, ask, ssh_factory, say, verify=None):
    """ct-setup transport KIND: from your computer. Preview, ask once, send the key, run the server half over ssh -t, then rewrite this computer's side and prove it."""
    from .. import ui as _ui
    from ..admin.ssh import Ssh, family_for
    from .install import deliver_source, remove_remote_dir
    from ..ui import GREEN, RED, YELLOW

    def emit(*x, **k):
        _ui.say(*x, **k)
    cfg = config.load()
    cfg.require("server.host", "server.admin_user")
    old, new = transport.for_config(cfg).kind, a.name
    if new not in transport.TRANSPORTS:
        emit("Say which connection to use", f"One of: {', '.join(transport.TRANSPORTS)}. The install uses {old} now.", tag="ERROR", color=RED, run="ct-setup transport direct")
        return 2
    if new == old:
        emit(f"The install already uses {old}", ("Configuration", cfg.source or "built-in defaults"), ("Server", cfg.server.host), "There is nothing to move.", tag="NOTE", color=YELLOW)
        return 0
    if new != "direct":
        emit("Moving back to GitHub is not done by this command yet", "Nothing was changed. Your GitHub repositories were never touched.",
             "Run ct-setup install and answer GitHub to the connection question: it sets the worker up for GitHub again.", tag="STOPPED", color=YELLOW, run="ct-setup install")
        return 1
    cfg2 = load_new_config(host.read(cfg.source) or "", new)
    ssh = (ssh_factory or (lambda h, u, p: Ssh(h, u, p, family=family_for(cfg))))(cfg.server.host, cfg.server.admin_user, cfg.server.ssh_port)
    pub = ensure_key(host, cfg2) if a.apply else None
    d, rev, _ = deliver_source(ssh, a.source, say=say)
    try:
        if pub:
            ssh.send(f"cat > {d}/mailbox-client.pub", (pub + "\n").encode())
        base = (f"env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={d}/src CT_CONFIG={cfg.paths.config}/ct.toml python3 -m ct.setup transport --here --to {new} --rev {rev} --source {d}"
                + (f" --mailbox-key-file {d}/mailbox-client.pub" if pub else "") + (" --no-sandbox-test" if getattr(a, "no_sandbox_test", False) else ""))
        r = ssh.capture(base + " --json")
        try:
            plan = json.loads((r.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            plan = None
        if r.returncode or plan is None:
            emit("The server could not plan the move", (r.stderr or r.stdout or "no answer").strip()[-600:], "Nothing was changed.", tag="ERROR", color=RED)
            return 1
        render_plan([(s["title"], s["lines"]) for s in plan["steps"]], old, new, where=f"on {cfg.server.host}")
        emit("On this computer", ("Configuration", f"{cfg.source}: the connection becomes {new}"), ("Mailbox key", f"{transport.for_config(cfg2).client_key(cfg2)} (made here if missing; its public half goes to the worker)"),
             ("The two clones", "point at the worker's mailbox"), ("GitHub", "not touched: your repositories and the worker's deploy keys stay as they are, as your way back"), tag="PREVIEW", color=_ui.PRIMARY)
        if not a.apply:
            emit("Nothing has been changed yet", tag="NEXT", color=GREEN, run_label="To do it", run=f"ct-setup transport {new} --apply")
            return 0
        if not a.yes:
            if not sys.stdin.isatty() or ask(_ui.confirm_prompt(f"Move {cfg.server.host} from {old} to {new}? The worker's job loop is briefly paused.")).strip().lower() != "y":
                emit("Not moved", "You answered no, or there was nobody to ask.", tag="STOPPED", color=YELLOW)
                return 1
        rc = ssh.interactive(f"sudo {base} --apply --yes")
    finally:
        remove_remote_dir(ssh, d)
    if rc:
        emit("The move stopped on the server", f"Exit {rc}. The step that failed is named in the card above.", ("Not changed", "your own configuration on this computer"),
             "Fix the cause, then run the same command again. It carries on from where it stopped.", tag="ERROR", color=RED, run_label="Carry on", run=f"ct-setup transport {new} --apply")
        return 1
    st = report_mod.Styler(styled=_ui._color_on(sys.stderr), stream=sys.stderr)
    notes, repos = rewrite_local(host, cfg, cfg2, new)
    for step, status, detail in notes:
        say(st.note(status, step, detail))
    bad = [n for n in notes if n[1] == "failed"]
    if bad:
        emit("This computer's clones were not pointed at the mailbox", *[f"{s}: {d_}" for s, _, d_ in bad], tag="ERROR", color=RED)
        return 1
    emit("Moved", f"{cfg.server.host} now passes jobs to your computer over SSH, with no GitHub in between.", ("Still on GitHub", f"{cfg.github.queue_repo} and {cfg.github.results_repo}, and the worker's two deploy keys: delete them there once Direct has worked for a while"),
         tag="DONE", color=GREEN, run_label="Check that it works", run=["ct-admin status", "ct-run submit sys-status --wait"])
    return 0
