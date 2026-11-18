"""ct-setup client: set up your own computer. Writes your ct.toml, makes sure SSH key login to the server works, creates the two
private GitHub repositories under your account, seeds the registry with the core classes, and clones both.

It never types a password and never touches your GitHub login: repository creation goes through the `gh` command you already signed in
to, and without it the exact steps are printed for you to do by hand.
"""
import os
import shlex
import sys
from pathlib import Path

from .. import config
from . import ptyrun, questions, report, toml_out
from .. import transport
from . import legacy
from .server import SetupError, missing_steps
from .report import sink as _sink

COMMANDS = ("ct-run", "ct-admin", "ct-setup", "ct-help")


SEED_REGISTRY = ("# YOUR job classes, added with `ct-admin adopt` after you test a command with `ct-run offload profile`.\n"
                 "# The core classes come from the core pack; do not copy them here (a class name can only be defined once).\n"
                 "# Optional sections: limits (size limits) and profiling (what counts as a good fit for this machine).\n"
                 "classes:\n")


class ClientSetup:
    def __init__(self, host, home, *, ask=None, say=None, dry_run=True, given=None, use_defaults=False, repos_factory=None,
                 queue_key=None, results_key=None, ui=None, advanced=False, confirm_card=None, scripts_dir="auto", interactive=None, styled=False, verbose=False, config_path=None, targeted=False):
        say = say or _sink()
        self.host, self.home = host, Path(home)
        self.config_override, self.targeted = config_path, targeted          # targeted: one more worker; its own configuration file, nothing of this computer's other installs is touched
        self.ask, self.say, self.dry_run, self.given, self.use_defaults = ask, say, dry_run, given or {}, use_defaults
        self.repos_factory, self.queue_key, self.results_key, self.ui = repos_factory, queue_key, results_key, ui
        self.advanced = advanced
        self.interactive = interactive or ptyrun.run_capture          # runs a command on the person's own terminal (so a passphrase can be typed)
        self.r = report.Styler(styled=styled or ui is not None, verbose=verbose)
        self.confirm_card = confirm_card                    # info -> bool: show a card (or question) asking permission to change something outside this tool
        self.scripts_dir = self.find_scripts_dir() if scripts_dir == "auto" else scripts_dir
        self.report, self.cfg = [], None

    @staticmethod
    def find_scripts_dir():
        """The folder holding the ct-* commands of the Python running this (a virtual environment's bin folder), or None when they are not installed there."""
        d = Path(sys.executable).parent
        return d if all((d / c).exists() for c in COMMANDS) else None

    def note(self, step, status, detail=""):
        self.report.append((step, status, detail))
        self.say(self.r.note(status, step, detail))

    def run(self, argv, step, check=True):
        line = self.r.cmd(shlex.join(argv)) if not self.dry_run else "    $ " + shlex.join(argv)
        if line:
            self.say(line)
        if self.dry_run:
            from .host import Result
            return Result(0)
        r = self.host.run(argv)
        if check and not r.ok:
            raise SetupError(f"{step} failed", f"`{shlex.join(argv)}` said: {r.out.strip()[:300]}")
        return r

    def prerequisites(self):
        missing = [t for t in ("git", "ssh") if not self.host.which(t)]
        if missing:
            raise SetupError("Missing tools", ", ".join(missing) + " must be installed first", ["On a Mac: xcode-select --install, then open a new terminal window.", "On Linux: sudo apt install git openssh-client (or your distribution's equivalent), then run this again."])
        self.gh = bool(self.host.which("gh"))
        self.note("tools", "ok", "git, ssh" + (", gh" if self.gh else "; gh not found, repository steps will be printed"))
        for what, fix in legacy.find(self.host):                           # nothing is removed here: these are the person's shell and Claude Code settings
            self.note("old install leftover", "found", f"{what}. Clear it: {fix}")

    def configure(self):
        if self.ui is not None:
            self.ui.begin(title="client")
        nested, missing = questions.collect("client", ask=self.ask, say=self.say, given=self.given, use_defaults=self.use_defaults, ui=self.ui, advanced=self.advanced,
                                            confirm="Show what would be done?" if self.dry_run else "Set up this computer now?")
        if self.ui is not None:
            self.ui.finish()
        if missing:
            raise SetupError("Some answers are needed", "No default exists for: " + ", ".join(missing), missing_steps(missing))
        clean, problems = questions.validate_answers(nested)
        if problems:
            raise SetupError("The answers do not make a valid configuration", "\n".join(problems))
        self.cfg = config.Config(clean)
        path = self.config_path()
        if self.ui is not None and not self.dry_run:
            self.say(self.r.info(f"Your configuration is written to {path}. Settings you did not change keep their defaults; the file lists only your answers."))
        else:
            self.say(self.r.kv("Configuration", path))
            self.say(self.r.block(toml_out.dump(nested).splitlines()))
        if not self.dry_run:
            self.host.write(path, toml_out.dump(nested), 0o600)
        self.note("configuration", "planned" if self.dry_run else "written", path)

    def _sub(self, text):
        if self.ui is not None and hasattr(self.ui, "sub"):
            self.ui.sub(text)
        else:
            self.say(self.r.head(text))

    def config_path(self):
        if self.config_override:
            return str(self.config_override)
        return str(Path(self.host.env.get("HOME", "~")).expanduser() / ".config" / "claude-tenta" / "ct.toml")

    def ssh_login(self):
        cfg, target = self.cfg, f"{self.cfg.server.admin_user}@{self.cfg.server.host}"
        if self.dry_run:
            self.say(self.r.info("$ ssh -o BatchMode=yes " + target + " true"))                  # a preview shows its commands
            self.note("SSH key login", "planned", target)
            return
        r = self.host.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", str(cfg.server.ssh_port), target, "true"])
        if r.ok:
            self.note("SSH key login", "ok", target)
            return
        self.note("SSH key login", "waiting", "not working yet")
        self.say(self.r.warn("Key login to the server does not work yet. If you have no key, make one:"))
        self.say(self.r.command("ssh-keygen -t ed25519"))
        self.say(self.r.info("Then install it (this asks for the server password once):"))
        self.say(self.r.command(f"ssh-copy-id -p {cfg.server.ssh_port} {target}"))

    def repo_slug(self, which):
        c = self.cfg.github
        return f"{c.owner}/{c.queue_repo if which == 'queue' else c.results_repo}"

    def create_repos(self):
        for which in ("queue", "results"):
            slug = self.repo_slug(which)
            if self.gh and self.host.run(["gh", "repo", "view", slug]).ok:
                self.note(f"{which} repository", "exists", slug)
            elif self.gh:
                self.run(["gh", "repo", "create", slug, "--private", "--description", f"claude-tenta {which} (private)"], f"create {slug}")
                self.note(f"{which} repository", "planned" if self.dry_run else "created", f"{slug} (private)")
            else:
                self.note(f"{which} repository", "manual", slug)
                self.say(self.r.info(f"Create a PRIVATE repository named {slug} at https://github.com/new  (empty: no README, no licence)"))

    def deploy_keys(self):
        """Add the server's two public keys when they were handed over (--queue-key / --results-key); otherwise say how."""
        for which, key, rw in (("queue", self.queue_key, False), ("results", self.results_key, True)):
            if not key:
                continue
            if not self.gh:
                self.note(f"{which} deploy key", "manual", f"add {key} to {self.repo_slug(which)} at github.com (Settings, Deploy keys)" + (", with write access" if rw else ""))
                continue
            self.run(["gh", "repo", "deploy-key", "add", str(key), "-R", self.repo_slug(which), "-t", f"{self.cfg.identity.name}-server", *(["--allow-write"] if rw else [])],
                     f"add the {which} deploy key")
            self.note(f"{which} deploy key", "planned" if self.dry_run else "added", "read and write" if rw else "read-only")

    def clone_and_seed(self, strict=False):
        if self.dry_run:
            self.note("clone and seed the registry", "planned")
            return
        repos = self.repos_factory(self.cfg)
        try:
            for what, path in repos.clone_all(self.cfg.paths.client_home):
                self.note(f"clone: {Path(path).name}", what)
        except Exception as e:                          # a ClientError: the repositories are not there yet
            why = getattr(e, "why", None) or str(e)
            self.note("clone", "waiting", why)
            if strict:                                   # Direct: the mailbox is already made, so nothing is "yet to come": this computer cannot read it
                raise SetupError("This computer cannot read the worker's mailbox", why, list(getattr(e, "steps", None) or []) or ["Run ct-setup install again once the cause above is fixed."])
            return
        repos.fetch_queue()
        if repos.remote_branch_exists(self.cfg.github.main_branch):
            self.note("registry", "exists", "left as it is")
        else:
            repos.push_files(self.cfg.github.main_branch, {"registry.yaml": SEED_REGISTRY}, "chore(registry): your own job classes start here")
            self.note("registry", "seeded", "empty: the core classes come from the core pack")

    @property
    def direct(self):
        return transport.for_config(self.cfg).kind == "direct"

    def mailbox_key(self):
        """The key this computer uses for the Direct mailbox on the worker. It has no passphrase (git uses it unattended), no other use, and the worker's mailbox account accepts
        only it, and only to run git on the two repositories."""
        key = transport.for_config(self.cfg).client_key(self.cfg)
        if self.host.exists(key) and self.host.exists(str(key) + ".pub"):
            self.note("mailbox key", "ok", f"{key}")
            return
        if self.dry_run:
            self.note("mailbox key", "planned", f"{key}")
            return
        os.makedirs(self.host.path(key.parent), mode=0o700, exist_ok=True)
        if not self.host.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ct-mailbox", "-f", str(self.host.path(key))]).ok:
            raise SetupError("The mailbox key could not be made", "ssh-keygen did not make it.", [f"Run: ssh-keygen -t ed25519 -N '' -f {key}", "Then run ct-setup install again."])
        self.note("mailbox key", "created", f"{key}")

    def approval_key(self):
        """The key that signs approvals on this computer (see ct.approvals). Made once; it is what lets you approve jobs here without logging in to the worker.
        On a terminal ssh-keygen asks for a passphrase (Enter for none), which then protects every approval; otherwise the key has none."""
        d = self.home / "approval"
        key, pub = d / "key", d / "key.pub"
        if self.host.exists(key) and self.host.exists(pub):
            self.note("approval key", "ok", f"{key}")
            return
        if self.dry_run:
            self.note("approval key", "planned", f"{key}")
            return
        os.makedirs(self.host.path(d), mode=0o700, exist_ok=True)
        argv = ["ssh-keygen", "-q", "-t", "ed25519", "-C", "ct-approval", "-f", str(self.host.path(key))]
        if self.ask is not None or self.ui is not None:
            self.say(self.r.info("This key signs the approvals you give from this computer. A passphrase protects it (you type it when you approve); press Enter for none."))
            rc, _ = self.interactive(argv)
            ok = rc == 0
        else:
            ok = self.host.run(argv + ["-N", ""]).ok
        if not ok or not self.host.exists(pub):
            self.note("approval key", "failed", "ssh-keygen did not make it; approvals will have to be given over SSH (ct-admin approve --ssh)")
            return
        self.note("approval key", "created", f"{key}")

    def commands_on_path(self):
        """Make `ct-run`, `ct-admin`, `ct-setup` and `ct-help` work from any new terminal, without typing where they live. They are linked into ~/.local/bin
        (never over a file that is already there), and if that folder is not on the PATH, one marked line is added to the shell's startup file, with permission."""
        src = self.scripts_dir
        if src is None:
            self.note("commands on your PATH", "skipped", "this Python has no ct-* commands next to it (it was not installed with pip)")
            return
        env = self.host.env
        home = Path(env.get("HOME", "~")).expanduser()
        link_dir = home / ".local" / "bin"
        if all(self.on_path(c, src / c) for c in COMMANDS):
            self.note("commands on your PATH", "ok", "already available in any terminal")
            return
        kept = []
        for c in COMMANDS:
            link, target = link_dir / c, src / c
            here = self.host.link_target(link)
            if here is not None and here == os.path.realpath(target):
                continue
            if here is not None:                                                   # something else is there: never overwritten
                kept.append(c)
                continue
            line = self.r.cmd(f"ln -s {target} {link}")
            if line:
                self.say(line)
            if not self.dry_run:
                self.host.symlink(link, target)
        if kept:
            self.note("commands on your PATH", "partial", f"{link_dir}/{kept[0]} already exists and is something else, so it was left alone" + ("" if len(kept) == 1 else f" (and {len(kept) - 1} more)")
                      + f"; remove it to use this one: rm {link_dir}/{kept[0]}")
        else:
            self.note("commands on your PATH", "planned" if self.dry_run else "linked", f"{link_dir}")
        if str(link_dir) in env.get("PATH", "").split(":"):
            if not self.dry_run:
                self.say(self.r.info("Already on your PATH: open a new terminal tab and ct-run, ct-admin, ct-setup and ct-help work."))
            return
        self.path_line(link_dir, home)

    def on_path(self, command, target):
        for d in self.host.env.get("PATH", "").split(":"):
            found = self.host.link_target(Path(d) / command) if d else None
            if found:
                return found == os.path.realpath(target)
        return False

    def path_line(self, link_dir, home):
        shell = Path(self.host.env.get("SHELL", "")).name
        rc = home / {"zsh": ".zshrc", "bash": ".bashrc", "fish": ".config/fish/config.fish"}.get(shell, "")
        line = f'export PATH="{link_dir}:$PATH"' if shell != "fish" else f"fish_add_path {link_dir}"
        if shell not in ("zsh", "bash", "fish"):
            self.say(self.r.info(f"{link_dir} is not on your PATH. Add it in your shell's startup file:"))
            self.say(self.r.command(line))
            return
        marker = "# added by ct-setup: the ct-* commands"
        current = self.host.read(rc) or ""
        if marker in current or str(link_dir) in current:
            self.say(self.r.info(f"{rc} already mentions {link_dir}: open a new terminal tab."))
            return
        info = {"id": "path-line", "label": "Change", "tag": "edits your shell startup file", "title": f"Put {link_dir} on your PATH",
                "why": "The ct commands are linked into that folder, but it is not on your PATH, so a terminal would say \"command not found\".",
                "risk": f"Adds two lines to the end of {rc} (a comment saying where they came from, and the export). Nothing else in the file is changed. Skip it to add the line yourself.",
                "now": "not on the PATH", "steps": [f"echo '{line}' >> {rc}"], "lockout": False}
        if self.dry_run:
            self.say(self.r.info(f"(would add to {rc}: {line})"))
            return
        yes = self.confirm_card(info) if self.confirm_card else False
        if not yes:
            self.say(self.r.info(f"Add this line to {rc} yourself, then open a new terminal tab:"))
            self.say(self.r.command(line))
            return
        mode = (self.host.path(rc).stat().st_mode & 0o777) if self.host.exists(rc) else 0o644
        self.host.write(rc, current + ("" if current.endswith("\n") or not current else "\n") + f"{marker}\n{line}\n", mode)
        self.note("shell startup file", "updated", f"{rc}: open a new terminal tab to use the commands")

    def run_all(self):
        self._sub("Checking this computer")
        self.prerequisites()
        self._sub("Configuration")
        self.configure()
        self._sub("Approval key")
        self.approval_key()
        self._sub("Commands on your PATH")
        if self.targeted:
            self.note("commands on your PATH", "ok", "already set up by the first install; left as they are")
        else:
            self.commands_on_path()
        if self.direct:
            self._sub("Mailbox key")
            self.mailbox_key()
        self._sub("SSH to the server")
        self.ssh_login()
        if self.direct:                         # the mailbox is on the worker: nothing exists to clone until the worker's half has made it (the installer calls finish_direct then)
            self.note("repositories", "later", "the worker makes them in its half")
            return self.report
        self._sub("Private repositories")
        self.create_repos()
        self.deploy_keys()
        self._sub("Clones and registry")
        self.clone_and_seed()
        return self.report

    def finish_direct(self):
        """Direct: after the worker made its mailbox, clone the two repositories over SSH and seed the registry."""
        self._sub("Clones and registry")
        self.clone_and_seed(strict=True)
        return self.report
