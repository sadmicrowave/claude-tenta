"""ct-admin: the SSH layer, job review and approval pinned to a digest, proposals, adopt/retire, and a verified install with rollback."""
import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from support import REGISTRY, RepoFixture, sh  # noqa: F401  (support sets HOME to a throwaway folder)

from ct import approvals, clihelp, registry, theme
from ct.admin import cli, jobs, regedit, release
from ct.admin import workers as workers_mod
from ct.admin.ssh import Ssh
from ct.client.repo import ClientError


INSTALLED = "cat /etc/testct/installed-rev 2>/dev/null"


class FakeSsh:
    """Records every remote command; `answers` maps a substring of a captured command to its stdout."""

    host, user = "worker.example", "admin"
    target = "admin" + "@" + "worker.example"

    def __init__(self):
        self.calls, self.captured, self.answers, self.rc = [], [], {}, {}
        self._run = lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", "")

    def argv(self, remote=None, **k):
        return ["ssh", "x", remote or ""]

    quote = staticmethod(Ssh.quote)

    def interactive(self, remote):
        self.calls.append(remote)
        return next((rc for key, rc in self.rc.items() if key in remote), 0)

    def capture(self, remote):
        self.captured.append(remote)
        hit = self.answers.get(remote) or next((self.answers[k] for k in sorted(self.answers, key=len) if k in remote and k != remote), None)
        if hit is not None:
            return subprocess.CompletedProcess([], 0, hit, "")
        return subprocess.CompletedProcess([], 0, "", "")


class SshTests(unittest.TestCase):
    def test_argv_is_key_only_and_short_lived(self):
        a = Ssh("server.example", "admin", 2222, control_dir="/tmp/ct-test-ctl").argv("true", batch=True, tty=False)
        self.assertEqual(a[0], "ssh")
        self.assertIn("BatchMode=yes", a)
        self.assertIn("ControlPersist=60", a)
        self.assertIn("admin" + "@" + "server.example", a)
        self.assertEqual(a[a.index("-p") + 1], "2222")
        self.assertEqual(a[-1], "true")
        self.assertNotIn("-t", a)
        self.assertIn("-t", Ssh("h", control_dir="/tmp/ct-test-ctl").argv("x", tty=True))

    def test_the_control_socket_path_fits_a_unix_socket_even_with_macos_style_temp_folders(self):
        import tempfile
        long_tmp = "/var/folders/k7/nb4y713s0dx6nd0d_zvfcqt40000gp/T"
        with unittest.mock.patch.object(tempfile, "gettempdir", return_value=long_tmp), unittest.mock.patch.dict(os.environ, {"TMPDIR": long_tmp}):
            s = Ssh("h", "u", control_dir=None)
            control = [a for a in s.argv("x") if a.startswith("ControlPath=")][0].split("=", 1)[1]
        # %C is 40 characters, and ssh appends a dot and 16 more while it connects
        self.assertLessEqual(len(control.replace("%C", "x" * 40)) + 17, 103, control)

    def test_an_address_that_looks_like_an_option_is_refused(self):
        with self.assertRaises(ClientError):
            Ssh("-oProxyCommand=evil")

    def test_a_failed_connection_is_explained(self):
        s = Ssh("h", run=lambda argv, **k: subprocess.CompletedProcess(argv, 255, "", "Permission denied (publickey)"), control_dir="/tmp/ct-test-ctl")
        with self.assertRaises(ClientError) as c:
            s.capture("true")
        self.assertIn("Permission denied", c.exception.why)

    def test_arguments_are_quoted(self):
        self.assertEqual(Ssh.quote(["approve", "a b", "x;y"]), "approve 'a b' 'x;y'")


class AdminBase(RepoFixture):
    def setUp(self):
        super().setUp()
        self.repos.push_files("main", {"registry.yaml": REGISTRY}, "chore: registry")
        self.ssh = FakeSsh()
        src = self.tmp / "release-src"
        sh("git", "init", "-q", "-b", "main", str(src))
        self.src_dir = src
        self.rel = [self.release_commit({"src/ct/a.py": "a=1\n", "README.md": "x\n"}, "first")]
        self.source = release.Source(self.tmp / "source.git", str(src), "main")
        self.tty = False
        self.ctx = cli.Context(self.repos, self.ssh, self.source, name="testct", config_dir="/etc/testct", code_dir="/opt/testct", home=self.home,
                               ask=lambda p: self.answers.pop(0), is_tty=lambda: self.tty, run_result=lambda jid: self.results_followed.append(jid) or 0)
        self.results_followed = []
        self.repos.pull_results = lambda: None     # the results repo is filled by hand below

    def release_commit(self, files, msg):
        for path, text in files.items():
            p = self.src_dir / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        sh("git", "add", "-A", cwd=self.src_dir)
        sh("git", "commit", "-q", "-m", msg, cwd=self.src_dir)
        return sh("git", "rev-parse", "HEAD", cwd=self.src_dir)

    def put_result(self, jid, **body):
        d = self.repos.results / "results"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{jid}.json").write_text(json.dumps({"id": jid, **body}))

    def put_job(self, jid, **extra):
        self.repos.push_files("jobs", {f"jobs/{jid}.json": json.dumps({"id": jid, "class": "careful", **extra})}, f"job {jid}")

    def admin(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args), ctx=self.ctx)
        return code, out.getvalue(), err.getvalue()


class WakeAndAudit(AdminBase):
    """One short command reaches every worker, or the one named, with nothing but its name: its address and account come from its own configuration."""

    def test_wake_and_audit_reach_the_main_worker_with_no_paths_or_options(self):
        self.ctx.workers_file = self.tmp / "workers.toml"
        self.assertEqual(self.admin("wake")[0], 0)
        self.assertEqual(self.admin("audit")[0], 0)
        self.assertEqual(self.ssh.calls, ["ct-worker wake", "sudo ct-update --audit"])

    def test_a_worker_is_named_once_and_found_by_that_name(self):
        self.ctx.workers_file = self.tmp / "workers.toml"
        code, out, err = self.admin("wake", "--worker", "nobody")
        self.assertEqual(code, 2)
        self.assertIn("no worker called 'nobody'", out + err)
        self.assertEqual(self.ssh.calls, [])
        self.assertIs(self.ctx.for_worker("testct"), self.ctx)                    # the main worker answers to its own name


class SignedApprovals(AdminBase):
    """Approving here: signed with a key on this computer and published; nothing is run on the worker."""

    def setUp(self):
        super().setUp()
        keydir = self.home / "approval"
        keydir.mkdir(parents=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(keydir / "key")], check=True)
        self.pub = (keydir / "key.pub").read_text()
        self.signers = self.tmp / "approvers"
        self.signers.write_text(approvals.with_approver("", self.pub))
        self.put_job("job-b")
        self.digest = jobs.digest_of(jobs.job_bytes(self.repos, "job-b"))
        self.tty = True

    def worker_publishes(self, **info):
        (self.repos.results / "registry.json").write_text(json.dumps(info))

    def published(self, jid):
        self.repos.fetch_queue()
        return self.repos.show_file(f"origin/{approvals.BRANCH}", approvals.path_in_repo(jid))

    def test_the_approval_is_signed_here_pinned_to_the_reviewed_file_and_nothing_runs_on_the_worker(self):
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()))
        self.answers.append("y")
        code, out, err = self.admin("approve", "job-b")
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.calls, [])                                           # no login, no sudo
        text = self.published("job-b")
        self.assertTrue(approvals.check(text, "job-b", self.digest, self.signers)[0])
        self.assertFalse(approvals.check(text, "job-b", "0" * 64, self.signers)[0])
        self.assertIn("No login", out + err)

    def test_approve_all_signs_every_viewed_job(self):
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()))
        self.put_result("job-b", status="awaiting_approval")
        self.admin("review")
        self.answers.append("y")
        code, _, _ = self.admin("approve", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.calls, [])
        self.assertTrue(approvals.check(self.published("job-b"), "job-b", self.digest, self.signers)[0])

    def test_a_worker_that_trusts_a_different_key_is_an_error_not_a_silent_fallback(self):
        self.worker_publishes(approvers=["SHA256:somebodyelse"])
        self.answers.append("y")
        code, out, err = self.admin("approve", "job-b")
        self.assertNotEqual(code, 0)
        self.assertIn("does not trust your approval key", out + err)
        self.assertIsNone(self.published("job-b"))

    def test_ssh_forces_the_old_way(self):
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()))
        self.answers.append("y")
        self.admin("approve", "job-b", "--ssh")
        self.assertEqual(self.ssh.calls, [f"ct-worker approve job-b --sha256 {self.digest}"])
        self.assertIsNone(self.published("job-b"))

    def test_a_worker_that_does_not_say_which_keys_it_trusts_gets_the_old_way(self):
        self.worker_publishes(capacity=4)                                               # an older release
        self.answers.append("y")
        self.admin("approve", "job-b")
        self.assertEqual(len(self.ssh.calls), 1)

    def test_without_an_approval_key_nothing_changes(self):
        (self.home / "approval" / "key").unlink()
        self.worker_publishes(approvers=[])
        self.answers.append("y")
        self.admin("approve", "job-b")
        self.assertEqual(len(self.ssh.calls), 1)


class SignedOrders(SignedApprovals):
    """ct-admin unlock, cancel and kill: signed here and published; the worker carries them out, no login."""

    def setUp(self):
        super().setUp()
        self.ctx.sleep = lambda s: None
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()))

    def orders(self):
        self.repos.fetch_queue()
        out = subprocess.run(["git", "-C", str(self.repos.queue), "ls-tree", "--name-only", f"origin/{approvals.BRANCH}", "control/"], capture_output=True, text=True).stdout
        return [self.repos.show_file(f"origin/{approvals.BRANCH}", n) for n in out.split()]

    def test_cancel_and_kill_publish_signed_orders_naming_the_job_and_log_in_nowhere(self):
        self.assertEqual(self.admin("cancel", "job-b")[0], 0)
        self.assertEqual(self.admin("kill", "job-b", "--hard")[0], 0)
        self.assertEqual(self.ssh.calls, [])
        recs = [approvals.check_control(t, self.signers) for t in self.orders()]
        self.assertTrue(all(ok for ok, _, _ in recs))
        self.assertEqual(sorted((r["action"], r["job"], bool(r.get("hard"))) for _, _, r in recs), [("cancel", "job-b", False), ("kill", "job-b", True)])

    def test_unlock_shows_what_happened_asks_and_signs_for_that_exact_lockdown(self):
        self.repos.pull_results = lambda: {"kind": "quarantine", "job": "job-x", "since": "2026-11-16T10:00:00Z"}
        self.answers.append("y")
        code, out, err = self.admin("unlock")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ssh.calls, [])
        (text,) = self.orders()
        ok, why, rec = approvals.check_control(text, self.signers)
        self.assertEqual((ok, rec["action"], rec["lock"]), (True, "unlock", "2026-11-16T10:00:00Z"))      # an old order cannot clear a later lockdown

    def test_answering_no_to_unlock_signs_nothing(self):
        self.repos.pull_results = lambda: {"kind": "quarantine", "job": "job-x", "since": "t"}
        self.answers.append("n")
        code, _, _ = self.admin("unlock")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.orders(), [])

    def test_unlock_when_the_worker_says_it_is_not_locked_signs_nothing(self):
        code, out, err = self.admin("unlock")
        self.assertEqual(code, 0)
        self.assertEqual(self.orders(), [])
        self.assertIn("Not locked", out + err)

    def test_ssh_forces_the_old_way_and_an_older_worker_gets_it_automatically(self):
        self.admin("cancel", "job-b", "--ssh")
        self.assertEqual(self.ssh.calls, ["ct-worker cancel job-b"])
        self.worker_publishes(capacity=4)
        self.admin("unlock")
        self.assertEqual(self.ssh.calls[-1], "ct-worker unlock")


def state_doc(**over):
    base = {"v": 1, "updated": "2026-11-16T12:00:00Z", "name": "w", "release": "9032dfbd1aaaa", "os": {"id": "ubuntu", "version": "26.04"},
            "hardware": {"cores": 2, "mem_total_mb": 15219, "cpu": "X", "native_claude": True, "capacity": 4},
            "claude": {"auth": "account", "tool": {"kind": "native", "version": "2.1.325"}}, "isolation": {"ready": True, "method": "bwrap"},
            "classes": ["a", "b"], "packages": ["apt:jq==1.7"], "approvers": [], "locked": False,
            "observed": {"load_1m": 0.3, "temp_c": 52, "mem_available_mb": 14000, "running": 0, "waiting": 0}}
    base.update(over)
    return base


class Fleet:
    """Stands in for a second worker's repositories: just its results folder."""

    def __init__(self, results, fail=False):
        self.results, self.fail = results, fail

    def pull_results(self):
        if self.fail:
            raise RuntimeError("network down")


class WorkersCommand(AdminBase):
    NOW = datetime_now = None

    def setUp(self):
        super().setUp()
        self.other = self.tmp / "other-results"
        self.other.mkdir()
        self.ctx.fleet = lambda: [("testct", self.repos), ("studio", Fleet(self.other, False))]
        self.ctx.workers_file = self.tmp / "workers.toml"
        self.put_state(self.repos.results, state_doc(updated=self.fresh()))
        self.put_state(self.other, state_doc(updated=self.fresh(), claude={"auth": "none"}, hardware={"cores": 8, "mem_total_mb": 32000, "native_claude": True}))

    def fresh(self):
        import time as _t
        return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())

    def put_state(self, folder, doc):
        (Path(folder) / "worker-state.json").write_text(json.dumps(doc))

    def test_every_worker_is_a_row_with_hardware_and_claude_state_side_by_side(self):
        code, out, err = self.admin("workers")
        self.assertEqual(code, 0, err)
        for needle in ("TESTCT", "STUDIO", "account: 2.1.325", "none", "ubuntu 26.04", "2 cores", "8 cores", "jail ON", "┃"):
            self.assertIn(needle, out)

    def test_claude_filter_keeps_only_workers_with_working_claude_access(self):
        code, out, _ = self.admin("workers", "--claude")
        self.assertEqual(code, 0)
        self.assertIn("TESTCT", out)
        self.assertNotIn("STUDIO", out)

    def test_a_locked_worker_or_one_whose_claude_tool_is_missing_is_not_a_claude_candidate(self):
        self.put_state(self.repos.results, state_doc(updated=self.fresh(), locked=True))
        self.put_state(self.other, state_doc(updated=self.fresh(), claude={"auth": "account", "tool": {"kind": "missing", "version": ""}}))
        code, out, err = self.admin("workers", "--claude")
        self.assertEqual(code, 1)
        self.assertIn("No worker matches", out + err)

    def test_the_older_pinned_release_is_labelled_so_it_can_be_compared(self):
        self.put_state(self.other, state_doc(updated=self.fresh(), claude={"auth": "account", "tool": {"kind": "legacy", "version": "2.1.112"}},
                                             hardware={"cores": 2, "mem_total_mb": 15000, "native_claude": False}))
        _, out, _ = self.admin("workers")
        self.assertIn("2.1.112 (older release, pinned)", out)
        self.assertIn("old CPU", out)

    def test_a_token_near_its_end_is_visible_and_an_expired_one_is_not_a_claude_candidate(self):
        self.put_state(self.repos.results, state_doc(updated=self.fresh(), claude={"auth": "account", "tool": {"kind": "native", "version": "2.1.325"}, "token_days": 340}))
        _, out, _ = self.admin("workers")
        self.assertIn("token 340 days old (renew soon)", out)
        self.put_state(self.repos.results, state_doc(updated=self.fresh(), claude={"auth": "account", "tool": {"kind": "native", "version": "2.1.325"}, "token_days": 400}))
        code, out, _ = self.admin("workers", "--claude")
        self.assertNotIn("TESTCT", out)

    def test_json_is_machine_readable_for_a_future_scheduler(self):
        code, out, _ = self.admin("workers", "--json")
        rows = json.loads(out)
        self.assertEqual([r["name"] for r in rows], ["testct", "studio"])
        self.assertEqual((rows[0]["has_claude"], rows[1]["has_claude"]), (True, False))

    def test_a_worker_that_published_nothing_is_a_row_not_a_crash(self):
        (self.other / "worker-state.json").unlink()
        code, out, _ = self.admin("workers")
        self.assertEqual(code, 0)
        self.assertIn("no state published", out)

    def test_an_old_reading_is_marked_stale(self):
        self.put_state(self.other, state_doc(updated="2020-01-01T00:00:00Z"))
        _, out, _ = self.admin("workers")
        self.assertIn(" STALE ", out)

    def test_a_worker_that_cannot_be_refreshed_is_noted_and_its_last_state_still_shown(self):
        self.ctx.fleet = lambda: [("testct", self.repos), ("studio", Fleet(self.other, True))]
        code, out, err = self.admin("workers")
        self.assertEqual(code, 0)
        self.assertIn("studio: could not refresh", out + err)
        self.assertIn("STUDIO", out)

    def test_adding_and_removing_a_worker_edits_the_list_and_validates_it(self):
        cfg = self.tmp / "studio.toml"
        cfg.write_text("[identity]\nname = \"studio\"\n")
        self.assertEqual(self.admin("workers", "add", "studio", "--config", str(cfg))[0], 0)
        self.assertEqual(workers_mod.load_list(self.ctx.workers_file), {"studio": str(cfg)})
        self.assertEqual(self.admin("workers", "remove", "studio")[0], 0)
        self.assertEqual(workers_mod.load_list(self.ctx.workers_file), {})
        self.assertEqual(self.admin("workers", "add", "Bad Name", "--config", str(cfg))[0], 2)
        self.assertEqual(self.admin("workers", "add", "x", "--config", str(self.tmp / "nope.toml"))[0], 2)
        self.assertEqual(self.admin("workers", "remove", "ghost")[0], 2)


class WorkerCards(unittest.TestCase):
    ROW = {"name": "testct", "os": "ubuntu 26.04", "cpu": "2 cores", "memory": "14.0 of 14.9 GB free", "claude": "account: 2.1.112 (older release, pinned)", "state": "jail ON",
           "release": "b3861ea", "load": "0.31 / 52 C", "age": "3 min ago", "has_claude": True, "locked": False, "stale": False, "packages": 1, "classes": 2}

    def test_each_worker_is_a_card_with_a_rail_a_chip_and_labelled_rows_in_the_wizards_style(self):
        lines = workers_mod.cards([self.ROW], width=80, color=False)
        self.assertEqual(lines[0], "┃  TESTCT ")
        for label in ("OS", "CPU", "MEMORY", "CLAUDE", "STATE", "RELEASE", "LOAD", "READING", "INSTALLED"):
            self.assertTrue(any(l.startswith("┃ " + label) for l in lines), label)
        self.assertIn("┃ INSTALLED 2 classes, 1 package", lines)
        values = {l[2:12].strip(): l[12:] for l in lines if l.startswith("┃ ") and len(l) > 12}
        self.assertEqual(values["CLAUDE"], "account: 2.1.112 (older release, pinned)")

    def test_no_line_is_longer_than_the_card_width_so_nothing_wraps(self):
        long = dict(self.ROW, claude="account: " + "x" * 200)
        lines = workers_mod.cards([long], width=60, color=False)
        self.assertTrue(all(len(l) <= 60 for l in lines), [len(l) for l in lines])
        self.assertTrue(any("…" in l for l in lines))

    def test_cards_are_stacked_with_a_blank_line_between_and_a_locked_one_says_so(self):
        locked = dict(self.ROW, name="studio", locked=True)
        lines = workers_mod.cards([self.ROW, locked], width=80, color=False)
        self.assertIn("", lines)
        self.assertIn("┃  STUDIO   LOCKED ", lines)

    def test_colour_gives_the_rail_a_blue_or_red_or_dim_code(self):
        blue = "\n".join(workers_mod.cards([self.ROW], width=80, color=True))
        self.assertIn(theme.code("primary"), blue)
        self.assertIn("38;5;203", "\n".join(workers_mod.cards([dict(self.ROW, locked=True)], width=80, color=True)))
        self.assertIn("\x1b[2m┃ ", "\n".join(workers_mod.cards([dict(self.ROW, stale=True)], width=80, color=True)))


class UpdateCommand(AdminBase):
    def setUp(self):
        super().setUp()
        self.ctx.sleep = lambda s: None
        self.ctx.home.mkdir(parents=True, exist_ok=True)
        self.tty = True
        self.old = self.rel[0]
        self.new = self.release_commit({"src/ct/a.py": "a=2\n"}, "the next release")

    def tag(self, name="release-9.9.9", notes="Adds things\nFixes others"):
        sh("git", "tag", "-a", name, "-m", notes, "HEAD", cwd=self.src_dir)

    def checkout(self):
        root = self.tmp / "checkout"
        sh("git", "clone", "-q", str(self.src_dir), str(root))
        sh("git", "-C", str(root), "reset", "-q", "--hard", self.old)
        return root

    def test_no_release_tag_yet_is_said_plainly(self):
        code, out, err = self.admin("update", "--check")
        self.assertEqual(code, 0)
        self.assertIn("No releases yet", out + err)

    def test_a_newer_release_is_shown_with_its_notes_and_check_changes_nothing(self):
        self.tag()
        root = self.checkout()
        orig = cli.selfupdate.checkout_root
        cli.selfupdate.checkout_root = lambda: root
        try:
            code, out, err = self.admin("update", "--check")
        finally:
            cli.selfupdate.checkout_root = orig
        self.assertEqual(code, 0)
        self.assertIn("release-9.9.9 is available", out + err)
        self.assertIn("Fixes others", out + err)
        self.assertEqual(sh("git", "-C", str(root), "rev-parse", "HEAD"), self.old)            # nothing was updated
        self.assertEqual(self.ssh.calls, [])

    def test_saying_yes_moves_a_clean_checkout_forward_to_exactly_the_release(self):
        self.tag()
        root = self.checkout()
        orig = cli.selfupdate.checkout_root
        cli.selfupdate.checkout_root = lambda: root
        try:
            self.answers.append("y")
            code, out, err = self.admin("update")
        finally:
            cli.selfupdate.checkout_root = orig
        self.assertEqual(code, 0, err)
        self.assertEqual(sh("git", "-C", str(root), "rev-parse", "HEAD"), self.new)

    def test_saying_no_changes_nothing(self):
        self.tag()
        root = self.checkout()
        orig = cli.selfupdate.checkout_root
        cli.selfupdate.checkout_root = lambda: root
        try:
            self.answers.append("n")
            self.admin("update")
        finally:
            cli.selfupdate.checkout_root = orig
        self.assertEqual(sh("git", "-C", str(root), "rev-parse", "HEAD"), self.old)

    def test_a_checkout_with_uncommitted_changes_is_never_touched(self):
        self.tag()
        root = self.checkout()
        (root / "README.md").write_text("my edit\n")
        orig = cli.selfupdate.checkout_root
        cli.selfupdate.checkout_root = lambda: root
        try:
            self.answers.append("y")
            code, out, err = self.admin("update")
        finally:
            cli.selfupdate.checkout_root = orig
        self.assertNotEqual(code, 0)
        self.assertIn("uncommitted changes", out + err)
        self.assertEqual((root / "README.md").read_text(), "my edit\n")

    def test_a_checkout_with_its_own_commits_is_not_overwritten(self):
        self.tag()
        root = self.checkout()
        (root / "mine.txt").write_text("mine\n")
        sh("git", "-C", str(root), "add", "-A")
        sh("git", "-C", str(root), "commit", "-q", "-m", "my own work")
        mine = sh("git", "-C", str(root), "rev-parse", "HEAD")
        orig = cli.selfupdate.checkout_root
        cli.selfupdate.checkout_root = lambda: root
        try:
            self.answers.append("y")
            code, out, err = self.admin("update")
        finally:
            cli.selfupdate.checkout_root = orig
        self.assertNotEqual(code, 0)
        self.assertEqual(sh("git", "-C", str(root), "rev-parse", "HEAD"), mine)

    def test_a_worker_on_another_release_is_offered_the_install_separately_and_only_on_yes(self):
        self.tag()
        (self.repos.results / "worker-state.json").write_text(json.dumps({"release": "0" * 40}))
        applied = []
        orig, orig_root = cli.apply, cli.selfupdate.checkout_root
        cli.apply = lambda ctx, a: applied.append(a.rev) or 0
        cli.selfupdate.checkout_root = lambda: self.checkout()
        try:
            self.answers.extend(["n", "y"])                                              # no to this computer, yes to the worker
            code, _, err = self.admin("update")
        finally:
            cli.apply, cli.selfupdate.checkout_root = orig, orig_root
        self.assertEqual(code, 0, err)
        self.assertEqual(applied, [self.new])

    def test_an_unreachable_source_is_an_error_with_what_to_try(self):
        self.ctx.source.url = str(self.tmp / "nowhere")
        code, out, err = self.admin("update", "--check")
        self.assertNotEqual(code, 0)
        self.assertIn("git ls-remote", out + err)


class CleanCommand(SignedApprovals):
    def setUp(self):
        super().setUp()
        self.ctx.sleep = lambda s: None
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()))

    def names(self):
        self.repos.fetch_queue()
        return subprocess.run(["git", "-C", str(self.repos.queue), "ls-tree", "-r", "--name-only", f"origin/{approvals.BRANCH}"], capture_output=True, text=True).stdout.split()

    def test_an_approval_for_a_finished_job_goes_and_one_for_a_waiting_job_stays(self):
        self.put_job("job-w")
        self.answers.extend(["y", "y"])
        self.admin("approve", "job-b")
        self.admin("approve", "job-w")
        self.put_result("job-b", status="ok")
        self.put_result("job-w", status="awaiting_approval")
        self.answers.append("y")
        code, out, err = self.admin("clean")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.names(), ["approvals/job-w.json"])

    def test_the_signed_registry_is_never_removed(self):
        self.admin("proposals", "publish")
        self.admin("cancel", "job-b")                                                  # an order, fresh: it stays too
        self.answers.append("y")
        self.admin("clean")
        self.assertIn(approvals.REGISTRY_PATH, self.names())
        self.assertTrue(any(n.startswith("control/") for n in self.names()))

    def test_an_expired_order_is_removed(self):
        record = approvals.make_control_record("cancel", job="job-b", now=1000, nonce="aaaa0000")
        text = approvals.bundle(record, approvals.sign(record, self.home / "approval" / "key", namespace=approvals.CONTROL_NAMESPACE))
        self.repos.push_files(approvals.BRANCH, {"control/aaaa0000.json": text}, "an old order")
        self.answers.append("y")
        self.admin("clean")
        self.assertEqual(self.names(), [])

    def test_answering_no_removes_nothing_and_an_empty_branch_says_so(self):
        self.assertIn("Nothing to tidy", "".join(self.admin("clean")[1:]))
        self.answers.append("y")
        self.admin("approve", "job-b")
        self.put_result("job-b", status="ok")
        self.answers.append("n")
        self.admin("clean")
        self.assertEqual(self.names(), ["approvals/job-b.json"])


class RootLevelReview(AdminBase):
    INFO = {"kind": "privileged", "privileged": True, "tier": "approve", "run": ["/usr/local/sbin/fan-profile", "{profile}"], "params": {"profile": {"enum": ["quiet", "balanced"]}},
            "writable": ["/etc/mbpfan.conf"], "network": False, "timeout_s": 20}

    def test_a_job_that_runs_as_root_is_labelled_with_the_exact_program_and_what_it_may_touch(self):
        text = jobs.describe({"id": "fan-1", "class": "fan", "params": {"profile": "quiet"}}, info=self.INFO, product="the Mini")
        self.assertTrue(text.startswith("RUNS AS ROOT on the Mini"))
        for needle in ("/usr/local/sbin/fan-profile quiet", "may write  /etc/mbpfan.conf, and nothing else", "network    none", "limit      20 s", "covers exactly this file"):
            self.assertIn(needle, text)
        self.assertIn('"profile": "quiet"', text)                                        # and the job file itself follows

    def test_parameters_the_action_would_refuse_are_shown_as_such(self):
        text = jobs.describe({"id": "fan-1", "class": "fan", "params": {"profile": "turbo"}}, info=self.INFO)
        self.assertIn("not allowed", text)

    def test_an_ordinary_job_is_not_labelled_and_approving_over_the_review_command_shows_the_banner(self):
        self.assertNotIn("RUNS AS ROOT", jobs.describe({"id": "j", "class": "careful"}, info={"kind": "command"}))
        self.repos.results.mkdir(parents=True, exist_ok=True)
        (self.repos.results / "registry.json").write_text(json.dumps({"classes": {"fan": self.INFO}}))
        self.repos.push_files("jobs", {"jobs/fan-1.json": json.dumps({"id": "fan-1", "class": "fan", "params": {"profile": "quiet"}})}, "job")
        self.put_result("fan-1", status="awaiting_approval")
        code, out, _ = self.admin("review")
        self.assertIn("RUNS AS ROOT", out)

    def test_pinning_a_program_asks_the_worker_read_only(self):
        self.admin("action-hash", "/usr/local/sbin/fan-profile")
        self.assertEqual(self.ssh.calls, ["ct-worker action-hash /usr/local/sbin/fan-profile"])


class ActionBase(AdminBase):
    """Shared setup: a worker that answers `action-hash --json`, and the fan-control pack."""
    PIN = "ab" * 32

    def health(self, rev):
        self.ssh.answers["systemctl"] = "timer-fetch=active\ntimer-step=active\ntimer-canary=active\nfailed=0\nregistry=0\nrev=" + rev + "\n"

    def setUp(self):
        super().setUp()
        self.ssh.answers[INSTALLED] = self.rel[0] + "\n"
        self.health(self.rel[0])
        self.ssh.answers["action-hash --json"] = json.dumps({"path": "/usr/local/sbin/fan-profile", "sha256": self.PIN, "ok": True, "problem": None}) + "\n"
        self.pack = Path(__file__).resolve().parents[1] / "packs" / "fan-control"

    def main_registry(self):
        self.repos.fetch_queue()
        return self.repos.show_file("origin/main", "registry.yaml")



class ActionAdd(ActionBase):
    """`ct-admin action add`: registering a root-level action is one command with one question, like adopt."""
    def test_one_command_adds_the_action_and_its_class_pinned_to_what_the_worker_reported(self):
        code, out, err = self.admin("action", "add", str(self.pack), "--yes")
        self.assertEqual(code, 0, err)
        text = self.main_registry()
        reg = registry.parse(text)
        self.assertEqual(reg["actions"]["fan-profile"]["sha256"], self.PIN)
        self.assertEqual(reg["classes"]["fan-profile"], {"kind": "privileged", "action": "fan-profile", "cost": "heavy"})
        self.assertIn("careful", reg["classes"])                                                     # what was there is still there
        self.assertEqual(registry.validate(reg, "/nonexistent-tools-root", allow_empty=True) and [x for x in registry.validate(reg, "/nonexistent-tools-root", allow_empty=True) if "tools" not in x], [])
        self.assertTrue(any(c.startswith("sudo ct-update --rev ") for c in self.ssh.calls))        # installed, like adopt

    def test_the_preview_says_it_runs_as_root_and_what_it_may_touch_before_anything_is_asked(self):
        self.tty, self.answers = True, ["n"]
        code, out, err = self.admin("action", "add", str(self.pack))
        self.assertIn("RUNS AS ROOT", err)
        for needle in ("/usr/local/sbin/fan-profile", self.PIN, "/sys/devices/platform/applesmc.768", "none", "needs your signature"):
            self.assertIn(needle, err)
        self.assertNotIn("fan-profile", self.main_registry().split("classes:")[0])                  # said no: nothing was changed

    def test_a_program_the_worker_says_anybody_could_change_is_refused(self):
        self.ssh.answers["action-hash --json"] = json.dumps({"path": "/usr/local/sbin/fan-profile", "sha256": self.PIN, "ok": False, "problem": "it is writable by others"}) + "\n"
        code, _, err = self.admin("action", "add", str(self.pack), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("cannot be a root-level action", err)
        self.assertNotIn("fan-profile", self.main_registry())

    def test_a_definition_with_a_shell_is_refused_before_the_worker_is_asked(self):
        bad = self.tmp / "bad.yaml"
        bad.write_text("actions:\n  x:\n    run: [/usr/bin/bash, '-c', '{cmd}']\n    params: {cmd: {regex: '.+'}}\n")
        code, _, err = self.admin("action", "add", str(bad), "--yes")
        self.assertEqual(code, 2)
        self.assertEqual(self.ssh.captured, [])

    def test_running_it_again_after_the_program_changed_only_moves_the_pin(self):
        self.admin("action", "add", str(self.pack), "--yes")
        new = "cd" * 32
        self.ssh.answers["action-hash --json"] = json.dumps({"path": "/usr/local/sbin/fan-profile", "sha256": new, "ok": True, "problem": None}) + "\n"
        code, _, err = self.admin("action", "add", str(self.pack), "--yes")
        self.assertEqual(code, 0, err)
        reg = registry.parse(self.main_registry())
        self.assertEqual(reg["actions"]["fan-profile"]["sha256"], new)
        self.assertEqual(len(reg["classes"]), len(registry.parse(REGISTRY)["classes"]) + 1)


class ActionRemoveAndList(ActionBase):
    """Removing mirrors adding, and a wrong word gets help instead of argparse's usage text."""
    def setUp(self):
        super().setUp()
        self.admin("action", "add", str(self.pack), "--yes")

    def test_remove_takes_the_action_and_the_class_that_runs_it(self):
        code, out, err = self.admin("action", "remove", "fan-profile", "--yes")
        self.assertEqual(code, 0, err)
        reg = registry.parse(self.main_registry())
        self.assertNotIn("fan-profile", reg.get("actions") or {})
        self.assertNotIn("fan-profile", reg["classes"])
        self.assertIn("careful", reg["classes"])
        self.assertIn("stays on the worker", err)

    def test_remove_also_accepts_the_pack_folder_it_was_added_from(self):
        code, _, err = self.admin("action", "remove", str(self.pack), "--yes")
        self.assertEqual(code, 0, err)
        self.assertNotIn("fan-profile", self.main_registry())

    def test_a_misspelt_name_lists_what_is_registered_and_suggests_the_close_one(self):
        code, _, err = self.admin("action", "remove", "fan-profil", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("Registered: fan-profile", err)
        self.assertIn("ct-admin action remove fan-profile", err)

    def test_the_same_action_twice_changes_nothing_and_says_so(self):
        before = self.main_registry()
        code, _, err = self.admin("action", "add", str(self.pack), "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("already registered", err)
        self.assertEqual(self.main_registry(), before)

    def test_the_same_program_under_a_second_name_is_refused(self):
        other = self.tmp / "other.yaml"
        other.write_text("actions:\n  fan-two:\n    run: [/usr/local/sbin/fan-profile, '{mode}']\n    params: {mode: {enum: [idle]}}\n    writable: []\n    timeout_s: 30\n"
                         "classes:\n  fan-two:\n    kind: privileged\n    action: fan-two\n")
        code, _, err = self.admin("action", "add", str(other), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("already registered, as 'fan-profile'", err)
        self.assertNotIn("fan-two", self.main_registry())

    def test_list_shows_each_action_with_its_pin_and_class(self):
        code, out, err = self.admin("action", "list")
        self.assertEqual(code, 0, err)
        for needle in ("fan-profile", "/usr/local/sbin/fan-profile", self.PIN[:16], "/sys/devices/platform/applesmc.768"):
            self.assertIn(needle, out)
        self.admin("action", "remove", "fan-profile", "--yes")
        self.assertIn("No root-level actions are registered", self.admin("action", "list")[2])

    def test_a_wrong_word_gets_the_choices_and_the_nearest_command_not_argparse_usage_text(self):
        code, _, err = self.admin("action", "lst")
        self.assertEqual(code, 2)
        self.assertNotIn("usage:", err)
        self.assertIn("accepts: add, remove, list", err)
        self.assertIn("ct-admin action list", err)
        code, _, err = self.admin("statu")
        self.assertIn("ct-admin status", err)
        self.assertNotIn("usage:", err)
        code, _, err = self.admin("action", "add")
        self.assertNotIn("usage:", err)


class RegistryTextEdits(unittest.TestCase):
    def test_entries_go_into_an_existing_section_or_make_one(self):
        from ct.admin import regedit
        text = "classes:\n  a:\n    kind: command\n"
        out = regedit.add_entries(text, "actions", ["  x:", "    run: []"])
        self.assertTrue(out.endswith("actions:\n  x:\n    run: []\n"))
        out = regedit.add_entries(out, "actions", ["  y:", "    run: []"])
        self.assertEqual(regedit.action_names(out), ["x", "y"])
        self.assertEqual(regedit.add_entries("actions: {}\nclasses:\n", "actions", ["  x:"]).splitlines()[:2], ["actions:", "  x:"])

    def test_a_pin_is_replaced_in_the_right_action_only(self):
        from ct.admin import regedit
        text = 'actions:\n  x:\n    sha256: "' + "1" * 64 + '"\n  y:\n    sha256: "' + "2" * 64 + '"\nclasses:\n'
        out = regedit.set_action_pin(text, "y", "3" * 64)
        self.assertIn("1" * 64, out)
        self.assertNotIn("2" * 64, out)
        self.assertIsNone(regedit.set_action_pin(text, "z", "3" * 64))


class FollowAfterApproving(unittest.TestCase):
    def test_the_command_ct_admin_runs_to_follow_a_job_is_one_the_client_accepts(self):
        """ct-admin follows an approved job with `result <id> --wait --approved`; the client's parser once refused that flag and the person saw a usage error."""
        from ct.client import cli as client_cli
        ap, _ = client_cli.build_parser()
        a = ap.parse_args(["result", "job-1", "--wait", "--approved"])
        self.assertTrue(a.approved and a.wait)
        import re
        src = Path(cli.__file__).read_text()
        self.assertIn('"result", jid, "--wait", "--approved"', src)                       # the call this test mirrors


class SignedRegistry(SignedApprovals):
    """Adding or removing a class from this computer: the registry on main is signed here and the worker installs it; no SSH, no sudo."""

    def setUp(self):
        super().setUp()
        self.ctx.sleep = lambda s: None                      # `retire` in a terminal waits for the worker to show the class gone: never really wait in a test

    def trusted(self):
        self.worker_publishes(approvers=approvals.fingerprints(self.signers.read_text()), classes={"careful": {}})

    def signed_registry(self):
        self.repos.fetch_queue()
        return self.repos.show_file(f"origin/{approvals.BRANCH}", approvals.REGISTRY_PATH)

    def test_retire_signs_the_registry_here_and_installs_nothing_over_ssh(self):
        self.trusted()
        code, out, err = self.admin("retire", "careful", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ssh.calls, [])                                           # no sudo ct-update, no login
        self.repos.fetch_queue()
        text = self.repos.show_file("origin/main", "registry.yaml")
        self.assertNotIn("  careful:", text)
        ok, why, rec = approvals.check_registry(self.signed_registry(), text, self.signers)
        self.assertTrue(ok, why)                                                       # what is signed is exactly what is now on main

    def test_the_signature_is_for_registry_changes_only_and_names_the_exact_text(self):
        self.trusted()
        self.admin("retire", "careful", "--yes")
        self.repos.fetch_queue()
        text = self.repos.show_file("origin/main", "registry.yaml")
        self.assertFalse(approvals.check_registry(self.signed_registry(), text + "# tampered\n", self.signers)[0])
        doc = json.loads(self.signed_registry())
        self.assertFalse(approvals.verify(doc["record"], doc["signature"], self.signers))            # not accepted as the approval of a job

    def test_publish_signs_what_is_on_main_now(self):
        self.trusted()
        code, _, err = self.admin("proposals", "publish")
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(self.signed_registry())
        self.assertEqual(self.ssh.calls, [])

    def test_merging_a_registry_proposal_publishes_it_when_the_worker_trusts_this_key(self):
        self.trusted()
        self.repos.push_files("registry/hello", {"registry.yaml": REGISTRY + "  hello:\n    tier: auto\n    cost: light\n    run: [/bin/echo, hi]\n"}, "registry: hello", base="main")
        self.answers.append("y")
        code, _, err = self.admin("proposals", "merge", "registry/hello")
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(self.signed_registry())

    def test_a_worker_that_does_not_trust_this_key_is_told_so_and_nothing_is_signed(self):
        self.worker_publishes(approvers=["SHA256:somebodyelse"])
        code, out, err = self.admin("retire", "careful", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("does not trust your approval key", out + err)
        self.assertIsNone(self.signed_registry())

    def test_without_a_trusted_key_the_old_ssh_install_is_used(self):
        self.worker_publishes(capacity=4)
        self.health(self.rel[0])
        self.admin("retire", "careful", "--yes")
        self.assertTrue(any(c.startswith("sudo ct-update --rev ") for c in self.ssh.calls))

    def health(self, rev):
        self.ssh.answers["systemctl"] = f"timer-fetch=active\ntimer-step=active\ntimer-canary=active\nfailed=0\nregistry=0\nrev={rev}\n"


class JobTests(AdminBase):
    def test_review_marks_what_you_saw_and_approve_all_sends_pinned_digests(self):
        self.put_job("job-a")
        self.put_result("job-a", status="awaiting_approval")
        code, out, _ = self.admin("review")
        self.assertEqual(code, 0)
        self.assertIn("job-a", out)
        digest = jobs.digest_of(jobs.job_bytes(self.repos, "job-a"))
        self.assertEqual(jobs.Viewed(self.home).load()["job-a"], digest)
        self.answers.append("y")
        self.tty = True
        code, _, _ = self.admin("approve", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.calls, [f"ct-worker approve --many job-a:{digest}"])

    def test_a_job_that_changed_after_you_viewed_it_is_not_approved_in_bulk(self):
        self.put_job("job-a")
        self.put_result("job-a", status="awaiting_approval")
        self.admin("review")
        self.put_job("job-a", params={"x": "changed"})
        self.tty = True
        code, out, err = self.admin("approve", "--all")
        self.assertNotEqual(code, 0)
        self.assertIn("changed since you viewed it", out)
        self.assertEqual(self.ssh.calls, [])

    def test_approve_all_skips_unviewed_jobs_and_asks_nothing_for_them(self):
        self.put_job("job-a")
        self.put_result("job-a", status="awaiting_approval")
        code, out, err = self.admin("approve", "--all")
        self.assertNotEqual(code, 0)
        self.assertIn("not viewed yet", out)
        self.assertEqual(self.ssh.calls, [])

    def test_approve_one_pins_the_digest_and_follows_the_result_in_a_terminal(self):
        self.put_job("job-b")
        digest = jobs.digest_of(jobs.job_bytes(self.repos, "job-b"))
        self.tty = True
        self.answers.append("y")
        code, out, _ = self.admin("approve", "job-b")
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.calls, [f"ct-worker approve job-b --sha256 {digest}"])
        self.assertEqual(self.results_followed, ["job-b"])
        self.assertIn(digest[:12], out)

    def test_answering_no_approves_nothing(self):
        self.put_job("job-b")
        self.tty = True
        self.answers.append("n")
        code, _, _ = self.admin("approve", "job-b")
        self.assertEqual(code, 1)
        self.assertEqual(self.ssh.calls, [])

    def test_without_a_terminal_it_refuses_to_guess_an_answer(self):
        self.put_job("job-b")
        code, _, err = self.admin("approve", "job-b")
        self.assertEqual(code, 2)
        self.assertIn("needs your answer", err)
        self.assertEqual(self.ssh.calls, [])

    def test_yes_skips_the_question_and_no_follow_returns_at_once(self):
        self.put_job("job-b")
        code, _, _ = self.admin("approve", "job-b", "--yes", "--no-follow")
        self.assertEqual(code, 0)
        self.assertEqual(self.results_followed, [])

    def test_a_job_gone_from_github_is_approved_on_the_worker(self):
        self.put_result("old-job", status="awaiting_approval")
        code, _, _ = self.admin("approve", "old-job", "--yes", "--no-follow")
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.calls, ["ct-worker approve old-job --yes"])
        self.assertEqual(jobs.Viewed(self.home).load()["old-job"], "")

    def test_a_job_that_is_nowhere_is_an_error(self):
        code, _, err = self.admin("approve", "nothing-here")
        self.assertEqual(code, 2)
        self.assertIn("No job called nothing-here", err)

    def test_bad_ids_are_refused_before_any_command_is_built(self):
        for cmd in (["approve", "A;rm"], ["cancel", "x y"], ["kill", "../x"]):
            code, _, _ = self.admin(*cmd)
            self.assertEqual(code, 2)
        self.assertEqual(self.ssh.calls, [])

    def test_passthrough_commands_call_the_workers_own_commands(self):
        for args, expect in ((["status", "--hours", "48"], "ct-worker status --hours 48"), (["unlock"], "ct-worker unlock"), (["quarantine"], "ct-worker quarantine"),
                             (["quarantine", "job-x"], "ct-worker quarantine job-x"), (["output", "job-x"], "ct-worker output job-x"), (["cancel", "job-x"], "ct-worker cancel job-x"),
                             (["kill", "job-x", "--hard"], "ct-worker kill job-x --hard")):
            self.ssh.calls.clear()
            self.assertEqual(self.admin(*args)[0], 0)
            self.assertEqual(self.ssh.calls, [expect])


class ProposalTests(AdminBase):
    def propose(self, name, text=None):
        reg = text or (REGISTRY + f"  {name}:\n    tier: auto\n    cost: light\n    run: [/bin/echo, {name}]\n")
        self.repos.push_files(f"registry/{name}", {"registry.yaml": reg}, f"feat(registry): add {name} class", base="main")

    def test_listing_shows_pending_and_merged_branches(self):
        self.propose("hello")
        code, out, _ = self.admin("proposals")
        self.assertEqual(code, 0)
        self.assertIn("registry/hello", out)
        self.assertIn("1 to review", out)

    def test_merge_squashes_into_main_and_deletes_the_branch(self):
        self.propose("hello")
        self.tty = True
        self.answers.append("y")
        code, _, err = self.admin("proposals", "merge", "registry/hello")
        self.assertEqual(code, 0)
        self.repos.fetch_queue()
        self.assertIn("  hello:", self.repos.show_file("origin/main", "registry.yaml"))
        self.assertFalse(self.repos.remote_branch_exists("registry/hello"))
        self.assertIn("Merged-Branch: registry/hello@", sh("git", "-C", str(self.repos.queue), "log", "-1", "--format=%B", "origin/main"))

    def test_an_invalid_registry_is_refused_before_it_is_merged(self):
        self.propose("bad", REGISTRY + "  bad:\n    tier: nonsense\n    run: not-a-list\n")
        self.tty = True
        code, _, err = self.admin("proposals", "merge", "registry/bad")
        self.assertEqual(code, 2)
        self.assertIn("invalid registry", err)
        self.assertTrue(self.repos.remote_branch_exists("registry/bad"))

    def test_main_jobs_and_data_cannot_be_merged(self):
        for b in ("main", "jobs", "data"):
            self.assertEqual(self.admin("proposals", "merge", b, "--yes")[0], 2)

    def test_merge_all_takes_every_pending_branch(self):
        self.propose("one")
        self.repos.fetch_queue()
        self.admin("proposals", "merge", "registry/one", "--yes")
        self.propose("two")
        code, _, _ = self.admin("proposals", "merge", "--all", "--yes")
        self.assertEqual(code, 0)
        self.repos.fetch_queue()
        text = self.repos.show_file("origin/main", "registry.yaml")
        self.assertIn("  two:", text)

    def test_tidy_deletes_only_merged_branches(self):
        self.propose("one")
        self.admin("proposals", "merge", "registry/one", "--yes")
        self.propose("two")
        code, out, _ = self.admin("proposals", "tidy", "--yes")
        self.assertEqual(code, 0)
        self.assertTrue(self.repos.remote_branch_exists("registry/two"))


class RegistryEditTests(unittest.TestCase):
    def test_a_class_and_its_comments_are_removed_and_nothing_else(self):
        text = "classes:\n  # keep\n  a:\n    tier: auto\n\n  # about b\n  b:\n    tier: auto\n    run: [x]\n\nlimits:\n  x: 1\n"
        out = regedit.without_class(text, "b")
        self.assertNotIn("about b", out)
        self.assertNotIn("  b:", out)
        self.assertIn("  a:", out)
        self.assertIn("limits:", out)
        self.assertIsNone(regedit.without_class(text, "zzz"))
        self.assertEqual(regedit.class_names(text), ["a", "b"])

    def test_profile_classes_are_recognised(self):
        self.assertTrue(regedit.is_profile_kind("classes:\n  profile-run:\n    kind: profile\n    cost: heavy\n", "profile-run"))
        self.assertFalse(regedit.is_profile_kind("classes:\n  a:\n    tier: auto\n", "a"))


class RetireAdoptTests(AdminBase):
    def setUp(self):
        super().setUp()
        self.applied = []
        self.ssh.answers[INSTALLED] = self.rel[0] + "\n"
        self.good = "timer-fetch=active\ntimer-step=active\ntimer-canary=active\nfailed=0\nregistry=0\nrev={rev}\n"

    def health(self, rev):
        self.ssh.answers["systemctl"] = self.good.format(rev=rev)

    def test_retire_removes_the_class_merges_and_installs(self):
        self.health(self.rel[0])
        code, out, err = self.admin("retire", "careful", "--yes")
        self.assertEqual(code, 0, err)
        self.repos.fetch_queue()
        self.assertNotIn("  careful:", self.repos.show_file("origin/main", "registry.yaml"))
        self.assertTrue(any(c.startswith("sudo ct-update --rev ") for c in self.ssh.calls))

    def test_the_profile_class_cannot_be_retired(self):
        code, _, err = self.admin("retire", "profile-run", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("cannot be retired", err)

    def test_an_unknown_class_lists_the_real_ones(self):
        code, _, err = self.admin("retire", "nope", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("careful", err)

    def test_adopt_needs_a_result(self):
        code, _, err = self.admin("adopt", "profile-zzz", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("No result", err)


class ReleaseTests(AdminBase):
    HEALTHY = "timer-fetch=active\ntimer-step=active\ntimer-canary=active\nfailed=0\nregistry=0\nrev={rev}\n"

    def setUp(self):
        super().setUp()
        self.new = self.release_commit({"src/ct/a.py": "a=2\n", "src/ct/update.py": "u=1\n"}, "second")

    def installed(self, rev):
        self.ssh.answers[INSTALLED] = rev + "\n"

    def health(self, rev, **over):
        text = self.HEALTHY.format(rev=rev)
        for k, v in over.items():
            text = "\n".join(f"{k}={v}" if line.startswith(k + "=") else line for line in text.splitlines()) + "\n"
        self.ssh.answers["systemctl"] = text

    def test_source_resolves_the_branch_tip_and_refuses_unmerged_commits(self):
        self.assertEqual(self.source.resolve(), self.new)
        sh("git", "checkout", "-q", "-b", "side", cwd=self.src_dir)
        off = self.release_commit({"x.txt": "x"}, "off branch")
        sh("git", "checkout", "-q", "main", cwd=self.src_dir)
        with self.assertRaises(ClientError):
            self.source.resolve(off)

    def test_diff_lists_root_run_files_first(self):
        self.installed(self.rel[0])
        code, out, _ = self.admin("diff")
        self.assertEqual(code, 0)
        self.assertLess(out.index("src/ct/update.py"), out.index("src/ct/a.py"))
        self.assertIn("1 of them run as root", out)

    def test_apply_installs_the_chosen_commit_and_checks_health(self):
        self.installed(self.rel[0])
        self.health(self.new)
        code, out, err = self.admin("apply", "--yes", "--no-verify")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ssh.calls, [f"sudo ct-update --rev {self.new}"])
        self.ssh.calls.clear()
        code, out, _ = self.admin("apply", "--yes", "--force")
        self.assertEqual(code, 0)
        self.assertIn("the three timers are running", out)
        self.assertIn("OK", out)

    def test_nothing_to_change_installs_nothing(self):
        self.installed(self.new)
        code, _, err = self.admin("apply", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.calls, [])
        self.assertIn("already at this version", err)

    def test_the_connection_check_runs_the_real_worker_command_not_one_named_after_the_install(self):
        runs = []
        self.ssh._run = lambda argv, **k: runs.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        self.assertEqual(cli.cmd_check(self.ctx, None), 0)
        self.assertEqual(runs[-1][-1], "ct-worker status")                    # `<install name> status` was an old install's command and could still exist and pass

    def test_a_failed_check_offers_the_rollback_and_does_it_on_yes(self):
        self.installed(self.rel[0])
        self.health(self.new, failed=2)
        self.tty = True
        self.answers[:] = ["y", "y"]            # install it, then roll it back
        calls = {"n": 0}
        real = release.health_checks

        def flaky(ssh, command, expect):
            calls["n"] += 1
            return real(ssh, command, expect) if calls["n"] == 1 else [("ok", True, "")]
        with mock.patch.object(release, "health_checks", flaky):
            code, out, err = self.admin("apply")
        self.assertEqual(code, 1)
        self.assertIn(f"sudo ct-update --rev {self.rel[0]}", self.ssh.calls)
        self.assertIn("Rolled back", err)

    def test_a_failed_check_without_a_terminal_tells_you_how_to_roll_back(self):
        self.installed(self.rel[0])
        self.health(self.new, failed=1)
        code, _, err = self.admin("apply", "--yes")
        self.assertEqual(code, 1)
        self.assertIn(f"ct-admin apply {self.rel[0][:9]} --no-verify", err)
        self.assertEqual(self.ssh.calls, [f"sudo ct-update --rev {self.new}"])

    def test_a_failing_install_stops_and_does_not_check(self):
        self.installed(self.rel[0])
        self.ssh.rc["ct-update"] = 1
        code, _, err = self.admin("apply", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("install failed", err)
        self.assertEqual(self.ssh.captured.count("x"), 0)

    def test_a_failed_service_is_named_and_the_check_says_where_to_read_why(self):
        self.assertEqual(release.failed_detail({"failed": "0"}), "0 failed")
        got = release.failed_detail({"failed": "1", "failed-units": "testct-registry-sync.service "})
        self.assertIn("testct-registry-sync.service", got)
        self.assertIn("journalctl -u testct-registry-sync.service", got)
        self.assertIn("failed-units", release.health_command("testct", "/etc/testct", "/opt/testct"))

    def test_the_health_command_checks_the_things_that_matter(self):
        c = release.health_command("testct", "/etc/testct", "/opt/testct")
        for needle in ("testct-$t.timer", "list-units --failed", "installed-rev", "check-registry /etc/testct/registry.yaml"):
            self.assertIn(needle, c)


class HelpTests(unittest.TestCase):
    def test_help_groups_the_commands(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["help"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        for heading in ("WORKER", "JOBS", "SECURITY", "REGISTRY", "RELEASE"):
            self.assertIn(heading, text)
        self.assertLess(text.index("WORKER"), text.index("JOBS"))
        self.assertLess(text.index("REGISTRY"), text.index("RELEASE"))
        for c in ("status", "approve", "unlock", "proposals", "apply"):
            self.assertIn(c, text)
        for gone in ("login", "logout", "branches", "tidy "):
            self.assertNotIn(f"\n  {gone}", text)

    def test_every_command_has_help_and_is_in_a_group(self):
        grouped = [c for _, names in cli.GROUPS for c in names]
        self.assertEqual(sorted(grouped), sorted(cli.HELP))
        self.assertEqual(sorted(cli.DISPATCH), sorted(cli.HELP))

    def test_per_command_help_and_unknown_commands(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["help", "apply"]), 0)
        self.assertIn("--no-verify", out.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(cli.main(["login"]), 2)


if __name__ == "__main__":
    unittest.main()
