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


KINDS = {"sys-status": {"kind": "command", "cost": "light"}, "claude-read": {"kind": "claude", "cost": "medium"}}


def w(name, connection="direct", age=30, pickup=5, **kw):
    s = state(**kw)
    reg = {"classes": {c: KINDS.get(c, {"kind": "command", "cost": "light"}) for c in s["classes"]}}        # the registry a worker publishes names exactly the classes it offers
    return worker_view(name, connection, s, reg, age, pickup)


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
        c = pick([w("old", age=3600), w("fresh", running=2)])
        self.assertEqual(c.worker, "fresh")
        c = pick([w("old", age=3600)])
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
