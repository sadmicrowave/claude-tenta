"""Worker health (R2): a worker silent for a day gets no new job, and a job it was holding with no result is lost, and queued again only if its class may be repeated."""
import io
import json
import stat
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

import support  # noqa: F401
from armada import sched
from armada.admin import workers as wk
from armada.client import cli, health, place, queue
from test_place import Ctx, Setup
from test_sched import state, worker_view

DAY = 86400
OLD = "2020-01-01T00:00:00Z"


class HCtx(Ctx):
    silent_hours = 24


class Silent(unittest.TestCase):
    """The pure part: which workers are left out, and why."""

    def pick(self, *ws):
        return sched.choose("sys-status", "command", list(ws))

    def view(self, name, age, **kw):
        return worker_view(name, "direct", state(), {"classes": {"sys-status": {"kind": "command", "cost": "light"}}, "capacity": 4}, age, 5, **kw)

    def test_a_reading_older_than_the_limit_leaves_the_worker_out_with_its_age(self):
        c = self.pick(self.view("quiet", 3 * DAY), self.view("live", 60))
        self.assertEqual(c.worker, "live")
        self.assertEqual(c.reasons["quiet"][0][0], "placement.silent")
        self.assertIn("3 days", dict((n, r) for n, _, r in c.lines)["quiet"])

    def test_a_reading_just_inside_the_limit_is_still_used_but_ranked_after_fresh_ones(self):
        c = self.pick(self.view("late", DAY - 60), self.view("live", 60))
        self.assertEqual(c.worker, "live")
        self.assertNotIn("late", c.reasons)

    def test_a_worker_that_never_published_is_not_silent_it_has_no_reading(self):
        c = self.pick(worker_view("blank", "direct", None, None, None, 5))
        self.assertEqual(c.reasons["blank"][0][0], "placement.no_state")

    def test_the_limit_is_the_setting(self):
        w = self.view("w", 3 * 3600, silent_after_s=7200)
        self.assertTrue(w.silent)
        self.assertEqual(self.pick(w).reasons["w"][0][0], "placement.silent")

    def test_when_every_worker_is_silent_the_job_is_refused_naming_each(self):
        c = self.pick(self.view("a", 2 * DAY), self.view("b", 5 * DAY))
        self.assertIsNone(c.worker)
        self.assertEqual([c.reasons[n][0][0] for n in ("a", "b")], ["placement.silent"] * 2)


class Wiring(Setup):
    """The placement wiring reads the reading's age from the worker's own state file and honours the setting."""

    def test_a_silent_worker_gets_no_job_through_the_real_wiring(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        s = json.loads((self.tmp / "r-vmx" / "worker-state.json").read_text())
        s["updated"] = OLD
        (self.tmp / "r-vmx" / "worker-state.json").write_text(json.dumps(s))
        views = place.gather(self.ents(self.cfgs["mini"]), lambda e: self.repos[e[0]], home=self.tmp / "home")
        c = place.decide("sys-status", views)
        self.assertNotEqual(c.worker, "vmx")
        self.assertEqual(c.reasons["vmx"][0][0], "placement.silent")


class LostBase(Setup):
    def setUp(self):
        super().setUp()
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        self.main = HCtx("mini", self.tmp / "home")
        self.main.repos = None
        self.looks = []

    def quiet(self, name):
        p = self.tmp / f"r-{name}" / "worker-state.json"
        s = json.loads(p.read_text())
        s["updated"] = OLD
        p.write_text(json.dumps(s))

    def sent(self, jid, worker="vmx", cls="sys-status", age=2 * DAY, repeatable=True):
        place.record(self.main.home, [jid], worker, cls)
        place._update(self.main.home, lambda d: d[jid].update(at=int(time.time() - age)))
        if repeatable:
            health.keep(self.main.home, jid, {"class": cls, "params": {"p": 1}})

    def result(self, worker, jid, status="done"):
        d = self.tmp / f"r-{worker}" / "results"
        d.mkdir(exist_ok=True)
        (d / f"{jid}.json").write_text(json.dumps({"status": status}))

    def sweep(self, now=None):
        return health.sweep(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], fetch=lambda r: self.looks.append(1) or r.pull_results(), now=now)



class Lost(LostBase):
    def test_a_job_on_a_silent_worker_with_no_result_is_lost_and_a_repeatable_one_is_queued_again(self):
        self.quiet("vmx")
        self.sent("scan-1")
        (jid, worker, why, again, outcome), = self.sweep()
        self.assertEqual((jid, worker, again, outcome), ("scan-1", "vmx", "scan-1-again", "again"))
        self.assertIn("published nothing", why)
        q = queue.find(self.main.home, "scan-1-again")
        self.assertEqual((q["class"], q["priority"]), ("sys-status", "normal"))
        self.assertEqual(queue.job_of(self.main.home, q), {"class": "sys-status", "params": {"p": 1}})
        self.assertIn("went silent", q["why"])
        mark = json.loads((self.main.home / "placements.json").read_text())["scan-1"]
        self.assertEqual(mark["again"], "scan-1-again")
        self.assertIn("vmx", mark["lost"]["why"])
        self.assertIsNone(health.kept(self.main.home, "scan-1"))                # the copy moved to the waiting list

    def test_a_class_not_marked_repeatable_is_only_marked_lost(self):
        self.quiet("vmx")
        self.sent("scan-1", repeatable=False)
        (jid, worker, why, again, outcome), = self.sweep()
        self.assertEqual((again, outcome), (None, "not_repeatable"))
        self.assertEqual(queue.waiting(self.main.home), [])
        self.assertIn("lost", json.loads((self.main.home / "placements.json").read_text())["scan-1"])

    def test_it_is_queued_again_once_not_at_every_command(self):
        self.quiet("vmx")
        self.sent("scan-1")
        self.sweep()
        self.assertEqual(self.sweep(now=time.time() + 2 * 3600), [])
        self.assertEqual([i["id"] for i in queue.waiting(self.main.home)], ["scan-1-again"])

    def test_a_worker_that_is_alive_is_not_losing_the_job_it_is_just_slow(self):
        self.sent("long-1")
        self.assertEqual(self.sweep(), [])
        self.assertEqual(queue.waiting(self.main.home), [])

    def test_a_job_younger_than_the_limit_is_left_alone_and_nothing_is_looked_at(self):
        self.quiet("vmx")
        self.sent("new-1", age=3600)
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.looks, [])                                         # the normal path costs no network call

    def test_a_job_with_a_final_result_is_not_lost_and_its_copy_goes(self):
        self.quiet("vmx")
        self.sent("done-1")
        self.result("vmx", "done-1")
        self.assertEqual(self.sweep(), [])
        self.assertIsNone(health.kept(self.main.home, "done-1"))
        self.assertEqual(self.looks, [])

    def test_a_pending_result_is_not_final(self):
        self.quiet("vmx")
        self.sent("run-1")
        self.result("vmx", "run-1", "running")
        self.assertEqual(len(self.sweep()), 1)

    def test_an_unreachable_worker_with_an_old_job_is_lost_with_the_real_error(self):
        self.repos["vmx"].fail = "ssh: connect to host vmx.example port 22: Connection timed out"
        self.sent("scan-1")
        (jid, worker, why, again, outcome), = self.sweep()
        self.assertIn("Connection timed out", why)
        self.assertEqual(again, "scan-1-again")

    def test_a_worker_that_comes_back_and_publishes_the_result_makes_the_job_final_again(self):
        self.quiet("vmx")
        self.sent("scan-1", repeatable=False)
        self.sweep()
        self.assertEqual([x[0] for x in health.lost_now(self.main.home, set())], ["scan-1"])
        self.result("vmx", "scan-1")
        self.assertEqual(health.lost_now(self.main.home, {"scan-1"}), [])        # the jobs list stops showing it
        self.assertEqual(self.sweep(), [])

    def test_a_repeatable_class_with_no_copy_says_so_instead_of_claiming_it_is_not_repeatable(self):
        for n in self.repos:
            p = self.tmp / f"r-{n}" / "registry.json"
            reg = json.loads(p.read_text())
            reg["classes"]["sys-status"]["repeatable"] = True
            p.write_text(json.dumps(reg))
        self.quiet("vmx")
        self.sent("old-1", repeatable=False)                                      # sent before copies were kept, or from another computer
        (_, _, _, again, outcome), = self.sweep()
        self.assertEqual((again, outcome), (None, "no_copy"))

    def test_two_commands_at_once_queue_a_lost_job_once(self):
        self.quiet("vmx")
        self.sent("scan-1")
        other = []

        def fetch(r):                                                             # while this command is looking, another one marks the job and queues it
            if not other:
                other.append(1)
                place._update(self.main.home, lambda d: d["scan-1"].update(lost={"why": "x", "at": 1}, again="scan-1-again"))
            r.pull_results()
        self.assertEqual(health.sweep(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], fetch=fetch), [])
        self.assertEqual(queue.waiting(self.main.home), [])

    def test_a_long_id_still_gets_a_valid_new_id(self):
        from armada.jobs import ID_RE
        jid = "a" * 64
        self.quiet("vmx")
        self.sent(jid)
        (_, _, _, again, _), = self.sweep()
        self.assertTrue(ID_RE.match(again), again)
        self.assertTrue(again.endswith("-again"))

    def test_the_jobs_list_lets_go_of_the_copy_of_a_finished_job(self):
        self.sent("done-1")
        health.forget_final(self.main.home, {"done-1", "never-sent-9"})
        self.assertIsNone(health.kept(self.main.home, "done-1"))

    def test_a_copy_that_is_too_big_or_has_no_job_id_is_not_kept(self):
        from unittest import mock
        self.assertFalse(health.keep(self.main.home, "../../escape", {"x": 1}))
        self.assertFalse((self.main.home.parent / "escape.json").exists())
        self.assertIsNone(health.kept(self.main.home, "../../escape"))
        with mock.patch.object(health, "KEEP_MAX_BYTES", 10):
            self.assertFalse(health.keep(self.main.home, "big-job-1", {"files": {"f": "x" * 100}}))
        self.assertIsNone(health.kept(self.main.home, "big-job-1"))

    def test_the_workers_are_looked_at_once_an_hour_at_most(self):
        self.sent("aaa-1")
        self.sent("bbb-2")
        self.sweep()                                                              # alive: not lost, but a look was taken
        n = len(self.looks)
        self.assertGreater(n, 0)
        self.sweep(now=time.time() + 60)
        self.assertEqual(len(self.looks), n)
        (self.main.home / "views.json").unlink(missing_ok=True)                      # the 5-second shared look is real-clock; an hour later it would be gone
        self.sweep(now=time.time() + 3700)
        self.assertGreater(len(self.looks), n)

    def test_copies_are_private_and_a_copy_without_a_placement_is_removed(self):
        self.sent("aaa-1")
        f = self.main.home / "sent" / "aaa-1.json"
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        health.keep(self.main.home, "orphan-9", {"x": 1})
        self.sweep()
        self.assertTrue(f.exists())
        self.assertIsNone(health.kept(self.main.home, "orphan-9"))

    def test_the_new_id_never_clashes_with_an_earlier_one(self):
        self.quiet("vmx")
        self.sent("scan-1")
        queue.add(self.main.home, "scan-1-again", "sys-status", "normal", {}, "")
        (_, _, _, again, _), = self.sweep()
        self.assertEqual(again, "scan-1-again2")

    def test_the_setting_moves_the_limit(self):
        self.sent("aaa-1", age=2 * 3600)
        self.quiet("vmx")
        self.main.silent_hours = 1
        self.assertEqual(len(self.sweep()), 1)


class Kept(Setup):
    """The copy is made when the job is SENT, over any connection, and only for a class marked repeatable."""

    def setUp(self):
        super().setUp()
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        self.main = HCtx("mini", self.tmp / "home")
        self.main.repos = None

    def mark(self, repeatable):
        for n in self.repos:
            p = self.tmp / f"r-{n}" / "registry.json"
            reg = json.loads(p.read_text())
            reg["classes"]["sys-status"]["repeatable"] = repeatable
            p.write_text(json.dumps(reg))

    def drain(self):
        return queue.drain(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], send=lambda repos, jid, job: None, check=lambda c, n: None)

    def test_a_repeatable_job_sent_from_the_waiting_list_leaves_a_copy_on_every_connection(self):
        self.mark(True)
        for jid in ("aaa-1", "bbb-2", "ccc-3"):
            queue.add(self.main.home, jid, "sys-status", "normal", {"params": {"id": jid}}, "")
        sent = self.drain()
        self.assertEqual(len(sent), 3)
        self.assertEqual({w for _, w in sent}, {"mini", "vmx", "ghx"})                  # direct and github alike
        for jid, _ in sent:
            self.assertEqual(health.kept(self.main.home, jid), {"params": {"id": jid}})

    def test_a_class_not_marked_repeatable_keeps_nothing(self):
        self.mark(False)
        queue.add(self.main.home, "aaa-1", "sys-status", "normal", {"params": {}}, "")
        self.drain()
        self.assertIsNone(health.kept(self.main.home, "aaa-1"))

    def test_a_direct_send_keeps_each_batch_with_its_own_files(self):
        health.keep_sent(self.main.home, {"classes": {"scan": {"repeatable": True}}}, "scan", ["scan-1", "scan-2"], [{"a": "1"}, {"b": "2"}], {"class": "scan"})
        self.assertEqual(health.kept(self.main.home, "scan-2"), {"class": "scan", "files": {"b": "2"}})
        health.keep_sent(self.main.home, {"classes": {"scan": {}}}, "scan", ["no-1"], [None], {"class": "scan"})
        self.assertIsNone(health.kept(self.main.home, "no-1"))
        health.keep_sent(self.main.home, {"classes": {"scan": {"repeatable": True}}}, "scan", ["zzz-1"], [None], {"class": "scan", "files_z": {"data": "x"}})
        self.assertEqual(health.kept(self.main.home, "zzz-1"), {"class": "scan", "files_z": {"data": "x"}})        # a compressed set is kept as sent

    def test_the_flag_comes_from_the_workers_published_registry(self):
        self.assertFalse(health.is_repeatable({"classes": {"x": {}}}, "x"))
        self.assertTrue(health.is_repeatable({"classes": {"x": {"repeatable": True}}}, "x"))
        self.assertFalse(health.is_repeatable(None, "x"))


class Dispatch(LostBase):
    """The real command line: an arm-run command notices the lost job by itself, and the jobs list shows it once."""

    def run_cmd(self, *argv):
        import re
        from unittest import mock
        main = mock.Mock(repos=self.repos["mini"], product="mini", home=self.main.home, admin="arm-admin", label_of=lambda jid, cls: cls, hints=[])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("armada.client.place.entries", self.ents), mock.patch("armada.client.cli.ClientRepos.from_config", side_effect=lambda c: self.repos[c.identity.name]), \
                mock.patch("armada.client.cli.jobs_of", lambda repos, admin: ({}, [])), redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv), ctx=main, cfg=self.cfgs["mini"])
        return rc, re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue() + err.getvalue())

    def test_a_queue_command_notices_marks_and_says_it(self):
        self.quiet("vmx")
        self.sent("scan-1")
        rc, text = self.run_cmd("queue")
        self.assertEqual(rc, 0, text)
        self.assertIn("A job was lost", text)
        self.assertIn("scan-1-again", text)
        self.assertIn("lost", json.loads((self.main.home / "placements.json").read_text())["scan-1"])

    def test_the_jobs_list_shows_a_lost_job_once_not_twice(self):
        self.quiet("vmx")
        self.sent("scan-1", repeatable=False)
        rc, text = self.run_cmd("jobs")
        self.assertEqual(rc, 0, text)
        self.assertEqual(text.count("A job was lost"), 1, text)
        self.assertIn("not marked safe to repeat", text)
        self.assertEqual(self.run_cmd("jobs")[1].count("A job was lost"), 1)         # and it is still listed next time

    def test_a_bad_id_chosen_by_the_person_is_refused_before_it_is_used_as_a_file_name(self):
        from types import SimpleNamespace
        self.assertEqual(cli.given_id(SimpleNamespace(id="my-job-1")), "my-job-1")
        self.assertIsNone(cli.given_id(SimpleNamespace(id=None)))
        with self.assertRaises(Exception) as cm:
            cli.given_id(SimpleNamespace(id="../../etc/x"))
        self.assertIn("job id", str(cm.exception).lower())


class Shown(unittest.TestCase):
    def test_the_lost_card_says_which_job_why_and_what_happened(self):
        def card(again):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                cli.emit_lost("scan-1", "vmx", "sys-status", "vmx has published nothing for 3 days and the job is 2 days old with no result", again)
            return out.getvalue() + err.getvalue()
        again, plain = card("scan-1-again"), card(None)
        for t in (again, plain):
            self.assertIn("A job was lost", t)
            self.assertIn("scan-1", t)
            self.assertIn("published nothing for 3 days", t)
        self.assertIn("queued again as scan-1-again", again)
        self.assertIn("not marked safe to repeat", plain)

    def test_admin_workers_shows_silent_with_the_age(self):
        doc = {"updated": OLD, "hardware": {"cores": 2}, "isolation": {"ready": True}}
        r = wk.row_for("vmx", doc)
        self.assertTrue(r["silent"])
        self.assertIn("SILENT", r["age"])
        self.assertIn("(SILENT)", wk.cards([r], color=False)[0] if isinstance(wk.cards([r], color=False), list) else wk.cards([r], color=False))
        self.assertFalse(wk.row_for("vmx", {**doc, "updated": "2999-01-01T00:00:00Z"})["silent"])
        self.assertFalse(wk.row_for("vmx", doc, silent_s=10 * 365 * DAY)["silent"])             # the setting


if __name__ == "__main__":
    unittest.main()
