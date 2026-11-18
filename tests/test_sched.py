"""Choosing a worker: every rule, and that the answer never depends on the order the workers are listed in."""
import itertools
import unittest

import support  # noqa: F401
from ct import sched, workerstate
from ct.sched import Prefs, worker_view


def state(*, running=0, waiting=0, capacity=4, cores=4, load=0.2, locked=False, jail=True, claude="account", paused=False, classes=("sys-status", "claude-read")):
    return {"v": 1, "updated": "2026-11-18T16:00:00Z", "name": "w", "hardware": {"cores": cores, "capacity": capacity},
            "isolation": {"ready": jail}, "locked": locked, "classes": list(classes), "claude": {"auth": claude, "tool": {"kind": "native", "version": "2"}},
            "claude_paused": paused, "observed": {"running": running, "waiting": waiting, "load_1m": load}}


KINDS = {"sys-status": {"kind": "command", "cost": "light"}, "claude-read": {"kind": "claude", "cost": "medium"}, "profile-run": {"kind": "profile", "cost": "heavy"}}


def w(name, connection="direct", age=30, pickup=5, sent=0, **kw):
    s = state(**kw)
    reg = {"classes": {c: KINDS.get(c, {"kind": "command", "cost": "light"}) for c in s["classes"]}}        # the registry a worker publishes names exactly the classes it offers
    return worker_view(name, connection, s, reg, age, pickup, sent_pending=sent)


def pick(workers, cls="sys-status", kind="command", prefs=None):
    return sched.choose(cls, kind, workers, prefs)


class Basics(unittest.TestCase):
    def test_the_worker_with_the_most_free_capacity_is_chosen_and_every_worker_gets_a_reason(self):
        c = pick([w("a", running=3), w("b", running=1), w("c", running=2)])
        self.assertEqual(c.worker, "b")
        self.assertEqual([n for n, _, _ in c.lines], ["a", "b", "c"])
        self.assertEqual(dict((n, v) for n, v, _ in c.lines), {"a": "not chosen", "b": "CHOSEN", "c": "not chosen"})
        self.assertIn("more capacity free", dict((n, r) for n, _, r in c.lines)["a"])

    def test_the_answer_never_depends_on_the_order_the_workers_are_listed_in(self):
        ws = [w("a", running=1), w("b", running=1), w("c", running=1, pickup=90), w("d", running=2)]
        answers = {pick(list(p)).worker for p in itertools.permutations(ws)}
        self.assertEqual(answers, {"a"})                                   # equal capacity and pick-up: the name decides, never the order

    def test_a_shorter_wait_for_the_worker_to_notice_beats_a_slower_one_at_equal_capacity(self):
        self.assertEqual(pick([w("slow", pickup=90), w("fast", pickup=5)]).worker, "fast")        # a GitHub worker polls; a Direct one answers in seconds

    def test_a_lighter_load_breaks_the_tie_after_that(self):
        self.assertEqual(pick([w("busy", load=3.5), w("calm", load=0.1)]).worker, "calm")

    def test_all_busy_still_picks_the_least_busy_and_says_it_will_queue(self):
        c = pick([w("a", running=4, waiting=2), w("b", running=4, waiting=0)])
        self.assertEqual((c.worker, c.busy_all), ("b", True))


class LeftOut(unittest.TestCase):
    def reason(self, c, name):
        return dict((n, r) for n, _, r in c.lines)[name]

    def test_an_unreachable_worker_is_left_out_with_what_was_seen(self):
        c = pick([worker_view("gone", "direct", state(), None, 10, 5, reachable=False, why_not="timed out"), w("ok")])
        self.assertEqual(c.worker, "ok")
        self.assertIn("timed out", self.reason(c, "gone"))

    def test_locked_isolation_off_no_state_and_a_class_it_does_not_offer(self):
        c = pick([w("locked", locked=True), w("nojail", jail=False), worker_view("blank", "direct", None, None, None, 5), w("nocls", classes=("other",)), w("ok")])
        self.assertEqual(c.worker, "ok")
        for name, word in (("locked", "LOCKED"), ("nojail", "isolation OFF"), ("blank", "no state"), ("nocls", "does not offer")):
            self.assertIn(word, self.reason(c, name))

    def test_nobody_eligible_says_why_for_each_and_chooses_none(self):
        c = pick([w("a", locked=True), w("b", jail=False)])
        self.assertIsNone(c.worker)
        self.assertEqual(len(c.lines), 2)

    def test_a_claude_job_needs_sign_in_a_tool_and_budget(self):
        c = pick([w("none", claude="none"), w("paused", paused=True), w("ok")], "claude-read", "claude")
        self.assertEqual(c.worker, "ok")
        self.assertIn("no Claude sign-in", self.reason(c, "none"))
        self.assertIn("paused", self.reason(c, "paused"))
        self.assertEqual(pick([w("none", claude="none")], "sys-status", "command").worker, "none")      # a plain command never needs Claude


class Preferences(unittest.TestCase):
    def test_a_dedicated_worker_takes_only_its_classes_and_is_left_out_of_everything_else(self):
        prefs = Prefs(dedicated={"lab": ["claude-read"]})
        self.assertEqual(pick([w("lab"), w("gen", running=3)], "sys-status", "command", prefs).worker, "gen")
        self.assertEqual(pick([w("lab", running=3), w("gen")], "claude-read", "claude", prefs).worker, "gen")           # freer, and the reserved worker is allowed this class
        self.assertEqual(pick([w("lab"), w("gen", running=3)], "claude-read", "claude", prefs).worker, "lab")
        c = pick([w("lab"), w("gen", running=3)], "sys-status", "command", prefs)
        self.assertIn("reserved for claude-read", dict((n, r) for n, _, r in c.lines)["lab"])

    def test_a_preferred_worker_wins_over_a_freer_one_but_not_over_a_locked_one(self):
        prefs = Prefs(preferred={"sys-status": ["slow", "fast"]})
        self.assertEqual(pick([w("slow", running=3), w("fast")], prefs=prefs).worker, "slow")
        self.assertEqual(pick([w("slow", locked=True), w("fast")], prefs=prefs).worker, "fast")

    def test_a_class_limited_to_one_connection_ignores_workers_reached_another_way(self):
        prefs = Prefs(connections={"sys-status": ["direct"]})
        c = pick([w("gh", connection="github"), w("d", running=3)], prefs=prefs)
        self.assertEqual(c.worker, "d")
        self.assertIn("limited to direct", dict((n, r) for n, _, r in c.lines)["gh"])


class Stale(unittest.TestCase):
    def test_an_old_reading_is_used_but_ranked_after_a_fresh_one_and_says_so(self):
        c = pick([w("old", age=9000), w("fresh", running=2)])
        self.assertEqual(c.worker, "fresh")
        c = pick([w("old", age=9000)])
        self.assertEqual(c.worker, "old")
        self.assertIn("min old", dict((n, r) for n, _, r in c.lines)["old"])


class TheRealPublisher(unittest.TestCase):
    """The state this module reads is built by the worker's own code; one test runs that real builder (a fake is only trusted while one test runs the real thing)."""

    def test_a_document_built_by_the_real_worker_state_builder_is_understood(self):
        class Jail:
            name = "bwrap"

            def ready(self):
                return True
        snap = {"cores": 2, "mem_total_mb": 2924, "load_1m": 0.3, "temp_c": None, "mem_available_mb": 2400, "disk_free_gb": 4.0, "running": 1, "waiting": 2, "at": "2026-11-18T16:00:00Z"}
        registry = {"classes": {"sys-status": {"kind": "command", "cost": "light"}}}
        _env, doc = workerstate.build(snapshot=snap, registry=registry, extra={"name": "w", "capacity": 4, "claude": {"auth": "none"}, "approvers": []}, jail=Jail(), locked=False, now_iso="2026-11-18T16:00:00Z")
        v = worker_view("w", "direct", doc, registry, 30, 5)
        self.assertEqual((v.capacity, v.busy, v.headroom), (4, 3, 1))
        self.assertEqual(pick([v]).worker, "w")


if __name__ == "__main__":
    unittest.main()


class TheWorkerKnowsBest(unittest.TestCase):
    """Found on the real fleet: this computer's configuration said Direct (5 s) for a worker that really listens on GitHub and looks every 300 s when idle."""

    def listening(self, name, transport, wait, believed="direct", **kw):
        s = state(**kw)
        s["transport"], s["pickup_max_s"] = transport, wait
        return worker_view(name, believed, s, {"classes": {"sys-status": KINDS["sys-status"]}}, 30, 5)

    def test_the_wait_the_worker_publishes_beats_what_this_computer_assumes_and_decides_between_equals(self):
        mini = self.listening("mini", "github", 300)                         # believed to be Direct here: 5 s assumed, 300 s real
        vmx = self.listening("vmx", "direct", 5)
        self.assertEqual((mini.wait_s, vmx.wait_s), (300, 5))
        c = pick([mini, vmx])
        self.assertEqual(c.worker, "vmx")
        self.assertIn("notices a job sooner (5 s against 300 s)", dict((n, r) for n, _, r in c.lines)["mini"])

    def test_a_worker_whose_real_connection_differs_from_the_setting_here_is_flagged_in_the_explanation(self):
        mini = self.listening("mini", "github", 300)
        c = pick([mini])
        self.assertIn("listens on github", dict((n, r) for n, _, r in c.lines)["mini"])
        self.assertEqual(self.listening("ok", "direct", 5).mismatch, "")

    def test_an_older_worker_that_publishes_neither_falls_back_to_what_this_computer_knows(self):
        w_old = worker_view("old", "github", state(), {"classes": {"sys-status": KINDS["sys-status"]}}, 30, 90)
        self.assertEqual((w_old.wait_s, w_old.listens_on, w_old.mismatch), (90, "github", ""))

    def test_a_class_limited_to_a_connection_goes_by_what_the_worker_listens_on(self):
        prefs = Prefs(connections={"sys-status": ["direct"]})
        c = pick([self.listening("mini", "github", 300)], prefs=prefs)
        self.assertIsNone(c.worker)

    def test_the_real_state_builder_publishes_them(self):
        class Jail:
            name = "bwrap"

            def ready(self):
                return True
        _e, doc = workerstate.build(snapshot={"cores": 2, "at": "x"}, registry={"classes": {}}, extra={"name": "w", "transport": "github", "pickup_max_s": 300, "claude": {"auth": "none"}}, jail=Jail(), locked=False, now_iso="x")
        self.assertEqual((doc["transport"], doc["pickup_max_s"]), ("github", 300))


class TheConfigurationAndTheCopiesDisagree(unittest.TestCase):
    def test_it_is_said_in_the_explanation(self):
        v = worker_view("mini", "github", state(), {"classes": {"sys-status": KINDS["sys-status"]}}, 30, 90, configured="direct")
        self.assertIn("configuration says direct, but its copies of the queue and results use github", v.mismatch)
        self.assertIn("WARNING", dict((n, r) for n, _, r in pick([v]).lines)["mini"])


class LoadIsWhatThisComputerSent(unittest.TestCase):
    """Found by the load test: the worker's own running/waiting counts are published about every half hour, so a burst saw every worker as idle and sent everything to the same one."""

    def test_an_old_count_from_the_worker_says_nothing_about_now(self):
        busy_but_old = w("old", age=1000, running=4)
        self.assertEqual(busy_but_old.busy, 0)
        self.assertEqual(w("fresh", age=30, running=3).busy, 3)

    def test_jobs_this_computer_sent_and_that_have_not_finished_count_as_load(self):
        self.assertEqual(w("a", age=1000, sent=5).busy, 5)
        c = pick([w("a", age=1000, sent=4), w("b", age=1000, sent=0)])
        self.assertEqual(c.worker, "b")

    def test_a_burst_spreads_across_workers_as_each_placement_is_counted(self):
        sent, placed = {"a": 0, "b": 0}, []
        for _ in range(8):
            c = pick([w("a", age=1000, sent=sent["a"], pickup=5), w("b", age=1000, sent=sent["b"], pickup=5)])
            sent[c.worker] += 1
            placed.append(c.worker)
        self.assertEqual((placed.count("a"), placed.count("b")), (4, 4))

    def test_free_capacity_comes_before_a_shorter_wait_so_a_burst_alternates_between_equals(self):
        sent, placed = {"fast": 0, "slow": 0}, []
        for _ in range(6):
            c = pick([w("fast", age=1000, sent=sent["fast"], pickup=5), w("slow", age=1000, sent=sent["slow"], pickup=300)])
            sent[c.worker] += 1
            placed.append(c.worker)
        self.assertEqual(placed, ["fast", "slow", "fast", "slow", "fast", "slow"])


class CostAware(unittest.TestCase):
    """A heavy job takes the whole of a capacity-4 worker; a light one takes a quarter. Counting jobs, not capacity points, sent a heavy job to a worker that could only queue it."""

    def test_a_heavy_job_goes_to_the_worker_it_fits_on_now_not_just_the_one_with_fewer_jobs(self):
        classes = ("sys-status", "profile-run")
        a = w("a", age=1000, sent=1, classes=classes)                       # one light job: 3 of 4 points free: a heavy job (4) would have to wait
        b = w("b", age=1000, sent=0, classes=classes, pickup=300)           # idle, and slower to notice: but the heavy job fits at once
        c = pick([a, b], "profile-run", "profile")
        self.assertEqual(c.worker, "b")
        self.assertIn("would have to wait", dict((n, r) for n, _, r in c.lines)["a"])

    def test_a_light_job_still_takes_the_freest_worker(self):
        a = w("a", age=1000, sent=1)
        b = w("b", age=1000, sent=0)
        self.assertEqual(pick([a, b]).worker, "b")

    def test_when_the_job_fits_nowhere_it_queues_on_the_worker_with_the_most_room_and_says_so(self):
        classes = ("sys-status", "profile-run")
        c = pick([w("a", age=1000, sent=3, classes=classes), w("b", age=1000, sent=2, classes=classes)], "profile-run", "profile")
        self.assertEqual((c.worker, c.busy_all), ("b", True))

    def test_a_job_that_fits_now_is_not_called_busy(self):
        c = pick([w("a", age=1000, sent=2)])
        self.assertFalse(c.busy_all)

    def test_a_medium_job_needs_two_points(self):
        a = w("a", age=1000, sent=3)                                         # 1 point free
        b = w("b", age=1000, sent=2)                                         # 2 free
        self.assertEqual(pick([a, b], "claude-read", "claude").worker, "b")

    def test_the_cost_of_a_class_is_what_each_workers_own_registry_says(self):
        heavy_here = worker_view("h", "direct", state(classes=("profile-run",)), {"classes": {"profile-run": {"kind": "profile", "cost": "heavy"}}}, 30, 5)
        light_there = worker_view("l", "direct", state(classes=("profile-run",)), {"classes": {"profile-run": {"kind": "profile", "cost": "light"}}}, 30, 5)
        self.assertEqual((heavy_here.cost_of("profile-run"), light_there.cost_of("profile-run")), (4, 1))


class TheWorkersOwnConfigurationDisagrees(unittest.TestCase):
    def test_it_is_said_with_what_to_run(self):
        s = state()
        s["transport"], s["transport_configured"], s["pickup_max_s"] = "github", "direct", 300
        v = worker_view("mini", "github", s, {"classes": {"sys-status": KINDS["sys-status"]}}, 30, 90)
        self.assertIn("its own configuration says direct, but it fetches over github", v.mismatch)
        self.assertIn("ct-setup install", v.mismatch)
        self.assertIn("WARNING", dict((n, r) for n, _, r in pick([v]).lines)["mini"])

    def test_a_worker_whose_configuration_agrees_says_nothing(self):
        s = state()
        s["transport"], s["pickup_max_s"] = "direct", 5
        self.assertEqual(worker_view("vmx", "direct", s, {"classes": {"sys-status": KINDS["sys-status"]}}, 30, 5).mismatch, "")
