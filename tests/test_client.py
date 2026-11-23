import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ct.client import cli, submit, view  # noqa: E402
from ct.client.repo import ClientError, ClientRepos  # noqa: E402
from support import Base, RepoFixture, sh  # noqa: E402

NOW = datetime(2026, 1, 2, 3, 0, 0, tzinfo=timezone.utc)
LIMITS = submit.file_limits(None)


class FilesAndBatches(unittest.TestCase):
    def test_globs(self):
        for rel, pat, want in (("src/a/b.js", "src/**/*.js", True), ("src/b.js", "src/**/*.js", True), ("lib/b.js", "src/**/*.js", False),
                               ("deep/x/y.py", "*.py", True), ("a.py", "src/*.py", False), ("src/a/b.js", "src/*.js", False), ("a.js", "?.js", True),
                               ("ab.js", "?.js", False), ("src/x.min.js", "src/**/*.js", True)):
            self.assertEqual(submit.glob_match(rel, pat), want, (rel, pat))

    def tree(self):
        d = Path(tempfile.mkdtemp())
        for rel, text in {"a.py": "x", "sub/b.py": "y", ".git/config": "no", ".git/HEAD": "no", ".env": "no", ".gitignore": "ig", "node_modules/m/i.js": "no", "dist/out.js": "no",
                          "big.txt": "x" * 2000, "bad name.txt": "ok", "tmpl/helper@.service.in": "t", "keep.md": "z"}.items():
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        (d / "binary.bin").write_bytes(b"\xff\xfe\x00")
        return d

    def test_collecting_sends_what_can_be_sent_and_accounts_for_every_file_it_does_not(self):
        d = self.tree()
        left = submit.LeftOut()
        files = submit.collect_dir(d, {**LIMITS, "max_file_kb": 1}, left=left)
        self.assertEqual(sorted(files), ["a.py", "bad name.txt", "keep.md", "sub/b.py", "tmpl/helper@.service.in"])         # a space and an @ are fine now
        self.assertEqual(sorted(left.excluded), [".env", ".git", ".gitignore", "dist", "node_modules"])                       # left out by rule, each named
        self.assertEqual(sorted((n, c) for n, c, _ in left.unsent), [("big.txt", "files.too_big"), ("binary.bin", "files.not_text")])
        self.assertEqual(sorted(submit.collect_dir(d, LIMITS, include=["*.py"])), ["a.py", "sub/b.py"])

    def test_a_hidden_file_or_folder_is_sent_when_the_include_names_it(self):
        d = self.tree()
        files = submit.collect_dir(d, LIMITS, include=[".gitignore", ".git/HEAD"], left=submit.LeftOut())
        self.assertEqual(sorted(files), [".git/HEAD", ".gitignore"])
        self.assertEqual(sorted(submit.collect_dir(d, LIMITS, include=["**/*.js"])), [])                                       # a wildcard does not reach into a skipped folder

    def test_a_job_that_would_run_on_an_incomplete_set_is_refused_unless_the_person_chooses(self):
        d = self.tree()
        left = submit.LeftOut()
        submit.collect_dir(d, {**LIMITS, "max_file_kb": 1}, left=left)
        with self.assertRaises(ClientError) as cm:
            submit.check_complete(left, allow=False)
        self.assertEqual(cm.exception.title, "Some files could not be sent")
        self.assertIn("--send-incomplete", " ".join(cm.exception.steps))
        self.assertEqual(cm.exception.facts[-1], ("Code", "files.incomplete"))
        submit.check_complete(left, allow=True)
        clean = submit.LeftOut()
        clean.exclude(".git")
        submit.check_complete(clean, allow=False)                                                                              # left out by rule is not incomplete

    def test_batches_follow_the_workers_published_limits(self):
        files = {f"f{i}.txt": "x" for i in range(120)}
        self.assertEqual(len(submit.plan_batches({"a": "x"}, False, LIMITS)), 1)
        info = {"limits": {"files": {"max_count": 50, "max_total_kb": 2000, "max_batches": 3}}}
        lim = submit.file_limits(info)
        self.assertEqual((lim["max_count"], lim["max_batches"], lim["max_file_kb"]), (50, 3, 100))     # unpublished keys keep the conservative default
        parts = submit.plan_batches(files, True, lim)
        self.assertEqual([len(p) for p in parts], [40, 40, 40])
        self.assertEqual(sorted(k for p in parts for k in p), sorted(files))                           # every file exactly once
        with self.assertRaises(ClientError) as cm:
            submit.plan_batches(files, False, lim)
        self.assertIn("needs its files together", cm.exception.why)
        with self.assertRaises(ClientError) as cm:
            submit.plan_batches({f"f{i}": "x" for i in range(200)}, True, lim)
        self.assertIn("would need 4 jobs", cm.exception.why)

    def test_a_huge_tree_is_never_split_into_many_jobs(self):
        with self.assertRaises(ClientError) as cm:
            submit.plan_batches({f"f{i}": "x" * 50_000 for i in range(10)}, True, submit.file_limits({"limits": {"files": {"max_total_kb": 100}}}))
        self.assertIn("never split", cm.exception.why)

    def test_old_defaults_apply_until_the_worker_publishes_its_limits(self):
        self.assertEqual(submit.file_limits(None), submit.OLD_FILE_LIMITS)
        self.assertEqual(submit.file_limits({"limits": {"files": {"bogus": 1}}}), submit.OLD_FILE_LIMITS)


class IdsAndJobs(unittest.TestCase):
    def test_ids_are_short_readable_and_unique(self):
        known = set()
        for _ in range(200):
            jid = submit.new_job_id("echo", known)
            self.assertRegex(jid, r"^echo-[2-9a-hj-km-np-z]{6}$")
            self.assertNotIn(jid, known)
            known.add(jid)
        self.assertEqual(len(submit.new_job_id("x" * 80)), 40 + 7)
        self.assertEqual(submit.job_ids("base", 1), ["base"])
        self.assertEqual(submit.job_ids("base", 3), ["base-b1", "base-b2", "base-b3"])

    def test_params_and_the_job_file(self):
        self.assertEqual(submit.parse_params(["n=3", "dir=src", "neg=-2", "ver=1.5", "eq=a=b"]), {"n": 3, "dir": "src", "neg": -2, "ver": "1.5", "eq": "a=b"})
        with self.assertRaises(ClientError):
            submit.parse_params(["nothing"])
        job = submit.stamp("job-0001", submit.build_job("echo", {"w": "hi"}, "a prompt", {"a.txt": "x"}), clock=lambda: NOW)
        self.assertEqual(job, {"id": "job-0001", "class": "echo", "params": {"w": "hi"}, "prompt": "a prompt", "files": {"a.txt": "x"}, "created": "2026-01-02T03:00:00Z"})
        self.assertNotIn("prompt", submit.build_job("echo"))


class Human(unittest.TestCase):
    def test_durations_and_ages(self):
        self.assertEqual([view.human_dur(s) for s in (45, 120, 840, 7500, 200000)], ["45 s", "2 min", "14 min", "2 h 05 min", "2 days 7 h"])
        self.assertEqual(view.human_age(None), "-")
        self.assertEqual([view.human_age(NOW - timedelta(seconds=s), NOW) for s in (10, 300, 7200, 200000)], ["just now", "5 min", "2 h 00 min", "2 days"])

    def test_phases(self):
        p = lambda res, **kw: view.phase_of(res, "job-1", "tct-admin", product="tct", **kw)[0]    # noqa: E731
        self.assertEqual(p(None), "sent: waiting for tct to notice it (an idle worker looks every 90 s)")
        self.assertEqual(p({"status": "queued"}), "queued on tct: waiting for a free slot")
        self.assertIn("2 min so far", p({"status": "queued"}, waited=130))
        self.assertIn("has not picked it up", p(None, waited=400))               # a job nobody picks up is no longer described as "within a minute"
        self.assertIn("tct-admin wake", p(None, waited=400))
        self.assertNotIn("has not picked it up", p(None, waited=130))            # a sleeping worker looks every 5 min: two minutes is not late
        self.assertEqual(p({"status": "awaiting_approval"}), "waiting for your approval (tct-admin approve job-1)")
        self.assertEqual(p({"status": "awaiting_approval"}, approved=True), "approved: waiting for tct to pick it up")
        self.assertEqual(p({"status": "running", "note": "fetching packages on tct"}), "fetching packages on tct")
        self.assertEqual(p({"status": "running"}), "running on tct")

    def test_the_job_list_orders_by_the_jobs_own_time_and_shows_waiting_jobs(self):
        results = {"a": {"id": "a", "status": "ok", "finished": "2026-01-01T00:00:00Z", "started": "2026-01-01T00:00:00Z"},
                   "b": {"id": "b", "status": "error", "finished": "2026-01-02T00:00:00Z", "note": "boom"},
                   "w": {"id": "w", "status": "awaiting_approval", "class": "careful"}}
        rows = view.status_rows(results, lambda jid: NOW if jid == "w" else None, None)
        self.assertEqual([r[2] for r in rows], ["w", "b", "a"])
        self.assertEqual(view.run_durations({"a": {"id": "a", "started": "2026-01-01T00:00:00Z", "finished": "2026-01-01T00:14:00Z", "status": "ok"},
                                             "b": {"id": "b", "status": "awaiting_approval"}}), {"a": "14 min"})

    def test_a_job_waiting_for_approval_for_more_than_a_day_is_listed_as_expired_not_waiting(self):
        results = {"w": {"id": "w", "status": "awaiting_approval", "class": "careful"}, "f": {"id": "f", "status": "awaiting_approval", "class": "careful"}}
        created = {"w": NOW - timedelta(hours=30), "f": NOW - timedelta(hours=2)}
        rows = view.status_rows(results, lambda jid: created[jid], None, now=NOW)
        status = {r[2]: r[1] for r in rows}
        self.assertEqual(status, {"w": "expired", "f": "awaiting_approval"})
        self.assertIn("send it again", [r for r in rows if r[2] == "w"][0][3])
        out = io.StringIO()
        view.render_status(rows, results, lambda jid, cls: cls, [], "ct-admin", out=out, now=NOW)
        self.assertIn("ct-admin approve f", out.getvalue())                              # the fresh one is still offered
        self.assertNotIn("ct-admin approve w", out.getvalue())                            # no command offered for a job that can no longer be approved

    def test_a_job_the_worker_has_not_picked_up_is_a_queued_row_with_a_nudge_after_five_minutes(self):
        listing = [("new", {"created": "2026-01-02T02:58:00Z", "class": "echo"}), ("stuck", {"created": "2026-01-02T02:00:00Z", "class": "echo"}),
                   ("old", {"created": "2025-12-01T00:00:00Z"}), ("known", {"created": "2026-01-02T02:59:00Z"})]
        rows = view.queued_rows({"known"}, listing, at=NOW, admin="tct-admin")
        self.assertEqual([r[2] for r in rows], ["new", "stuck"])                                  # an old one with no result is not shown as queued
        self.assertIn("looks about once a minute", rows[0][3])
        self.assertIn("has not picked this up in 60 min", rows[1][3])
        self.assertIn("tct-admin check", rows[1][3])

    def test_render_status_and_classes(self):
        out = io.StringIO()
        results = {"w": {"id": "w", "status": "awaiting_approval", "class": "careful"}, "a": {"id": "a", "status": "ok", "class": "echo", "finished": "2026-01-01T00:00:00Z"}}
        rows = view.status_rows(results, lambda jid: NOW, None, now=NOW)
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}):
            view.render_status(rows, results, lambda jid, cls: cls or "-", [("on your computer", "tct-admin approve {}"), ("on tct", "tct approve {}")], "tct-admin", out=out, now=NOW)
        text = out.getvalue()
        for needle in ("Your most recent jobs, newest first", "Waiting for you", "tct-admin approve w", "echo"):
            self.assertIn(needle, text)
        self.assertNotIn("macbook", text)
        out = io.StringIO()
        info = {"updated": "2026-01-02T00:00:00Z", "mem_total_mb": 8000, "capacity": 4,
                "classes": {"echo": {"kind": "command", "cost": "light", "tier": "auto", "run": ["/bin/echo", "{word}"], "params": ["word"]},
                            "profile-run": {"kind": "profile", "cost": "heavy", "tier": "approve"}}}
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}):
            view.render_classes(info, pending=["newclass"], excluded=2, out=out, product="tct")
        text = out.getvalue()
        for needle in ("What this worker can run for you today", "tct", "echo", "/bin/echo {word}", "word", "profile one exact command", "Proposed, NOT installed yet", "newclass", "Excluded after profiling: 2"):
            self.assertIn(needle, text)

    def test_result_formatting_for_a_profile_run_shows_verdict_and_estimate_comparison(self):
        res = {"id": "profile-abc123", "status": "ok", "output": "done", "note": "", "metrics": {"wall_s": 3.0, "cpu_s": 2.5, "peak_rss_mb": 300, "mem_total_mb": 8000, "peak_temp_c": 60, "disk_mb": 1},
               "profile": {"argv": ["ls", "-l"], "what": "lists", "verdict": "fits", "cost": "light", "reasons": ["3.0s, 300 MB"], "estimates": {"time_s": 1, "mem_mb": 100}}}
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}):
            head, output, tail = view.format_result(res, admin="tct-admin", suggest_name="ls-long")
        text = "\n".join(head + tail)
        for needle in ("JOB     profile-abc123", "STATUS  ok", "Command", "ls -l", "MEASURED", "VERDICT  fits   cost light", "x3.0 over the estimate",
                       "tct-admin adopt profile-abc123 --name ls-long"):
            self.assertIn(needle, text)
        self.assertEqual(output, "done")


class DirectPace(unittest.TestCase):
    def test_what_a_person_is_told_to_expect_matches_the_pace_of_the_connection(self):
        from ct import config
        cfg = config.Config(config.validate({"server": {"admin_user": "me", "host": "h"}, "github": {"owner": "o"}, "transport": {"kind": "direct"}})[0])
        try:
            view.set_pace(cfg.schedule, direct=True)
            self.assertNotIn("90 s", view.pace_text())
            self.assertIn("every few seconds", view.pace_text())
            view.set_pace(cfg.schedule)
            self.assertIn("90 s", view.pace_text())
        finally:
            view.set_pace(cfg.schedule)


class Repo(RepoFixture):
    def test_clone_is_idempotent(self):
        self.assertEqual([w for w, _ in self.repos.clone_all(self.home)], ["exists", "exists"])
        self.assertTrue(self.repos.is_set_up())

    def test_a_clone_that_is_already_there_follows_the_address_the_configuration_now_says(self):
        """Seen on a real machine: set up again for another connection, the clones kept the old address, so jobs were sent and read the old way."""
        import subprocess
        new = self.tmp / "elsewhere"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.repos.queue_url), str(new)], check=True)
        moved = ClientRepos(self.repos.queue, self.repos.results, str(new), self.repos.results_url)
        self.assertEqual([w for w, _ in moved.clone_all(self.home)], ["repointed", "exists"])
        got = subprocess.run(["git", "-C", str(self.repos.queue), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(got, str(new))
        self.assertEqual([w for w, _ in moved.clone_all(self.home)], ["exists", "exists"])           # and a second run changes nothing

    def test_a_bad_address_is_explained(self):
        bad = ClientRepos(self.tmp / "x" / "queue", self.tmp / "x" / "results", str(self.tmp / "nope.git"), str(self.tmp / "nope2.git"))
        with self.assertRaises(ClientError) as cm:
            bad.clone_all(self.tmp / "x")
        self.assertEqual(cm.exception.title, "Could not clone a repository")
        self.assertIn("GitHub", str(cm.exception.facts))
        self.assertIn("ssh -T git@github.com", cm.exception.steps[0])
        with self.assertRaises(ClientError):
            bad.need_setup()

    def test_jobs_are_pushed_with_plumbing_to_an_orphan_branch_then_on_top_of_it(self):
        (self.repos.queue / "scratch.txt").write_text("half-edited, never committed")
        c1 = self.repos.push_files("jobs", {"jobs/a.json": "{}\n"}, "job: a")
        c2 = self.repos.push_files("jobs", {"jobs/b.json": "{}\n"}, "job: b")
        log = sh("git", "log", "--format=%h|%an|%s", "jobs", cwd=self.tmp / "queue.git").splitlines()
        self.assertEqual([l.split("|")[1:] for l in log], [["ct-client", "job: b"], ["ct-client", "job: a"]])
        self.assertEqual(sh("git", "ls-tree", "--name-only", "-r", "jobs", cwd=self.tmp / "queue.git").splitlines(), ["jobs/a.json", "jobs/b.json"])
        self.assertEqual(sh("git", "rev-parse", "origin/jobs", cwd=self.repos.queue), c2)
        self.assertNotIn("scratch.txt", sh("git", "ls-tree", "--name-only", "-r", "jobs", cwd=self.tmp / "queue.git"))     # the working tree is never read
        self.assertNotEqual(c1, c2)
        self.assertFalse((self.repos.queue / ".git" / "ct-client.index").exists())

    def test_a_push_that_loses_a_race_is_fetched_and_retried_never_forced(self):
        self.repos.push_files("jobs", {"jobs/a.json": "{}\n"}, "job: a")
        other = self.tmp / "other"
        sh("git", "clone", "-q", str(self.tmp / "queue.git"), str(other))
        sh("git", "checkout", "-q", "jobs", cwd=other)
        (other / "jobs" / "x.json").write_text("{}\n")
        sh("git", "add", "-A", cwd=other)
        sh("git", "commit", "-q", "-m", "someone else's job", cwd=other)
        sh("git", "push", "-q", "origin", "jobs", cwd=other)
        self.repos.push_files("jobs", {"jobs/b.json": "{}\n"}, "job: b")                        # fetches first, so it builds on the new tip
        names = sh("git", "ls-tree", "--name-only", "-r", "jobs", cwd=self.tmp / "queue.git").splitlines()
        self.assertEqual(sorted(names), ["jobs/a.json", "jobs/b.json", "jobs/x.json"])           # nobody's job was lost

    def test_a_dead_remote_raises_after_the_retries(self):
        sh("git", "remote", "set-url", "origin", str(self.tmp / "gone.git"), cwd=self.repos.queue)
        with self.assertRaises(ClientError) as cm:
            self.repos.push_files("jobs", {"jobs/a.json": "{}\n"}, "job: a")
        self.assertIn("Could not push", cm.exception.title)
        self.assertEqual(self.sleeps, [2] * 8)             # eight tries now: a burst of simultaneous sends loses races and must win on a later try

    def test_results_are_mirrored_even_after_the_worker_rewrites_history(self):
        self.job("job-0001")
        self.w.fetch()
        self.w.execute("job-0001")
        self.assertIsNone(self.repos.pull_results())
        self.assertEqual(self.repos.read_result("job-0001")["status"], "ok")
        self.w.io.prune_results(days=14, force=True)                                            # history is squashed: a rebase would break
        self.assertIsNone(self.repos.pull_results())
        self.assertEqual(self.repos.read_result("job-0001")["status"], "ok")
        self.assertEqual(self.repos.result_ids(), {"job-0001"})

    def test_an_empty_results_repo_is_not_an_error(self):
        self.assertIsNone(self.repos.pull_results())
        self.assertEqual((self.repos.result_ids(), self.repos.registry_info(), self.repos.read_result("x")), (set(), None, None))

    def test_a_lock_is_reported_with_its_details(self):
        self.lockdown.engage("quarantine", "job-0001", {"mac"}, cls="c")
        self.w.publish_security()
        sec = self.repos.pull_results()
        self.assertEqual((sec["locked"], sec["job"], sec["rules"]), (True, "job-0001", ["mac"]))
        self.assertNotIn("detail", sec)

    def test_job_known(self):
        sub_job = submit.stamp("job-0009", submit.build_job("echo"))
        self.repos.push_files("jobs", {"jobs/job-0009.json": json.dumps(sub_job)}, "job")
        self.assertTrue(self.repos.job_known("job-0009"))
        self.assertFalse(self.repos.job_known("job-0008"))


class Prune(RepoFixture):
    def test_old_finished_jobs_go_but_new_and_unfinished_ones_are_kept(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        files = {f"jobs/{n}.json": json.dumps({"id": n, "created": c}) for n, c in (("old-done", old), ("old-waiting", old), ("new-done", new))}
        self.repos.push_files("jobs", files, "jobs")
        for n, st in (("old-done", "ok"), ("old-waiting", "awaiting_approval"), ("new-done", "ok")):
            self.w.io.publish({"id": n, "status": st})
        msg = self.repos.prune_jobs(days=14, force=True)
        self.assertIn("1 commit -> 1, dropped 1 job", msg)
        names = sh("git", "ls-tree", "--name-only", "-r", "jobs", cwd=self.tmp / "queue.git").splitlines()
        self.assertEqual(sorted(names), ["jobs/new-done.json", "jobs/old-waiting.json"])          # the unfinished job is never dropped

    def test_nothing_happens_below_the_threshold_or_without_a_branch(self):
        self.assertEqual(self.repos.prune_jobs(), "no jobs branch yet")
        self.repos.push_files("jobs", {"jobs/a.json": "{}\n"}, "job")
        self.assertIn("no prune needed (threshold 150)", self.repos.prune_jobs())

    def test_a_job_pushed_during_the_prune_is_never_lost(self):
        self.repos.push_files("jobs", {"jobs/a.json": json.dumps({"created": "2020-01-01T00:00:00Z"})}, "job")
        self.w.io.publish({"id": "a", "status": "ok"})
        orig = self.repos.show_file

        def racing(ref, path):
            if not getattr(self, "raced", False):
                self.raced = True
                other = self.tmp / "other"
                sh("git", "clone", "-q", "-b", "jobs", str(self.tmp / "queue.git"), str(other))
                (other / "jobs" / "late.json").write_text("{}\n")
                sh("git", "add", "-A", cwd=other)
                sh("git", "commit", "-q", "-m", "late job", cwd=other)
                sh("git", "push", "-q", "origin", "jobs", cwd=other)
            return orig(ref, path)
        self.repos.show_file = racing
        self.assertIn("refused", self.repos.prune_jobs(days=1, force=True))
        self.assertIn("jobs/late.json", sh("git", "ls-tree", "--name-only", "-r", "jobs", cwd=self.tmp / "queue.git"))


class EndToEnd(RepoFixture):
    def test_submit_through_the_worker_and_back(self):
        code, out, err = self.run_cli("submit", "echo", "-p", "word=hello", "--no-wait")
        jid = out.strip()
        self.assertEqual(code, 0)
        self.assertRegex(jid, r"^echo-[2-9a-hj-km-np-z]{6}$")
        self.assertIn("Queued", out + err)
        self.assertIn(jid, out + err)
        self.w.fetch()
        self.assertEqual(self.spawned, [jid])
        self.w.execute(jid)
        code, out, err = self.run_cli("result", jid)
        self.assertEqual(code, 0)
        self.assertIn("JOB     " + jid, err)
        self.assertIn("STATUS  ok", err)
        self.assertIn("fine", out)
        self.assertEqual(self.runner.calls[0]["argv"], ["/bin/echo", "hello"])
        code, out, err = self.run_cli("result", jid, "--json")
        self.assertEqual(json.loads(out)["status"], "ok")

    def test_files_travel_in_the_job(self):
        d = Path(tempfile.mkdtemp())
        (d / "in.txt").write_text("hello")
        code, out, err = self.run_cli("submit", "files", "--dir", str(d), "--no-wait")
        self.assertEqual(code, 0)
        self.w.fetch()
        self.w.execute(out.strip())
        self.assertEqual(self.runner.calls[0]["files"], ["in.txt"])

    def test_the_command_lists_what_it_left_out_and_stops_on_a_file_it_could_not_send(self):
        d = Path(tempfile.mkdtemp())
        (d / "in.txt").write_text("hello")
        (d / ".git").mkdir()
        (d / ".git" / "HEAD").write_text("ref")
        (d / "blob.bin").write_bytes(b"\xff\xfe\x00")
        code, out, err = self.run_cli("submit", "files", "--dir", str(d), "--no-wait")
        self.assertEqual(code, 2)
        text = out + err
        self.assertIn("Files left out by rule", text)
        self.assertIn(".git", text)
        self.assertIn("Some files could not be sent", text)
        self.assertIn("blob.bin", text)
        self.assertIn("files.incomplete", text)
        code, out, err = self.run_cli("submit", "files", "--dir", str(d), "--send-incomplete", "--no-wait")
        self.assertEqual(code, 0, out + err)
        self.w.fetch()
        self.w.execute(out.strip())
        self.assertEqual(self.runner.calls[0]["files"], ["in.txt"])

    def test_a_set_of_files_too_big_for_the_class_is_refused_with_the_fix(self):
        d = Path(tempfile.mkdtemp())
        for i in range(210):                                                                    # the worker's published limit is 200 files per job
            (d / f"f{i}.txt").write_text("x")
        self.w.fetch()                                                                          # publishes the registry info (limits)
        self.repos.pull_results()
        code, out, err = self.run_cli("submit", "files", "--dir", str(d), "--no-wait")
        self.assertEqual(code, 2)
        self.assertIn("These files do not fit in one job", out + err)
        self.assertIn("--include", out + err)

    def test_a_locked_worker_refuses_new_jobs_before_anything_is_pushed(self):
        self.lockdown.engage("quarantine", "job-0001", {"mac"}, cls="c")
        self.w.publish_security()
        code, out, err = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        self.assertEqual(code, 2)
        self.assertIn("The worker is locked", out + err)
        self.assertIn("ct-admin unlock", out + err)
        self.repos.fetch_queue()
        self.assertFalse(self.repos.remote_branch_exists("jobs"))

    def test_classes_and_jobs(self):
        self.assertEqual(self.run_cli("classes")[0], 1)
        self.w.fetch()
        code, out, err = self.run_cli("classes")
        self.assertEqual(code, 0)
        self.assertIn("What this worker can run for you today", out)
        self.assertIn("testct", out)
        self.assertIn("careful", out)
        code, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        jid = out.strip()
        code, out, err = self.run_cli("jobs")
        self.assertIn(jid, out)
        self.assertIn("queued", out)
        self.w.fetch()
        self.w.execute(jid)
        self.assertRegex(self.run_cli("jobs")[1], rf"JOB\s+{jid}\n\s+STATUS\s+OK")

    def test_an_approval_job_shows_where_to_approve_it(self):
        code, out, err = self.run_cli("submit", "careful", "--no-wait")
        jid = out.strip()
        self.w.fetch()
        code, out, err = self.run_cli("jobs")
        self.assertIn("Waiting for you", out)
        self.assertIn(f"tct-admin approve {jid}", out)
        code, out, err = self.run_cli("result", jid)
        self.assertEqual(code, 0)
        self.assertIn("Not approved yet", out + err)
        self.assertIn(f"tct-admin approve {jid}", out + err)
        self.assertEqual(self.admin_calls, [])                                                   # a captured run is never offered the choice

    def test_at_a_terminal_a_waiting_job_can_be_approved_on_the_spot(self):
        _, out, _ = self.run_cli("submit", "careful", "--no-wait")
        jid = out.strip()
        self.w.fetch()
        self.ctx.is_tty = lambda: True
        self.answers = ["y"]
        self.run_cli("result", jid)
        self.assertEqual(self.admin_calls, [["approve", jid]])
        self.answers = ["n"]
        self.admin_calls.clear()
        code, out, err = self.run_cli("result", jid)
        self.assertEqual(self.admin_calls, [])
        self.assertIn(f"tct-admin approve {jid}", out + err)

    def test_unknown_and_not_yet_picked_up_jobs(self):
        code, out, err = self.run_cli("result", "job-nosuch1")
        self.assertEqual(code, 1)
        self.assertIn("No such job", out + err)
        self.assertIn("job-nosuch1", out + err)
        _, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        code, out2, err = self.run_cli("result", out.strip())
        self.assertEqual(code, 0)
        self.assertIn("No result yet", out2 + err)

    def test_prune_and_help_and_bad_commands(self):
        code, out, err = self.run_cli("prune")
        self.assertIn("no jobs branch yet", out + err)
        code, out, err = self.run_cli("help")
        self.assertEqual(code, 0)
        for word in ("setup", "submit", "result", "jobs", "classes", "prune", "offload"):
            self.assertIn(word, out)
        self.assertEqual(self.run_cli("nonsense")[0], 2)
        self.assertEqual(self.run_cli()[0], 0)                                                   # no arguments is help

    def test_not_set_up_is_explained(self):
        lonely = cli.Context(ClientRepos(self.tmp / "no" / "q", self.tmp / "no" / "r", "x", "y"), product="testct")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["jobs"], ctx=lonely), 2)
        self.assertIn("not set up yet", out.getvalue() + err.getvalue())
        self.assertIn("ct-run setup", out.getvalue() + err.getvalue())


class Waiting(RepoFixture):
    def clock(self):
        t = [1000.0]
        self.ctx.clock = lambda: t[0]
        self.ctx.sleep = lambda s: t.__setitem__(0, t[0] + s)

    def test_it_returns_the_result_as_soon_as_the_job_finishes(self):
        _, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        jid = out.strip()
        self.w.fetch()
        self.w.execute(jid)
        code, out, err = self.run_cli("result", jid, "--wait")
        self.assertEqual(code, 0)
        self.assertIn("STATUS  ok", err)

    def test_a_job_that_never_finishes_times_out_politely(self):
        self.clock()
        _, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        code, out, err = self.run_cli("result", out.strip(), "--wait", "--timeout", "3")
        self.assertEqual(code, 1)
        self.assertIn("Stopped waiting", out + err)
        self.assertIn("3 s", out + err)
        self.assertIn("The job may still run", out + err)

    def test_waiting_stops_when_the_worker_locks_itself(self):
        self.clock()
        _, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        self.lockdown.engage("quarantine", "job-0001", {"mac"}, cls="c")
        self.w.publish_security()
        code, out2, err = self.run_cli("result", out.strip(), "--wait", "--timeout", "30")
        self.assertEqual(code, 3)
        self.assertIn("the worker is locked", out2 + err)
        self.assertIn("tct-admin unlock", out2 + err)

    def test_the_approval_instruction_is_given_once_while_waiting(self):
        self.clock()
        _, out, _ = self.run_cli("submit", "careful", "--no-wait")
        jid = out.strip()
        self.w.fetch()
        code, out, err = self.run_cli("result", jid, "--wait", "--timeout", "12")
        self.assertEqual(code, 1)
        self.assertEqual((out + err).count("Waiting for your approval"), 1)

    def test_a_failed_job_returns_a_failing_exit_code(self):
        _, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        jid = out.strip()
        self.runner.reply = ("error", 1, "ValueError: bad\n", {}, "")
        self.w.fetch()
        self.w.execute(jid)
        self.assertEqual(self.run_cli("result", jid)[0], 1)

    def test_submit_waits_by_default_in_a_terminal_and_not_when_captured(self):
        self.runner.reply = ("ok", 0, "fine\n", {}, "")
        self.ctx.is_tty = lambda: False
        code, out, err = self.run_cli("submit", "echo", "-p", "word=hi")
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^echo-")                                               # captured: prints the id and returns
        self.assertNotIn("STATUS", err)


class Config(unittest.TestCase):
    def test_the_context_is_built_from_the_configuration(self):
        from ct import config
        cfg = config.load(None, env={"CT_CONFIG": "", "CT_IDENTITY_NAME": "homect", "CT_GITHUB_OWNER": "alice", "CT_PATHS_CLIENT_HOME": "~/.homect"})
        ctx = cli.Context.from_config(cfg)
        self.assertEqual((ctx.product, ctx.admin), ("homect", "ct-admin"))
        self.assertEqual((ctx.repos.queue_url, ctx.repos.results_url), ("git@github.com:alice/homect-queue.git", "git@github.com:alice/homect-results.git"))          # derived from the install name
        self.assertEqual(str(ctx.repos.queue), str(Path("~/.homect/queue").expanduser()))
        self.assertEqual(ctx.hints, [("on your computer", "ct-admin approve {}"), ("on the worker", "ct-worker approve {}")])
        self.assertIn("ct-worker", ctx.own_tools)
        self.assertIn("ct-admin", ctx.own_tools)
        self.assertNotIn("example-owner", json.dumps([ctx.repos.queue_url, ctx.repos.results_url]))


if __name__ == "__main__":
    unittest.main()


class RoundTrip(unittest.TestCase):
    RES = {"id": "j", "status": "ok", "created": "2026-11-17T04:49:00Z", "started": "2026-11-17T04:49:30Z", "finished": "2026-11-17T04:49:36Z",
           "metrics": {"wall_s": 0.02, "cpu_s": 0.01, "peak_rss_mb": 0, "mem_total_mb": 15219, "peak_temp_c": 52, "disk_mb": 0.0}}

    SEEN = datetime(2026, 11, 17, 4, 49, 39, tzinfo=timezone.utc)          # three seconds after the worker published it

    def test_the_round_trip_ends_when_the_result_is_in_hand_not_when_the_worker_finished(self):
        self.assertEqual(view.round_trip(self.RES, self.SEEN), (39, 30, 6, 3, None))
        tail = "\n".join(view.format_result(self.RES, received=self.SEEN)[2])
        for needle in ("ROUND TRIP", "total", "39 s  from sending the job to having its result here", "waiting", "30 s  until the worker started it", "running", "6 s  on the worker", "coming back", "3 s  the result published"):
            self.assertIn(needle, tail)

    def test_a_worker_that_says_when_it_saw_the_job_splits_the_waiting_into_noticing_and_starting(self):
        res = {**self.RES, "seen": "2026-11-17T04:49:25Z"}
        self.assertEqual(view.round_trip(res, self.SEEN), (39, 30, 6, 3, 25))
        rows = dict(view.result_card(res, received=self.SEEN)["meta"])["Round trip"]
        labels = [r[0] for r in rows]
        self.assertIn("Noticing", labels)
        self.assertIn("Starting", labels)
        self.assertNotIn("Waiting", labels)
        self.assertIn("25 s  until the worker's next look at GitHub found it", [r[1] for r in rows])
        self.assertIn("5 s  your approval, a free slot, and the next start pass", [r[1] for r in rows])

    def test_the_card_says_the_same(self):
        rows = dict(view.result_card(self.RES, received=self.SEEN)["meta"])["Round trip"]
        self.assertEqual([r[0] for r in rows], ["Total", "Waiting", "Running", "Coming back"])
        self.assertIn(("Total", "39 s  from sending the job to having its result here"), rows)

    def test_without_a_moment_of_receiving_it_is_measured_to_now(self):
        self.assertGreater(view.round_trip(self.RES)[0], 36)               # the sample result is long past: the trip is as long as it has been since it was sent

    def test_an_older_result_without_a_send_time_just_leaves_the_row_out(self):
        res = {k: v for k, v in self.RES.items() if k != "created"}
        self.assertIsNone(view.round_trip(res, self.SEEN))
        self.assertNotIn("round trip", "\n".join(view.format_result(res, received=self.SEEN)[2]))

    def test_two_clocks_that_disagree_never_give_a_negative_time(self):
        self.assertEqual(view.round_trip({**self.RES, "created": "2026-11-17T04:50:00Z"}, self.SEEN)[0], 0)


class ProgressLine(unittest.TestCase):
    def test_the_live_line_is_one_line_cut_with_a_mark_never_wrapped(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
            def fileno(self):
                raise OSError
        out = Tty()
        with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((60, 24))), mock.patch.dict(os.environ, {"CT_COLOR": "never"}) if False else mock.patch.dict(os.environ, {}):
            p = view.Progress(stream=out, clock=lambda: 0)
            p.draw("sent: waiting for a worker to notice it " * 4, view.YELLOW)
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", out.getvalue())
        self.assertLessEqual(len(line), 59)
        self.assertIn("…", line)
        self.assertTrue(line.endswith("0:00"))


class WaitingChip(unittest.TestCase):
    def test_every_live_line_starts_with_its_status_as_a_capitals_chip_in_the_colour_of_that_status(self):
        from ct import theme
        class Tty(io.StringIO):
            def isatty(self):
                return True
            def fileno(self):
                raise OSError
        for colour, role, text, word in ((view.YELLOW, "wait_chip", "sent: waiting for the worker to notice it", " SENT "), (view.CYAN, "primary_chip", "running on the worker", " RUNNING "),
                                          (view.RED, "bad_chip", "approved, but it was not picked up", " APPROVED, ")):
            out = Tty()
            with mock.patch.dict(os.environ, {"CT_COLOR": "always"}):
                view.Progress(stream=out, clock=lambda: 0).draw(text, colour)
            line = out.getvalue()
            self.assertIn(theme.code(role), line, text)
            self.assertIn(word.strip().rstrip(","), re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).upper(), text)
            self.assertIn(text.partition(" ")[2], re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line), text)


class NoBackgroundSsh(unittest.TestCase):
    """A shared ssh connection (ControlMaster) leaves a background process holding the captured output pipes of the git command, so every pass or fetch hangs until it exits.
    The end-to-end test found this on a real machine; keep it out."""
    def test_nothing_in_the_package_starts_a_background_ssh_master_for_git(self):
        src = Path(__file__).resolve().parents[1] / "src" / "ct"
        for f in src.rglob("*.py"):
            text = f.read_text()
            if ("ControlMaster auto" in text or "ControlMaster=auto" in text) and f.name != "ssh.py":                                  # admin/ssh.py's own use is for interactive commands, not captured git output
                self.fail(f"{f.relative_to(src)} turns on ssh connection sharing")


class FasterPolling(unittest.TestCase):
    def test_a_waiting_client_asks_github_every_two_seconds_not_five(self):
        src = Path(cli.__file__).read_text()
        self.assertIn("for _ in range(10):", src)
        self.assertNotIn("for _ in range(25):", src)
