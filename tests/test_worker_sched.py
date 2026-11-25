"""ct-worker: smart sleep, capacity, the watchdog, shared computers and housekeeping."""
import unittest
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from ct import gitio, util  # noqa: E402
from support import Base, sh  # noqa: E402


class SmartSleep(Base):
    """An idle worker stops asking GitHub every pass; any work ends the idleness at once."""

    def setUp(self):
        super().setUp()
        self.syncs = 0
        real = self.io.sync
        self.io.sync = lambda: (setattr(self, "syncs", self.syncs + 1), real())[1]
        self.settings.idle_after_s, self.settings.idle_fetch_s = 600, 90

    def age(self, name, seconds):
        p = self.state / name
        t = time.time() - seconds
        os.utime(p, (t, t))

    def go_idle(self):
        self.w.fetch()                                                   # a first pass: leaves the stamps
        (self.state / "activity.stamp").touch()
        (self.state / "last-sync.stamp").touch()
        self.age("activity.stamp", 3600)
        self.age("last-sync.stamp", 5)

    def test_a_worker_with_nothing_to_do_skips_the_check_between_slow_polls(self):
        self.go_idle()
        before = self.syncs
        self.assertEqual(self.w.fetch(), [])
        self.assertEqual(self.syncs, before)
        self.assertTrue(self.w.idle_now)

    def test_after_a_longer_quiet_it_sleeps_and_checks_only_at_the_sleep_pace(self):
        self.settings.sleep_after_s, self.settings.sleep_fetch_s = 7200, 300
        self.go_idle()
        self.age("activity.stamp", 8000)                                  # quiet for longer than sleep_after_s
        self.age("last-sync.stamp", 100)                                  # past the idle pace (90) but not the sleep pace (300)
        before = self.syncs
        self.w.fetch()
        self.assertEqual((self.syncs, self.w.mode), (before, "sleep"))
        self.age("last-sync.stamp", 400)
        self.w.fetch()
        self.assertEqual(self.syncs, before + 1)

    def test_sleep_mode_can_be_turned_off_and_work_wakes_it(self):
        self.settings.sleep_after_s, self.settings.sleep_fetch_s = 7200, 0
        self.go_idle()
        self.age("activity.stamp", 8000)
        self.age("last-sync.stamp", 100)
        before = self.syncs
        self.w.fetch()
        self.assertEqual((self.syncs, self.w.mode), (before + 1, "idle"))     # no sleep mode: it stays at the idle pace
        self.settings.sleep_fetch_s = 300
        self.job("job-0002", "echo")
        self.age("last-sync.stamp", 400)
        self.w.fetch()                                                    # the slow check finds the job ...
        self.w.fetch()                                                    # ... and from then on the worker is active again
        self.assertEqual(self.w.mode, "active")

    def test_it_still_checks_at_the_slow_pace(self):
        self.go_idle()
        self.age("last-sync.stamp", 100)                                  # longer than idle_fetch_s
        before = self.syncs
        self.w.fetch()
        self.assertEqual(self.syncs, before + 1)

    def test_a_new_job_is_seen_by_the_next_slow_check_and_ends_the_idleness(self):
        self.go_idle()
        self.job("job-0001", "echo")
        self.age("last-sync.stamp", 100)
        self.w.fetch()
        self.assertFalse(self.w.idle_now and not self.w.has_work())
        self.assertTrue((self.state / "activity.stamp").stat().st_mtime > time.time() - 60)
        before = self.syncs
        self.w.fetch()                                                    # not idle any more: every pass checks again
        self.assertEqual(self.syncs, before + 1)

    def test_a_job_waiting_for_approval_keeps_the_normal_pace(self):
        self.job("job-0001", "careful")
        self.w.fetch()
        self.age("activity.stamp", 3600)
        self.age("last-sync.stamp", 5)
        before = self.syncs
        self.w.fetch()
        self.assertEqual(self.syncs, before + 1)

    def test_a_locked_worker_keeps_the_normal_pace_so_an_unlock_order_is_seen_quickly(self):
        self.go_idle()
        self.lockdown.engage("quarantine", "job-0009", {"private_ip"}, cls="echo")
        before = self.syncs
        self.w.fetch()
        self.assertEqual(self.syncs, before + 1)

    def test_zero_turns_smart_sleep_off(self):
        self.settings.idle_fetch_s = 0
        self.go_idle()
        before = self.syncs
        self.w.fetch()
        self.assertEqual(self.syncs, before + 1)


class Capacity(Base):
    def test_strict_fifo_within_the_capacity_budget(self):
        for jid, cls in (("job-0001", "echo"), ("job-0002", "big"), ("job-0003", "echo")):
            self.job(jid, cls)
            time.sleep(0.05)
        self.w.fetch()
        self.assertEqual(self.spawned, ["job-0001"])                                          # big needs all 4 points: it waits, and job-0003 waits behind it
        self.assertTrue(any("job-0002 needs 4" in m for m in self.logged))

    def test_a_loaded_or_hot_machine_starts_nothing(self):
        self.job("job-0001")
        self.sensors.t = 99
        self.w.fetch()
        self.assertEqual(self.spawned, [])
        self.assertTrue(any("core temp 99C" in m for m in self.logged))
        self.sensors.t = 40
        self.load = 9.0                                                                          # a busy machine, whatever this computer is doing
        self.w.step()
        self.assertEqual(self.spawned, [])
        self.assertTrue(any("load" in m for m in self.logged))

    def test_two_step_passes_never_overlap(self):
        with gitio.FileLock(self.state / "step.lock", wait=0) as held:
            self.assertTrue(held.got)
            self.job("job-0001")
            self.w.fetch()
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.w.step(), ["job-0001"])

    def test_a_job_already_running_is_not_started_twice(self):
        self.job("job-0001")
        self.w.fetch()
        self.assertEqual(self.w.step(), [])
        self.assertEqual(self.spawned, ["job-0001"])


class Watchdog(Base):
    def test_a_crashed_job_gets_an_error_result(self):
        self.job("job-0001")
        self.w.fetch()                                                                        # started; the fake process id is not alive
        self.w.reap()
        self.w.publish_pending()
        res = self.result("job-0001")
        self.assertEqual(res["status"], "error")
        self.assertIn("crash or reboot", res["note"])

    def test_a_job_past_its_deadline_is_asked_to_stop_then_killed(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.make_worker(is_job_process=lambda pid, jid: pid == proc.pid and proc.poll() is None)
        marker = self.state / "running" / "job-0009"
        marker.write_text(json.dumps({"pid": proc.pid, "weight": 1, "started": util.iso(), "deadline": time.time() - 5}))
        self.w.reap()
        self.assertIn("term_sent", json.loads(marker.read_text()))
        proc.wait(timeout=10)                                                                 # SIGTERM stopped it
        self.assertEqual(proc.returncode, -signal.SIGTERM)
        self.assertTrue(any("exceeded its time limit" in m for m in self.logged))

    def test_a_job_that_ignores_sigterm_is_hard_killed(self):
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        time.sleep(0.5)
        self.make_worker(is_job_process=lambda pid, jid: pid == proc.pid and proc.poll() is None)
        marker = self.state / "running" / "job-0009"
        marker.write_text(json.dumps({"pid": proc.pid, "weight": 1, "started": util.iso(), "deadline": time.time() - 100, "term_sent": time.time() - 60}))
        self.w.reap()
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertEqual(json.loads(marker.read_text())["watchdog"], "hard-killed")
        self.make_worker()                                                                     # now the process is gone
        self.w.reap()
        self.w.publish_pending()
        self.assertEqual(self.result("job-0009")["status"], "killed")

    def test_kill_job_only_signals_a_process_that_really_is_that_job(self):
        marker = self.state / "running" / "job-0009"
        marker.write_text(json.dumps({"pid": os.getpid(), "weight": 1}))
        self.assertFalse(self.w.kill_job("job-0009"))                                          # is_job_process says no: our own pid is untouched
        self.assertFalse(self.w.kill_job("job-0000"))

    def test_results_that_could_not_be_pushed_are_kept_and_retried(self):
        self.job("job-0001")
        sh("git", "remote", "set-url", "origin", str(self.tmp / "nowhere.git"), cwd=self.tmp / "w-results")
        self.run_all()
        self.assertTrue((self.state / "finished" / "job-0001.json").exists())                 # kept locally
        self.assertFalse((self.state / "done" / "job-0001").exists())
        sh("git", "remote", "set-url", "origin", str(self.tmp / "results.git"), cwd=self.tmp / "w-results")
        self.w.publish_pending()
        self.assertEqual(self.result("job-0001")["status"], "ok")
        self.assertTrue((self.state / "done" / "job-0001").exists())


class Housekeeping(Base):
    def test_old_snapshots_work_folders_and_kept_output_are_cleaned_but_unfinished_work_is_kept(self):
        old = time.time() - 10 * 86400
        (self.state / "inbox" / "job-done1.json").write_text("{}")
        (self.state / "done" / "job-done1").write_text("")
        (self.state / "inbox" / "job-wait1.json").write_text("{}")
        (self.state / "work" / "job-old11").mkdir()
        (self.state / "work" / "job-run11").mkdir()
        (self.state / "running" / "job-run11").write_text("{}")
        for p in (self.state / "inbox" / "job-done1.json", self.state / "inbox" / "job-wait1.json", self.state / "work" / "job-old11", self.state / "work" / "job-run11"):
            os.utime(p, (old, old))
        self.w.clean_work()
        self.assertFalse((self.state / "inbox" / "job-done1.json").exists())
        self.assertTrue((self.state / "inbox" / "job-wait1.json").exists())
        self.assertFalse((self.state / "work" / "job-old11").exists())
        self.assertTrue((self.state / "work" / "job-run11").exists())

    def test_results_history_is_pruned_at_most_once_a_day(self):
        self.settings.prune_threshold = 0
        self.assertIsNotNone(self.w.maybe_prune_results())
        self.assertIsNone(self.w.maybe_prune_results())

    def test_the_decoys_are_planted_under_the_configured_identity(self):
        self.settings.identity = "myinstall"
        self.w.fetch()
        self.assertTrue((self.home / ".git-credentials").read_text().startswith("https://myinstall:"))


class SharedComputer(Base):
    """A computer other people use: no new job while a person is logged in (a running one finishes), or outside its working hours; the worker says why."""

    def setUp(self):
        super().setUp()
        from datetime import datetime, timezone
        self.noon = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)

    def hold(self, fixed_clock=False, **kw):
        for k, v in kw.items():
            setattr(self.settings, k, v)
        self.make_worker(**({"clock": lambda: self.noon} if fixed_clock else {}))                       # a fixed clock only where the hours matter: jobs are dated by the real one

    def test_nothing_changes_unless_the_owner_asks_for_it(self):
        self.sensors.people = ["alice"]
        self.assertIsNone(self.w.busy_reason())                                         # the default: a computer of its own

    def test_a_person_logged_in_holds_new_jobs_and_the_owners_own_account_does_not_count(self):
        self.hold(pause_when_logged_in=True, ignore_users=("agent",))
        self.sensors.people = ["agent"]
        self.assertIsNone(self.w.busy_reason())
        self.sensors.people = ["agent", "alice", "bob"]
        self.assertEqual(self.w.busy_reason(), "a person is using this computer (alice, bob)")
        self.assertEqual(self.w.snapshot()["held"], "a person is using this computer (alice, bob)")          # published, so the scheduler and the cards can say it
        self.job("job-0001")
        self.w.fetch()
        self.assertEqual(self.spawned, [])                                                # queued, not started
        self.sensors.people = []
        self.w.fetch()
        self.assertEqual(self.spawned, ["job-0001"])                                      # started as soon as nobody is there

    def test_a_working_window_holds_jobs_outside_it_including_one_that_crosses_midnight(self):
        import re
        local = self.noon.astimezone().strftime("%H:%M")
        self.hold(True, window=f"{local}-23:59" if local < "23:59" else "00:00-00:01")
        self.assertIsNone(self.w.busy_reason())                                           # now is inside
        h = int(local[:2])
        outside_start = f"{(h + 2) % 24:02d}:00"
        outside_end = f"{(h + 4) % 24:02d}:00"
        self.hold(True, window=f"{outside_start}-{outside_end}")
        self.assertRegex(self.w.busy_reason(), r"outside its working hours")
        crossing = f"{(h + 2) % 24:02d}:00-{(h + 1) % 24:02d}:00"                       # all day except the hour from now to now+1: now is inside a window that crosses midnight
        self.hold(True, window=crossing)
        self.assertIsNone(self.w.busy_reason())


if __name__ == "__main__":
    unittest.main()
