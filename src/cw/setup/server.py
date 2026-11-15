"""cw-setup server: turn a clean Ubuntu or Debian machine into a worker, converging to the configuration.

Run as root on the server. Every step prints what it will do; --dry-run changes nothing. Steps are safe to repeat: each looks at the
machine first and does only what is missing. The order is fixed so that nothing is ever half-secured: accounts and folders, then code,
then the repository keys, then the hardening modules, then a check.
"""
import json
import shlex
import subprocess
from pathlib import Path

from .. import config, render, update
from . import detect as detect_mod
from . import hardening, questions, toml_out


class SetupError(Exception):
    def __init__(self, title, why=None, steps=None):
        super().__init__(title)
        self.title, self.why, self.steps = title, why, steps or []


def alias_for(cfg, which):
    """The ssh host alias a clone uses, so each repository has its own deploy key (GitHub allows a key on one repository only)."""
    return f"github-{cfg.identity.name}-{which}"


def remote_url(cfg, which):
    repo = cfg.github.queue_repo if which == "queue" else cfg.github.results_repo
    return f"git@{alias_for(cfg, which)}:{cfg.github.owner}/{repo}.git"


def ssh_config_text(cfg):
    keydir = f"{cfg.paths.worker_home}/.ssh"
    out = ["# Written by cw-setup: one deploy key per repository (GitHub allows a key on one repository only).\n"]
    for which in ("queue", "results"):
        out.append(f"Host {alias_for(cfg, which)}\n  HostName github.com\n  User git\n  IdentityFile {keydir}/id_{which}\n  IdentitiesOnly yes\n"
                   "  StrictHostKeyChecking accept-new\n")
    return "\n".join(out)


def deploy_key_commands(cfg, pubkeys):
    """What the person runs on their own computer to add the two keys (queue read-only, results with write)."""
    rows = []
    for which, repo, rw in (("queue", cfg.github.queue_repo, False), ("results", cfg.github.results_repo, True)):
        f = f"{cfg.identity.name}-{which}.pub"
        rows.append((f, pubkeys.get(which, ""), f"gh repo deploy-key add {f} -R {cfg.github.owner}/{repo} -t {cfg.identity.name}-server" + (" --allow-write" if rw else "")))
    return rows


class ServerSetup:
    def __init__(self, host, source, *, ask=None, say=print, confirm=None, dry_run=True, given=None, use_defaults=False, rev=None,
                 systemctl=None, sysctl=None, sandbox=None, is_root=None, install=update.install_release, registry_installer=update.install_registry,
                 install_packages=None):
        self.host, self.source, self.ask, self.say, self.confirm = host, Path(source), ask, say, confirm
        self.dry_run, self.given, self.use_defaults, self.rev = dry_run, given or {}, use_defaults, rev
        self.systemctl, self.sysctl, self.sandbox, self.install = systemctl, sysctl, sandbox, install
        self.registry_installer, self.install_packages = registry_installer, install_packages
        self.is_root = is_root if is_root is not None else (lambda: __import__("os").geteuid() == 0)
        self.report = []                               # [(step, status, detail)]
        self.cfg = None

    # ---- helpers ----
    def note(self, step, status, detail=""):
        self.report.append((step, status, detail))
        self.say(f"  {status.upper():<9} {step}" + (f"  ({detail})" if detail else ""))

    def run(self, argv, *, step, as_user=None):
        """Show it, then (unless dry run) run it. Returns the Result; a failure raises SetupError naming the command."""
        argv = ["runuser", "-u", as_user, "--", *argv] if as_user else list(argv)
        self.say("    $ " + shlex.join(argv))
        if self.dry_run:
            from .host import Result
            return Result(0)
        r = self.host.run(argv)
        if not r.ok:
            raise SetupError(f"{step} failed", f"`{shlex.join(argv)}` said: {r.out.strip()[:300]}")
        return r

    # ---- steps ----
    def preflight(self):
        facts = detect_mod.detect(self.host)
        for what, value in facts.summary():
            self.say(f"  {what:<22} {value}")
        problem = facts.os_problem()
        if problem:
            raise SetupError("This machine is not supported", problem)
        if not self.dry_run and not self.is_root():
            raise SetupError("Run this as root", "It creates accounts and installs system files.", ["sudo python3 -m cw.setup server"])
        if not self.host.run(["python3", "-c", "import yaml"]).ok:
            raise SetupError("PyYAML is missing", "The worker reads its job registry with it.", ["sudo apt-get install -y python3-yaml"])
        self.note("checked the machine", "ok", f"{facts.os_id} {facts.os_version}")
        return facts

    def configure(self, facts):
        nested, missing = questions.collect("server", ask=self.ask, say=self.say, given=self.given, suggestions=facts.suggestions(), use_defaults=self.use_defaults)
        if missing:
            raise SetupError("Some answers are needed", "No default exists for: " + ", ".join(missing), ["Answer the questions, or pass them with --set section.key=value"])
        clean, problems = questions.validate_answers(nested)
        if problems:
            raise SetupError("The answers do not make a valid configuration", "\n".join(problems))
        unknown = [m for m in clean["hardening"]["modules"] if m not in hardening.MODULES]
        if unknown:
            raise SetupError("Unknown hardening module", ", ".join(unknown), [f"Known: {', '.join(hardening.MODULES)}"])
        self.cfg = config.Config(clean)
        text = toml_out.dump(nested)
        path = self.cfg.path("config_file")
        self.say(f"\n  The configuration, written to {path}:\n")
        self.say("    " + text.replace("\n", "\n    "))
        if not self.dry_run:
            self.host.write(path, text, 0o644)
        self.note("configuration", "planned" if self.dry_run else "written", path)

    def accounts_and_folders(self):
        cfg = self.cfg
        if not self.host.run(["id", cfg.server.admin_user]).ok and not self.dry_run:
            raise SetupError(f"The account {cfg.server.admin_user!r} does not exist", "It is the account you log in as; create it first.", [f"sudo adduser {cfg.server.admin_user}"])
        if not self.host.run(["id", cfg.server.worker_user]).ok:
            self.run(["useradd", "--system", "--create-home", "--home-dir", cfg.paths.worker_home, "--shell", "/usr/sbin/nologin", cfg.server.worker_user], step="create the worker account")
            self.note("worker account", "created", cfg.server.worker_user)
        else:
            self.note("worker account", "exists", cfg.server.worker_user)
        w = cfg.server.worker_user
        for path, mode, owner, group in ((cfg.paths.code, "755", "root", "root"), (cfg.paths.config, "755", "root", "root"),
                                         (cfg.paths.state_root, "750", w, w), (f"{cfg.paths.worker_home}/.ssh", "700", w, w)):
            self.run(["install", "-d", "-m", mode, "-o", owner, "-g", group, path], step="create folders")
        secrets = cfg.path("secrets")
        if not self.host.exists(secrets):
            self.say(f"    (create {secrets}, empty, owner root, group {w}, mode 640)")
            if not self.dry_run:
                self.host.write(secrets, "# Secrets only (API keys, alert tokens). Never put these in cw.toml.\n", 0o640)
                self.host.run(["chown", f"root:{w}", secrets])
        self.note("folders", "ok")

    def install_code(self):
        cfg = self.cfg
        rev = self.rev or self.host.run(["git", "-C", str(self.source), "rev-parse", "HEAD"]).out.strip()
        if not update.REV_RE.match(rev):
            raise SetupError("Cannot tell which release this is", f"{self.source} is not a git checkout; pass --rev <commit> if you know it.")
        self.say(f"    install release {rev[:9]} from {self.source} into {cfg.paths.code}")
        if self.dry_run:
            files = update.desired_files(self.source, cfg)
            self.note("code, units and wrappers", "planned", f"{len(files)} files")
            return
        report = self.install(self.source, cfg, rev, prefix=str(self.host.root), sandbox=self.sandbox, systemctl=self.systemctl, sysctl=self.sysctl, out=self.say)
        if not report.ok:
            raise SetupError("The install was refused", "; ".join(report.errors))
        self.note("code, units and wrappers", "installed", f"{len(report.updated)} files")

    def deploy_keys(self):
        cfg, w = self.cfg, self.cfg.server.worker_user
        keydir, pubs = f"{cfg.paths.worker_home}/.ssh", {}
        for which in ("queue", "results"):
            key = f"{keydir}/id_{which}"
            if not self.host.exists(key):
                self.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{cfg.identity.name}-{which}", "-f", key], step="create a deploy key", as_user=w)
            pubs[which] = (self.host.read(key + ".pub") or "").strip()
        if not self.dry_run:
            self.host.write(f"{keydir}/config", ssh_config_text(cfg), 0o600)
            self.host.run(["chown", f"{w}:{w}", f"{keydir}/config"])
        self.note("repository keys", "ready")
        return pubs

    def clones(self, pubs):
        cfg, w = self.cfg, self.cfg.server.worker_user
        pending = False
        for which, dest in (("queue", cfg.path("queue_clone")), ("results", cfg.path("results_clone"))):
            if self.host.exists(dest + "/.git"):
                self.note(f"{which} clone", "exists")
                continue
            test = ["git", "ls-remote", "--exit-code", remote_url(cfg, which)]
            if self.dry_run or not self.host.run(["runuser", "-u", w, "--", *test]).ok:
                if not self.dry_run:
                    pending = True
                    continue
            if not self.dry_run:
                self.run(["git", "clone", "--quiet", remote_url(cfg, which), dest], step=f"clone the {which} repository", as_user=w)
            self.note(f"{which} clone", "planned" if self.dry_run else "cloned")
        if pending:
            self.say("\n  The server cannot reach your repositories yet. Add its two public keys on your own computer (they are shown below),")
            self.say("  then run this command again; it continues from here.\n")
            for fname, pub, cmd in deploy_key_commands(cfg, pubs):
                self.say(f"    echo '{pub}' > {fname}\n    {cmd}\n")
            self.note("repository clones", "waiting", "add the deploy keys, then run again")
        return pending

    def registry_step(self):
        """Install the job registry the worker will trust (the queue repository's, validated; the core pack on a brand-new install)."""
        if self.dry_run:
            self.say("    install the job registry from the queue repository (validated first), or the core pack on a fresh install")
            self.note("job registry", "planned")
            return
        try:
            result = self.registry_installer(self.cfg, self.source, run=lambda argv: (lambda r: (r.rc, r.out))(self.host.run(argv)), prefix=str(self.host.root),
                                             install_packages=self.install_packages, out=self.say)
        except update.InstallError as e:
            raise SetupError("The job registry was not installed", str(e), ["Fix registry.yaml on the main branch of your queue repository, then run this again"])
        self.note("job registry", result)

    def hardening_step(self):
        cfg, results = self.cfg, {}
        for mid in cfg.hardening.modules:
            mod = hardening.MODULES[mid]
            status, detail = hardening.run_module(mod, self.host, cfg, dry_run=self.dry_run, out=self.say, confirm=self.confirm)
            results[mid] = {"status": status, "detail": detail}
            self.note(f"hardening: {mid}", status, detail)
        if not self.dry_run:
            self.host.write(f"{cfg.paths.config}/hardening.json", json.dumps(results, indent=2, sort_keys=True) + "\n", 0o644)
        return results

    def start_timers(self):
        cfg = self.cfg
        timers = [cfg.unit("fetch.timer"), cfg.unit("step.timer")] + ([cfg.unit("update.timer")] if cfg.updates.mode == "signed-tags" else [])
        for t in timers:
            self.run(["systemctl", "enable", "--now", t], step="start the timers")
        self.note("timers", "planned" if self.dry_run else "running", ", ".join(timers))

    def run_all(self):
        """Returns the report. Raises SetupError for anything that must stop the wizard (the report so far is in self.report)."""
        self.say("\nChecking this machine")
        facts = self.preflight()
        self.say("\nConfiguration")
        self.configure(facts)
        self.say("\nAccounts and folders")
        self.accounts_and_folders()
        self.say("\nCode")
        self.install_code()
        self.say("\nRepository access")
        pubs = self.deploy_keys()
        if self.clones(pubs):
            return self.report
        self.say("\nJob registry")
        self.registry_step()
        self.say("\nHardening")
        self.hardening_step()
        self.say("\nStarting")
        self.start_timers()
        return self.report
