"""cw-setup client: set up your own computer. Writes your cw.toml, makes sure SSH key login to the server works, creates the two
private GitHub repositories under your account, seeds the registry with the core classes, and clones both.

It never types a password and never touches your GitHub login: repository creation goes through the `gh` command you already signed in
to, and without it the exact steps are printed for you to do by hand.
"""
import shlex
from pathlib import Path

from .. import config
from . import questions, toml_out
from .server import SetupError


SEED_REGISTRY = ("# YOUR job classes, added with `cw-admin adopt` after you test a command with `cw-run offload profile`.\n"
                 "# The core classes come from the core pack; do not copy them here (a class name can only be defined once).\n"
                 "# Optional sections: limits (size limits) and profiling (what counts as a good fit for this machine).\n"
                 "classes:\n")


class ClientSetup:
    def __init__(self, host, home, *, ask=None, say=print, dry_run=True, given=None, use_defaults=False, repos_factory=None,
                 queue_key=None, results_key=None, ui=None):
        self.host, self.home = host, Path(home)
        self.ask, self.say, self.dry_run, self.given, self.use_defaults = ask, say, dry_run, given or {}, use_defaults
        self.repos_factory, self.queue_key, self.results_key, self.ui = repos_factory, queue_key, results_key, ui
        self.report, self.cfg = [], None

    def note(self, step, status, detail=""):
        self.report.append((step, status, detail))
        self.say(f"  {status.upper():<9} {step}" + (f"  ({detail})" if detail else ""))

    def run(self, argv, step, check=True):
        self.say("    $ " + shlex.join(argv))
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
            self.ui.begin(title="cw-setup client")
        nested, missing = questions.collect("client", ask=self.ask, say=self.say, given=self.given, use_defaults=self.use_defaults, ui=self.ui,
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
        self.say(f"\n  Your configuration, written to {path}:\n\n    " + toml_out.dump(nested).replace("\n", "\n    "))
        if not self.dry_run:
            self.host.write(path, toml_out.dump(nested), 0o600)
        self.note("configuration", "planned" if self.dry_run else "written", path)

    def config_path(self):
        return str(Path(self.host.env.get("HOME", "~")).expanduser() / ".config" / "claude-ballast" / "cw.toml")

    def ssh_login(self):
        cfg, target = self.cfg, f"{self.cfg.server.admin_user}@{self.cfg.server.host}"
        if self.dry_run:
            self.say("    $ ssh -o BatchMode=yes " + target + " true")
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

    def run_all(self):
        self.say("\nChecking this computer")
        self.prerequisites()
        self.say("\nConfiguration")
        self.configure()
        self.say("\nSSH to the server")
        self.ssh_login()
        self.say("\nPrivate repositories")
        self.create_repos()
        self.deploy_keys()
        self.say("\nClones and registry")
        self.clone_and_seed()
        return self.report
