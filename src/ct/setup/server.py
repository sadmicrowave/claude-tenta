"""ct-setup server: turn a clean Ubuntu or Debian machine into a worker, converging to the configuration.

Run as root on the server. Every step prints what it will do; --dry-run changes nothing. Steps are safe to repeat: each looks at the
machine first and does only what is missing. The order is fixed so that nothing is ever half-secured: accounts and folders, then code,
then the repository keys, then the hardening modules, then a check.
"""
import json
import re
import shlex
import subprocess
import time
from pathlib import Path

from .. import approvals, claudeupdate, config, render, transport, update
from . import detect as detect_mod
from . import hardening, questions, report, toml_out
from .report import sink as _sink


class SetupError(Exception):
    def __init__(self, title, why=None, steps=None):
        super().__init__(title)
        self.title, self.why, self.steps = title, why, steps or []


def missing_steps(missing):
    """What to do when answers have no default: answer when asked, or give that answer on the command line."""
    first = sorted(missing)[0] if missing else "section.key"
    return ["Run ct-setup install again and answer each question that is asked.", f"Or give the answer on the command line, for example: ct-setup install --set {first}=<value>"]


EXIT_WAITING = 10          # the server half stopped because the repositories cannot be reached yet (the deploy keys are not added); run it again to continue
CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"          # Anthropic's documented installer for Linux; it needs no root
CLAUDE_LEGACY = "2.1.112"          # the last release that is plain JavaScript (runs on Node, on processors the native program cannot run on); later ones are native only
CLAUDE_QUESTIONS = {
    "account": {"key": "claude.token", "title": "Paste your Claude token",
                "why": "On your own computer, open a terminal and run `claude setup-token`. Sign in in the browser it opens, then copy the long token it prints and paste it here. It lasts about a year.",
                "risk": "Anyone with this token can use your Claude account. It is stored only on this worker, readable by root and the worker account, and given only to Claude jobs.",
                "examples": ["a long single line starting with sk-ant-"]},
    "api-key": {"key": "claude.token", "title": "Paste your Anthropic API key",
                "why": "Create one in the Anthropic Console (API keys) and paste it here. Jobs that use Claude are billed to that account per use.",
                "risk": "Anyone with this key can spend on your API account. It is stored only on this worker, readable by root and the worker account, and given only to Claude jobs.",
                "examples": ["a long single line starting with sk-ant-"]},
}
PREREQUISITES = (("python3-yaml", ["python3", "-c", "import yaml"]), ("git", ["git", "--version"]))


def read_secret(ui, q):
    """One hidden answer: on the terminal screen when there is one, else hidden at a plain terminal, else '' (nobody to ask)."""
    if ui is not None:
        return ui.ask_secret(q)
    import sys
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass(f"  {q['title']} (hidden; Enter to skip): ").strip()
    return ""


def alias_for(cfg, which):
    return transport.for_config(cfg).alias(cfg, which)


def remote_url(cfg, which):
    """The address of the worker's own clone: what the transport of the configuration says."""
    return transport.for_config(cfg).worker_url(cfg, which)


def ssh_config_text(cfg):
    return transport.for_config(cfg).worker_ssh_config(cfg)


def deploy_key_commands(cfg, pubkeys):
    """What the person runs on their own computer to add the two keys (queue read-only, results with write)."""
    rows = []
    for which, repo, rw in (("queue", cfg.github.queue_repo, False), ("results", cfg.github.results_repo, True)):
        f = f"{cfg.identity.name}-{which}.pub"
        rows.append((f, pubkeys.get(which, ""), f"gh repo deploy-key add {f} -R {cfg.github.owner}/{repo} -t {cfg.identity.name}-server" + (" --allow-write" if rw else "")))
    return rows


class ServerSetup:
    def __init__(self, host, source, *, ask=None, say=None, confirm=None, dry_run=True, given=None, use_defaults=False, rev=None,
                 systemctl=None, sysctl=None, sandbox=None, is_root=None, install=update.install_release, registry_installer=update.install_registry,
                 install_packages=None, wait_for_keys=0, sleep=time.sleep, clock=time.monotonic, ui=None, advanced=False, claude_secrets=None, approver_pubkey=None, mailbox_pubkey=None, verbose=False):
        say = say or _sink()
        self.host, self.source, self.ask, self.say, self.confirm = host, Path(source), ask, say, confirm
        self.dry_run, self.given, self.use_defaults, self.rev = dry_run, given or {}, use_defaults, rev
        self.systemctl, self.sysctl, self.sandbox, self.install = systemctl, sysctl, sandbox, install
        self.registry_installer, self.install_packages = registry_installer, install_packages
        self.wait_for_keys, self._sleep, self._clock, self.ui = wait_for_keys, sleep, clock, ui
        self.advanced = advanced
        self.r = report.Styler(styled=ui is not None, verbose=verbose)          # prints notes, headings and commands in the setup's style (plain text without a terminal)
        self.mailbox_pubkey = mailbox_pubkey            # Direct: the PUBLIC key your computer uses for the mailbox
        self.approver_pubkey = approver_pubkey          # the administrator's approval PUBLIC key: lets this worker check approvals signed on their own computer
        self.claude_secrets = claude_secrets or {}          # {KEY: value} handed over by the installer on the administrator's computer, so nothing is asked here
        self.is_root = is_root if is_root is not None else (lambda: __import__("os").geteuid() == 0)
        self.report = []                               # [(step, status, detail)]
        self.cfg = None
        self.facts = detect_mod.Facts()
        self.alert_url = ""

    # ---- helpers ----
    def note(self, step, status, detail=""):
        self.report.append((step, status, detail))
        self.say(self.r.note(status, step, detail))

    def run(self, argv, *, step, as_user=None):
        """Show it, then (unless dry run) run it. Returns the Result; a failure raises SetupError naming the command."""
        argv = ["runuser", "-u", as_user, "--", *argv] if as_user else list(argv)
        line = self.r.cmd(shlex.join(argv)) if not self.dry_run else "    $ " + shlex.join(argv)
        if line:
            self.say(line)
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
        self.facts = facts
        for what, value in facts.summary():
            self.say(self.r.kv(what, value))
        problem = facts.os_problem()
        if problem:
            raise SetupError("This machine is not supported", problem)
        if not self.dry_run and not self.is_root():
            raise SetupError("Run this as root", "It creates accounts and installs system files.", ["sudo python3 -m ct.setup server"])
        self.note("checked the machine", "ok", f"{facts.os_id} {facts.os_version}")
        self.prerequisites()
        return facts

    def prerequisites(self):
        """Install what the worker itself needs that a fresh server may lack (the registry reader and git). Needs no question: they are not optional."""
        missing = [pkg for pkg, probe in PREREQUISITES if not self.host.run(probe).ok]
        if missing:
            self.say("\n  The worker needs " + ", ".join(missing) + ", which this machine lacks.")
            self.run(["apt-get", "install", "-y", *missing], step="install what the worker needs")
        self.note("prerequisites", "installed" if missing and not self.dry_run else "planned" if missing else "ok", ", ".join(missing))

    def configure(self, facts):
        if self.ui is not None:
            self.ui.begin(title="server")
        nested, missing = questions.collect("server", ask=self.ask, say=self.say, given=self.given, suggestions=facts.suggestions(), use_defaults=self.use_defaults, ui=self.ui, advanced=self.advanced,
                                            confirm="Show what would be done?" if self.dry_run else "Set up the server now?")
        if self.ui is not None:
            self.ui.finish()
        if missing:
            raise SetupError("Some answers are needed", "No default exists for: " + ", ".join(missing), missing_steps(missing))
        uses_claude = (nested.get("claude") or {}).get("auth", "none") != "none"
        if uses_claude or "registry" in nested:                   # the read-only Claude class comes with the install exactly when Claude is used
            reg = nested.setdefault("registry", {})
            reg["packs"] = [p for p in (reg.get("packs") or ["core"]) if p != "claude"] + (["claude"] if uses_claude else [])
        clean, problems = questions.validate_answers(nested)
        if problems:
            raise SetupError("The answers do not make a valid configuration", "\n".join(problems))
        unknown = [m for m in clean["hardening"]["modules"] if m not in hardening.MODULES]
        if unknown:
            raise SetupError("Unknown hardening module", ", ".join(unknown), [f"Remove {', '.join(unknown)} from hardening.modules in your answers or ct.toml, or use one of: {', '.join(hardening.MODULES)}."])
        self.cfg = config.Config(clean)
        self.alert_url = (nested.get("alerts") or {}).get("url") or ""
        public = {sec: {k: v for k, v in vals.items() if not (sec == "alerts" and k == "url")} for sec, vals in nested.items()}
        text = toml_out.dump(public)                              # nothing private is written here: the alert address goes to secrets.env
        path = self.cfg.path("config_file")
        if self.ui is not None and not self.dry_run:                  # on a terminal the settings are not dumped: the answers were just reviewed, the file is named
            self.say(self.r.info(f"Your settings are written to {path}; the alert address, if any, is kept in the secrets file instead."))
        else:
            self.say(f"\n  The configuration, written to {path}:\n")
            self.say("    " + text.replace("\n", "\n    "))
            if self.alert_url:
                self.say("    (the alert address is private, so it is kept in the secrets file, not here)")
        if not self.dry_run:
            self.host.write(path, text, 0o644)
        self.note("configuration", "planned" if self.dry_run else "written", path)

    def accounts_and_folders(self):
        cfg = self.cfg
        if not self.host.run(["id", cfg.server.admin_user]).ok and not self.dry_run:
            raise SetupError(f"The account {cfg.server.admin_user!r} does not exist", "It is the account you log in as; create it first.", [f"sudo adduser {cfg.server.admin_user}"])
        owner = self.host.run(["stat", "-c", "%U", cfg.path("queue_clone")])             # an install that already exists belongs to the account it was made for
        have = owner.out.strip() if owner.ok else ""
        if have and have not in ("root", "UNKNOWN", cfg.server.worker_user) and self.host.run(["id", have]).ok:
            raise SetupError(f"This machine's worker already runs as {have}, but this install would use {cfg.server.worker_user}",
                             f"Creating a second account would leave {have}'s repositories and state unreadable and every job stuck. Nothing was changed by this check.",
                             [f"Keep the existing account: ct-setup install --set server.worker_user={have}"])
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
                self.host.write(secrets, "# Secrets only (API keys, alert tokens). Never put these in ct.toml.\n", 0o640)
                self.host.run(["chown", f"root:{w}", secrets])
        self.store_alert_address(secrets)
        self.note("folders", "ok")

    def store_alert_address(self, secrets):
        """The alert address is a private topic name (anyone who knows it can read the alerts), so it lives in the secrets file with the other secrets."""
        if self.dry_run or not self.alert_url:
            return
        text = self.host.read(secrets) or "# Secrets only (API keys, alert tokens). Never put these in ct.toml.\n"
        self.host.write(secrets, config.set_env_var(text, "CT_ALERT_URL", self.alert_url), 0o640)
        self.host.run(["chown", f"root:{self.cfg.server.worker_user}", secrets])

    def install_approver(self):
        """Give the worker the key whose signed approvals it will accept (root-owned, in its own file: a key trusted for release tags is never trusted for this)."""
        if not self.approver_pubkey:
            return
        path = self.cfg.path("approvers")
        try:
            text = approvals.with_approver(self.host.read(path), self.approver_pubkey)
        except ValueError as e:
            self.note("approver key", "failed", str(e))
            return
        if self.dry_run:
            self.note("approver key", "planned", f"approvals signed with {approvals.fingerprint(self.approver_pubkey)} will be accepted")
            return
        self.host.write(path, text, 0o644)
        self.note("approver key", "installed", f"approvals signed with {approvals.fingerprint(self.approver_pubkey)} are accepted; no login is needed to approve jobs")

    def remove_legacy_commands(self):
        """Earlier installs named the commands after the install (`<name>` and `<name>-update`). The update wrapper is a root-run installer, so a copy that nothing
        uses any more is removed rather than left lying around."""
        n = self.cfg.identity.name
        gone = [p for p in (f"/usr/local/bin/{n}", f"/usr/local/sbin/{n}-update")
                if p not in (f"{render.BIN_DIR}/{render.WORKER_CMD}", f"{render.SBIN_DIR}/{render.UPDATE_CMD}") and self.host.exists(p)]
        for p in gone:
            self.say(f"    (remove the old command {p})")
            if not self.dry_run:
                self.host.remove(p)
        if gone:
            self.note("old command names", "planned" if self.dry_run else "removed", ", ".join(gone))

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

    def mailbox(self):
        """Direct transport: the mailbox account, the worker's two bare repositories, and the one key that may use them. Nothing here reaches GitHub or any other machine."""
        cfg, w = self.cfg, self.cfg.server.worker_user
        t = transport.for_config(cfg)
        mail, home = t.mail_user(cfg), cfg.path("mailbox")
        if not self.host.run(["id", mail]).ok:
            self.run(["useradd", "--system", "--user-group", "--home-dir", home, "--no-create-home", "--shell", "/bin/sh", mail], step="create the mailbox account")
            self.note("mailbox account", "created", f"{mail} (its key can run only git on the two repositories)")
        else:
            self.note("mailbox account", "exists", mail)
        groups = self.host.run(["id", "-nG", w]).out.split() if self.host.run(["id", w]).ok else []
        if mail not in groups:
            self.run(["usermod", "-aG", mail, w], step="let the worker read the mailbox")       # the worker reads the queue through this group
        self.run(["install", "-d", "-m", "750", "-o", mail, "-g", mail, home], step="create the mailbox folder")
        self.run(["install", "-d", "-m", "755", "-o", "root", "-g", "root", f"{home}/.ssh"], step="create the mailbox's key folder")
        for which, owner in (("queue", mail), ("results", w)):                 # jobs come in through the mailbox account; results are written by the worker and only read through it
            path = t.repo_path(cfg, which)
            if not self.host.exists(path + "/HEAD"):
                self.run(["git", "init", "--bare", "--quiet", "-b", "main", path], step=f"create the {which} repository")
                if which == "queue":        # the mailbox account writes it and the worker reads it through the group. NOT the results: the worker's services run with RestrictSUIDSGID, which refuses git's group-shared (setgid) object files ("unable to create temporary file")
                    self.run(["git", "-C", path, "config", "core.sharedRepository", "group"], step=f"let the group read the {which} repository")
                self.run(["chown", "-R", f"{owner}:{mail}", path], step=f"hand the {which} repository to {owner}")
                self.run(["chmod", "-R", "u+rwX,g+rX,o-rwx", path], step=f"close the {which} repository to everyone else")
                if which == "results":      # new folders inherit the mailbox group from their parent (the kernel does it; nothing calls chmod, which the sandbox refuses), so the mailbox account can read what the worker adds
                    self.run(["find", path, "-type", "d", "-exec", "chmod", "g+s", "{}", "+"], step="make new results folders belong to the mailbox group")
                self.note(f"{which} repository", "created", path)
            else:
                self.note(f"{which} repository", "exists", path)
        gitconfig = self.host.read(f"{cfg.paths.worker_home}/.gitconfig") or ""
        for which in ("queue", "results"):                                       # git refuses another account's repository unless told it is meant
            path = t.repo_path(cfg, which)
            if path not in gitconfig:
                self.run(["git", "config", "--global", "--add", "safe.directory", path], step="allow the worker to read the mailbox", as_user=w)
        keyfile = f"{cfg.paths.config}/mailbox-client.pub"
        if self.mailbox_pubkey and self.mailbox_pubkey.strip().startswith("ssh-") and not self.dry_run:
            self.host.write(keyfile, self.mailbox_pubkey.strip() + "\n", 0o644)           # a public key: it only lets that computer in, and only to the mailbox shell
        pub = (self.host.read(keyfile) or "").strip()
        if pub.startswith("ssh-"):
            if not self.dry_run:
                self.host.write(f"{home}/.ssh/authorized_keys", f'restrict,command="{render.mail_shell_path(cfg)}" {pub}\n', 0o644)
            self.note("mailbox key", "installed", "your computer's key, allowed to run only the mailbox shell")
        else:
            self.note("mailbox key", "waiting", f"your computer's key arrives with ct-setup install ({keyfile})")
        self.note("repository keys", "ready", "none needed for Direct")
        return {}

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
            for which, pub in pubs.items():           # public halves only: readable by the administrator, so ct-setup on their computer can add them to GitHub for them
                if pub:
                    self.host.write(f"{cfg.paths.config}/deploy-keys/{which}.pub", pub + "\n", 0o644)
        self.note("repository keys", "ready")
        return pubs

    def reachable(self, which):
        cfg, w = self.cfg, self.cfg.server.worker_user
        # no --exit-code: a brand-new, empty repository is reachable too
        return self.host.run(["runuser", "-u", w, "--", "git", "ls-remote", remote_url(cfg, which)]).ok

    def wait_until_reachable(self, pending):
        """Wait (up to wait_for_keys seconds) for the repositories to become reachable: the person, or ct-setup on their computer, is adding the deploy keys."""
        self.say(f"\n  Waiting for the deploy keys to be added to GitHub (up to {self.wait_for_keys} s)...")
        deadline, last = self._clock() + self.wait_for_keys, self._clock()
        while pending and self._clock() < deadline:
            self._sleep(5)
            pending = [w for w in pending if not self.reachable(w)]
            if pending and self._clock() - last >= 30:
                last = self._clock()
                self.say(f"    still waiting for: {', '.join(pending)}")
        return pending

    def clones(self, pubs):
        cfg, w = self.cfg, self.cfg.server.worker_user
        todo = [(which, dest) for which, dest in (("queue", cfg.path("queue_clone")), ("results", cfg.path("results_clone")))]
        pending = []
        for which, dest in todo:
            if self.host.exists(dest + "/.git"):
                self.note(f"{which} clone", "exists")
            elif not self.dry_run and not self.reachable(which):
                pending.append(which)
        if pending and self.wait_for_keys:
            pending = self.wait_until_reachable(pending)
        for which, dest in todo:
            if self.host.exists(dest + "/.git") or which in pending:
                continue
            if not self.dry_run:
                self.run(["git", "clone", "--quiet", remote_url(cfg, which), dest], step=f"clone the {which} repository", as_user=w)
            self.note(f"{which} clone", "planned" if self.dry_run else "cloned")
        if pending:
            self.say("\n  The server cannot reach your repositories yet. Its two public keys must be added to GitHub (ct-setup on your computer does this for you")
            self.say("  when GitHub's `gh` command is available; otherwise do it by hand). Then run this command again; it continues from here.\n")
            for fname, pub, cmd in deploy_key_commands(cfg, pubs):
                self.say(f"    echo '{pub}' > {fname}\n    {cmd}\n")
            self.note("repository clones", "waiting", "add the deploy keys, then run again")
        return bool(pending)

    def registry_step(self):
        """Install the job registry the worker will trust (the queue repository's, validated; the core pack on a brand-new install)."""
        if self.dry_run:
            self.say("    install the job registry from the queue repository (validated first), or the core pack on a fresh install")
            self.note("job registry", "planned")
            return
        try:
            result = self.registry_installer(self.cfg, self.source, run=lambda argv: (lambda r: (r.rc, r.out))(self.host.run(argv)), prefix=str(self.host.root),
                                             install_packages=self.install_packages, out=self.say)
        except update.FetchError as e:                                     # the worker could not even read the queue repository: not a problem with its contents
            c = self.cfg
            raise SetupError("The worker could not read the queue repository, so the job registry was not installed", str(e),
                             [f"See the real reason: ssh -t {c.server.admin_user}@{c.server.host} 'sudo -u {c.server.worker_user} git -C {c.path('queue_clone')} fetch origin'",
                              "If it says Permission denied (publickey): run ct-setup install again; it adds the missing deploy key on GitHub.",
                              "If it says could not resolve host, or timed out: check the server's internet connection, then run ct-setup install again.",
                              "If it says cannot change to the folder, Permission denied: the folder belongs to another account. ls -l shows the owner; run ct-setup install --set server.worker_user=<that owner>."])
        except update.InstallError as e:
            g = self.cfg.github
            raise SetupError("The job registry was not installed", str(e),
                             [f"Open registry.yaml on the main branch of https://github.com/{g.owner}/{g.queue_repo}",
                              "In registry.yaml, change the line or setting the message above names, and commit it to main.",
                              "Then run ct-setup install again."])
        self.note("job registry", result)

    def hardening_step(self):
        cfg, results = self.cfg, {}
        total = len(cfg.hardening.modules)
        for i, mid in enumerate(cfg.hardening.modules, 1):
            mod = hardening.MODULES[mid]
            present = (lambda info, i=i: self.ui.protection({**info, "index": i, "total": total})) if self.ui is not None else None
            status, detail = hardening.run_module(mod, self.host, cfg, dry_run=self.dry_run, out=self.say, confirm=self.confirm, present=present)
            results[mid] = {"status": status, "detail": detail}
            self.note(f"hardening: {mid}", status, detail)
        if not self.dry_run:
            self.host.write(f"{cfg.paths.config}/hardening.json", json.dumps(results, indent=2, sort_keys=True) + "\n", 0o644)
        return results

    # ---- Claude: the tool, and how its jobs sign in ----
    def claude_step(self):
        """Only when the install says jobs may use Claude (claude.auth is not none): install the `claude` tool for the worker account, then store the sign-in the person
        gives. Everything else about the worker works without any of this."""
        cfg = self.cfg
        auth = cfg.claude.auth
        if auth == "none":
            self.forget_claude_secrets()
            self.note("Claude sign-in", "skipped", "not used on this worker; every other job runs without it")
            return
        w, home = cfg.server.worker_user, cfg.paths.worker_home
        if self.host.exists(f"{home}/.local/bin/claude"):
            self.note("Claude tool", "ok", "already installed for the worker")
        elif self.approve_claude_install(w):
            if not self.host.run(["curl", "--version"]).ok and not self.dry_run:
                self.run(["apt-get", "install", "-y", "curl"], step="install curl")
            try:
                if self.facts.claude_native:
                    self.run(["env", f"HOME={home}", "bash", "-c", CLAUDE_INSTALL], step="install Claude Code", as_user=w)
                else:
                    self.install_claude_legacy(w, home)
                if not self.dry_run and not self.host.run(["runuser", "-u", w, "--", f"{home}/.local/bin/claude", "--version"]).ok:
                    raise SetupError("The Claude tool does not run on this machine", "It installed but `claude --version` failed.")
            except SetupError as e:                      # a Claude tool that cannot be installed must never stop the rest of the worker from starting
                self.note("Claude tool", "failed", f"{e.title}: {e.why}"[:240])
                self.note("Claude sign-in", "skipped", "Claude jobs will not run on this worker; set claude.auth to none, or try a newer machine")
                return
            self.note("Claude tool", "planned" if self.dry_run else "installed", f"{home}/.local/bin/claude")
        else:
            self.note("Claude tool", "skipped", "you chose not to install it; Claude jobs will not run until it is installed")
        self.claude_secret(auth)

    def legacy_command(self, home):
        return ["env", f"HOME={home}", "sh", "-c",
                f"npm install --no-audit --no-fund --prefix {home}/.local @anthropic-ai/claude-code@{CLAUDE_LEGACY} && mkdir -p {home}/.local/bin "
                f"&& ln -sf ../node_modules/.bin/claude {home}/.local/bin/claude"]

    def install_claude_legacy(self, w, home):
        """The older release, from npm, into the worker's own home. Needs Node 18 or newer, which is installed if it is missing."""
        node = self.host.run(["node", "--version"])
        found = re.match(r"v(\d+)", node.out.strip()) if node.ok else None
        major = int(found.group(1)) if found else 0
        if not self.dry_run and major < 18:
            self.run(["apt-get", "install", "-y", "nodejs", "npm"], step="install Node for the Claude tool")
        self.run(self.legacy_command(home), step="install Claude Code", as_user=w)

    def forget_claude_secrets(self):
        """Choosing none after having chosen a sign-in removes what was stored, so no old credential is left on the worker."""
        path = self.cfg.path("secrets")
        text = self.host.read(path)
        if self.dry_run or not text or not any(k in config.read_env_file_text(text) for k in config.CLAUDE_SECRET_KEYS.values()):
            return
        for k in config.CLAUDE_SECRET_KEYS.values():
            text = config.set_env_var(text, k, None)
        self.host.write(path, text, 0o640)
        self.host.run(["chown", f"root:{self.cfg.server.worker_user}", path])
        self.note("Claude sign-in", "removed", "the stored token or key was deleted from the worker")

    def approve_claude_install(self, worker):
        if self.facts.claude_native:
            info = {"id": "claude-install", "label": "Install", "tag": "downloads from claude.ai", "title": "Install Claude Code for the worker",
                    "why": "Jobs that use Claude run the `claude` command-line tool as the worker account. Anthropic's own installer puts it in the worker's home folder; it needs no root.",
                    "risk": "This downloads and runs Anthropic's installer script from claude.ai, as the worker account (not root). The tool updates itself afterwards. Skip it to install the tool yourself.",
                    "now": "not installed", "steps": [f"runuser -u {worker} -- {CLAUDE_INSTALL}"], "lockout": False}
        else:
            info = {"id": "claude-install", "label": "Install", "tag": "older release", "title": f"Install Claude Code {CLAUDE_LEGACY} for the worker",
                    "why": "This processor is too old for the current Claude Code, which is a native program that dies here with \"Illegal instruction\". The last plain-JavaScript release runs on Node instead and was tried on this kind of machine.",
                    "risk": f"This installs the pinned release {CLAUDE_LEGACY} from npm, as the worker account (not root), and installs Node if it is missing. It does not update itself, so it misses later fixes, and Anthropic may one day stop accepting an old release. Skip it to leave Claude jobs off.",
                    "now": "not installed", "steps": [f"runuser -u {worker} -- {shlex.join(self.legacy_command(self.cfg.paths.worker_home))}"], "lockout": False}
        if self.dry_run:
            return True
        if self.ui is not None:
            return self.ui.protection(info)
        self.say("    $ " + info["steps"][0])
        return self.confirm("Install Claude Code for the worker?") if self.confirm else True

    def claude_secret(self, auth):
        cfg = self.cfg
        key, path, w = config.CLAUDE_SECRET_KEYS[auth], cfg.path("secrets"), cfg.server.worker_user
        if self.dry_run:
            self.note("Claude sign-in", "planned", f"would ask for the {'token' if auth == 'account' else 'key'} and keep it in {path} as {key}")
            return
        text = self.host.read(path) or "# Secrets only (API keys, alert tokens). Never put these in ct.toml.\n"
        value = self.claude_secrets.get(key, "")                 # one just given always replaces what is stored
        if not value and config.read_env_file_text(text).get(key):
            self.note("Claude sign-in", "ok", f"{key} is already set")
            return
        value = value or self.read_secret(CLAUDE_QUESTIONS[auth])
        if not value:
            self.note("Claude sign-in", "skipped", "nothing was entered; Claude jobs fail until you add it (run ct-setup install again)")
            return
        if len(value) < 20 or any(c.isspace() for c in value):
            raise SetupError("That does not look like a token", "It should be one long line with no spaces. Run ct-setup install again to enter it.")
        other = next(k for a, k in config.CLAUDE_SECRET_KEYS.items() if a != auth)
        text = config.set_env_var(config.set_env_var(text, other, None), key, value)
        self.host.write(path, text, 0o640)
        self.host.run(["chown", f"root:{w}", path])
        if auth == "account":                              # a subscription token lasts about a year: remember when this one was made so the worker can say when it is running out
            self.host.write(f"{cfg.paths.config}/{claudeupdate.TOKEN_FILE}", json.dumps({"set_at": int(time.time()), "route": "account"}) + "\n", 0o644)
        self.note("Claude sign-in", "stored", f"{key} in {path}, readable only by root and the worker account")

    def read_secret(self, q):
        return read_secret(self.ui, q)

    def start_timers(self):
        cfg = self.cfg
        timers = ([cfg.unit("fetch.timer"), cfg.unit("step.timer")] + ([cfg.unit("update.timer")] if cfg.updates.mode == "signed-tags" else [])
                  + ([cfg.unit("claude-update.timer")] if cfg.claude.auth != "none" else []) + [cfg.unit("registry-sync.timer")])
        for t in timers:
            self.run(["systemctl", "enable", "--now", t], step="start the timers")
        self.note("timers", "planned" if self.dry_run else "running", ", ".join(timers))

    @property
    def waiting(self):
        return any(status == "waiting" for _, status, _ in self.report)

    def run_all(self):
        """Returns the report. Raises SetupError for anything that must stop the wizard (the report so far is in self.report)."""
        self.say(self.r.head("Checking this machine"))
        facts = self.preflight()
        self.say(self.r.head("Configuration"))
        self.configure(facts)
        self.say(self.r.head("Accounts and folders"))
        self.accounts_and_folders()
        self.say(self.r.head("Code"))
        self.install_code()
        self.remove_legacy_commands()
        self.install_approver()
        self.say(self.r.head("Repository access"))
        pubs = self.mailbox() if transport.for_config(self.cfg).kind == "direct" else self.deploy_keys()
        if self.clones(pubs):
            return self.report
        self.say(self.r.head("Job registry"))
        self.registry_step()
        self.say(self.r.head("Hardening"))
        self.hardening_step()
        self.say(self.r.head("Claude"))
        self.claude_step()
        self.say(self.r.head("Starting"))
        self.start_timers()
        return self.report
