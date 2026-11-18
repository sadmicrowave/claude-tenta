"""ct-setup install: the whole setup from your own computer, in one command.

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
import socket
import re
import subprocess
import sys
import tarfile
import shlex
import time
import tomllib
from pathlib import Path

from .. import config, transport
from .. import ui as ui_mod
from ..admin.ssh import Ssh
from ..client.repo import ClientError
from . import detect, keys as keys_mod, ptyrun, questions, report, toml_out
from .client import ClientSetup
from .remote import RemoteHost
from .server import CLAUDE_QUESTIONS, EXIT_WAITING, SetupError, missing_steps, read_secret
from .report import sink as _sink

CONNECTION = {"server.host", "server.admin_user", "server.ssh_port"}
REMOTE_DIR = re.compile(r"^/tmp/ct-install\.[A-Za-z0-9]{6,12}$")


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
    r = ssh.capture("umask 077; mktemp -d /tmp/ct-install.XXXXXX")
    d = (r.stdout or "").strip()
    if r.returncode or not REMOTE_DIR.match(d):
        raise SetupError("Could not make a working folder on the server", (r.stderr or d)[:200])
    return d


def deliver_source(ssh, source, archive=archive_source, say=None):
    """Send the committed files of the checkout to a fresh working folder on the server. Returns (folder, revision)."""
    say = say or _sink()
    data, rev, dirty = archive(source)
    if dirty:
        ui_mod.say("You have uncommitted changes", ("The server gets", f"the last commit, {rev[:9]}"), "The changes you have not committed are not included.",
                   tag="NOTE", color=ui_mod.YELLOW, stream=sys.stderr)
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


CLAUDE_ON_THIS_COMPUTER = "curl -fsSL https://claude.ai/install.sh | bash"


class Installer:
    def __init__(self, source, *, ssh_factory, local, say=None, ask=None, given=None, use_defaults=False, dry_run=False, repos_factory=None,
                 wait_for_keys=900, poll_s=5, verify=None, sleep=time.sleep, archive=archive_source, home=None, ui=None, advanced=False, interactive=None, verbose=False, config_path=None, targeted=False, suggest=None, resolve4=None):
        say = say or _sink()
        self.resolve4 = resolve4 or (lambda host, port: socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0])       # the worker's IPv4 address, asked of the network again
        self.config_override, self.targeted, self.suggest = config_path, targeted, dict(suggest or {})      # targeted: adding one worker, with its own configuration file and folder
        self.source, self.ssh_factory, self.machine, self.say, self.ask = Path(source), ssh_factory, local, say, ask
        self.given, self.use_defaults, self.dry_run, self.repos_factory = dict(given or {}), use_defaults, dry_run, repos_factory
        self.wait_for_keys, self.poll_s, self.verify, self._sleep, self.archive = wait_for_keys, poll_s, verify, sleep, archive
        self.home = Path(home) if home else Path(local.env.get("HOME", "~")).expanduser() / ".ct"
        self.ssh = self.cfg = self.facts = None
        self.ui, self.advanced = ui, advanced
        self.verbose = verbose
        self.r = report.Styler(styled=ui is not None, verbose=verbose)
        self.interactive = interactive or ptyrun.run_capture          # runs a command on the person's own terminal and returns (exit code, what it printed)
        self.claude_secret = None                                                      # (KEY, value) from the sign-in below; goes to the server, never to a file here
        self.report = []
        self.held = []                  # lines kept back while the question screen is up (printing in the middle of it would corrupt the redraw)

    # ---- what an earlier run left behind ----
    def config_file(self):
        if self.config_override:
            return str(self.config_override)
        return str(Path(self.machine.env.get("HOME", "~")).expanduser() / ".config" / "claude-tenta" / "ct.toml")

    def answers_file(self):
        return str(self.home / "install-answers.toml")

    def previous_answers(self):
        """The answers of an earlier run, as defaults for this one: running install again never starts from a blank page. They come from the configuration the
        client half wrote and from the full set of answers saved at the end of the questions (the server's questions are not in the client's configuration)."""
        out = {}
        for path in (self.config_file(), self.answers_file()):
            try:
                raw = tomllib.loads(self.machine.read(path) or "")
            except (OSError, ValueError):
                continue
            out.update({k: v for k, v in flat(raw).items() if k in questions.BY_KEY})
        return out

    def save_answers(self, nested):
        """Keep every answer except secrets (those live only in the server's secrets file) so the next run can offer them again."""
        if self.dry_run:
            return
        keep = {sec: {k: v for k, v in vals.items() if not questions.BY_KEY.get(f"{sec}.{k}", {}).get("secret")} for sec, vals in nested.items()}
        self.home.mkdir(parents=True, exist_ok=True)
        self.machine.write(self.answers_file(), toml_out.dump(keep, full=True), 0o600)

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
            raise SetupError("The configuration from the earlier run is not valid", "\n".join(problems), ["Run ct-setup install again and answer the questions."])
        self.cfg = config.Config(clean, self.config_file())
        self.first_job()
        return self.report

    def note(self, step, status, detail=""):
        self.report.append((step, status, detail))
        self.out(self.r.note(status, step, detail))

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
                                          suggestions={**self.suggest, **self.previous_answers()})
        if missing:
            raise SetupError("Some answers are needed", "No default exists for: " + ", ".join(missing), missing_steps(missing))
        c = conn.get("server", {})
        self.ssh = self.ssh_factory(c["host"], c["admin_user"], c.get("ssh_port", 22))
        try:
            self.ssh.capture("true")
        except ClientError as e:
            raise SetupError(f"Cannot log in to {c['host']} over SSH", e.why, ["Install your SSH key on the server first (this asks for its password once):", f"ssh-copy-id {self.ssh.target}",
                                                                      "If SSH is not on port 22, run this again with --advanced, which asks for the port."])
        self.facts = detect.detect(RemoteHost(self.ssh))
        for what, value in self.facts.summary():
            self.out(self.r.kv(what, value))
        problem = self.facts.os_problem()
        if problem:
            raise SetupError("The server is not supported", problem)
        self.note("connected to the server", "ok", f"{self.ssh.target}, {self.facts.os_id} {self.facts.os_version}")
        self.existing_worker = self.server_worker_account()
        return conn

    def server_worker_account(self):
        """The worker account an install already on this server runs as (from its own configuration), or None. Installing again, from a new computer, after a rename or with the
        saved answers lost, must keep that account: a second one would leave the first's repositories and state unreadable."""
        name = self.given.get("identity.name") or self.previous_answers().get("identity.name") or "claude-tenta"
        try:
            r = self.ssh.capture(f"cat /etc/{shlex.quote(str(name))}/ct.toml 2>/dev/null")
            account = (tomllib.loads(r.stdout or "").get("server") or {}).get("worker_user")
        except (ClientError, ValueError, OSError, AttributeError):
            return None
        return account if isinstance(account, str) and account else None

    # ---- 2. questions ----
    def ask_everything(self, conn):
        """Every other question, as one flow (so the up arrow can walk back through all of them). Returns the answers, or BACK if the person stepped back to
        the connection questions."""
        if self.ui is None:
            self.say("\nQuestions")
        given = {**({"server.worker_user": self.existing_worker} if getattr(self, "existing_worker", None) else {}), **self.given, **flat(conn)}       # an explicit answer still wins
        pre = [(questions.BY_KEY[k]["label"], questions.show(v)) for k, v in flat(conn).items() if k in questions.BY_KEY]
        rest, missing = questions.collect(("client", "server"), ask=self.ask, say=self.say, given=given, suggestions={**self.facts.suggestions(), **self.suggest, **self.previous_answers()}, use_defaults=self.use_defaults,
                                          ui=self.ui, allow_back=self.ui is not None, pre_items=pre, advanced=self.advanced,
                                          confirm="Show what would be done?" if self.dry_run else "Set everything up now?")
        if rest is questions.BACK:
            return questions.BACK
        if self.ui is not None:
            self.ui.finish()
        self.asked = True
        self.flush()
        if missing:
            raise SetupError("Some answers are needed", "No default exists for: " + ", ".join(missing), missing_steps(missing))
        nested = merge(conn, rest)
        clean, problems = questions.validate_answers(nested)
        if problems:
            raise SetupError("The answers do not make a valid configuration", "\n".join(problems))
        self.cfg = config.Config(clean)
        try:                                                           # from here on the worker's own IPv6 setting decides how this computer connects to it (see admin.ssh.family_for)
            from ..admin.ssh import family_for
            self.ssh.family = family_for(self.cfg)
        except AttributeError:
            pass
        self.save_answers(nested)
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
        c = ClientSetup(self.machine, self.home, say=self.say, dry_run=self.dry_run, given=flat(nested), use_defaults=True, repos_factory=self.repos_factory,
                        confirm_card=self.confirm_card, interactive=self.interactive, styled=self.ui is not None, verbose=self.verbose,
                        config_path=self.config_override, targeted=self.targeted,
                        **({"ask": self.ask} if self.ask is not None and self.ui is None else {}))
        self.client = c
        c.run_all()
        self.report += c.report

    # ---- Claude sign-in, on this computer, right after the questions ----
    def confirm_card(self, info):
        """Ask permission for a change outside this tool: an amber card on a terminal screen, a y/N question at a plain terminal, otherwise no (nothing is changed unasked)."""
        if self.ui is not None:
            return self.ui.protection(info)
        if self.ask is not None:
            self.say("    $ " + info["steps"][0])
            return self.ask(ui_mod.confirm_prompt(f"{info['title']}?")).strip().lower() == "y"
        return False

    def local_claude(self):
        """A `claude` on this computer that really runs, or None. The one on PATH can be broken (an npm install whose native program never downloaded), so the
        copy Anthropic's installer puts in ~/.local/bin is tried too."""
        home = Path(self.machine.env.get("HOME", "~")).expanduser()
        for exe in (self.machine.which("claude") or "claude", str(home / ".local" / "bin" / "claude")):
            if self.machine.run([exe, "--version"], timeout=30).ok:
                return exe
        return None

    def offer_local_claude_install(self):
        info = {"id": "claude-install-here", "label": "Install", "tag": "downloads from claude.ai", "title": "Install Claude Code on this computer",
                "why": "The sign-in token is made by Claude Code's own `setup-token` command, which needs a working Claude Code on this computer. The one here is missing or does not run.",
                "risk": "This downloads and runs Anthropic's installer script from claude.ai as you (no sudo). Skip it if you would rather paste a token you made on another machine.",
                "now": "not usable", "steps": [CLAUDE_ON_THIS_COMPUTER], "lockout": False}
        if self.ui is not None:
            yes = self.ui.protection(info)
        elif self.ask is not None:
            self.say("    $ " + CLAUDE_ON_THIS_COMPUTER)
            yes = self.ask(ui_mod.confirm_prompt("Install Claude Code on this computer?")).strip().lower() == "y"
        else:
            yes = False
        if yes:
            r = self.machine.run(["bash", "-c", CLAUDE_ON_THIS_COMPUTER], timeout=600)
            if not r.ok:
                self.say("  The installer said: " + r.out.strip()[-300:])
        return yes

    def token_works(self, exe, token):
        """Try the token once with the smallest possible request, so a token that was read wrongly is caught here and not weeks later on the server. Only a clear
        authentication failure rejects it; a network problem does not (it is then used as it is)."""
        r = self.machine.run([exe, "-p", "Reply with the single word ok.", "--max-turns", "1", "--output-format", "json"], timeout=120, env={"CLAUDE_CODE_OAUTH_TOKEN": token})
        compact = r.out.replace(" ", "")
        return not ('"api_error_status":401' in compact or "authentication_error" in compact)

    def claude_signin(self):
        """When the install uses Claude (claude.auth is account or api-key): make the sign-in here and hold it for the server. For `account` this runs
        `claude setup-token` on this computer, which opens the browser, then asks for the token it printed. Nothing is written to a file on this computer."""
        auth = self.cfg.claude.auth
        if auth == "none" or self.dry_run:
            return
        key = config.CLAUDE_SECRET_KEYS[auth]
        self._heading("Claude sign-in")
        q = dict(CLAUDE_QUESTIONS[auth])
        if auth == "account":
            exe = self.local_claude() or (self.offer_local_claude_install() and self.local_claude())
            if exe:
                self.say("  Opening Claude's sign-in. Finish it in your browser; the token it prints is read from here, so there is nothing to copy.\n")
                rc, printed = self.interactive([exe, "setup-token"])
                token = ptyrun.extract_token(printed) if rc == 0 else None
                if token and self.token_works(exe, token):
                    self.claude_secret = (key, token)
                    self.note("Claude sign-in", "ok", "token read from the sign-in and checked; it goes only to the server")
                    return
                if rc != 0:
                    self.say("  `claude setup-token` did not finish. You can paste a token made another way.")
                elif token:
                    self.say("  The token it printed was refused when tried, so it was not used. Paste a fresh one, or leave this empty to skip.")
                else:
                    self.say("  The sign-in finished but no token could be read from its output. Paste the token it printed.")
                q["why"] = "Paste the long token that `claude setup-token` printed. Leave it empty to keep a sign-in the server already has."
            else:
                self.say("  Claude Code does not run on this computer, so make the token on a machine where it does: `claude setup-token`, then paste it below.")
        for _ in range(3):
            token = (read_secret(self.ui, q) or "").strip()
            if not token or (len(token) >= 20 and not any(c.isspace() for c in token)):
                break
            self.say("  That does not look like a token (one long line, no spaces). Try again, or leave it empty to skip.")
            token = ""
        if token:
            self.claude_secret = (key, token)
            self.note("Claude sign-in", "ok", "token received; it goes only to the server")
        else:
            self.say("  Nothing entered: the server keeps any sign-in it already has; otherwise Claude jobs will not run until one is added (run ct-setup install again).")

    # ---- 4. the server ----
    def deliver(self, nested):
        d, rev, size = deliver_source(self.ssh, self.source, self.archive, self.say)
        self.ssh.send(f"cat > {d}/answers.toml", toml_out.dump(nested, full=True).encode())
        pub = self.machine.read(str(self.home / "approval" / "key.pub"))
        self.approver_sent = bool(pub and pub.strip())
        if self.approver_sent:
            self.ssh.send(f"cat > {d}/approver.pub", pub.encode())                       # a public key: it only lets the worker CHECK approvals
        self.mailbox_sent = False
        if getattr(getattr(self, "client", None), "direct", False):
            mail = self.machine.read(str(transport.for_config(self.cfg).client_key(self.cfg)) + ".pub")
            if mail and mail.strip().startswith("ssh-"):
                self.ssh.send(f"cat > {d}/mailbox-client.pub", mail.encode())             # a public key: it lets this computer into the mailbox shell and nothing else
                self.mailbox_sent = True
        if self.claude_secret:
            self.ssh.send(f"umask 077; cat > {d}/claude.secrets", f"{self.claude_secret[0]}={self.claude_secret[1]}\n".encode())
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
        base = (f"env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={d}/src python3 -m ct.setup server --no-banner --answers {d}/answers.toml --rev {rev} --source {d}"
                + (f" --secrets-file {d}/claude.secrets" if self.claude_secret else "")
                + (f" --approver-file {d}/approver.pub" if getattr(self, "approver_sent", False) else "") + (f" --mailbox-key-file {d}/mailbox-client.pub" if getattr(self, "mailbox_sent", False) else "") + (" --verbose" if self.verbose else ""))
        if self.dry_run:
            r = self.ssh.capture(base)
            self.say((r.stdout or "") + (r.stderr or ""))
            return
        user, host = self.ssh.user or "the administrator", self.cfg.server.host
        if self.ui is not None:
            self.ui.sudo_card(user, host)
            prompt = None             # the card above says what is asked; sudo's own prompt (newer sudo wraps a custom one in "[sudo: ...]") stays as it is
        else:
            self.say(f"  The server will ask for {user}'s sudo password, and for a yes to each protection. Only the server sees the password.\n")
            prompt = f"[sudo] password for {user}: "
        # one remote shell: ask for the password first (`sudo -v`), leave a marker, then run the setup under the sudo that is already unlocked.
        # Nothing else prints until the marker exists, so the password prompt is never interleaved with progress lines.
        marker = f"{d}/.authenticated"
        ask = f"-p {shlex.quote(prompt)} " if prompt else ""                      # (a variable, not a quote inside the f-string: Python 3.11 refuses that)
        script = f"sudo -n true 2>/dev/null || sudo {ask}-v; sudo -n true && touch {marker} && sudo {base} --apply --wait-for-keys {self.wait_for_keys}"
        remote = f"sh -c {shlex.quote(script)}"
        proc = self.ssh.popen(remote, tty=True)
        dk = keys_mod.DeployKeys(self.cfg, self.machine.run, self.machine.which, say=self.say, note=self.note)
        shown = False
        authed = False
        while proc.poll() is None:
            self._left_edge()                                  # ssh -t turns the terminal's newline handling off; what is printed here must still start at the left edge
            if not authed:
                authed = self.ssh.capture(f"test -e {marker}").returncode == 0
                if not authed:
                    self._sleep(1)
                    continue
            got = {} if getattr(getattr(self, "client", None), "direct", False) else keys_mod.published(self.ssh, self.cfg.paths.config)      # Direct has no deploy keys; the files an earlier GitHub install left behind must not start the loop (seen: "could not check the deploy keys on None/...")
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
                             [f"Add the queue key at https://github.com/{self.cfg.github.owner}/{self.cfg.github.queue_repo}/settings/keys (leave write access OFF).",
                              f"Add the results key at https://github.com/{self.cfg.github.owner}/{self.cfg.github.results_repo}/settings/keys (tick Allow write access).",
                              "The keys themselves were printed above. Then run ct-setup install again; it continues from here."])
        if rc != 0:
            raise SetupError(f"The server setup stopped (exit {rc})", "Read the messages above. Running ct-setup install again continues where it left off.")
        self.note("server setup", "done")
        self.mark("server", rev=rev)

    def reresolve_over_ipv4(self):
        """The worker's IPv6 was just switched off (the wizard's own question), so this computer must stop using the IPv6 addresses the network still remembers for its name (mDNS keeps them for a while)
        and ask again for its IPv4 one. ssh and git are told to use IPv4 only (admin.ssh.family_for); this proves a name for it exists and says which address it is. Found on a real worker, where the
        stale IPv6 addresses cost a connect timeout each, 30 seconds for every connection."""
        from ..admin.ssh import family_for
        if family_for(self.cfg) != "inet":
            return
        host, port = self.cfg.server.host, int(self.cfg.server.ssh_port)
        try:
            ip = self.resolve4(host, port)
        except OSError as e:
            self.note("connection", "failed", f"{host} has no IPv4 address on this network ({e}); the worker's IPv6 is off, so it cannot be reached by that name. Use its IPv4 address as server.host")
            return
        self.note("connection", "ok", f"this computer reaches the worker over IPv4 only now ({ip}), because its IPv6 is off")

    def hold_worker(self):
        """After a failed setup: stop the worker's job loop. Returns a plain sentence saying what is now true, or the one command to run. The setup's own sudo cannot be reused here (sudo remembers a
        password per terminal, and this is another connection), so unless sudo needs no password a person at a terminal is asked first."""
        c = self.cfg
        units = [c.unit(p) for p in ("fetch.timer", "step.timer", "registry-sync.timer", "update.timer", "claude-update.timer")]
        cmd = "systemctl disable --now " + " ".join(units)
        stopped = "The worker's job loop was stopped, so it fetches and runs nothing until the setup is finished."
        try:
            if self.ssh.capture(f"sudo -n {cmd}").returncode == 0:
                self.note("worker job loop", "stopped", "the setup did not finish, so nothing is fetched or started on the worker")
                return stopped
        except Exception:
            pass
        if self.ask is not None and sys.stdin.isatty():
            answer = self.ask(ui_mod.confirm_prompt("The worker's job loop is running although its setup failed. Stop it now? (it asks for the administrator's sudo password on the worker)")).strip().lower()
            if answer in ("", "y", "yes") and self.ssh.interactive(f"sudo {cmd}") == 0:
                self.note("worker job loop", "stopped", "the setup did not finish, so nothing is fetched or started on the worker")
                return stopped
        return f"The worker's job loop may still be running: stop it with `ssh -t {self.ssh.user}@{self.ssh.host} 'sudo {cmd}'`"

    def _left_edge(self):
        try:
            if sys.stdin.isatty():
                ptyrun.keep_newline_translation(sys.stdin.fileno())
        except (ValueError, OSError):
            pass

    def cleanup(self, d):
        remove_remote_dir(self.ssh, d)

    # ---- 5. first job ----
    def first_job(self):
        self._heading("A first job", "4 of 4")
        ok = self.verify(self.cfg) if self.verify else True
        if ok:
            self.report.append(("a job went through the whole loop", "ok", ""))      # shown in the closing card, not as a stray row of its own
        else:
            self.note("a job went through the whole loop", "failed")
        if ok:
            self.mark("job")
        else:
            self.diagnose_worker()
        return ok

    def diagnose_worker(self):
        """The first job did not complete: ask the server why, so the person is not left staring at "queued". Read-only; any failure to ask is ignored."""
        if self.ssh is None or self.cfg is None:
            return
        name = self.cfg.identity.name
        try:
            r = self.ssh.capture(f"for u in fetch step; do printf '%s: ' $u; systemctl is-active {name}-$u.service; done; "
                                 f"journalctl -u {name}-fetch.service -u {name}-step.service -n 8 --no-pager -o cat 2>&1 | tail -8")
        except Exception:
            return
        text = (r.stdout or "").strip()
        if text:
            self.say("\n  What the server says about its own passes:")
            for line in text.splitlines():
                self.say("    " + line[:160])
            self.say("  More: ct-admin status")

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
        self.claude_signin()
        self.client_half(nested)
        d = None
        try:
            d, rev = self.deliver(nested)
            self.server_half(d, rev)
        finally:
            self.cleanup(d)
        if not self.dry_run:
            self.reresolve_over_ipv4()
        if getattr(getattr(self, "client", None), "direct", False) and not self.dry_run:
            try:
                self.report += self.client.finish_direct()
            except SetupError as e:                                   # the worker's half is done and its timers run, but this computer cannot use it: stop the job loop, say why
                stopped = self.hold_worker()
                raise SetupError(e.title, e.why, [stopped, *e.steps])
        if not self.dry_run:
            self.first_job()
        return self.report


def default_verify(cfg, say=None, sleep=time.sleep, attempts=8, wait_s=15):
    """Send `sys-status` and wait for its result; the worker needs a minute after its first start to publish what it can run, so try a few times."""
    say = say or _sink()
    from ..admin import cli as admin_cli
    from ..client import cli as client_cli
    ok = False
    for i in range(attempts):
        client_cli.LAST_RESULT.clear()
        if client_cli.main(["submit", "sys-status", "--here", "--wait", "--timeout", "200"]) == 0:
            ok = True
            break
        last = client_cli.LAST_RESULT
        if last.get("locked") or last.get("status"):        # a real answer (locked, or the job ran and failed): trying again would only repeat it
            why = "the worker is locked" if last.get("locked") else f"the job ended with status {last['status']}"
            say(f"  Not trying again: {why}. Fix that first (ct-admin quarantine, ct-admin output <job>), then run ct-setup install again; it continues from here.")
            return False
        say(f"  (the worker is still starting; trying again, {i + 1} of {attempts})")
        sleep(wait_s)
    return ok and admin_cli.main(["check"]) == 0
