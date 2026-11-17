"""ct-setup install: one command from your computer. A fake SSH connection and a fake local machine record everything; real git repositories stand in for GitHub."""
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import RepoFixture, sh  # noqa: F401  (support sets HOME to a throwaway folder)
from test_setup import OS_RELEASE, FakeHost  # noqa: E402

from ct import config
from ct.admin.ssh import Ssh
from ct.client.repo import ClientError
from ct.setup import cli as setup_cli
from ct.setup import install, keys, toml_out
from ct.setup.host import Result
from ct.setup.server import EXIT_WAITING, SetupError

QUEUE_PUB = "ssh-ed25519 QQQQQQQQ claude-tenta-queue"
RESULTS_PUB = "ssh-ed25519 RRRRRRRR claude-tenta-results"
REMOTE = "/tmp/ct-install.AbC123"
GIVEN = {"server.host": "worker.example", "server.admin_user": "admin", "github.owner": "someone"}


class FakeProc:
    """A server-half process that runs for a few polls, publishing its deploy keys on the way, then exits with `rc`."""

    def __init__(self, ssh, rc, publish_after=2, polls=6):
        self.ssh, self.rc, self.publish_after, self.polls, self.n, self.returncode = ssh, rc, publish_after, polls, 0, None

    def poll(self):
        self.n += 1
        if self.n == 1:
            self.ssh.exists.add(REMOTE + "/.authenticated")                 # the password was entered: the remote shell leaves this marker
        if self.n == self.publish_after:
            self.ssh.files["/etc/claude-tenta/deploy-keys/queue.pub"] = QUEUE_PUB + "\n"
            self.ssh.files["/etc/claude-tenta/deploy-keys/results.pub"] = RESULTS_PUB + "\n"
        if self.n >= self.polls:
            self.returncode = self.rc
            return self.rc
        return None


class FakeSsh:
    host, user = "worker.example", "admin"
    target = "admin" + "@" + "worker.example"
    quote = staticmethod(Ssh.quote)

    def __init__(self, os_release=OS_RELEASE, rc=0, connect=True):
        self.files = {"/etc/os-release": os_release, "/proc/meminfo": "MemTotal:        8000000 kB\n", "/proc/cpuinfo": "processor : 0\nprocessor : 1\n"}
        self.exists = {"/run/systemd/system"}
        self.calls, self.sent, self.popen_calls, self.rc, self.connect = [], [], [], rc, connect

    def capture(self, remote):
        self.calls.append(remote)
        if not self.connect:
            raise ClientError("Could not connect to worker.example", "Permission denied (publickey)")
        parts = shlex.split(remote.replace("2>/dev/null", "").strip()) if "mktemp" not in remote else ["mktemp"]
        if parts[:1] == ["cat"]:
            text = self.files.get(parts[1])
            return subprocess.CompletedProcess(remote, 0 if text is not None else 1, text or "", "")
        if parts[:2] == ["test", "-e"]:
            return subprocess.CompletedProcess(remote, 0 if parts[2] in self.exists else 1, "", "")
        if parts[:1] == ["mktemp"]:
            return subprocess.CompletedProcess(remote, 0, REMOTE + "\n", "")
        if remote.startswith("env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="):
            return subprocess.CompletedProcess(remote, 0, "PREVIEW OUTPUT\n", "")
        return subprocess.CompletedProcess(remote, 0, "", "")

    def send(self, remote, data):
        self.sent.append((remote, data))
        return subprocess.CompletedProcess(remote, 0, b"", b"")

    def popen(self, remote, tty=True):
        self.popen_calls.append((remote, tty))
        return FakeProc(self, self.rc)


class InstallBase(RepoFixture):
    def setUp(self):
        super().setUp()
        self.sign_in_output = ""                                          # what a fake `claude setup-token` prints
        self.said, self.machine = [], FakeHost(env={"HOME": str(self.tmp / "me"), "TOOLS": "git ssh gh"})
        self.machine.replies["gh repo view"] = Result(0)
        self.machine.replies["gh repo deploy-key list"] = Result(0, "[]")
        self.ssh = FakeSsh()
        self.verified = []
        self.repos.clone_all = lambda home: [("exists", str(self.repos.queue)), ("exists", str(self.repos.results))]

    def installer(self, ssh=None, **kw):
        ssh = ssh or self.ssh
        kw.setdefault("archive", lambda src: (b"TARDATA", "a" * 40, False))
        self.interactive_calls = []
        kw.setdefault("interactive", lambda argv: (self.interactive_calls.append(argv), (0, self.sign_in_output))[1])       # never runs a real command on the machine the tests run on
        return install.Installer(self.tmp, ssh_factory=lambda h, u, p: ssh, local=self.machine, say=self.said.append, given={**GIVEN, **kw.pop("given", {})},
                                 use_defaults=True, repos_factory=lambda cfg: self.repos, poll_s=1, sleep=lambda s: None,
                                 verify=kw.pop("verify", lambda cfg: self.verified.append(cfg.github.owner) or True), home=self.tmp / "client-home", **kw)

    def ran_local(self, prefix):
        return self.machine.ran(prefix)


class HappyPath(InstallBase):
    def test_one_command_does_both_sides_and_the_deploy_keys_and_a_first_job(self):
        inst = self.installer()
        inst.run_all()
        # this side
        self.assertTrue(self.machine.exists(self.machine.env["HOME"] + "/.config/claude-tenta/ct.toml"))
        # the release and the answers went to a private folder on the server, and only there
        sent = dict(self.ssh.sent)
        self.assertEqual(sent[f"tar -xf - -C {REMOTE}"], b"TARDATA")
        self.assertIn(f"cat > {REMOTE}/answers.toml", sent)
        # the server half ran under sudo, over a terminal, waiting for the keys, from exactly that commit
        remote, tty = self.ssh.popen_calls[0]
        self.assertTrue(tty)
        self.assertTrue(remote.startswith("sh -c "))
        self.assertIn("sudo -p", remote)                                    # the password is asked first, with a prompt that says what it is
        self.assertIn(f"-v; sudo -n true && touch {REMOTE}/.authenticated && sudo env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={REMOTE}/src python3 -m ct.setup server", remote)
        for piece in ("--apply", "--no-banner", "--wait-for-keys 900", f"--answers {REMOTE}/answers.toml", f"--rev {'a' * 40}", f"--source {REMOTE}"):
            self.assertIn(piece, remote)
        # the keys went to GitHub with the right access, without anyone copying them
        adds = self.ran_local("gh repo deploy-key add")
        self.assertEqual(len(adds), 2)
        by_repo = {a[a.index("-R") + 1]: a for a in adds}
        self.assertNotIn("--allow-write", by_repo["someone/claude-tenta-queue"])
        self.assertIn("--allow-write", by_repo["someone/claude-tenta-results"])
        self.assertEqual(by_repo["someone/claude-tenta-queue"][by_repo["someone/claude-tenta-queue"].index("-t") + 1], "claude-tenta-server")
        # the working folder was removed and the first job was sent
        self.assertIn(f"rm -rf {REMOTE}", self.ssh.calls)
        self.assertEqual(self.verified, ["someone"])
        self.assertTrue(any("deploy key for someone/claude-tenta-queue" in line and "read-only" in line for line in self.said))
        self.assertTrue(any("deploy key for someone/claude-tenta-results" in line and "read and write" in line for line in self.said))

    def test_a_key_that_is_already_on_github_is_not_added_twice(self):
        self.machine.replies["gh repo deploy-key list -R someone/claude-tenta-queue"] = Result(0, json.dumps([{"key": QUEUE_PUB.rsplit(" ", 1)[0], "title": "old", "readOnly": True}]))
        self.installer().run_all()
        adds = self.ran_local("gh repo deploy-key add")
        self.assertEqual(len(adds), 1)
        self.assertIn("someone/claude-tenta-results", adds[0])
        self.assertTrue(any("PRESENT" in line for line in self.said))

    def test_the_answers_sent_to_the_server_are_complete_and_valid(self):
        self.installer().run_all()
        text = dict(self.ssh.sent)[f"cat > {REMOTE}/answers.toml"].decode()
        import tomllib
        data = tomllib.loads(text)
        clean, problems = config.validate(data)
        self.assertEqual(problems, [])
        self.assertEqual(data["github"]["owner"], "someone")
        self.assertIn("lan_deny_out", data["network"])               # an answer equal to its default is still written
        self.assertIn("worker_user", data["server"])
        self.assertEqual(clean["server"]["admin_user"], "admin")

    def test_the_servers_own_facts_become_the_suggestions(self):
        ssh = FakeSsh()
        ssh.files["/proc/meminfo"] = "MemTotal:        2000000 kB\n"        # about 2 GB
        inst = self.installer(ssh=ssh)
        inst.run_all()
        text = dict(ssh.sent)[f"cat > {REMOTE}/answers.toml"].decode()
        self.assertIn('profile = "old-laptop"', text)

    def test_everything_is_asked_once_here_and_nothing_is_asked_on_the_server(self):
        self.installer().run_all()
        self.assertEqual(len(self.ssh.popen_calls), 1)
        self.assertNotIn("--yes", self.ssh.popen_calls[0][0])


class FirstJobRetries(unittest.TestCase):
    """The first job is tried again only while the worker is still starting, never after it gave a real answer."""

    def run_verify(self, outcomes):
        """outcomes: one (exit code, LAST_RESULT) per attempt."""
        from ct.client import cli as client_cli
        from ct.admin import cli as admin_cli
        script, said, calls = list(outcomes), [], []

        def fake_main(argv):
            calls.append(argv)
            rc, last = script.pop(0)
            client_cli.LAST_RESULT.clear()
            client_cli.LAST_RESULT.update(last)
            return rc
        saved = (client_cli.main, admin_cli.main)
        client_cli.main, admin_cli.main = fake_main, lambda argv: 0
        try:
            ok = install.default_verify(None, say=said.append, sleep=lambda s: None, attempts=8)
        finally:
            client_cli.main, admin_cli.main = saved
        return ok, len(calls), said

    def test_a_worker_that_is_locked_is_not_retried(self):
        ok, tries, said = self.run_verify([(3, {"locked": True})] * 8)
        self.assertEqual((ok, tries), (False, 1))
        self.assertTrue(any("Not trying again" in s and "locked" in s for s in said))

    def test_a_job_that_ran_and_failed_is_not_retried(self):
        ok, tries, said = self.run_verify([(1, {"status": "error", "note": "boom"})] * 8)
        self.assertEqual((ok, tries), (False, 1))
        self.assertTrue(any("status error" in s for s in said))

    def test_no_answer_yet_is_retried_until_it_works(self):
        ok, tries, said = self.run_verify([(1, {}), (1, {}), (0, {"status": "ok"})])
        self.assertEqual((ok, tries), (True, 3))
        self.assertEqual(sum("still starting" in s for s in said), 2)


class OldProcessor(InstallBase):
    def test_the_claude_question_is_still_asked_and_the_summary_says_an_older_release_is_used(self):
        self.ssh.files["/proc/cpuinfo"] = "processor : 0\nflags : fpu sse sse2 sse4_1\n"
        inst = self.installer(given={"claude.auth": "account"})
        inst.run_all()
        answers = dict(self.ssh.sent)[f"cat > {REMOTE}/answers.toml"].decode()
        self.assertIn('auth = "account"', answers)
        self.assertTrue(any("needs an older release here" in line for line in self.said))


class ClaudeSignInHere(InstallBase):
    """The sign-in happens on this computer, right after the questions: setup-token, then a hidden paste, and the token goes only to the server."""

    TOKEN = "sk-ant-oat01-" + "a" * 40
    TOKEN2 = "sk-ant-oat01-" + "b" * 40

    class Screen:
        def __init__(self, tokens, install=True):
            self.tokens, self.install, self.cards, self.asked, self.headings = list(tokens), install, [], [], []

        def heading(self, text, step=None):
            self.headings.append(text)

        def sub(self, text):
            pass

        def protection(self, info):
            self.cards.append(info["id"])
            return self.install

        def ask_secret(self, q):
            self.asked.append(q)
            return self.tokens.pop(0) if self.tokens else ""

    def signed_in(self, auth="account", tokens=None, install=True, dry=False):
        screen = self.Screen([self.TOKEN] if tokens is None else tokens, install)
        inst = self.installer(ui=None, dry_run=dry)
        inst.ui = screen                                            # only the sign-in uses it here, so the whole question flow need not be driven
        inst.cfg = config.Config(config.validate({"claude": {"auth": auth}})[0])
        inst.ssh = self.ssh
        inst.claude_signin()
        return inst, screen

    PRINTED = ("\n Your OAuth token (valid for 1 year):\n\n " + TOKEN[:60] + "\n " + TOKEN[60:] + "\n\n Store this token securely.\n")

    def test_account_runs_setup_token_here_and_reads_the_token_it_prints_without_asking(self):
        self.sign_in_output = self.PRINTED
        inst, screen = self.signed_in()
        self.assertEqual(self.interactive_calls, [["claude", "setup-token"]])
        self.assertEqual(screen.asked, [])                                  # nothing to paste
        self.assertEqual(inst.claude_secret, ("CLAUDE_CODE_OAUTH_TOKEN", self.TOKEN))
        self.assertTrue(self.ran_local("claude -p"))                        # and it was tried once before being trusted
        self.assertNotIn(self.TOKEN, "\n".join(self.said))

    def test_a_token_that_is_refused_when_tried_is_not_used_and_the_person_is_asked(self):
        self.sign_in_output = self.PRINTED
        self.machine.replies["claude -p"] = Result(0, '{"type":"result","is_error":true,"api_error_status":401,"result":"authentication_error"}')
        inst, screen = self.signed_in(tokens=[self.TOKEN2])
        self.assertEqual(len(screen.asked), 1)
        self.assertEqual(inst.claude_secret[1], self.TOKEN2)
        self.assertTrue(any("refused when tried" in line for line in self.said))

    def test_a_network_failure_while_trying_the_token_does_not_throw_it_away(self):
        self.sign_in_output = self.PRINTED
        self.machine.replies["claude -p"] = Result(1, "Could not reach the network")
        inst, screen = self.signed_in()
        self.assertEqual((screen.asked, inst.claude_secret[1]), ([], self.TOKEN))

    def test_a_sign_in_that_prints_no_token_falls_back_to_a_paste(self):
        self.sign_in_output = "Signed in.\n"
        inst, screen = self.signed_in()
        self.assertEqual(len(screen.asked), 1)
        self.assertIn("Paste the long token", screen.asked[0]["why"])
        self.assertTrue(any("no token could be read" in line for line in self.said))

    def test_the_token_goes_to_the_server_in_its_own_private_file_and_nowhere_else(self):
        inst, _ = self.signed_in()
        d, rev = inst.deliver({"server": {"admin_user": "admin"}})
        sent = dict(self.ssh.sent)
        secret_cmd = f"umask 077; cat > {d}/claude.secrets"
        self.assertEqual(sent[secret_cmd], f"CLAUDE_CODE_OAUTH_TOKEN={self.TOKEN}\n".encode())
        self.assertNotIn(self.TOKEN.encode(), sent[f"cat > {d}/answers.toml"])                  # never in the answers
        self.assertFalse(any(self.TOKEN in line for line in self.said))

    def test_a_broken_claude_here_offers_the_installer_then_uses_the_copy_it_installs(self):
        broken = {"claude --version": Result(1, "Error: claude native binary not installed.")}
        self.machine.replies.update(broken)
        home_claude = str(Path(self.machine.env["HOME"]) / ".local" / "bin" / "claude")
        self.machine.replies[f"{home_claude} --version"] = Result(1, "not yet")
        installed = []

        def after_install(argv, **kw):
            return None
        inst, screen = self.signed_in()
        self.assertIn("claude-install-here", screen.cards)
        self.assertTrue(self.ran_local("bash -c curl -fsSL https://claude.ai/install.sh | bash"))
        self.assertEqual(self.interactive_calls, [])                  # the copy it installed still does not run in this fake, so no sign-in was launched

    def test_declining_the_install_still_allows_pasting_a_token_made_elsewhere(self):
        self.machine.replies["claude --version"] = Result(1, "broken")
        self.machine.replies[f"{Path(self.machine.env['HOME']) / '.local' / 'bin' / 'claude'} --version"] = Result(1, "not installed")
        inst, screen = self.signed_in(install=False)
        self.assertFalse(self.ran_local("bash -c curl"))
        self.assertEqual(self.interactive_calls, [])
        self.assertEqual(inst.claude_secret[1], self.TOKEN)

    def test_api_key_is_only_pasted_nothing_is_run_here(self):
        inst, screen = self.signed_in(auth="api-key")
        self.assertEqual(self.interactive_calls, [])
        self.assertEqual(inst.claude_secret[0], "ANTHROPIC_API_KEY")

    def test_none_and_a_preview_do_nothing(self):
        for auth, dry in (("none", False), ("account", True)):
            inst, screen = self.signed_in(auth=auth, dry=dry)
            self.assertEqual((self.interactive_calls, screen.asked, inst.claude_secret), ([], [], None))

    def test_an_empty_paste_sends_no_secret_and_says_what_that_means(self):
        inst, _ = self.signed_in(tokens=[""])
        self.assertIsNone(inst.claude_secret)
        d, rev = inst.deliver({"server": {"admin_user": "admin"}})
        self.assertFalse(any("claude.secrets" in remote for remote, _ in self.ssh.sent))
        self.assertTrue(any("keeps any sign-in it already has" in line for line in self.said))

    def test_something_that_is_not_a_token_is_asked_for_again(self):
        inst, screen = self.signed_in(tokens=["two words", self.TOKEN])
        self.assertEqual(len(screen.asked), 2)
        self.assertEqual(inst.claude_secret[1], self.TOKEN)

    def test_the_server_command_carries_the_secrets_file_only_when_there_is_one(self):
        inst, _ = self.signed_in()
        inst.ui = None
        inst.cfg = config.Config(config.validate({"server": {"host": "worker.example", "admin_user": "admin"}, "claude": {"auth": "account"}})[0])
        inst.server_half(REMOTE, "a" * 40)
        self.assertIn(f"--secrets-file {REMOTE}/claude.secrets", self.ssh.popen_calls[-1][0])
        inst.claude_secret = None
        inst.server_half(REMOTE, "a" * 40)
        self.assertNotIn("--secrets-file", self.ssh.popen_calls[-1][0])


class ApproverKeyDelivery(InstallBase):
    def test_the_public_key_goes_to_the_server_and_the_command_says_where(self):
        pub = "ssh-" + "ed25519 " + "AAAAC3NzaC1lZDI1NTE5AAAAI" + "PUBLICKEY" * 4 + " ct-approval"
        (self.tmp / "client-home" / "approval").mkdir(parents=True)
        self.machine.put(str(self.tmp / "client-home" / "approval" / "key.pub"), pub + "\n")
        inst = self.installer()
        inst.ssh = self.ssh
        d, rev = inst.deliver({"server": {"admin_user": "admin"}})
        self.assertEqual(dict(self.ssh.sent)[f"cat > {d}/approver.pub"], (pub + "\n").encode())
        inst.cfg = config.Config(config.validate({"server": {"host": "worker.example", "admin_user": "admin"}})[0])
        inst.server_half(d, rev)
        self.assertIn(f"--approver-file {d}/approver.pub", self.ssh.popen_calls[-1][0])

    def test_without_a_key_nothing_is_sent(self):
        inst = self.installer()
        inst.ssh = self.ssh
        d, rev = inst.deliver({"server": {"admin_user": "admin"}})
        self.assertFalse(any("approver.pub" in remote for remote, _ in self.ssh.sent))


class Rerun(InstallBase):
    """Running install again after a stopped run: earlier answers are the defaults, and a finished server setup is not repeated."""

    def finished_server_but_not_the_job(self, ok_job=False):
        first = self.installer(verify=lambda cfg: ok_job)
        first.run_all()
        return first

    def test_a_stopped_first_job_is_resumed_without_the_questions_or_the_server(self):
        self.finished_server_but_not_the_job()
        popens, asked = len(self.ssh.popen_calls), []
        again = self.installer(given={}, ask=lambda q: asked.append(q) or "")
        again.run_all()
        self.assertEqual(len(self.ssh.popen_calls), popens, "the server setup was not run again")
        self.assertEqual(self.verified, ["someone"])
        self.assertTrue(any("did not finish the first job" in line for line in self.said))
        self.assertIn("job", again.progress())

    def test_a_finished_install_is_not_resumed(self):
        self.finished_server_but_not_the_job(ok_job=True)
        self.assertFalse(self.installer().resumable())

    def test_a_changed_release_is_installed_again(self):
        self.finished_server_but_not_the_job()
        changed = self.installer(archive=lambda src: (b"TARDATA", "b" * 40, False))
        self.assertFalse(changed.resumable())

    def test_a_preview_never_records_progress(self):
        inst = self.installer(dry_run=True)
        inst.run_all()
        self.assertEqual(inst.progress(), {})

    def test_the_servers_answers_are_remembered_too_but_not_secrets(self):
        inst = self.installer(given={"hardware.profile": "workstation", "network.ipv6": "disabled", "alerts.kind": "ntfy", "alerts.url": "https://ntfy.example/private-topic"})
        inst.run_all()
        prev = self.installer(given={}).previous_answers()
        self.assertEqual((prev["hardware.profile"], prev["network.ipv6"], prev["alerts.kind"]), ("workstation", "disabled", "ntfy"))
        self.assertNotIn("alerts.url", prev)
        self.assertNotIn("private-topic", self.machine.read(inst.answers_file()))

    def test_the_earlier_answers_are_the_defaults(self):
        self.finished_server_but_not_the_job()
        again = self.installer(given={})
        self.assertEqual(again.previous_answers()["server.host"], "worker.example")
        self.assertEqual(again.previous_answers()["github.owner"], "someone")


class WithTheScreen(InstallBase):
    def installer_with_screen(self, events):
        from test_tui import prompter
        ui, term = prompter(events)
        inst = install.Installer(self.tmp, ssh_factory=lambda h, u, p: self.ssh, local=self.machine, say=self.said.append, given={}, use_defaults=False, ask=None, ui=ui,
                                 repos_factory=lambda cfg: self.repos, poll_s=1, sleep=lambda s: None, verify=lambda cfg: True, home=self.tmp / "client-home",
                                 archive=lambda src: (b"TARDATA", "a" * 40, False))
        return inst, term

    def test_the_questions_use_the_screen_and_the_facts_wait_until_they_are_done(self):
        from test_tui import typed
        from ct.setup import questions
        total = questions.count_to_ask(("client", "server"), {})
        self.assertEqual(total, 7)                                   # the server, your account on it, GitHub, the machine, IPv6, alerts, Claude sign-in: everything else has a safe default
        events = typed("worker.example", "admin") + typed("someone", "", "", "", "") + ["enter"]       # the connection questions, the rest, then Enter on the review
        inst, term = self.installer_with_screen(events)
        inst.run_all()
        screen = term.screen.text()
        self.assertIn("@claude-tenta", screen)
        self.assertIn(f"{total} of {total} done", screen)
        for label, value in (("Server address", "worker.example"), ("Admin account", "admin"), ("GitHub account", "someone")):
            self.assertRegex(screen, rf"✓ {label}\s+{value}")
        self.assertEqual(len([ln for ln in screen.splitlines() if "✓" in ln]), total)
        said = "\n".join(self.said)
        self.assertIn("Operating system", said)
        self.assertIn("connected to the server", said)
        self.assertEqual(len(self.ssh.popen_calls), 1)

    def test_the_defaults_land_in_the_answers_sent_to_the_server(self):
        from test_tui import typed
        events = typed("worker.example", "admin") + typed("someone", "", "", "", "") + ["enter"]
        inst, _ = self.installer_with_screen(events)
        inst.run_all()
        import tomllib
        data = tomllib.loads(dict(self.ssh.sent)[f"cat > {REMOTE}/answers.toml"].decode())
        clean, problems = config.validate(data)
        self.assertEqual(problems, [])
        self.assertEqual(clean["server"]["ssh_port"], 22)                              # not asked, so the default
        self.assertEqual(clean["github"]["queue_repo"], "claude-tenta-queue")          # derived from the install name

    def test_stepping_back_from_the_first_other_question_returns_to_the_connection_questions(self):
        from test_tui import typed
        events = typed("worker.example", "admin") + ["up"] + ["enter", "enter"] + typed("someone", "", "", "", "") + ["enter"]
        inst, term = self.installer_with_screen(events)
        inst.run_all()
        self.assertEqual(len([c for c in self.ssh.calls if c == "true"]), 2)            # it connected again after the person went back
        self.assertEqual(len(self.ssh.popen_calls), 1)

    def test_advanced_settings_are_asked_only_when_asked_for(self):
        from test_tui import typed
        from ct.setup import questions
        every = questions.count_to_ask(("client", "server"), {}, advanced=True)
        self.assertGreater(every, 15)
        inst, term = self.installer_with_screen(typed("worker.example", "admin", "2222") + typed("someone") + typed(*[""] * (every - 4)) + ["enter"])
        inst.advanced = True
        inst.run_all()
        self.assertIn("SSH port", term.screen.text())

    def test_without_a_terminal_nothing_changes_in_what_is_printed(self):
        self.installer().run_all()
        said = self.said
        self.assertLess([i for i, line in enumerate(said) if "Connecting to the server" in line][0], [i for i, line in enumerate(said) if "Operating system" in line][0])


class Failures(InstallBase):
    def test_an_unsupported_server_is_refused_before_anything_changes(self):
        ssh = FakeSsh(os_release="ID=fedora\nVERSION_ID=40\n")
        with self.assertRaises(SetupError) as e:
            self.installer(ssh=ssh).run_all()
        self.assertIn("not supported", e.exception.why)
        self.assertFalse(self.machine.exists(self.machine.env["HOME"] + "/.config/claude-tenta/ct.toml"))
        self.assertEqual(ssh.sent, [])
        self.assertEqual(self.ran_local("gh"), [])

    def test_a_server_that_cannot_be_logged_into_says_how_to_fix_it(self):
        with self.assertRaises(SetupError) as e:
            self.installer(ssh=FakeSsh(connect=False)).run_all()
        self.assertIn("ssh-copy-id admin" + "@" + "worker.example", " ".join(e.exception.steps))
        self.assertFalse(self.machine.exists(self.machine.env["HOME"] + "/.config/claude-tenta/ct.toml"))

    def test_missing_connection_answers_are_named(self):
        inst = install.Installer(self.tmp, ssh_factory=lambda *a: self.ssh, local=self.machine, say=self.said.append, given={}, use_defaults=True)
        with self.assertRaises(SetupError) as e:
            inst.run_all()
        self.assertIn("server.host", e.exception.why)

    def test_the_server_waiting_for_keys_is_reported_and_the_folder_still_removed(self):
        ssh = FakeSsh(rc=EXIT_WAITING)
        with self.assertRaises(SetupError) as e:
            self.installer(ssh=ssh).run_all()
        self.assertIn("still waiting for its deploy keys", e.exception.args[0])
        self.assertIn(f"rm -rf {REMOTE}", ssh.calls)
        self.assertEqual(self.verified, [])

    def test_a_failed_server_run_stops_and_cleans_up(self):
        ssh = FakeSsh(rc=3)
        with self.assertRaises(SetupError) as e:
            self.installer(ssh=ssh).run_all()
        self.assertIn("exit 3", e.exception.args[0])
        self.assertIn(f"rm -rf {REMOTE}", ssh.calls)
        self.assertEqual(self.verified, [])

    def test_an_odd_working_folder_name_is_never_used_or_removed(self):
        ssh = FakeSsh()
        orig = ssh.capture
        ssh.capture = lambda remote: subprocess.CompletedProcess(remote, 0, "/etc\n", "") if "mktemp" in remote else orig(remote)
        with self.assertRaises(SetupError):
            self.installer(ssh=ssh).run_all()
        self.assertFalse([c for c in ssh.calls if c.startswith("rm -rf")])
        self.assertEqual(ssh.sent, [])


class WithoutGh(InstallBase):
    def test_the_manual_steps_are_shown_with_the_keys_and_the_right_access(self):
        self.machine.env["TOOLS"] = "git ssh"
        self.installer().run_all()
        self.assertEqual(self.ran_local("gh "), [])
        text = "\n".join(self.said)
        self.assertIn("https://github.com/someone/claude-tenta-queue/settings/keys", text)
        self.assertIn("leave write access OFF", text)
        self.assertIn("https://github.com/someone/claude-tenta-results/settings/keys", text)
        self.assertIn("'Allow write access' ticked", text)
        self.assertIn(QUEUE_PUB, text)
        self.assertEqual(text.count("gh` is not available"), 1)


class Preview(InstallBase):
    def test_a_preview_creates_nothing_and_asks_for_no_password(self):
        inst = self.installer(dry_run=True)
        inst.run_all()
        self.assertEqual(self.ssh.popen_calls, [])
        self.assertEqual(self.ran_local("gh repo create"), [])
        self.assertEqual(self.ran_local("gh repo deploy-key add"), [])
        self.assertFalse(self.machine.exists(self.machine.env["HOME"] + "/.config/claude-tenta/ct.toml"))
        previews = [c for c in self.ssh.calls if c.startswith("env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=")]
        self.assertEqual(len(previews), 1)
        self.assertNotIn("sudo", previews[0])
        self.assertNotIn("--apply", previews[0])
        self.assertIn("PREVIEW OUTPUT", "\n".join(self.said))
        self.assertIn(f"rm -rf {REMOTE}", self.ssh.calls)
        self.assertEqual(self.verified, [])


class Packaging(unittest.TestCase):
    def test_a_git_checkout_is_sent_as_its_committed_files_only(self):
        d = Path(tempfile.mkdtemp())
        sh("git", "init", "-q", "-b", "main", str(d))
        (d / "src").mkdir()
        (d / "src" / "a.py").write_text("a = 1\n")
        sh("git", "add", "-A", cwd=d)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x", cwd=d)
        (d / "src" / "a.py").write_text("a = 2\n")                         # an uncommitted change
        (d / "secret.txt").write_text("untracked\n")
        data, rev, dirty = install.archive_source(d)
        import io
        import tarfile
        names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
        self.assertIn("src/a.py", names)
        self.assertNotIn("secret.txt", names)
        self.assertFalse([n for n in names if n.startswith(".git")])
        self.assertEqual(len(rev), 40)
        self.assertTrue(dirty)
        self.assertEqual(tarfile.open(fileobj=io.BytesIO(data)).extractfile("src/a.py").read(), b"a = 1\n")      # the committed content, not the edit

    def test_a_plain_folder_is_identified_by_its_contents(self):
        d = Path(tempfile.mkdtemp())
        (d / "src").mkdir()
        (d / "src" / "a.py").write_text("a = 1\n")
        (d / "src" / "__pycache__").mkdir()
        (d / "src" / "__pycache__" / "x.pyc").write_bytes(b"x")
        data, rev, dirty = install.archive_source(d)
        import io
        import tarfile
        self.assertEqual(tarfile.open(fileobj=io.BytesIO(data)).getnames(), ["src", "src/a.py"])
        self.assertRegex(rev, r"^[0-9a-f]{40}$")
        self.assertFalse(dirty)

    def test_toml_full_writes_every_answer(self):
        text = toml_out.dump({"network": {"lan_deny_out": True, "ipv6": "enabled"}}, full=True)
        self.assertIn("lan_deny_out = true", text)
        self.assertIn('ipv6 = "enabled"', text)
        self.assertNotIn("lan_deny_out", toml_out.dump({"network": {"lan_deny_out": True}}))


class DeployKeyUnits(unittest.TestCase):
    def cfg(self):
        return config.Config(config.validate({"github": {"owner": "someone"}, "server": {"admin_user": "admin"}})[0])

    def test_a_failed_add_reports_the_reason_and_is_retried_next_time(self):
        calls = []

        def run(argv):
            calls.append(argv)
            if argv[3] == "list":
                return Result(0, "[]")
            return Result(1, "HTTP 403: Resource not accessible by personal access token")
        dk = keys.DeployKeys(self.cfg(), run, lambda n: "/usr/bin/gh")
        out = dk.sync({"queue": QUEUE_PUB})
        self.assertIn("HTTP 403", out[0][1])
        self.assertNotIn("queue", dk.done)
        dk.sync({"queue": QUEUE_PUB})
        self.assertEqual(len([c for c in calls if c[3] == "add"]), 2)

    def test_published_reads_only_real_public_keys(self):
        ssh = FakeSsh()
        ssh.files["/etc/claude-tenta/deploy-keys/queue.pub"] = "ssh-ed25519 AAA c\n"
        ssh.files["/etc/claude-tenta/deploy-keys/results.pub"] = "not a key\n"
        self.assertEqual(keys.published(ssh, "/etc/claude-tenta"), {"queue": "ssh-ed25519 AAA c"})

    def test_complete_needs_both_keys(self):
        dk = keys.DeployKeys(self.cfg(), lambda a: Result(0, "[]"), lambda n: "gh")
        dk.sync({"queue": QUEUE_PUB})
        self.assertFalse(dk.complete)
        dk.sync({"results": RESULTS_PUB})
        self.assertTrue(dk.complete)


class Command(InstallBase):
    def test_the_install_command_runs_through_the_cli_and_a_preview_says_so(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = setup_cli.main(["install", "--dry-run", "--yes", "--source", str(self.tmp), "--set", "server.host=worker.example", "--set", "server.admin_user=admin",
                                   "--set", "github.owner=someone"], host=self.machine, repos_factory=lambda cfg: self.repos, ssh_factory=lambda h, u, p: self.ssh)
        self.assertEqual(code, 0)
        self.assertIn("Nothing was changed on the server", err.getvalue())

    def test_help_lists_install(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup_cli.main(["help"])
        self.assertIn("install", out.getvalue())


if __name__ == "__main__":
    unittest.main()
