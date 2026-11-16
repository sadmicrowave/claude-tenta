"""cw-setup install: the whole setup from your own computer, in one command.

  1. connect to the server over SSH (key login) and learn about it, so the questions start from sensible suggestions and an unsupported machine is refused up front;
  2. ask every question once, here;
  3. set up this side (your configuration, the two private repositories, the clones);
  4. send the release to the server and run the server half there over `ssh -t` (you type the administrator's sudo password once, and only the server sees it);
  5. while it runs, take the server's public deploy keys from where it publishes them and add them to GitHub with `gh`, so nothing is copied by hand;
  6. send a first job through the whole loop and check the connection.

Nothing is left behind on the server: the folder it received the release in, which also holds your answers, is removed when it finishes.
"""
import hashlib
import io
import json
import re
import subprocess
import tarfile
import shlex
import time
import tomllib
from pathlib import Path

from .. import config
from ..admin.ssh import Ssh
from ..client.repo import ClientError
from . import detect, keys as keys_mod, questions, toml_out
from .client import ClientSetup
from .remote import RemoteHost
from .server import EXIT_WAITING, SetupError

CONNECTION = {"server.host", "server.admin_user", "server.ssh_port"}
REMOTE_DIR = re.compile(r"^/tmp/cw-install\.[A-Za-z0-9]{6,12}$")


def flat(nested):
    return {f"{s}.{k}": v for s, d in nested.items() for k, v in d.items()}


def merge(*parts):
    out = {}
    for p in parts:
        for section, values in p.items():
            out.setdefault(section, {}).update(values)
    return out


def archive_source(source, run=subprocess.run):
    """(tar bytes, revision, dirty): the committed files of the checkout (no history, no untracked or ignored files). A folder that is not a git checkout is
    tarred as it is and identified by the hash of its contents."""
    src = Path(source)
    if (src / ".git").exists():
        r = run(["git", "-C", str(src), "archive", "--format=tar", "HEAD"], capture_output=True)
        if r.returncode:
            raise SetupError("Could not package the release", (r.stderr or b"").decode("utf-8", "replace")[:200])
        rev = run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = bool(run(["git", "-C", str(src), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout.strip())
        return r.stdout, rev, dirty
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for d in ("src", "templates", "packs"):
            if (src / d).is_dir():
                t.add(src / d, arcname=d, filter=lambda i: None if "__pycache__" in i.name or i.name.startswith(".") else i)
    data = buf.getvalue()
    return data, hashlib.sha1(data).hexdigest(), False


def remote_dir(ssh):
    """A private (mode 0700) working folder on the server. The name is checked against a strict pattern before it is ever used or removed."""
    r = ssh.capture("umask 077; mktemp -d /tmp/cw-install.XXXXXX")
    d = (r.stdout or "").strip()
    if r.returncode or not REMOTE_DIR.match(d):
        raise SetupError("Could not make a working folder on the server", (r.stderr or d)[:200])
    return d


def deliver_source(ssh, source, archive=archive_source, say=print):
    """Send the committed files of the checkout to a fresh working folder on the server. Returns (folder, revision)."""
    data, rev, dirty = archive(source)
    if dirty:
        say("\n  NOTE  You have uncommitted changes. The server gets the last COMMIT (" + rev[:9] + "); the changes are not included.")
    d = remote_dir(ssh)
    r = ssh.send(f"tar -xf - -C {d}", data)
    if r.returncode:
        raise SetupError("Could not send the release to the server", (r.stderr or b"").decode("utf-8", "replace")[:200])
    return d, rev, len(data)


def remove_remote_dir(ssh, d):
    if d and REMOTE_DIR.match(d):
        try:
            ssh.capture(f"rm -rf {d}")
        except ClientError:
            pass


class Installer:
    def __init__(self, source, *, ssh_factory, local, say=print, ask=None, given=None, use_defaults=False, dry_run=False, repos_factory=None,
                 wait_for_keys=900, poll_s=5, verify=None, sleep=time.sleep, archive=archive_source, home=None, ui=None, advanced=False):
        self.source, self.ssh_factory, self.machine, self.say, self.ask = Path(source), ssh_factory, local, say, ask
        self.given, self.use_defaults, self.dry_run, self.repos_factory = dict(given or {}), use_defaults, dry_run, repos_factory
        self.wait_for_keys, self.poll_s, self.verify, self._sleep, self.archive = wait_for_keys, poll_s, verify, sleep, archive
        self.home = Path(home) if home else Path(local.env.get("HOME", "~")).expanduser() / ".cw"
        self.ssh = self.cfg = self.facts = None
        self.ui, self.advanced = ui, advanced
        self.report = []
        self.held = []                  # lines kept back while the question screen is up (printing in the middle of it would corrupt the redraw)

    # ---- what an earlier run left behind ----
    def config_file(self):
        return str(Path(self.machine.env.get("HOME", "~")).expanduser() / ".config" / "claude-ballast" / "cw.toml")

    def previous_answers(self):
        """The answers of an earlier run (the configuration it wrote), as defaults for this one: running install again never starts from a blank page."""
        try:
            raw = tomllib.loads(self.machine.read(self.config_file()) or "")
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in flat(raw).items() if k in questions.BY_KEY}

    @property
    def state_file(self):
        return self.home / "install-state.json"

    def progress(self):
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def mark(self, phase, **info):
        """Remember a finished phase so a stopped install can carry on from there. A preview never writes it."""
        if self.dry_run:
            return
        state = self.progress()
        state[phase] = {"at": int(time.time()), **info}
        if phase == "server":
            state.pop("job", None)                 # a new server setup means the first job has not been proven yet
        self.home.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state))

    def resumable(self):
        """True when the server was set up by an earlier run but its first job never finished, and the configuration is still there."""
        st = self.progress()
        if self.dry_run or "server" not in st or "job" in st or not self.machine.exists(self.config_file()):
            return False
        try:
            _data, rev, dirty = self.archive(self.source)
        except Exception:
            return False
        return rev == st["server"].get("rev") and not dirty             # a changed release must be installed again, not skipped

    def resume_first_job(self):
        clean, problems = config.validate(tomllib.loads(self.machine.read(self.config_file()) or ""))
        if problems:
            raise SetupError("The configuration from the earlier run is not valid", "\n".join(problems), ["Run cw-setup install again and answer the questions."])
        self.cfg = config.Config(clean, self.config_file())
        self.first_job()
        return self.report

    def note(self, step, status, detail=""):
        self.report.append((step, status, detail))
        self.out(f"  {status.upper():<9} {step}" + (f"  ({detail})" if detail else ""))

    def out(self, line):
        """Print now, or hold until the questions are done when the question screen is up."""
        if self.ui is not None and not getattr(self, "asked", False):
            self.held.append(line)
        else:
            self.say(line)

    def flush(self):
        for line in self.held:
            self.say(line)
        self.held = []

    # ---- 1. connect and learn ----
    def connect(self):
        """The connection questions (their own short pass), then log in and look at the server."""
        self.held = []
        self.asked = False
        if self.ui is not None:
            self.ui.begin(title="install")
            self.ui.total_hint = questions.count_to_ask(("client", "server"), self.given, advanced=self.advanced)
        else:
            self.say("\nConnecting to the server")
        conn, missing = questions.collect("client", ask=self.ask, say=self.say, given=self.given, use_defaults=self.use_defaults, only=CONNECTION, ui=self.ui, advanced=self.advanced,
                                          suggestions=self.previous_answers())
        if missing:
            raise SetupError("Some answers are needed", "No default exists for: " + ", ".join(missing), ["Answer the questions, or pass them with --set section.key=value"])
        c = conn.get("server", {})
        self.ssh = self.ssh_factory(c["host"], c["admin_user"], c.get("ssh_port", 22))
        try:
            self.ssh.capture("true")
        except ClientError as e:
            raise SetupError(f"Cannot log in to {c['host']} over SSH", e.why, ["Install your SSH key on the server first (this asks for its password once):", f"ssh-copy-id {self.ssh.target}",
                                                                      "If SSH is not on port 22, run this again with --advanced, which asks for the port."])
        self.facts = detect.detect(RemoteHost(self.ssh))
        for what, value in self.facts.summary():
            self.out(f"  {what:<22} {value}")
        problem = self.facts.os_problem()
        if problem:
            raise SetupError("The server is not supported", problem)
        self.note("connected to the server", "ok", f"{self.ssh.target}, {self.facts.os_id} {self.facts.os_version}")
        return conn

    # ---- 2. questions ----
    def ask_everything(self, conn):
        """Every other question, as one flow (so the up arrow can walk back through all of them). Returns the answers, or BACK if the person stepped back to
        the connection questions."""
        if self.ui is None:
            self.say("\nQuestions")
        given = {**self.given, **flat(conn)}
        pre = [(questions.BY_KEY[k]["label"], questions.show(v)) for k, v in flat(conn).items() if k in questions.BY_KEY]
        rest, missing = questions.collect(("client", "server"), ask=self.ask, say=self.say, given=given, suggestions={**self.facts.suggestions(), **self.previous_answers()}, use_defaults=self.use_defaults,
                                          ui=self.ui, allow_back=self.ui is not None, pre_items=pre, advanced=self.advanced,
                                          confirm="Show what would be done?" if self.dry_run else "Set everything up now?")
        if rest is questions.BACK:
            return questions.BACK
        if self.ui is not None:
            self.ui.finish()
        self.asked = True
        self.flush()
        if missing:
            raise SetupError("Some answers are needed", "No default exists for: " + ", ".join(missing), ["Answer the questions, or pass them with --set section.key=value"])
        nested = merge(conn, rest)
        clean, problems = questions.validate_answers(nested)
        if problems:
            raise SetupError("The answers do not make a valid configuration", "\n".join(problems))
        self.cfg = config.Config(clean)
        return nested

    def settle(self):
        """Connect, then ask everything; if the person steps back to the connection questions, do that part again (their answers are ready to change)."""
        while True:
            conn = self.connect()
            nested = self.ask_everything(conn)
            if nested is not questions.BACK:
                return nested

    # ---- 3. this side ----
    def client_half(self, nested):
        self._heading("Your computer", "2 of 4")
        c = ClientSetup(self.machine, self.home, say=self.say, dry_run=self.dry_run, given=flat(nested), use_defaults=True, repos_factory=self.repos_factory)
        c.run_all()
        self.report += c.report

    # ---- 4. the server ----
    def deliver(self, nested):
        d, rev, size = deliver_source(self.ssh, self.source, self.archive, self.say)
        self.ssh.send(f"cat > {d}/answers.toml", toml_out.dump(nested, full=True).encode())
        self.note("release sent to the server", "ok", f"commit {rev[:9]}, {size // 1024} KB")
        return d, rev

    def _heading(self, text, step=None):
        if self.ui is not None:
            self.ui.heading(text, step)
        else:
            self.say("\n" + text)

    def server_half(self, d, rev):
        self._heading("The server", "3 of 4")
        # no bytecode: root runs this from the administrator's folder and must not leave files there that the administrator cannot remove
        base = f"env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={d}/src python3 -m cw.setup server --no-banner --answers {d}/answers.toml --rev {rev} --source {d}"
        if self.dry_run:
            r = self.ssh.capture(base)
            self.say((r.stdout or "") + (r.stderr or ""))
            return
        user, host = self.ssh.user or "the administrator", self.cfg.server.host
        if self.ui is not None:
            self.ui.sudo_card(user, host)
            prompt = self.ui.sudo_prompt(user, host)
        else:
            self.say(f"  The server will ask for {user}'s sudo password, and for a yes to each protection. Only the server sees the password.\n")
            prompt = f"[sudo] password for {user}: "
        # one remote shell: ask for the password first (`sudo -v`), leave a marker, then run the setup under the sudo that is already unlocked.
        # Nothing else prints until the marker exists, so the password prompt is never interleaved with progress lines.
        marker = f"{d}/.authenticated"
        remote = f"sh -c {shlex.quote(f'sudo -p {shlex.quote(prompt)} -v && touch {marker} && sudo {base} --apply --wait-for-keys {self.wait_for_keys}')}"
        proc = self.ssh.popen(remote, tty=True)
        dk = keys_mod.DeployKeys(self.cfg, self.machine.run, self.machine.which, say=self.say)
        shown = False
        authed = False
        while proc.poll() is None:
            if not authed:
                authed = self.ssh.capture(f"test -e {marker}").returncode == 0
                if not authed:
                    self._sleep(1)
                    continue
                self._heading("Setting up the server", "3 of 4")
            got = keys_mod.published(self.ssh, self.cfg.paths.config)
            if len(got) == 2 and not dk.complete:
                if dk.available:
                    for which, status in dk.sync(got):
                        if status not in ("added", "present"):
                            self.say(f"  ERROR     could not add the {which} deploy key: {status}")
                elif not shown:
                    shown = True
                    self.say("\n  `gh` is not available here, so add the server's keys to GitHub yourself:")
                    for row in dk.manual_steps(got):
                        self.say("  " + row)
            self._sleep(self.poll_s)
        rc = proc.returncode
        if rc == EXIT_WAITING:
            raise SetupError("The server is still waiting for its deploy keys", "They were not added in time.",
                             ["Add them (see above), then run cw-setup install again; it continues from here."])
        if rc != 0:
            raise SetupError(f"The server setup stopped (exit {rc})", "Read the messages above. Running cw-setup install again continues where it left off.")
        self.note("server setup", "done")
        self.mark("server", rev=rev)

    def cleanup(self, d):
        remove_remote_dir(self.ssh, d)

    # ---- 5. first job ----
    def first_job(self):
        self._heading("A first job", "4 of 4")
        ok = self.verify(self.cfg) if self.verify else True
        self.note("a job went through the whole loop", "ok" if ok else "failed")
        if ok:
            self.mark("job")
        return ok

    def wants_to_resume(self):
        """An earlier run set the server up but stopped before the first job finished. Offer to carry on from there; with nobody to ask, carry on."""
        st = self.progress()
        self.say(f"\nAn earlier install finished the server setup (release {str(st['server'].get('rev', ''))[:9]}) but did not finish the first job.")
        if self.ask is None or self.use_defaults:
            return True
        answer = self.ask("  Carry on with the first job? [Y = carry on, n = start over with your earlier answers ready] ").strip().lower()
        return answer in ("", "y", "yes")

    def run_all(self):
        if self.resumable() and self.wants_to_resume():
            return self.resume_first_job()
        nested = self.settle()
        self.client_half(nested)
        d = None
        try:
            d, rev = self.deliver(nested)
            self.server_half(d, rev)
        finally:
            self.cleanup(d)
        if not self.dry_run:
            self.first_job()
        return self.report


def default_verify(cfg, say=print, sleep=time.sleep, attempts=8, wait_s=15):
    """Send `sys-status` and wait for its result; the worker needs a minute after its first start to publish what it can run, so try a few times."""
    from ..admin import cli as admin_cli
    from ..client import cli as client_cli
    ok = False
    for i in range(attempts):
        if client_cli.main(["submit", "sys-status", "--wait", "--timeout", "200"]) == 0:
            ok = True
            break
        say(f"  (the worker is still starting; trying again, {i + 1} of {attempts})")
        sleep(wait_s)
    return ok and admin_cli.main(["check"]) == 0
