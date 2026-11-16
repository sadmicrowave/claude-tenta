"""cw-setup client: set up your own computer. Writes your cw.toml, makes sure SSH key login to the server works, creates the two
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
from .server import SetupError

COMMANDS = ("cw-run", "cw-admin", "cw-setup", "cw-help")


SEED_REGISTRY = ("# YOUR job classes, added with `cw-admin adopt` after you test a command with `cw-run offload profile`.\n"
                 "# The core classes come from the core pack; do not copy them here (a class name can only be defined once).\n"
                 "# Optional sections: limits (size limits) and profiling (what counts as a good fit for this machine).\n"
                 "classes:\n")


class ClientSetup:
    def __init__(self, host, home, *, ask=None, say=print, dry_run=True, given=None, use_defaults=False, repos_factory=None,
                 queue_key=None, results_key=None, ui=None, advanced=False, confirm_card=None, scripts_dir="auto", interactive=None, styled=False, verbose=False):
        self.host, self.home = host, Path(home)
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
        """The folder holding the cw-* commands of the Python running this (a virtual environment's bin folder), or None when they are not installed there."""
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
            raise SetupError("Missing tools", ", ".join(missing) + " must be installed first", ["On a Mac: xcode-select --install"])
        self.gh = bool(self.host.which("gh"))
        self.note("tools", "ok", "git, ssh" + (", gh" if self.gh else "; gh not found, repository steps will be printed"))

    def configure(self):
        if self.ui is not None:
            self.ui.begin(title="client")
        nested, missing = questions.collect("client", ask=self.ask, say=self.say, given=self.given, use_defaults=self.use_defaults, ui=self.ui, advanced=self.advanced,
                                            confirm="Show what would be done?" if self.dry_run else "Set up this computer now?")
        if self.ui is not None:
            self.ui.finish()
        if missing:
            raise SetupError("Some answers are needed", "No default exists for: " + ", ".join(missing), ["Answer the questions, or pass them with --set section.key=value"])
        clean, problems = questions.validate_answers(nested)
        if problems:
            raise SetupError("The answers do not make a valid configuration", "\n".join(problems))
        self.cfg = config.Config(clean)
        path = self.config_path()
        if self.ui is not None and not self.dry_run:
            self.say(self.r.info(f"Your configuration is written to {path}. Settings you did not change keep their defaults; the file lists only your answers."))
        else:
            self.say(f"\n  Your configuration, written to {path}:\n\n    " + toml_out.dump(nested).replace("\n", "\n    "))
        if not self.dry_run:
            self.host.write(path, toml_out.dump(nested), 0o600)
        self.note("configuration", "planned" if self.dry_run else "written", path)

    def _sub(self, text):
        if self.ui is not None and hasattr(self.ui, "sub"):
            self.ui.sub(text)
        else:
            self.say(self.r.head(text))

    def config_path(self):
        return str(Path(self.host.env.get("HOME", "~")).expanduser() / ".config" / "claude-ballast" / "cw.toml")

    def ssh_login(self):
        cfg, target = self.cfg, f"{self.cfg.server.admin_user}@{self.cfg.server.host}"
        if self.dry_run:
            self.say("    $ ssh -o BatchMode=yes " + target + " true")                  # a preview shows its commands
            self.note("SSH key login", "planned", target)
            return
        r = self.host.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", str(cfg.server.ssh_port), target, "true"])
        if r.ok:
            self.note("SSH key login", "ok", target)
            return
        self.note("SSH key login", "waiting", "not working yet")
        self.say("\n  Key login to the server does not work yet. If you have no key: ssh-keygen -t ed25519")
        self.say(f"  Then install it (this asks for the server password once):\n\n    ssh-copy-id -p {cfg.server.ssh_port} {target}\n")

    def repo_slug(self, which):
        c = self.cfg.github
        return f"{c.owner}/{c.queue_repo if which == 'queue' else c.results_repo}"

    def create_repos(self):
        for which in ("queue", "results"):
            slug = self.repo_slug(which)
            if self.gh and self.host.run(["gh", "repo", "view", slug]).ok:
                self.note(f"{which} repository", "exists", slug)
            elif self.gh:
                self.run(["gh", "repo", "create", slug, "--private", "--description", f"claude-ballast {which} (private)"], f"create {slug}")
                self.note(f"{which} repository", "planned" if self.dry_run else "created", f"{slug} (private)")
            else:
                self.note(f"{which} repository", "manual", slug)
                self.say(f"    Create a PRIVATE repository named {slug} at https://github.com/new  (empty: no README, no licence)")

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

    def clone_and_seed(self):
        if self.dry_run:
            self.note("clone and seed the registry", "planned")
            return
        repos = self.repos_factory(self.cfg)
        try:
            for what, path in repos.clone_all(self.cfg.paths.client_home):
                self.note(f"clone: {Path(path).name}", what)
        except Exception as e:                          # a ClientError: the repositories are not there yet
            self.note("clone", "waiting", getattr(e, "why", None) or str(e))
            return
        repos.fetch_queue()
        if repos.remote_branch_exists(self.cfg.github.main_branch):
            self.note("registry", "exists", "left as it is")
        else:
            repos.push_files(self.cfg.github.main_branch, {"registry.yaml": SEED_REGISTRY}, "chore(registry): your own job classes start here")
            self.note("registry", "seeded", "empty: the core classes come from the core pack")

    def approval_key(self):
        """The key that signs approvals on this computer (see cw.approvals). Made once; it is what lets you approve jobs here without logging in to the worker.
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
        argv = ["ssh-keygen", "-q", "-t", "ed25519", "-C", "cw-approval", "-f", str(self.host.path(key))]
        if self.ask is not None or self.ui is not None:
            self.say("    This key signs the approvals you give from this computer. A passphrase protects it (you type it when you approve); press Enter for none.")
            rc, _ = self.interactive(argv)
            ok = rc == 0
        else:
            ok = self.host.run(argv + ["-N", ""]).ok
        if not ok or not self.host.exists(pub):
            self.note("approval key", "failed", "ssh-keygen did not make it; approvals will have to be given over SSH (cw-admin approve --ssh)")
            return
        self.note("approval key", "created", f"{key}")

    def commands_on_path(self):
        """Make `cw-run`, `cw-admin`, `cw-setup` and `cw-help` work from any new terminal, without typing where they live. They are linked into ~/.local/bin
        (never over a file that is already there), and if that folder is not on the PATH, one marked line is added to the shell's startup file, with permission."""
        src = self.scripts_dir
        if src is None:
            self.note("commands on your PATH", "skipped", "this Python has no cw-* commands next to it (it was not installed with pip)")
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
                self.say("    Already on your PATH: open a new terminal tab and cw-run, cw-admin, cw-setup and cw-help work.")
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
            self.say(f"    {link_dir} is not on your PATH. Add it in your shell's startup file:\n      {line}")
            return
        marker = "# added by cw-setup: the cw-* commands"
        current = self.host.read(rc) or ""
        if marker in current or str(link_dir) in current:
            self.say(f"    {rc} already mentions {link_dir}: open a new terminal tab.")
            return
        info = {"id": "path-line", "label": "Change", "tag": "edits your shell startup file", "title": f"Put {link_dir} on your PATH",
                "why": "The cw commands are linked into that folder, but it is not on your PATH, so a terminal would say \"command not found\".",
                "risk": f"Adds two lines to the end of {rc} (a comment saying where they came from, and the export). Nothing else in the file is changed. Skip it to add the line yourself.",
                "now": "not on the PATH", "steps": [f"echo '{line}' >> {rc}"], "lockout": False}
        if self.dry_run:
            self.say(f"    (would add to {rc}: {line})")
            return
        yes = self.confirm_card(info) if self.confirm_card else False
        if not yes:
            self.say(f"    Add this line to {rc} yourself, then open a new terminal tab:\n      {line}")
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
        self.commands_on_path()
        self._sub("SSH to the server")
        self.ssh_login()
        self._sub("Private repositories")
        self.create_repos()
        self.deploy_keys()
        self._sub("Clones and registry")
        self.clone_and_seed()
        return self.report
