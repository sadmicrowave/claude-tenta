"""Part 2 of 2 of the test_worker_jobs tests (Lifecycle, WorkerState, JobCeiling)."""
import unittest
import json
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from support import REGISTRY, Base, sh  # noqa: E402


class Lifecycle(Base):
    def test_an_auto_job_goes_from_a_requesters_push_to_a_published_result(self):
        self.job("job-0001")
        self.w.fetch()
        self.assertEqual(self.spawned, ["job-0001"])                                      # fetch queued it and the step pass started it
        self.assertTrue((self.state / "running" / "job-0001").exists())
        self.assertEqual(json.loads((self.state / "running" / "job-0001").read_text())["weight"], 1)
        self.w.execute("job-0001")
        res = self.result("job-0001")
        self.assertEqual((res["status"], res["exit_code"], res["output"], res["class"]), ("ok", 0, "fine\n", "echo"))
        self.assertEqual(res["metrics"]["peak_rss_mb"], 40)
        call = self.runner.calls[0]
        self.assertEqual(call["argv"], ["/bin/echo", "hi"])                                # the argv was built from the template, not a shell string
        self.assertEqual((call["timeout"], call["kw"]["network"], call["kw"]["claude"]), (30, False, False))
        self.assertTrue((self.state / "done" / "job-0001").exists())
        self.assertFalse((self.state / "queued" / "job-0001.json").exists())
        self.assertFalse((self.state / "finished" / "job-0001.json").exists())

    def test_a_finished_job_is_never_run_again(self):
        self.job("job-0001")
        self.run_all()
        self.w.fetch()
        self.assertEqual((self.spawned, len(self.runner.calls)), ([], 1))

    def test_running_status_is_published_first_so_the_client_can_tell_it_was_picked_up(self):
        self.job("job-0001")
        self.w.fetch()
        seen = []
        orig = self.runner.__call__
        self.runner.reply = ("ok", 0, "x", {}, "")

        def peek(argv, timeout, wd, **kw):
            seen.append(self.result("job-0001")["status"])
            return orig(argv, timeout, wd, **kw)
        self.w.runner = peek
        self.w.execute("job-0001")
        self.assertEqual(seen, ["running"])
        self.assertEqual(self.result("job-0001")["status"], "ok")

    def test_input_files_are_written_into_the_job_folder_only_for_classes_that_accept_them(self):
        self.job("job-0001", "files", files={"in.txt": "hello", "sub/b.txt": "x"})
        self.job("job-0002", "echo", files={"in.txt": "hello"})
        self.run_all()
        self.assertEqual(self.runner.calls[0]["files"], ["in.txt", "sub"])
        self.assertEqual(self.result("job-0002")["status"], "rejected")
        self.assertIn("does not accept files", self.result("job-0002")["note"])

    def test_a_file_is_never_written_outside_the_job_folder_even_if_the_name_check_were_wrong(self):
        from unittest import mock
        from ct import jobs
        with mock.patch.object(jobs, "file_name_problem", return_value=None):                  # the name check is the first defence; this is the second, on the path actually written
            self.job("job-0001", "files", files={"../escape.txt": "pwned"})
            self.run_all()
        res = self.result("job-0001")
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["refusal"], "files.name.path")
        self.assertFalse((self.state / "work" / "escape.txt").exists())
        self.assertFalse((self.state / "escape.txt").exists())

    def test_the_write_path_runs_the_same_name_check_again_on_a_windows_worker(self):
        from unittest import mock
        from ct import jobs
        with mock.patch.object(jobs, "check_files", side_effect=lambda files, cls, limits: files), mock.patch.object(jobs, "_on_windows", return_value=True):      # intake let it through: the write is the last defence
            self.job("job-0001", "files", files={"C:evil.txt": "x", "nul.txt": "y"})
            self.run_all()
        res = self.result("job-0001")
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["refusal"], "files.name.path")
        self.assertFalse((self.state / "work" / "job-0001" / "C:evil.txt").exists())

    def test_a_job_whose_file_left_the_repo_is_still_handled_from_its_snapshot(self):
        self.job("job-0001")
        self.w.fetch()
        self.w._p("running", "job-0001").unlink()
        self.spawned.clear()
        req = self.tmp / "requester"
        sh("git", "rm", "-q", "jobs/job-0001.json", cwd=req)
        sh("git", "commit", "-q", "-m", "gone", cwd=req)
        sh("git", "push", "-q", "origin", "HEAD:refs/heads/jobs", cwd=req)
        self.w.fetch()
        self.assertEqual(self.w.orphan_inbox({}), [self.state / "inbox" / "job-0001.json"])
        self.w.execute("job-0001")
        self.assertEqual(self.result("job-0001")["status"], "ok")


class WorkerState(Base):
    """worker-state.json: published when the environment changes, refreshed at most every half hour."""
    FACTS = {"name": "testct", "release": "abc", "os": {"id": "ubuntu", "version": "26.04"}, "cpu": "X", "native_claude": True, "capacity": 4,
             "claude": {"auth": "none"}, "approvers": []}

    def setUp(self):
        super().setUp()
        self.facts = dict(self.FACTS)
        self.make_worker(state_facts=lambda: self.facts)

    def state_commits(self):
        log = sh("git", "log", "--format=%s", "main", cwd=self.tmp / "results.git")
        return sum(1 for l in log.splitlines() if l == "worker state")

    def published(self):
        r = subprocess.run(["git", "-C", str(self.tmp / "results.git"), "show", "main:worker-state.json"], capture_output=True, text=True)
        return json.loads(r.stdout)

    def test_it_is_published_and_says_what_the_worker_is(self):
        self.w.fetch()
        doc = self.published()
        self.assertEqual((doc["name"], doc["os"]["version"], doc["claude"]["auth"], doc["locked"]), ("testct", "26.04", "none", False))
        self.assertIn("careful", doc["classes"])
        self.assertIn("load_1m", doc["observed"])

    def test_nothing_is_published_again_while_nothing_changed(self):
        self.w.fetch()
        self.w.fetch()
        self.w.fetch()
        self.assertEqual(self.state_commits(), 1)

    def test_a_changed_environment_is_published_at_once(self):
        self.w.fetch()
        self.facts["claude"] = {"auth": "account", "tool": {"kind": "native", "version": "2.1.325"}}
        self.w.fetch()
        self.assertEqual(self.state_commits(), 2)
        self.assertEqual(self.published()["claude"]["tool"]["version"], "2.1.325")

    def test_a_lockdown_is_published_even_though_a_locked_worker_does_no_other_work(self):
        self.w.fetch()
        self.lockdown.engage("quarantine", "job-0009", {"private_ip"}, cls="echo")
        self.w.fetch()
        self.assertTrue(self.published()["locked"])

    def test_the_readings_are_refreshed_after_half_an_hour(self):
        self.w.fetch()
        stamp = self.state / "worker-state.stamp"
        old = json.loads(stamp.read_text())
        old["at"] -= 3600
        stamp.write_text(json.dumps(old))
        time.sleep(1.1)                                                   # the file carries its own time to the second; an identical second would be no change to commit
        self.w.fetch()
        self.assertEqual(self.state_commits(), 2)

    def test_a_worker_with_no_facts_provider_publishes_nothing(self):
        self.make_worker()
        self.w.fetch()
        r = subprocess.run(["git", "-C", str(self.tmp / "results.git"), "show", "main:worker-state.json"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_a_provider_that_fails_does_not_break_the_pass(self):
        def boom():
            raise RuntimeError("no cpuinfo")
        self.make_worker(state_facts=boom)
        self.w.fetch()
        self.assertTrue(any("worker state" in m for m in self.logged))


class JobCeiling(Base):
    """A worker accepts only what it can hold (security spot-check F5): a ceiling from its free disk and memory (or [hardware] max_job_mb), checked on the local clone BEFORE the job file is read, a disk reserve that keeps it from
    starting jobs when nearly full, and the published limits following the ceiling on Direct."""

    def worker_with(self, **over):
        import dataclasses
        self.settings = dataclasses.replace(self.settings, **over)
        self.make_worker()

    def shows(self):
        calls = []
        real = self.w.io.git_bytes
        def spy(repo, *args):
            calls.append(args)
            return real(repo, *args)
        self.w.io.git_bytes = spy
        return calls

    def test_a_job_over_the_ceiling_is_refused_before_its_file_is_read(self):
        self.worker_with(max_job_mb=1)
        calls = self.shows()
        self.job("job-0001", "files", files={"big.txt": "x" * 2_000_000})
        self.run_all()
        res = self.result("job-0001")
        self.assertEqual((res["status"], res["refusal"]), ("rejected", "job.too_big_for_worker"))
        self.assertIn("1 MB", res["note"])
        self.assertFalse([c for c in calls if c and c[0] == "show"], "the whole job was read before its size was checked")
        self.assertTrue(any(c[:2] == ("cat-file", "-s") for c in calls))
        self.assertFalse((self.state / "inbox" / "job-0001.json").exists())
        self.assertEqual(self.runner.calls, [])

    def test_a_job_under_the_ceiling_is_accepted_and_runs(self):
        self.worker_with(max_job_mb=1)
        self.job("job-0002", "files", files={"small.txt": "x" * 1000})
        self.run_all()
        self.assertEqual(self.result("job-0002")["status"], "ok")

    def test_the_automatic_ceiling_is_the_smallest_of_a_quarter_of_free_disk_a_quarter_of_free_memory_and_one_gigabyte(self):
        for disk_gb, mem_mb, want in ((100, 4000, 1000), (1, 4000, 250), (1000, 100000, 1024), (100, 400, 100)):
            self.w.snapshot = lambda d=disk_gb, m=mem_mb: {"disk_free_gb": d, "mem_available_mb": m}
            self.assertEqual(self.w.job_ceiling_mb(), want, (disk_gb, mem_mb))
        self.worker_with(max_job_mb=7)
        self.w.snapshot = lambda: {"disk_free_gb": 1000, "mem_available_mb": 100000}
        self.assertEqual(self.w.job_ceiling_mb(), 7)                                              # the owner's number wins

    def test_a_nearly_full_disk_makes_the_worker_wait_instead_of_starting_a_job(self):
        from unittest import mock
        self.worker_with(disk_reserve=True)
        usage = lambda free_gb, total_gb: mock.Mock(free=int(free_gb * 1e9), total=int(total_gb * 1e9))      # noqa: E731
        self.assertEqual(self.w.disk_reserve_gb(total_gb=100), 10.0)                              # 10% of the volume ...
        self.assertEqual(self.w.disk_reserve_gb(total_gb=10), 2.0)                                # ... but never less than 2 GB
        with mock.patch("ct.worker.shutil.disk_usage", return_value=usage(1.5, 100)):
            self.assertIn("disk nearly full", self.w.busy_reason())
        with mock.patch("ct.worker.shutil.disk_usage", return_value=usage(50, 100)):
            self.assertIsNone(self.w.busy_reason())

    def test_on_direct_the_published_file_limits_follow_the_ceiling_and_on_github_they_do_not(self):
        self.worker_with(transport="direct", max_job_mb=50)
        self.w.load_registry()
        files = self.w.limits["files"]
        self.assertEqual((files["max_total_kb"], files["max_file_kb"]), (50_000, 50_000))
        self.assertGreater(files["max_count"], 200)
        self.worker_with(transport="github", max_job_mb=50)
        self.w.load_registry()
        self.assertEqual(self.w.limits["files"]["max_total_kb"], 2000)                            # GitHub keeps the old defaults
        self.assertEqual(self.w.limits["files"]["max_file_kb"], 500)

    def test_a_changed_connection_republishes_the_limits_though_the_registry_did_not_change(self):
        pub = lambda: json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["limits"]["files"]["max_total_kb"]       # noqa: E731
        self.worker_with(transport="github", max_job_mb=50)
        self.assertTrue(self.w.publish_registry_info(self.w.load_registry()))
        self.assertEqual(pub(), 2000)
        self.worker_with(transport="direct", max_job_mb=50)
        self.assertTrue(self.w.publish_registry_info(self.w.load_registry()))
        self.assertEqual(pub(), 50_000)

    def test_a_limit_the_owner_set_in_the_registry_still_wins_on_direct(self):
        self.registry_path.write_text(REGISTRY + "limits:\n  files:\n    max_total_kb: 100\n")
        self.worker_with(transport="direct", max_job_mb=50)
        self.w.load_registry()
        self.assertEqual(self.w.limits["files"]["max_total_kb"], 100)

    def test_the_worker_publishes_its_ceiling(self):
        import dataclasses
        self.settings = dataclasses.replace(self.settings, max_job_mb=64)
        self.make_worker(state_facts=lambda: {"name": "testct", "release": "0" * 40, "os": {"id": "ubuntu"}, "cpu": "x", "capacity": 4})
        self.w.fetch()
        r = subprocess.run(["git", "-C", str(self.tmp / "results.git"), "show", "main:worker-state.json"], capture_output=True, text=True)
        self.assertEqual(json.loads(r.stdout)["observed"]["job_ceiling_mb"], 64)

    def test_a_waiting_job_is_started_from_a_small_sidecar_not_by_parsing_the_whole_job(self):
        self.job("job-0003", "files", files={"in.txt": "hello"})
        self.w.fetch()
        meta = self.state / "queued" / "job-0003.meta"
        self.assertEqual(json.loads(meta.read_text())["class"], "files")
        self.w._p("queued", "job-0003.json").write_bytes(b"not json at all " * 1000)               # step must not need to parse it to learn the class
        self.w._p("running", "job-0003").unlink(missing_ok=True)                                   # (fetch already started it once)
        self.spawned.clear()
        self.w.step()
        self.assertEqual(self.spawned, ["job-0003"])


if __name__ == "__main__":
    unittest.main()
