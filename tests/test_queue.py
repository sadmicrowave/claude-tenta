"""The waiting list on this computer (R1): a job that fits no worker waits in order, survives a kill, and is sent by the next command once a worker has room."""
import os
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from ct.client import place, queue
from test_place import Ctx, Setup

SRC = str(Path(__file__).resolve().parent.parent / "src")


class Store(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_order_is_priority_then_age(self):
        for jid, prio, at in (("a-1", "normal", 10), ("b-2", "high", 30), ("c-3", "low", 5), ("d-4", "high", 20), ("e-5", "normal", 1)):
            queue.add(self.home, jid, "sys-status", prio, {}, "busy", now=at)
        self.assertEqual([i["id"] for i in queue.waiting(self.home)], ["d-4", "b-2", "e-5", "a-1", "c-3"])

    def test_the_file_is_private(self):
        queue.add(self.home, "a-1", "x", "normal", {"files": {"f": "secret"}}, "busy")
        self.assertEqual(stat.S_IMODE((self.home / "queue.json").stat().st_mode), 0o600)

    def test_an_unknown_priority_is_normal(self):
        self.assertEqual(queue.add(self.home, "a-1", "x", "urgent", {}, "busy")["priority"], "normal")

    def test_a_job_survives_a_process_killed_right_after_adding_it(self):
        code = f"import sys,os,signal; sys.path.insert(0, {SRC!r}); from ct.client import queue; queue.add({str(self.home)!r}, 'killed-1', 'sys-status', 'high', {{'p': 1}}, 'busy'); os.kill(os.getpid(), signal.SIGKILL)"
        r = subprocess.run([sys.executable, "-c", code])
        self.assertEqual(r.returncode, -signal.SIGKILL)
        got = queue.waiting(self.home)
        self.assertEqual([(i["id"], i["priority"], queue.job_of(self.home, i)) for i in got], [("killed-1", "high", {"p": 1})])

    def test_the_payload_has_its_own_private_file_and_leaves_with_the_job(self):
        queue.add(self.home, "a-1", "x", "normal", {"files": {"f": "secret"}}, "busy")
        f = self.home / "queue" / "a-1.json"
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertNotIn("secret", (self.home / "queue.json").read_text())
        queue.remove(self.home, "a-1")
        self.assertFalse(f.exists())

    def test_a_job_id_already_waiting_is_refused(self):
        queue.add(self.home, "a-1", "x", "normal", {}, "busy")
        with self.assertRaises(ValueError):
            queue.add(self.home, "a-1", "x", "normal", {}, "busy")

    def test_cancel_and_priority_work_on_waiting_jobs_only(self):
        queue.add(self.home, "a-1", "x", "normal", {}, "busy")
        queue.add(self.home, "b-2", "x", "normal", {}, "busy")
        self.assertTrue(queue.set_priority(self.home, "a-1", "high"))
        self.assertEqual(queue.find(self.home, "a-1")["priority"], "high")
        self.assertTrue(queue.claim(self.home, "b-2", "w"))
        self.assertFalse(queue.claim(self.home, "b-2", "w"))                  # a second drain cannot take what is being sent
        self.assertFalse(queue.cancel(self.home, "b-2"))                      # on its way: not cancelled
        self.assertFalse(queue.set_priority(self.home, "b-2", "low"))
        self.assertTrue(queue.cancel(self.home, "a-1"))
        self.assertIsNone(queue.find(self.home, "a-1"))
        self.assertFalse(queue.cancel(self.home, "nope-1"))

    def test_a_broken_file_is_an_empty_list_not_a_crash(self):
        (self.home / "queue.json").write_text("{not json")
        self.assertEqual(queue.waiting(self.home), [])
        queue.add(self.home, "a-1", "x", "normal", {}, "busy")
        self.assertEqual(len(queue.waiting(self.home)), 1)


class Drain(Setup):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.main = Ctx("mini", self.tmp / "home")
        self.main.repos = None

    def drain(self):
        return queue.drain(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], send=lambda repos, jid, job: self.sent.append((jid, job)), check=lambda c, n: None)

    def test_nothing_is_sent_while_every_worker_is_full_and_the_reason_is_kept(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n, running=4, capacity=4)
        queue.add(self.main.home, "a-1", "sys-status", "normal", {"x": 1}, "")
        self.assertEqual(self.drain(), [])
        self.assertIsNone(queue.find(self.main.home, "a-1").get("sending"))
        self.assertEqual(self.sent, [])
        self.assertIn("busy", queue.find(self.main.home, "a-1")["why"])

    def test_when_room_appears_the_highest_priority_goes_first_and_leaves_the_list(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n, running=3, capacity=4)                                  # one free slot in the whole fleet, one slot per worker
        self.repos["mini"].pull_results = lambda: None
        import time
        queue.add(self.main.home, "low-1", "sys-status", "low", {}, "", now=time.time() - 60)
        queue.add(self.main.home, "high-1", "sys-status", "high", {}, "", now=time.time() - 5)
        done = self.drain()
        self.assertEqual([j for j, _ in done][0], "high-1")
        self.assertEqual([j for j, _ in self.sent][0], "high-1")
        self.assertIsNone(queue.find(self.main.home, "high-1"))

    def test_a_failed_send_keeps_the_job_with_the_real_error(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        queue.add(self.main.home, "a-1", "sys-status", "normal", {}, "")

        def boom(repos, jid, job):
            raise OSError("the worker has locked itself")
        done = queue.drain(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], send=boom, check=lambda c, n: None)
        self.assertEqual(done, [])
        self.assertIn("locked itself", queue.find(self.main.home, "a-1")["why"])
        self.assertEqual(place.pending_sent(self.main.home, "mini", self.repos["mini"]), 0)          # the reservation was released

    def test_the_placement_is_recorded_so_results_are_found_on_that_worker(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        queue.add(self.main.home, "a-1", "sys-status", "normal", {}, "")
        (jid, worker), = self.drain()
        self.assertEqual(place.worker_of(self.main.home, jid), worker)

    def test_two_drains_at_once_send_a_job_once(self):
        import threading
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        queue.add(self.main.home, "a-1", "sys-status", "normal", {}, "")
        gate = threading.Event()

        def slow_send(repos, jid, job):
            self.sent.append(jid)
            gate.wait(2)
        t = threading.Thread(target=lambda: queue.drain(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], send=slow_send, check=lambda c, n: None))
        t.start()
        while not self.sent:
            pass
        second = queue.drain(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], send=slow_send, check=lambda c, n: None)
        gate.set()
        t.join()
        self.assertEqual((second, self.sent), ([], ["a-1"]))

    def test_a_send_cut_short_is_asked_about_at_the_one_worker_only(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        asked = []
        for n, arrived in (("a-1", True), ("b-2", False)):
            queue.add(self.main.home, n, "sys-status", "normal", {}, "")
            queue.claim(self.main.home, n, "vmx", now=1)                         # marked sending long ago, then the process died
        for r in self.repos.values():
            r.job_known = lambda jid, who=r: asked.append((who, jid)) or jid == "a-1"
        done = queue.drain(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], send=lambda *a: None, check=lambda c, n: None, now=1000)
        self.assertEqual({asked_who for asked_who, _ in asked}, {self.repos["vmx"]})        # not every worker: the recorded one
        self.assertIn(("a-1", "vmx"), done)                                      # it had arrived: placed, not sent twice
        self.assertEqual(place.worker_of(self.main.home, "a-1"), "vmx")
        self.assertNotIn("a-1", queue.ids(self.main.home))
        self.assertIn("b-2", [j for j, _ in done] + list(queue.ids(self.main.home)))   # it had not: waiting again, or sent now

    def test_a_job_that_waited_too_long_ends_with_the_reason(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n, running=4, capacity=4)
        queue.add(self.main.home, "old-1", "sys-status", "normal", {}, "", now=0)
        dropped = []
        queue.drain(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], send=lambda *a: None, check=lambda c, n: None, now=queue.EXPIRE_S + 5, dropped=dropped)
        self.assertEqual([j for j, _ in dropped], ["old-1"])
        self.assertIn("waited", dropped[0][1])
        self.assertEqual(queue.waiting(self.main.home), [])

    def test_a_command_no_worker_offers_any_more_ends_at_once(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        queue.add(self.main.home, "gone-1", "removed-class", "normal", {}, "")
        dropped = []
        self.assertEqual(queue.drain(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], send=lambda *a: None, check=lambda c, n: None, dropped=dropped), [])
        self.assertIn("no worker offers removed-class", dropped[0][1])

    def test_hold_returns_the_explanation_and_reserves_nothing_when_every_worker_is_full(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n, running=4, capacity=4)
        (ctx, ch), main, _ = self.route_hold()
        self.assertIsNotNone(ctx.held)
        self.assertFalse((main.home / "placements.json").exists())

    def route_hold(self):
        main = Ctx("mini", self.tmp / "home2")
        r = place.route(main, self.cfgs["mini"], "sys-status", None, entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], make_ctx=lambda e: main, hold=True)
        return r, main, None


class Screens(Setup):
    def test_queue_list_cancel_and_priority(self):
        from ct.client import cli
        home = self.tmp / "hq"
        queue.add(home, "q-1", "sys-status", "normal", {}, "every worker is busy")
        ctx = Ctx("mini", home)
        a = lambda **k: type("A", (), k)()
        rc, _, err = self.run_cli(cli.cmd_queue, ctx, a(queue_cmd=None))
        self.assertIn("q-1", err)
        self.assertEqual(self.run_cli(cli.cmd_queue, ctx, a(queue_cmd="priority", id="q-1", level="high"))[0], 0)
        self.assertEqual(queue.find(home, "q-1")["priority"], "high")
        self.assertEqual(self.run_cli(cli.cmd_queue, ctx, a(queue_cmd="cancel", id="q-1"))[0], 0)
        rc, _, err = self.run_cli(cli.cmd_queue, ctx, a(queue_cmd="cancel", id="q-1"))
        self.assertEqual(rc, 1)
        self.assertIn("No such waiting job", err)

    """What the person sees: submit says it is waiting, jobs and result list it, and the id is printed for a script."""

    def run_cli(self, fn, *args):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(*args)
        return rc, out.getvalue(), err.getvalue()

    def test_hold_jobs_queues_prints_the_id_and_says_why(self):
        from ct.client import cli
        for n in ("mini", "vmx", "ghx"):
            self.add(n, running=4, capacity=4)
        (ctx, ch), main, _ = Drain.route_hold(self)
        a = type("A", (), {"cls": "sys-status", "id": "wait-1", "priority": "high"})()
        ctx.repos = type("R", (), {"result_ids": lambda s: []})()
        rc, out, err = self.run_cli(cli.hold_jobs, ctx, a, [{}], {"class": "sys-status"}, ch)
        self.assertEqual(rc, 0)
        self.assertEqual(out.split(), ["wait-1"])
        self.assertIn("Waiting for a worker", err)
        self.assertEqual(queue.find(main.home, "wait-1")["priority"], "high")

    def test_result_of_a_waiting_job_says_waiting_not_no_such_job(self):
        from ct.client import cli
        home = self.tmp / "h"
        queue.add(home, "wait-2", "sys-status", "normal", {}, "every worker is busy")
        ctx = Ctx("mini", home)
        ctx.repos = type("R", (), {"need_setup": lambda s: None})()
        a = type("A", (), {"id": "wait-2", "wait": True, "timeout": 1, "json": False})()
        rc, _, err = self.run_cli(cli.cmd_result, ctx, a)
        self.assertEqual(rc, 0)
        self.assertIn("Waiting for a worker", err)
        self.assertNotIn("No such job", err)


if __name__ == "__main__":
    unittest.main()
