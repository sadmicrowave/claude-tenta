"""ct-admin: update, release, release mismatch, root-level review, retire and adopt."""
import argparse
import io
import json
import tempfile
import subprocess
from unittest import mock

from admin_support import AdminBase, INSTALLED, sh  # noqa: F401  (also points HOME at a throwaway folder)

from ct.admin import cli, jobs, release
from ct.client.repo import ClientError


class UpdateEveryWorker(AdminBase):
    """`ct-admin update` with several workers: one lookup, one notes card, this computer once, a roster before anything is asked, each worker in turn, one summary, the worst exit code."""

    def setUp(self):
        super().setUp()
        self.ctx.sleep = lambda s: None
        self.ctx.home.mkdir(parents=True, exist_ok=True)
        self.old = self.rel[0]
        self.new = self.release_commit({"src/ct/a.py": "a=2\n"}, "the next release")
        sh("git", "tag", "-a", "release-9.9.9", "-m", "Adds things\nFixes others", "HEAD", cwd=self.src_dir)
        self.visited, self.lookups, self.local_updates = [], [], []
        self.tty = True

    def worker(self, name, release=None, platform=None):
        folder = self.tmp / f"state-{name}"
        folder.mkdir(exist_ok=True)
        (folder / "worker-state.json").write_text(json.dumps({k: v for k, v in (("release", release), ("platform", platform)) if v}))
        c = mock.Mock(repos=mock.Mock(results=folder), ssh=mock.Mock(host=f"{name}.lan"), home=self.ctx.home)
        c.name = name
        return (name, c)

    def run_update(self, targets, *extra, answers=()):
        self.answers.extend(answers)

        def fake_apply(c, a):                                      # what the real apply does with the outcomes list, for each kind of worker
            self.visited.append((c.name, a.rev))
            if c.name == "broken":
                raise ClientError("broken is unreachable")
            if c.name == "declined":
                raise SystemExit(1)
            if c.name == "winbox":                                  # the real apply leaves another platform alone
                a.outcomes.append((c.name, "left"))
                return 1
            a.outcomes.append((c.name, "installed"))
            return 0
        real_check = cli.updatecheck.check_now
        with mock.patch.object(self.ctx, "targets", return_value=targets), mock.patch.dict(cli.DISPATCH, {"apply": fake_apply}), \
                mock.patch.object(cli.updatecheck, "check_now", side_effect=lambda url, **k: self.lookups.append(url) or real_check(url, **k)), \
                mock.patch.object(cli.selfupdate, "checkout_root", return_value=None), \
                mock.patch.object(cli.selfupdate, "update_package", side_effect=lambda url, tag: self.local_updates.append(tag)):
            return self.admin("update", *extra)

    def fleet(self):
        return [("testct", self.ctx), self.worker("current", self.new), self.worker("broken", self.old), self.worker("declined", self.old), self.worker("good", self.old), self.worker("winbox", self.old, "windows")]

    def test_every_worker_is_visited_one_failing_one_declined_and_the_summary_and_exit_code_say_so(self):
        (self.ctx.repos.results / "worker-state.json").write_text(json.dumps({"release": self.old}))
        code, out, err = self.run_update(self.fleet(), "--yes")
        text = out + err
        self.assertEqual([n for n, _ in self.visited], ["testct", "broken", "declined", "good", "winbox"])      # the current one is not visited; the others are, whatever happened to the one before
        self.assertEqual({r for _, r in self.visited}, {self.new})                                              # one release for all of them
        self.assertEqual(len(self.lookups), 1, "the release is looked up once")
        self.assertEqual(self.local_updates, ["release-9.9.9"], "this computer is updated once")
        self.assertEqual(text.count("Fixes others"), 1, text)                                                   # what changed is shown once
        self.assertIn("update: broken", text)                                                                  # each worker has its heading, under the command the person typed
        self.assertEqual(code, 2)                                                                              # the worst of them (the unreachable one)
        self.assertIn("update finished: 6 workers", text)
        for line in ("Updated", "Already current", "Left alone", "Failed or declined"):
            self.assertIn(line, text)
        self.assertIn("broken", text)

    def test_the_devices_are_listed_before_anything_is_installed_or_asked(self):
        self.answers.append("n")
        code, out, err = self.run_update(self.fleet(), answers=["n"])
        text = out + err
        self.assertIn("The update would touch 7 devices", text)
        self.assertIn("good (good.lan)", text)
        self.assertIn("current (current.lan)", text)
        self.assertIn("winbox (winbox.lan, runs windows)", text)
        self.assertEqual(self.local_updates, [])                    # "no" for this computer; each worker is still asked its own question (the real apply asks it)

    def test_check_shows_the_devices_and_changes_nothing(self):
        code, out, err = self.run_update(self.fleet(), "--check")
        self.assertEqual((code, self.visited, self.local_updates), (0, [], []))
        self.assertIn("The update would touch", out + err)

    def test_every_worker_current_is_said_once(self):
        code, out, err = self.run_update([("a", self.worker("a", self.new)[1]), self.worker("b", self.new)], answers=["n"])
        self.assertEqual(self.visited, [])
        self.assertIn("update finished: 2 workers", out + err)

    def test_worker_narrows_it_to_one_and_the_single_worker_card_is_as_before(self):
        (self.ctx.repos.results / "worker-state.json").write_text(json.dumps({"release": self.old}))
        code, out, err = self.run_update([("testct", self.ctx)], "--check")
        self.assertIn("A new release is available", out + err)
        self.assertNotIn("The update would touch", out + err)


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


if __name__ == "__main__":
    unittest.main()
