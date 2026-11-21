"""Set a Windows computer up as a worker, from this computer, over SSH (scope: docs/design/WINDOWS-WORKER-LOOP.md and WINDOWS-SPECIAL-USE.md): the same job `ct-setup install` does for a Linux server, with the Windows steps.

`WindowsInstaller` has the interface the add-worker flow already uses for any worker (`run_all()` returns the report rows, a `SetupError` says what failed and what to do): it writes this computer's own
configuration for the worker, makes the mailbox key, then runs the steps on the Windows computer (tools, layout, release, mailbox, the worker's files, the start task), clones the two mailbox repositories here
and proves the whole loop with a first job sent the way `ct-run` sends one. Every step is idempotent, so running it again continues from where it stopped."""
import os
import subprocess
import time
from pathlib import Path

from .. import config, transport, updatecheck
from . import report as report_mod, winfiles, winlayout, winmailbox, winrelease, wintask, wintools
from .server import SetupError

FIRST_JOB = "win-status"


class WindowsInstaller:
    def __init__(self, conf, home, name, host, user, suggest=None, *, run=None, say=None, find_release=None, repos_factory=None, submit=None, sleep=time.sleep, release_commit=None, wait_s=120, styled=False):
        self.conf, self.home, self.name, self.host, self.user = Path(conf), Path(home), name, host, user
        self.suggest = suggest or {}
        self.port = int(self.suggest.get("server.ssh_port", 22) or 22)
        self.run = run or wintools.ssh_runner(user, host, self.port)
        self.say = say or (lambda m: None)
        self.find_release = find_release or updatecheck.check_now
        self.repos_factory, self.submit, self.sleep, self.wait_s = repos_factory, submit, sleep, wait_s
        self.release_commit = release_commit or os.environ.get("CT_WINDOWS_RELEASE_COMMIT")          # for testing a commit that has no release tag yet; never needed in normal use
        self.report = []
        self.r = report_mod.Styler(styled=styled, verbose=False)          # the setup's own style for its progress rows (plain text without a terminal)

    # ---- helpers ----
    def note(self, what, status, detail=""):
        self.report.append((what, status, detail))
        self.say(self.r.note(status, what, detail))

    def step(self, what, fn, fix):
        try:
            return fn()
        except wintools.WinToolsError as e:
            self.report.append((what, "failed", f"{e.what}: {e.detail}"))
            raise SetupError("A step of the Windows setup failed", f"{e.what}: {e.detail}", fix + ["Run the same command again: it continues from where it stopped."], facts=[("Step", what), ("Computer", self.host)])

    # ---- this computer ----
    def client_config(self):
        """This computer's configuration for the worker: written once, kept as it is when it is already there."""
        if not self.conf.exists():
            self.conf.parent.mkdir(parents=True, exist_ok=True)
            self.conf.write_text(winfiles.windows_config(self.name, self.host, self.user, ssh_port=self.port, client_home=str(self.home)))
            self.note("your configuration", "written", str(self.conf))
        else:
            self.note("your configuration", "kept", str(self.conf))
        return config.load(self.conf)

    def mailbox_key(self, cfg):
        """The key this computer uses for the mailbox: made here if it is not there, with no other use. Returns the public half."""
        key = Path(transport.for_config(cfg).client_key(cfg))
        if not key.exists():
            key.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key), "-C", f"{cfg.identity.name} mailbox"], capture_output=True, text=True)
            if r.returncode:
                raise SetupError("The mailbox key could not be made", (r.stderr or r.stdout).strip(), ["Run: ssh-keygen -t ed25519 -N '' -f /tmp/ct-test-key  (it must make a key without an error)"])
            key.chmod(0o600)
            self.note("mailbox key", "made", str(key))
        else:
            self.note("mailbox key", "kept", str(key))
        return " ".join(Path(str(key) + ".pub").read_text().split()[:2])          # the key type and the key; a comment (which may hold spaces) is not sent, so nothing can ride along in the authorized_keys line

    def release(self, cfg):
        if self.release_commit:
            return "(given)", self.release_commit
        entry = self.find_release(cfg.updates.source, platform="windows")
        if entry.get("error"):
            raise SetupError("The Windows release could not be looked up", entry["error"], [f"Open {cfg.updates.source} in a browser on this computer; if it does not load, the connection to it is the problem"])
        if not entry.get("tag"):
            raise SetupError("No Windows release has been published yet", "A Windows worker is installed from a release-win-X.Y.Z tag of the project, and there is none.", ["Ask the project's owner to publish one, or give a commit with --set updates.source=<your fork>"])
        return entry["tag"], entry["commit"]

    def reach(self):
        """Before anything else: can this computer log in to the Windows computer over SSH and run PowerShell? A computer that is asleep, off, on another network or without key login is the most common failure, and
        it must not be reported as a failure of the first real step with advice about the internet."""
        try:
            wintools.parse_result(self.run(list(wintools.POWERSHELL), "Write-Output ('CT-RESULT ' + '{}')\n\n")[1])
        except wintools.WinToolsError as e:
            said = e.detail
            denied = any(w in said.lower() for w in ("permission denied", "publickey"))
            raise SetupError("The Windows computer cannot be reached over SSH", said.strip()[-300:] or "no answer",
                             [f"Is it on, awake and on this network? Try: ssh {self.user}@{self.host} hostname",
                              *([f"Key login is not set up for that account: ssh-copy-id {self.user}@{self.host}  (on Windows the key goes in C:\\ProgramData\\ssh\\administrators_authorized_keys for an administrator)"] if denied else []),
                              "Run the same command again once that works: nothing was changed on it."], facts=[("Computer", self.host), ("Account", self.user)])

    # ---- the whole job ----
    def run_all(self):
        self.reach()
        cfg = self.client_config()
        pub = self.mailbox_key(cfg)
        tag, commit = self.release(cfg)
        tools = self.step("tools", lambda: wintools.install(self.run), ["The computer needs internet access to github.com and python.org for git and Python."])
        self.note("tools", "installed" if tools.get("changed") else "ready", f"{tools['git']}; {tools['python']}")
        lay = self.step("accounts and folders", lambda: winlayout.ensure(self.run), ["Run this as an account that is an administrator on the Windows computer."])
        self.note("accounts and folders", "set up" if lay["changed"] else "ready", f"worker account {lay['user']}")
        rel = self.step("release", lambda: winrelease.fetch(self.run, cfg.updates.source, commit), [f"The computer must reach {cfg.updates.source}"])
        self.note("release", "fetched" if rel["changed"] else "ready", f"{tag} ({rel['commit'][:9]})")
        mb = self.step("mailbox", lambda: winmailbox.ensure(self.run, name=cfg.identity.name, key=pub), ["The OpenSSH server must be installed on the Windows computer (Settings > Optional features)."])
        self.note("mailbox", "set up" if mb["changed"] else "ready", f"account {mb['user']}; only git on two repositories")
        files = self.step("worker files", lambda: winfiles.ensure(self.run, winfiles.windows_config(cfg.identity.name, self.host, self.user, ssh_port=self.port), winfiles.pack_text()), [])
        self.note("worker files", "written" if files["changed"] else "ready", "configuration, job classes, clones of the mailbox")
        task = self.step("worker task", lambda: (wintask.ensure(self.run), wintask.start(self.run))[0], [])
        self.note("worker task", "registered" if task["changed"] else "ready", "starts at boot as the worker account; started now")
        self.clone_here(cfg)
        self.first_job(cfg)
        return self.report

    def clone_here(self, cfg):
        from ..client import repo as client_repo
        repos = (self.repos_factory or client_repo.ClientRepos.from_config)(cfg)
        try:
            rows = repos.clone_all(self.home)
        except Exception as e:                       # ClientError carries a title and steps; say them plainly
            raise SetupError("This computer could not reach the worker's mailbox", str(getattr(e, "why", None) or e), list(getattr(e, "steps", []) or []) + [f"Try: ssh -i {transport.for_config(cfg).client_key(cfg)} {transport.for_config(cfg).mail_user(cfg)}@{self.host} true  (it must print the mail shell's refusal, not a password prompt)"])
        self.note("your copies of the mailbox", "ready", ", ".join(r[0] for r in rows))
        self.repos = repos

    def first_job(self, cfg):
        """Send the pack's test job the way ct-run does and wait for its result: the worker is not set up until a job has gone there and come back."""
        from ..client import submit as client_submit
        jid = client_submit.new_job_id(FIRST_JOB)
        client_submit.push_job(self.repos, jid, client_submit.build_job(FIRST_JOB, {}))
        t0 = time.monotonic()
        res = None
        while time.monotonic() - t0 < self.wait_s:
            self.sleep(5)
            self.repos.pull_results()
            res = self.repos.read_result(jid)
            if res and res.get("status") not in ("running", "queued", "approved", "waiting"):
                break
        if not res or res.get("status") != "ok":
            self.report.append(("first job", "failed", f"{FIRST_JOB}: {(res or {}).get('status', 'no result yet')}"))
            raise SetupError("The worker is set up, but its first job did not come back", f"The test job {FIRST_JOB} ended as {(res or {}).get('status', 'no result within ' + str(self.wait_s) + ' seconds')}.",
                             [f"See why: ssh {self.user}@{self.host} type C:\\ProgramData\\claude-tenta\\state\\worker.log", "Run the same command again once it is fixed: nothing is set up twice."])
        self.note("first job", "ok", f"{FIRST_JOB} ran on the Windows computer and came back")
