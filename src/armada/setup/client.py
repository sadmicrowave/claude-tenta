"""arm-setup client: set up your own computer. Writes your armada.toml, makes sure SSH key login to the server works, creates the two
private GitHub repositories under your account, seeds the registry with the core classes, and clones both.

It never types a password and never touches your GitHub login: repository creation goes through the `gh` command you already signed in
to, and without it the exact steps are printed for you to do by hand.
"""
from . import refuse
import os
import shlex
import sys
from pathlib import Path

from .. import config
from . import ptyrun, questions, report, toml_out
from .. import transport
from .server import SetupError, missing_steps
from .report import sink as _sink

COMMANDS = ("arm-run", "arm-admin", "arm-setup", "arm-help")


SEED_REGISTRY = ("# YOUR job classes, added with `arm-admin adopt` after you test a command with `arm-run offload profile`.\n"
                 "# The core classes come from the core pack; do not copy them here (a class name can only be defined once).\n"
                 "# Optional sections: limits (size limits) and profiling (what counts as a good fit for this machine).\n"
                 "classes:\n")


ALSO_FOR_THE_CLIENT = ("network.ipv6",)       # asked on the server's side, but this computer's own configuration needs it too: with the worker's IPv6 off, ssh must not wait for addresses it no longer has


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
        """The folder holding the arm-* commands of the Python running this (a virtual environment's bin folder), or None when they are not installed there."""
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
            raise refuse.error("setup.command.failed", ("Step", step), command=shlex.join(argv), said=r.out.strip()[:300])
        return r

    def prerequisites(self):
        missing = [t for t in ("git", "ssh") if not self.host.which(t)]
        if missing:
            raise refuse.error("client.tools.missing", steps=["On a Mac: xcode-select --install, then open a new terminal window.", "On Linux: sudo apt install git openssh-client (or your distribution's equivalent), then run this again."], missing=", ".join(missing))
        self.gh = bool(self.host.which("gh"))
        self.note("tools", "ok", "git, ssh" + (", gh" if self.gh else "; gh not found (it is offered at the repository step)"))

    def configure(self):
        if self.ui is not None:
            self.ui.begin(title="client")
        nested, missing = questions.collect("client", ask=self.ask, say=self.say, given=self.given, use_defaults=self.use_defaults, ui=self.ui, advanced=self.advanced,
                                            confirm="Show what would be done?" if self.dry_run else "Set up this computer now?")
        if self.ui is not None:
            self.ui.finish()
        if missing:
            raise refuse.error("setup.answers.missing", steps=missing_steps(missing), missing=", ".join(missing))
        for key in ALSO_FOR_THE_CLIENT:                                      # a worker's setting that decides how THIS computer must connect to it
            if key in self.given:
                section, _, name = key.partition(".")
                nested.setdefault(section, {})[name] = self.given[key]
        clean, problems = questions.validate_answers(nested)
        if problems:
            raise refuse.error("setup.config.invalid", problems="\n".join(problems))
        self.cfg = config.Config(clean)
        path = self.config_path()
        from ..admin import workers as workers_mod
        hit = workers_mod.repo_conflict(self.cfg, path, Path(path).parent / "workers.toml")          # before anything is written or created: one private repository pair per worker, never shared
        if hit:
            raise refuse.error("worker.repo.shared", slug=hit[0], other=hit[1])
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
        return str(Path(self.host.env.get("HOME", "~")).expanduser() / ".config" / "armada" / "armada.toml")

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

    def check_owner(self):
        """The account the repositories are created under must be the one `gh` is signed in as, or an organisation it belongs to. Said BEFORE anything is created, with the account it IS signed in as
        (seen: the wizard was answered with the server's login name, and the first `gh repo create` died with a permissions error from GraphQL, after the configuration was already written)."""
        if not self.gh or self.dry_run:
            return
        me = self.host.run(["gh", "api", "user", "--jq", ".login"])
        if not me.ok or not me.out.strip():
            return                                                                      # not signed in or offline: the create step will say so
        login = me.out.strip()
        orgs = self.host.run(["gh", "api", "user/orgs", "--jq", ".[].login"])
        allowed = {login.lower()} | {o.strip().lower() for o in (orgs.out.splitlines() if orgs.ok else []) if o.strip()}
        if self.cfg.github.owner.lower() not in allowed:
            raise refuse.error("client.github.not_yours", steps=[f"Run arm-setup install again and answer {login} to the GitHub account question (it is your GitHub username, not the login on the server).",
                              "Or sign in as that account first: gh auth login"], login=login, owner=self.cfg.github.owner)
        self.note("GitHub account", "ok", f"signed in as {login}; repositories go under {self.cfg.github.owner}")

    GH_PAGE = "https://cli.github.com"

    def gh_install_command(self):
        """The command that installs gh with this computer's package manager, or None (the official page is then the way)."""
        for tool, argv in (("brew", ["brew", "install", "gh"]), ("apt-get", ["sudo", "apt-get", "install", "-y", "gh"]), ("dnf", ["sudo", "dnf", "install", "-y", "gh"])):
            if self.host.which(tool):
                return argv
        return None

    def github_tool(self):
        """The GitHub tool `gh` makes the repositories, so it must be installed and signed in. Each is offered (an amber card: yes does it, here, on this terminal); declining, or a run nobody
        is answering, prints the commands and the repository steps are done by hand, as they always could be."""
        if self.dry_run:
            return
        if not self.gh:
            argv = self.gh_install_command()
            info = {"id": "gh-install", "label": "Install", "tag": "installs a program on this computer", "title": "Install the GitHub tool (gh)",
                    "why": "The private repositories are made and wired up with GitHub's own command-line tool, gh. It is not installed on this computer.",
                    "risk": "Installs gh with " + (f"`{argv[0]}`" if argv else "your package manager") + ", from your system's own package source. " + ("It may ask for this computer's password (sudo). " if argv and argv[0] == "sudo" else "")
                    + "Skip it to install gh yourself, or to create the two repositories by hand instead.",
                    "now": "not installed", "steps": [shlex.join(argv)] if argv else [f"see {self.GH_PAGE} for your system"], "lockout": False}
            if argv and self.confirm_card and self.confirm_card(info):
                rc, text = self.interactive(argv)
                self.gh = bool(self.host.which("gh"))
                if not self.gh:
                    self.note("GitHub tool (gh)", "failed", f"`{shlex.join(argv)}` did not install it (exit {rc}): {str(text or '').strip()[-300:]}")
            if not self.gh:
                self.say(self.r.info(f"gh is needed to make the repositories for you. Install it ({self.GH_PAGE}), then run arm-setup install again:" if not argv else "To install gh yourself, then run arm-setup install again:"))
                self.say(self.r.command(shlex.join(argv) if argv else self.GH_PAGE))
                self.note("GitHub tool (gh)", "manual", "not installed: the repositories are created by hand (steps below)")
                return
            self.note("GitHub tool (gh)", "installed")
        if self.host.run(["gh", "auth", "status"]).ok:
            return
        login = ["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "ssh", "--web"]
        info = {"id": "gh-login", "label": "Sign in", "tag": "signs this computer in to GitHub", "title": "Sign in to GitHub",
                "why": "gh is installed but not signed in, so it cannot create the repositories under your account.",
                "risk": "Opens GitHub's own sign-in in your browser and shows a one-time code to type there. This tool never sees your password. Skip it to run `gh auth login` yourself, or to create the repositories by hand.",
                "now": "not signed in", "steps": [shlex.join(login)], "lockout": False}
        if self.confirm_card and self.confirm_card(info):
            self.interactive(login)
        if self.host.run(["gh", "auth", "status"]).ok:
            self.note("GitHub sign-in", "ok")
            return
        self.say(self.r.info("To sign in yourself, then run arm-setup install again:"))
        self.say(self.r.command("gh auth login"))
        self.gh = False                                                                 # the repository steps are printed instead, as when gh is missing
        self.note("GitHub sign-in", "manual", "not signed in: the repositories are created by hand (steps below)")

    def create_repos(self):
        self.github_tool()
        self.check_owner()
        for which in ("queue", "results"):
            slug = self.repo_slug(which)
            if self.gh and self.host.run(["gh", "repo", "view", slug]).ok:
                self.note(f"{which} repository", "exists", slug)
            elif self.gh:
                self.run(["gh", "repo", "create", slug, "--private", "--description", f"armada {which} (private)"], f"create {slug}")
                self.note(f"{which} repository", "planned" if self.dry_run else "created", f"{slug} (private)")
                self.run(["gh", "repo", "edit", slug, "--add-topic", "armada"], f"group {slug} with the fleet", check=False)          # a topic only groups the repositories on GitHub; nothing depends on it
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
                raise refuse.error("client.mailbox.unreadable", steps=list(getattr(e, "steps", None) or []) or ["Run arm-setup install again once the cause above is fixed."], why=why)
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
        if not self.host.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "arm-mailbox", "-f", str(self.host.path(key))]).ok:
            raise refuse.error("setup.mailbox_key.not_made", steps=[f"Run: ssh-keygen -t ed25519 -N '' -f {key}", "Then run arm-setup install again."], reason="ssh-keygen did not make it.")
        self.note("mailbox key", "created", f"{key}")

    def approval_key(self):
        """The key that signs approvals on this computer (see armada.approvals). Made once; it is what lets you approve jobs here without logging in to the worker.
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
        argv = ["ssh-keygen", "-q", "-t", "ed25519", "-C", "arm-approval", "-f", str(self.host.path(key))]
        if self.ask is not None or self.ui is not None:
            self.say(self.r.info("This key signs the approvals you give from this computer. A passphrase protects it (you type it when you approve); press Enter for none."))
            rc, _ = self.interactive(argv)
            ok = rc == 0
        else:
            ok = self.host.run(argv + ["-N", ""]).ok
        if not ok or not self.host.exists(pub):
            self.note("approval key", "failed", "ssh-keygen did not make it; approvals will have to be given over SSH (arm-admin approve --ssh)")
            return
        self.note("approval key", "created", f"{key}")

    def commands_on_path(self):
        """Make `arm-run`, `arm-admin`, `arm-setup` and `arm-help` work from any new terminal, without typing where they live. They are linked into ~/.local/bin
        (never over a file that is already there), and if that folder is not on the PATH, one marked line is added to the shell's startup file, with permission."""
        src = self.scripts_dir
        if src is None:
            self.note("commands on your PATH", "skipped", "this Python has no arm-* commands next to it (it was not installed with pip)")
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
                self.say(self.r.info("Already on your PATH: open a new terminal tab and arm-run, arm-admin, arm-setup and arm-help work."))
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
        marker = "# added by arm-setup: the arm-* commands"
        current = self.host.read(rc) or ""
        if marker in current or str(link_dir) in current:
            self.say(self.r.info(f"{rc} already mentions {link_dir}: open a new terminal tab."))
            return
        info = {"id": "path-line", "label": "Change", "tag": "edits your shell startup file", "title": f"Put {link_dir} on your PATH",
                "why": "The armada commands are linked into that folder, but it is not on your PATH, so a terminal would say \"command not found\".",
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
