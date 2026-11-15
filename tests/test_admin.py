"""cw-admin: the SSH layer, job review and approval pinned to a digest, proposals, adopt/retire, and a verified install with rollback."""
import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from support import REGISTRY, RepoFixture, sh  # noqa: F401  (support sets HOME to a throwaway folder)

from cw import clihelp
from cw.admin import cli, jobs, regedit, release
from cw.admin.ssh import Ssh
from cw.client.repo import ClientError


INSTALLED = "cat /etc/testcw/installed-rev 2>/dev/null"


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
        a = Ssh("server.example", "admin", 2222, control_dir="/tmp/cw-test-ctl").argv("true", batch=True, tty=False)
        self.assertEqual(a[0], "ssh")
        self.assertIn("BatchMode=yes", a)
        self.assertIn("ControlPersist=60", a)
        self.assertIn("admin" + "@" + "server.example", a)
        self.assertEqual(a[a.index("-p") + 1], "2222")
        self.assertEqual(a[-1], "true")
        self.assertNotIn("-t", a)
        self.assertIn("-t", Ssh("h", control_dir="/tmp/cw-test-ctl").argv("x", tty=True))

    def test_an_address_that_looks_like_an_option_is_refused(self):
        with self.assertRaises(ClientError):
            Ssh("-oProxyCommand=evil")

    def test_a_failed_connection_is_explained(self):
        s = Ssh("h", run=lambda argv, **k: subprocess.CompletedProcess(argv, 255, "", "Permission denied (publickey)"), control_dir="/tmp/cw-test-ctl")
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
        self.rel = [self.release_commit({"src/cw/a.py": "a=1\n", "README.md": "x\n"}, "first")]
        self.source = release.Source(self.tmp / "source.git", str(src), "main")
        self.tty = False
        self.ctx = cli.Context(self.repos, self.ssh, self.source, name="testcw", config_dir="/etc/testcw", code_dir="/opt/testcw", home=self.home,
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
        self.assertEqual(self.ssh.calls, [f"testcw approve --many job-a:{digest}"])

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
        self.assertEqual(self.ssh.calls, [f"testcw approve job-b --sha256 {digest}"])
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
        self.assertEqual(self.ssh.calls, ["testcw approve old-job --yes"])
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
        for args, expect in ((["status", "--hours", "48"], "testcw status --hours 48"), (["unlock"], "testcw unlock"), (["quarantine"], "testcw quarantine"),
                             (["quarantine", "job-x"], "testcw quarantine job-x"), (["output", "job-x"], "testcw output job-x"), (["cancel", "job-x"], "testcw cancel job-x"),
                             (["kill", "job-x", "--hard"], "testcw kill job-x --hard")):
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
        self.assertTrue(any(c.startswith("sudo testcw-update --rev ") for c in self.ssh.calls))

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
        self.new = self.release_commit({"src/cw/a.py": "a=2\n", "src/cw/update.py": "u=1\n"}, "second")

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
        self.assertLess(out.index("src/cw/update.py"), out.index("src/cw/a.py"))
        self.assertIn("1 of them run as root", out)

    def test_apply_installs_the_chosen_commit_and_checks_health(self):
        self.installed(self.rel[0])
        self.health(self.new)
        code, out, err = self.admin("apply", "--yes", "--no-verify")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ssh.calls, [f"sudo testcw-update --rev {self.new}"])
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
        self.assertIn(f"sudo testcw-update --rev {self.rel[0]}", self.ssh.calls)
        self.assertIn("Rolled back", err)

    def test_a_failed_check_without_a_terminal_tells_you_how_to_roll_back(self):
        self.installed(self.rel[0])
        self.health(self.new, failed=1)
        code, _, err = self.admin("apply", "--yes")
        self.assertEqual(code, 1)
        self.assertIn(f"cw-admin apply {self.rel[0][:9]} --no-verify", err)
        self.assertEqual(self.ssh.calls, [f"sudo testcw-update --rev {self.new}"])

    def test_a_failing_install_stops_and_does_not_check(self):
        self.installed(self.rel[0])
        self.ssh.rc["testcw-update"] = 1
        code, _, err = self.admin("apply", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("install failed", err)
        self.assertEqual(self.ssh.captured.count("x"), 0)

    def test_the_health_command_checks_the_things_that_matter(self):
        c = release.health_command("testcw", "/etc/testcw", "/opt/testcw")
        for needle in ("testcw-$t.timer", "list-units --failed", "installed-rev", "check-registry /etc/testcw/registry.yaml"):
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
