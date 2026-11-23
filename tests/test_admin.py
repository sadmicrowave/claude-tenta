"""ct-admin: the SSH layer, job review and approval pinned to a digest, proposals, adopt/retire, and a verified install with rollback."""
import argparse
import io
from types import SimpleNamespace
import re
import json
import tempfile
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from support import REGISTRY, RepoFixture, sh  # noqa: F401  (support sets HOME to a throwaway folder)

from ct import approvals, clihelp, registry, theme, util
from ct.admin import cli, jobs, regedit, release
from ct.admin import workers as workers_mod
from ct.admin.ssh import Ssh
from ct.client.repo import ClientError


INSTALLED = "cat /etc/testct/installed-rev 2>/dev/null"


class FakeSsh:
    """Records every remote command; `answers` maps a substring of a captured command to its stdout."""

    host, user, port = "worker.example", "admin", 22
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

    def test_a_session_sends_keep_alives_so_a_stalled_path_is_noticed_or_woken(self):
        a = Ssh("h", control_dir="/tmp/ct-test-ctl").argv("x", tty=True)
        self.assertIn("ServerAliveInterval=10", a)
        self.assertIn("ServerAliveCountMax=6", a)

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
        self.assertEqual(self.admin("tripwire-test")[0], 0)
        self.assertEqual(self.ssh.calls, ["ct-worker wake", "sudo ct-update --audit", "sudo ct-update --tripwire-test"])

    def test_a_worker_is_named_once_and_found_by_that_name(self):
        self.ctx.workers_file = self.tmp / "workers.toml"
        code, out, err = self.admin("wake", "--worker", "nobody")
        self.assertEqual(code, 2)
        self.assertIn("There is no such worker", out + err)
        self.assertIn("nobody", out + err)
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

    def registry_info(self):
        return None                                   # the real ClientRepos reads registry.json from the results clone; a stand-in worker published none


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
        for needle in ("TESTCT", "STUDIO", "account: 2.1.325", "none", "ubuntu 26.04", "2 cores", "8 cores", "jail ON"):
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
        self.assertIn("(STALE)", out)

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


ANSI_ = __import__("re").compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class WorkerCards(unittest.TestCase):
    ROW = {"name": "testct", "os": "ubuntu 26.04", "cpu": "2 cores", "memory": "14.0 of 14.9 GB free", "claude": "account: 2.1.112 (older release, pinned)", "state": "jail ON",
           "release": "b3861ea", "load": "0.31 / 52 C", "age": "3 min ago", "has_claude": True, "locked": False, "stale": False, "packages": 1, "classes": 2, "class_names": ["alpha", "beta"]}

    def test_the_card_names_the_platform_only_when_it_is_not_linux(self):
        win = workers_mod.cards([{**self.ROW, "platform": "windows"}], width=90, color=False)
        self.assertTrue(any(l.startswith("  PLATFORM") and "windows" in l for l in win), win)
        for usual in ("", "linux"):
            self.assertFalse(any("PLATFORM" in l for l in workers_mod.cards([{**self.ROW, "platform": usual}], width=90, color=False)))

    def test_a_held_shared_computer_says_why_in_its_card(self):
        plain = workers_mod.cards([{**self.ROW, "purpose": "shared with other uses", "held": "a person is using this computer (alice)"}], width=90, color=False)
        self.assertTrue(any(l.startswith("  AVAILABILITY") and "held: a person is using this computer (alice)" in l for l in plain))
        self.assertFalse(any("AVAILABILITY" in l for l in workers_mod.cards([self.ROW], width=90, color=False)))

    def test_what_the_computer_is_used_for_is_a_row_and_a_relaxed_jail_is_said_in_the_state(self):
        doc = dict(self.DOC) if hasattr(self, "DOC") else None
        row = workers_mod.row_for("lab", {**(doc or {"name": "lab", "hardware": {}, "observed": {}, "os": {}, "claude": {"auth": "none"}}), "isolation": {"ready": True, "mode": "relaxed", "purpose": "special-use"}})
        self.assertEqual((row["state"], row["purpose"]), ("jail ON, relaxed", "special use"))
        plain = workers_mod.cards([{**self.ROW, "purpose": "special use"}], width=80, color=False)
        self.assertTrue(any(l.startswith("  PURPOSE") and l.endswith("special use") for l in plain))
        self.assertFalse(any("PURPOSE" in l for l in workers_mod.cards([self.ROW], width=80, color=False)))          # an older release says nothing about it: no row

    def test_each_worker_is_a_card_with_a_rail_a_chip_and_labelled_rows_in_the_wizards_style(self):
        lines = workers_mod.cards([self.ROW], width=80, color=False)                       # plain (a pipe): the name, then labelled rows
        self.assertEqual(lines[0], "TESTCT")
        for label in ("OS", "CPU", "MEMORY", "CLAUDE", "STATE", "RELEASE", "LOAD", "READING", "CLASSES", "PACKAGES"):
            self.assertTrue(any(l.startswith("  " + label) for l in lines), label)
        self.assertTrue(any(l.startswith("  CLASSES") and l.endswith("alpha") for l in lines))                   # the classes by name, one per line
        self.assertTrue(any(l.strip() == "beta" for l in lines))
        self.assertTrue(any(l.startswith("  PACKAGES") and l.endswith("1 package") for l in lines))
        values = {l[2:12].strip(): l[12:] for l in lines if l.startswith("  ") and len(l) > 12}
        self.assertEqual(values["CLAUDE"], "account: 2.1.112 (older release, pinned)")
        framed = [ANSI_.sub("", x) for x in workers_mod.cards([self.ROW], width=80, color=True)]            # on a terminal: one framed card per worker
        self.assertTrue(any(x.startswith("╭─ testct") for x in framed))
        self.assertTrue(any(x.startswith("│ CLASSES") for x in framed))

    def test_no_line_is_longer_than_the_card_width_so_nothing_wraps(self):
        long = dict(self.ROW, claude="account: " + "x" * 200)
        lines = workers_mod.cards([long], width=60, color=False)
        self.assertTrue(all(len(l) <= 60 for l in lines), [len(l) for l in lines])
        self.assertTrue(any("…" in l for l in lines))

    def test_cards_are_stacked_with_a_blank_line_between_and_a_locked_one_says_so(self):
        locked = dict(self.ROW, name="studio", locked=True)
        lines = workers_mod.cards([self.ROW, locked], width=80, color=False)
        self.assertIn("", lines)
        self.assertIn("STUDIO (LOCKED)", lines)

    def test_colour_gives_the_frame_the_colour_of_its_state(self):
        blue = "\n".join(workers_mod.cards([self.ROW], width=80, color=True))
        self.assertIn(theme.code("primary"), blue)
        self.assertIn("38;5;203", "\n".join(workers_mod.cards([dict(self.ROW, locked=True)], width=80, color=True)))          # red while locked
        self.assertIn(theme.code("warn"), "\n".join(workers_mod.cards([dict(self.ROW, stale=True)], width=80, color=True)))    # yellow while its reading is stale


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
        self.assertIn("A new release is available", out + err)
        self.assertIn("release-9.9.9", out + err)
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
        self.assertIn("Already registered", err)
        self.assertEqual(self.main_registry(), before)

    def test_the_same_program_under_a_second_name_is_refused(self):
        other = self.tmp / "other.yaml"
        other.write_text("actions:\n  fan-two:\n    run: [/usr/local/sbin/fan-profile, '{mode}']\n    params: {mode: {enum: [idle]}}\n    writable: []\n    timeout_s: 30\n"
                         "classes:\n  fan-two:\n    kind: privileged\n    action: fan-two\n")
        code, _, err = self.admin("action", "add", str(other), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("That program is already registered", err)
        self.assertIn("fan-profile", err)
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
        self.assertIn("The choices are: add, list, remove", err)
        self.assertIn("lst", err)
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

    def test_approve_one_refuses_an_expired_job_before_showing_or_signing_anything(self):
        self.put_job("job-old", created=util.iso(util.now() - timedelta(days=2)))
        self.tty = True
        self.answers.append("y")
        code, out, err = self.admin("approve", "job-old")
        self.assertNotEqual(code, 0)
        self.assertIn("That job has expired", out + err)
        self.assertIn("job-old", out + err)
        self.assertEqual(self.ssh.calls, [])
        self.assertEqual(self.answers, ["y"])                                          # it asked nothing: the answer was never used
        self.assertNotIn("job-old", jobs.Viewed(self.home).load())

    def test_approve_all_skips_an_expired_job_and_says_why(self):
        self.put_job("job-old", created=util.iso(util.now() - timedelta(days=2)))
        self.put_result("job-old", status="awaiting_approval")
        self.admin("review")
        code, out, err = self.admin("approve", "--all")
        self.assertNotEqual(code, 0)
        self.assertIn("expired", out)
        self.assertEqual(self.ssh.calls, [])

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
        self.assertIn("No such job", err)
        self.assertIn("nothing-here", err)

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


class SystemBranchesAreNotProposals(unittest.TestCase):
    """Seen on the real fleet: the branch that holds the signed approvals was listed as '9 to review', and `merge --all` or `tidy` would have treated it as a change."""

    def test_the_approvals_branch_is_reserved_like_main_and_jobs(self):
        from ct import approvals
        from ct.admin import proposals
        self.assertIn(approvals.BRANCH, proposals.RESERVED)
        p = proposals.Proposals(SimpleNamespace(git=lambda repo, *a, **k: SimpleNamespace(stdout="main jobs approvals feature-x registry/ssh-version\n")))
        p.repos.queue = "q"
        self.assertEqual(p.branches(), ["feature-x", "registry/ssh-version"])
        with self.assertRaises(ClientError):
            p.check_name("approvals")


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

    def test_a_diff_with_no_root_run_change_shows_no_empty_box(self):
        self.installed(self.rel[0])
        with mock.patch.object(self.source, "diff_text", return_value=""), mock.patch.object(cli._ui, "document") as doc:
            self.assertEqual(self.admin("diff")[0], 0)
        doc.assert_not_called()

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
        self.assertIn("already at", err)
        self.assertEqual(err.count("Nothing to change"), 1)

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


class ReleaseMismatch(AdminBase):
    """Workers that run different releases are a fleet fact: said by `ct-admin workers`, `status` and `check`, by the watch, and apply never silently replaces a commit the release source does not have."""

    def rows(self, **revs):
        return [(n, r, rel) for n, (r, rel) in revs.items()]

    def card(self, rows):
        out = io.StringIO()
        with mock.patch.object(cli, "fleet_releases", return_value=rows):
            shown = cli.release_card(self.ctx, out)
        return shown, out.getvalue()

    def test_matching_current_workers_say_nothing(self):
        self.assertFalse(self.card(self.rows(a=("f4c9ae7aa", "current"), b=("f4c9ae7aa", "current")))[0])
        self.assertFalse(self.card(self.rows(a=("f4c9ae7aa", "unknown")))[0])

    def test_workers_on_different_releases_get_one_card_naming_who_runs_what_and_the_command(self):
        shown, text = self.card(self.rows(main=("f4c9ae7aa", "behind"), tenta2=("f4c9ae7aa", "behind"), tenta3=("13a3aa960", "unpublished")))
        self.assertTrue(shown)
        for needle in ("do not all run the same release", "f4c9ae7", "13a3aa9", "behind the release apply would install", "not in the release source", "ct-admin apply"):
            self.assertIn(needle, text)
        self.assertIn("ct-admin apply --force --worker tenta3", text)

    def test_a_worker_behind_on_its_own_is_still_said(self):
        shown, text = self.card(self.rows(a=("aaa1111aa", "behind")))
        self.assertTrue(shown)

    def test_the_watch_says_it_once_and_again_only_when_it_changes(self):
        from ct.admin import watch
        mem, sent, calls = {}, [], []

        class Mem:
            def load(self):
                return dict(mem)

            def save(self, data):
                mem.clear()
                mem.update(data)

        class N:
            def send(self, title, text, **kw):
                sent.append(title)
        sig = ["a:x:behind,b:y:current"]
        for _ in range(3):
            watch.one_pass([], Mem(), N(), releases=lambda: (sig[0], "a runs x"))
        self.assertEqual(sent, ["Workers run different releases"])                           # three passes, one alert
        sig[0] = ""
        watch.one_pass([], Mem(), N(), releases=lambda: None)                                 # fixed: quiet
        sig[0] = "a:x:behind,b:z:current"
        watch.one_pass([], Mem(), N(), releases=lambda: (sig[0], "again"))
        self.assertEqual(len(sent), 2)                                                        # a new mismatch is said again

    def test_apply_does_not_silently_replace_a_commit_the_release_source_does_not_have(self):
        ctx = mock.Mock()
        ctx.name, ctx.ssh.host = "tenta3", "h"
        ctx.source.resolve.return_value = "f" * 40
        ctx.source.relation.return_value = "unpublished"
        ctx.repos.results = tempfile.mkdtemp()
        with mock.patch.object(cli.release, "installed_rev", return_value="1" * 40), mock.patch.object(cli, "emit") as e, mock.patch.object(cli, "show_diff") as diff:
            rc = cli.apply(ctx, argparse.Namespace(rev=None, yes=True, force=False, no_verify=False, full=False))
        self.assertEqual(rc, 1)
        diff.assert_not_called()                                                              # nothing was shown as "the change" and nothing installed
        ctx.ssh.interactive.assert_not_called()
        self.assertIn("release source does not have", e.call_args[0][0])

    def test_apply_for_the_fleet_decides_the_release_once(self):
        a, b = mock.Mock(), mock.Mock()
        a.name, b.name = "first", "second"
        seen = []
        self.ctx.source = mock.Mock()
        self.ctx.source.resolve.return_value = "c" * 40
        with mock.patch.object(self.ctx, "targets", return_value=[("first", a), ("second", b)]), mock.patch.dict(cli.DISPATCH, {"apply": lambda c, args: seen.append(args.rev) or 0}):
            self.admin("apply")
        self.assertEqual(seen, ["c" * 40, "c" * 40])




class ClassSetTests(AdminBase):
    """ct-admin class set / show: change a class's tier, network access and tool folders without editing registry.yaml by hand."""

    def setUp(self):
        super().setUp()
        self.ssh.answers[INSTALLED] = self.rel[0] + "\n"
        self.ssh.answers["systemctl"] = "timer-fetch=active\ntimer-step=active\ntimer-canary=active\nfailed=0\nregistry=0\nrev={}\n".format(self.rel[0])
        self.ctx.sleep = lambda s: None

    def registry(self):
        self.repos.fetch_queue()
        return self.repos.show_file("origin/main", "registry.yaml")

    def test_the_tier_changes_in_place_and_the_change_is_installed(self):
        code, out, err = self.admin("class", "set", "careful", "--tier", "auto", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("approve  ->  auto", out + err)
        self.assertRegex(self.registry(), r"(?m)^  careful:\n(?:    .*\n)*?    tier: auto\n")
        self.assertTrue(any(c.startswith("sudo ct-update --rev ") for c in self.ssh.calls))

    def test_turning_the_network_on_for_an_automatic_class_also_makes_it_need_approval(self):
        code, out, err = self.admin("class", "set", "echo", "--network", "on", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("also set to approve", out + err)
        block = re.search(r"(?m)^  echo:\n((?:    .*\n)+)", self.registry()).group(1)
        self.assertIn("network: true", block)
        self.assertIn("tier: approve", block)

    def test_network_without_approval_needs_the_explicit_flag_and_a_person_answering_yes_does_not_count(self):
        code, out, err = self.admin("class", "set", "echo", "--network", "on", "--tier", "auto", "--yes")
        self.assertEqual(code, 2)                                                           # no terminal: --yes does not answer it
        self.assertIn("answering this question yourself", err)
        self.assertNotIn("network: true", self.registry())
        self.ctx.is_tty, self.ctx.ask = (lambda: True), (lambda p: "n")
        self.assertEqual(self.admin("class", "set", "echo", "--network", "on", "--tier", "auto")[0], 1)
        self.assertNotIn("network: true", self.registry())
        self.ctx.ask = lambda p: "y"
        self.assertEqual(self.admin("class", "set", "echo", "--network", "on", "--tier", "auto")[0], 0)
        self.assertIn("network: true", self.registry())

    def test_tool_folders_can_be_listed_or_repeated_and_removed_with_none(self):
        self.assertEqual(self.admin("class", "set", "careful", "--tools", "/opt/tools/nmap", "/opt/tools/bin", "--tools", "/srv/more", "--yes")[0], 0)
        self.assertIn("tools: [/opt/tools/nmap, /opt/tools/bin, /srv/more]", self.registry())
        self.assertEqual(self.admin("class", "set", "careful", "--tools", "none", "--yes")[0], 0)
        self.assertNotIn("tools:", self.registry())

    def test_a_folder_that_is_not_plain_and_a_missing_change_and_an_unchanged_class_are_explained(self):
        code, _, err = self.admin("class", "set", "careful", "--tools", "relative/bin", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("invalid registry", err)
        self.assertEqual(self.admin("class", "set", "careful", "--yes")[0], 2)
        code, out, err = self.admin("class", "set", "careful", "--tier", "approve", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to change", out + err)

    def test_show_says_what_a_class_may_do(self):
        self.admin("class", "set", "careful", "--tools", "/opt/tools/nmap", "--yes")
        code, out, err = self.admin("class", "show", "careful")
        self.assertEqual(code, 0, err)
        for needle in ("Tier", "approve", "Network", "/opt/tools/nmap", "NOT inside a folder this worker allows"):
            self.assertIn(needle, out + err)

    def test_a_class_that_is_not_anywhere_is_refused_with_the_real_ones(self):
        code, _, err = self.admin("class", "show", "nope")
        self.assertEqual(code, 2)
        self.assertIn("No such class", err)
        self.assertIn("careful", err)

    def test_a_class_from_a_pack_can_be_shown_but_not_set(self):
        self.repos.registry_info = lambda: {"classes": {"fromapack": {"tier": "auto", "cost": "light", "network": True, "tools": ["/opt/x"]}}}
        code, out, err = self.admin("class", "show", "fromapack")
        self.assertEqual(code, 0, err)
        for needle in ("a pack (change it there)", "auto", "yes: jobs of this class can reach the network", "/opt/x"):
            self.assertIn(needle, out + err)
        code, _, err = self.admin("class", "set", "fromapack", "--tier", "approve", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("comes from a pack", err)


class RegeditSetFields(unittest.TestCase):
    TEXT = "classes:\n  # about a\n  a:\n    tier: approve\n    run: [/bin/a]\n\n  b:\n    tier: auto\n    tools:\n      - /opt/x\n      - /opt/y\n    run: [/bin/b]\n"

    def test_a_line_is_replaced_in_place_a_new_one_is_added_at_the_end_and_comments_stay(self):
        out = regedit.set_class_fields(self.TEXT, "a", {"tier": "auto", "network": True})
        self.assertIn("  # about a\n  a:\n    tier: auto\n    run: [/bin/a]\n    network: true\n", out)
        self.assertIn("  b:\n    tier: auto", out)                                       # the other class is untouched

    def test_a_list_written_under_the_setting_is_replaced_whole_and_none_removes_it(self):
        out = regedit.set_class_fields(self.TEXT, "b", {"tools": ["/opt/z"]})
        self.assertIn("    tools: [/opt/z]\n    run: [/bin/b]\n", out)
        self.assertNotIn("- /opt/x", out)
        self.assertNotIn("tools", regedit.set_class_fields(self.TEXT, "b", {"tools": None}))
        self.assertIsNone(regedit.set_class_fields(self.TEXT, "nope", {"tier": "auto"}))


class BeforeApprovingAToolClass(AdminBase):
    """Which tool a class with tool folders would really run is said before the person signs, and in `class show`."""
    INFO = {"kind": "command", "cost": "light", "tier": "approve", "run": ["scan"], "tools": ["/opt/tools/a", "/opt/tools/b"]}
    CHECK = {"program": "scan", "found": "/opt/tools/a/scan", "where": "tool folder",
             "folders": [{"path": "/opt/tools/a", "exists": True, "allowed": True}, {"path": "/opt/tools/b", "exists": False, "allowed": True}]}

    def text_of(self, card):
        import re
        return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(" ".join(str(x) for x in d) if isinstance(d, tuple) else str(d) for d in card["details"]))

    def test_the_review_card_names_the_folders_the_program_found_and_each_folders_state(self):
        from ct.admin import jobs
        card = jobs.review_card({"id": "scan-abc123", "class": "scan"}, None, "lab", info={**self.INFO, "tool_check": self.CHECK}, digest="a" * 64)
        text = self.text_of(card)
        for needle in ("Tool folders /opt/tools/a", "/opt/tools/b", "Found /opt/tools/a/scan  (in a tool folder)", "/opt/tools/b  missing on the worker"):
            self.assertIn(needle, text)

    def test_a_program_that_is_found_nowhere_is_said_loudly_and_an_unknown_state_is_not_invented(self):
        from ct.admin import jobs
        none = jobs.tool_check_rows({**self.CHECK, "found": None, "where": None}, ["/opt/tools/a"])
        self.assertTrue(any("NOT FOUND" in str(v) for _, v in none))
        unknown = jobs.tool_check_rows(None, ["/opt/tools/a"])
        self.assertTrue(any("not known yet" in str(v) for _, v in unknown))

    def test_class_show_reads_it_from_the_workers_published_state(self):
        (self.repos.results / "worker-state.json").write_text(json.dumps({"updated": "2999-01-01T00:00:00Z", "isolation": {"mode": "relaxed", "tool_roots": ["/opt/tools"]}, "tool_check": {"careful": self.CHECK}}))
        self.admin("class", "set", "careful", "--tools", "/opt/tools/a", "/opt/tools/b", "--yes")
        self.repos.registry_info = lambda: {"classes": {"careful": self.INFO}}
        code, out, err = self.admin("class", "show", "careful")
        self.assertEqual(code, 0, err)
        for needle in ("Program", "scan", "/opt/tools/a/scan", "in a tool folder", "missing on the worker"):
            self.assertIn(needle, out + err)


if __name__ == "__main__":
    unittest.main()


class Locate(AdminBase):
    """ct-admin locate: the worker's address changed; it is found by the SSH host key this computer recorded for it, and a name is never proof."""
    KEY = {("ssh-ed25519", "AAAA-the-worker")}

    def setUp(self):
        super().setUp()
        self.conf = self.tmp / "testct.toml"
        self.conf.write_text('[identity]\nname = "testct"\n[server]\nhost = "worker.example"\nadmin_user = "admin"\n')
        self.ctx.cfg = mock.Mock(source=str(self.conf))
        self.ctx.ssh.port = 22
        from ct.setup import discover
        self.discover = discover
        self.devs = [discover.Device("192.0.2.31", name="worker.example"), discover.Device("192.0.2.32", name="another.example")]

    def locate(self, *args, shown=None, recorded=None, answers=("y",)):
        self.answers = list(answers)
        self.tty = True
        keys = shown or {"192.0.2.31": {("ssh-ed25519", "IMPOSTOR")}, "192.0.2.32": self.KEY}            # the machine with the worker's NAME shows another key
        with mock.patch("ct.rediscover.recorded_keys", return_value=self.KEY if recorded is None else recorded), \
             mock.patch("ct.rediscover.shown_keys", side_effect=lambda ip, port=22, run=None: keys.get(ip, set())), \
             mock.patch("ct.rediscover.resolve_mdns", return_value=None), mock.patch("ct.rediscover._arp", return_value={}), \
             mock.patch.object(self.discover, "local_networks", return_value=[("en0", __import__("ipaddress").IPv4Interface("192.0.2.5/24"))]), mock.patch.object(self.discover, "scan", return_value=(self.devs, [])):
            return self.admin("locate", *args)

    def test_the_machine_with_the_recorded_key_is_found_and_its_address_saved_after_asking(self):
        code, out, err = self.locate()
        self.assertEqual(code, 0, out + err)
        self.assertIn("192.0.2.32", out)
        self.assertIn('host = "192.0.2.32"  # was: worker.example', self.conf.read_text())

    def test_saying_no_changes_nothing(self):
        code, out, _ = self.locate(answers=("n",))
        self.assertEqual(code, 0)
        self.assertIn('host = "worker.example"', self.conf.read_text())
        self.assertIn("Nothing was changed", out)

    def test_a_worker_that_never_connected_here_cannot_be_found_and_nothing_is_guessed(self):
        code, out, _ = self.locate(recorded=set())
        self.assertEqual(code, 1)
        self.assertIn("NOT FOUND", out.upper())
        self.assertIn('host = "worker.example"', self.conf.read_text())

    def test_a_worker_still_at_its_address_is_left_alone_with_no_scan(self):
        keys = {"worker.example": self.KEY}
        with mock.patch.object(self.discover, "scan", side_effect=AssertionError("no scan needed")):
            code, out, _ = self.locate(shown=keys)
        self.assertEqual(code, 0)
        self.assertIn("is where its configuration says", out)


class RememberingWhereTheWorkerAnswered(AdminBase):
    """Found testing on a real machine: the last address that worked was only written by `watch` and `locate`, so a user who never ran them had nothing for that rung."""

    def test_an_ordinary_successful_command_notes_the_address_and_a_failed_one_does_not(self):
        from ct import rediscover
        with mock.patch("socket.gethostbyname", return_value="192.0.2.44"), mock.patch("ct.rediscover._arp", return_value={}):
            self.ssh._run = lambda argv, **k: subprocess.CompletedProcess(argv, 255, "", "no route")
            self.admin("check")
            self.assertEqual(rediscover.last_seen(self.home, "testct"), {})
            self.ssh._run = lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", "")
            self.admin("check")
        self.assertEqual(rediscover.last_seen(self.home, "testct")["ip"], "192.0.2.44")


class ALostWorkerThatWasReinstalled(Locate):
    def test_a_machine_with_no_matching_key_says_a_reinstalled_worker_has_a_new_key_and_how_to_trust_it(self):
        code, out, _ = self.locate(shown={"192.0.2.31": {("ssh-ed25519", "OTHER")}, "192.0.2.32": {("ssh-ed25519", "OTHER2")}})
        self.assertEqual(code, 1)
        self.assertIn("ssh-keygen -R worker.example", out)
        self.assertIn("new SSH key", out)


class ACommandOnAJobSentToAnotherWorker(AdminBase):
    """A job the scheduler placed on another worker is approved, read and stopped THERE, by the same command with the same id."""

    def test_the_context_of_the_worker_it_was_sent_to_is_used(self):
        from ct.client import place
        place.record(self.ctx.home, ["j-elsewhere"], "other")
        seen = []
        other = mock.Mock(name="other-ctx")
        other.name = "other"
        with mock.patch.object(self.ctx, "for_worker", side_effect=lambda n: seen.append(n) or other), mock.patch.dict(cli.DISPATCH, {"output": lambda c, a: seen.append(("ran-on", c.name)) or 0}):
            self.admin("output", "j-elsewhere")
        self.assertEqual(seen, ["other", ("ran-on", "other")])

    def test_a_job_with_no_recorded_placement_stays_on_the_main_worker(self):
        seen = []
        with mock.patch.dict(cli.DISPATCH, {"output": lambda c, a: seen.append(c.name) or 0}):
            self.admin("output", "j-here")
        self.assertEqual(seen, ["testct"])


class EveryCommandKnowsWhichWorkersItActsOn(AdminBase):
    """The standing rule: a command about one worker is never scheduled, and a command that acts on workers acts on every one unless --worker narrows it. Every ct-admin command is classified here,
    so a new command cannot be added without saying which it is."""
    FLEET = {"apply", "diff", "check", "status", "quarantine", "unlock"}                  # every worker, one after the other, each asked its own questions
    PICK = {"update", "clean", "adopt", "retire", "class", "action", "proposals"}                   # one worker: the main one, or the one named with --worker
    OWN_FLAG = {"wake", "audit", "tripwire-test", "locate"}                                # already every worker (or the one named) with their own --worker
    BY_NATURE = {"watch", "workers"}                                                       # look at the whole fleet by definition
    BY_JOB = {"approve", "review", "cancel", "kill", "output"}                             # follow the job to the worker it was sent to (placements)
    LOCAL = {"theme", "action-hash"}                                                       # about this computer only

    def test_every_command_is_in_exactly_one_class_and_the_flag_exists_where_it_should(self):
        ap, sp = cli.build_parser()
        classes = [self.FLEET, self.PICK, self.OWN_FLAG, self.BY_NATURE, self.BY_JOB, self.LOCAL]
        every = set(sp.choices)
        self.assertEqual(set().union(*classes), every, "classify the new command above")
        self.assertEqual(sum(len(c) for c in classes), len(every), "a command is in two classes")
        for name in self.FLEET | self.PICK | self.OWN_FLAG:
            self.assertTrue(any("--worker" in a.option_strings for a in sp.choices[name]._actions), f"{name} has no --worker")
        self.assertEqual(set(cli.FLEET_COMMANDS), self.FLEET)
        self.assertEqual(set(cli.PICK_COMMANDS), self.PICK)

    def test_no_internal_send_is_left_to_the_scheduler(self):
        """The checks that are ABOUT a worker (the install check, the wizard's first job) must say so: --here."""
        import re
        root = Path(__file__).resolve().parents[1] / "src" / "ct"
        for f in root.rglob("*.py"):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if re.search(r'\.main\(\["submit"', line):
                    self.assertIn('"--here"', line, f"{f.name}:{n} sends a job without pinning it to the worker it is about")


class FleetWideCommands(AdminBase):
    def two(self):
        a, b = mock.Mock(name="a"), mock.Mock(name="b")
        a.name, b.name = "first", "second"
        return a, b

    def run_diff(self, *extra, targets=None, inner=None):
        a, b = self.two()
        seen = []
        with mock.patch.object(self.ctx, "targets", side_effect=lambda n=None: seen.append(n) or (targets or [("first", a), ("second", b)])), \
                mock.patch.dict(cli.DISPATCH, {"diff": inner or (lambda c, args: calls.append(c.name) or 0)}):
            calls = []
            out = self.admin("diff", *extra)
        return out, calls, seen

    def test_every_worker_is_visited_with_a_heading_each_when_there_are_several(self):
        (code, out, err), calls, seen = self.run_diff()
        self.assertEqual((code, calls, seen), (0, ["first", "second"], [None]))
        self.assertIn("first", out + err)
        self.assertIn("second", out + err)

    def test_the_next_steps_footer_is_said_once_after_the_last_worker_not_under_every_card(self):
        """Seen on `ct-admin status` with three workers: 'From here you can' and its three commands under every card."""
        from ct import ui as _ui

        def inner(c, args):
            print(f"card for {c.name}")
            _ui.related(("Jobs waiting for you", "ct-admin review"), ("Compare every worker you run", "ct-admin workers"), force=True)
            return 0
        (code, out, err), calls, seen = self.run_diff(inner=inner)
        text = out + err
        self.assertEqual(code, 0)
        self.assertEqual(text.count("From here you can"), 1, text)
        self.assertEqual(text.count("ct-admin review"), 1, text)
        self.assertEqual(text.count("card for"), 2)                                     # the content is still repeated for every worker
        self.assertGreater(text.index("From here you can"), text.rindex("card for"))              # and the footer comes after the last of them

    def test_a_single_worker_still_gets_its_footer_straight_after_its_card(self):
        from ct import ui as _ui
        a, b = self.two()
        (code, out, err), calls, seen = self.run_diff(targets=[("first", a)], inner=lambda c, args: _ui.related(("Jobs waiting for you", "ct-admin review"), force=True) and 0)
        self.assertEqual((out + err).count("From here you can"), 1)

    def test_worker_narrows_it_to_the_one_named(self):
        a, b = self.two()
        (code, out, err), calls, seen = self.run_diff("--worker", "second", targets=[("second", b)])
        self.assertEqual((calls, seen), (["second"], ["second"]))

    def test_one_worker_failing_or_being_declined_does_not_skip_the_others_and_is_reflected_in_the_exit_code(self):
        def inner(c, args):
            calls.append(c.name)
            if c.name == "first":
                raise ClientError("first is unreachable")
            return 0
        calls = []
        (code, out, err), called, _ = self.run_diff(inner=inner)
        self.assertEqual(called if called else calls, ["first", "second"])
        self.assertNotEqual(code, 0)
        self.assertIn("first is unreachable", out + err)

    def test_a_single_worker_is_run_exactly_as_before_with_no_heading(self):
        a, _ = self.two()
        (code, out, err), calls, _ = self.run_diff(targets=[("first", a)])
        self.assertEqual((code, calls), (0, ["first"]))
        self.assertNotIn("WORKER", out + err)


class ApplyOnAnAlreadyCurrentWorker(AdminBase):
    """Seen on a real `ct-admin apply`: an up-to-date worker got two 'Nothing to change' cards, and a hint (--force) that would have forced every worker."""

    def run_apply(self, *extra, targets=1):
        sha = self.rel[0]
        out = io.StringIO()
        with mock.patch.object(release, "installed_rev", return_value=sha), mock.patch.object(self.ctx.source, "resolve", return_value=sha):
            with redirect_stdout(out), redirect_stderr(out):
                code = cli.main(["apply", *extra], ctx=self.ctx)
        return code, out.getvalue()

    def test_it_says_nothing_to_change_once(self):
        code, text = self.run_apply()
        self.assertEqual(code, 0)
        self.assertEqual(text.count("Nothing to change"), 1, text)

    def test_it_refuses_a_worker_of_another_platform_and_changes_nothing(self):
        (self.repos.results / "worker-state.json").write_text(json.dumps({"platform": "windows"}))
        code, text = self.run_apply("--force")
        self.assertEqual(code, 1, text)
        self.assertIn("This release is not for that worker", text)
        self.assertIn("windows", text)
        self.assertNotIn("Install ", text)

    def test_a_worker_that_publishes_no_platform_is_linux(self):
        (self.repos.results / "worker-state.json").write_text(json.dumps({"release": "0" * 40}))
        code, text = self.run_apply()
        self.assertNotIn("not for that worker", text)

    def test_the_force_hint_names_the_worker_so_it_does_not_force_them_all(self):
        code, text = self.run_apply()
        self.assertIn(f"ct-admin apply --force --worker {self.ctx.name}", text)
