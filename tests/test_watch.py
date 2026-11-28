import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from armada.admin import watch
from armada.util import iso

NOW = 1_800_000_000.0
FRESH = {"updated": iso(__import__("datetime").datetime.fromtimestamp(NOW - 600, __import__("datetime").timezone.utc))}
LOCKED = {"locked": True, "since": "2026-11-17T10:00:00Z"}


def kinds(events):
    return [k for k, _, _ in events]


class Decide(unittest.TestCase):
    def d(self, sec=None, state=FRESH, ok=True, prev=None, now=NOW):
        return watch.decide("studio", sec or {}, state, ok, prev, now)

    def test_a_quiet_healthy_worker_raises_nothing_and_is_asked_again_at_the_normal_pace(self):
        events, mem = self.d()
        self.assertEqual(events, [])
        self.assertEqual(mem["next_at"], NOW + watch.BASE_S)

    def test_locking_down_alerts_once_not_on_every_look(self):
        events, mem = self.d(LOCKED)
        self.assertEqual(kinds(events), ["locked"])
        events, mem = self.d(LOCKED, prev=mem, now=NOW + 60)
        self.assertEqual(events, [])

    def test_a_reminder_comes_at_the_workers_own_schedule_and_only_once_each(self):
        fresh_lock = {"locked": True, "since": iso(__import__("datetime").datetime.fromtimestamp(NOW - 1800, __import__("datetime").timezone.utc))}
        events, mem = self.d(fresh_lock)
        self.assertEqual(kinds(events), ["locked"])
        events, mem = self.d(fresh_lock, prev=mem, now=NOW + 1000)           # 47 minutes in: not yet an hour
        self.assertEqual(events, [])
        events, mem = self.d(fresh_lock, prev=mem, now=NOW + 1900)           # past the hour
        self.assertEqual(kinds(events), ["reminder"])
        events, _ = self.d(fresh_lock, prev=mem, now=NOW + 4000)
        self.assertEqual(events, [])

    def test_a_lock_first_noticed_long_after_it_began_is_announced_once_not_reminded_at_once(self):
        _, mem = self.d(LOCKED)
        events, _ = self.d(LOCKED, prev=mem, now=NOW + 60)
        self.assertEqual(events, [])

    def test_unlocking_is_announced_and_forgets_the_lock(self):
        _, mem = self.d(LOCKED)
        events, mem = self.d({"locked": False}, prev=mem, now=NOW + 60)
        self.assertEqual(kinds(events), ["unlocked"])
        self.assertIsNone(mem["locked_since"])

    def test_a_worker_that_stops_reporting_is_noticed_once_and_again_when_it_returns(self):
        old = {"updated": "2026-11-17T00:00:00Z"}
        events, mem = self.d(state=old, now=NOW)
        self.assertEqual(kinds(events), ["silent"])
        events, mem = self.d(state=old, prev=mem, now=NOW + 60)
        self.assertEqual(events, [])
        events, mem = self.d(state=FRESH, prev=mem, now=NOW + 120)
        self.assertEqual(kinds(events), ["back"])

    def test_one_dropped_fetch_is_not_news_but_three_in_a_row_are(self):
        mem = None
        for i in range(2):
            events, mem = self.d(ok=False, prev=mem, now=NOW + i * 100)
            self.assertEqual(events, [])
        events, mem = self.d(ok=False, prev=mem, now=NOW + 300)
        self.assertEqual(kinds(events), ["unreachable"])
        events, mem = self.d(ok=False, prev=mem, now=NOW + 900)
        self.assertEqual(events, [])                          # told once
        events, mem = self.d(prev=mem, now=NOW + 2000)
        self.assertEqual(kinds(events), ["recovered"])

    def test_a_failing_worker_is_asked_less_often_each_time_up_to_a_ceiling(self):
        delays = [watch.next_delay(n) for n in range(0, 8)]
        self.assertEqual(delays[0], watch.BASE_S)
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[-1], watch.CEILING_S)

    def test_a_worker_with_no_published_state_yet_is_not_called_silent(self):
        events, _ = self.d(state=None)
        self.assertEqual(events, [])


class Repos:
    def __init__(self, results, fail=False):
        self.results, self.fail, self.pulls = Path(results), fail, 0

    def pull_results(self):
        self.pulls += 1
        if self.fail:
            raise OSError("no route")

    def security(self):
        try:
            return json.loads((self.results / "security.json").read_text())
        except (OSError, ValueError):
            return {}


class Notifier:
    def __init__(self):
        self.sent = []

    def send(self, title, body, **kw):
        self.sent.append(title)


class OnePass(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mem = watch.Memory(self.tmp / "watch.json")
        self.n = Notifier()

    def worker(self, name, **doc):
        d = self.tmp / name
        d.mkdir()
        (d / "security.json").write_text(json.dumps(doc.get("security", {})))
        (d / "worker-state.json").write_text(json.dumps(doc.get("state", FRESH)))
        return name, Repos(d, fail=doc.get("fail", False))

    def test_only_workers_that_are_due_are_asked_and_a_dead_one_does_not_hold_up_the_rest(self):
        a, b = self.worker("a", fail=True), self.worker("b")
        watch.one_pass([a, b], self.mem, self.n, now=NOW)
        self.assertEqual((a[1].pulls, b[1].pulls), (1, 1))
        watch.one_pass([a, b], self.mem, self.n, now=NOW + 5)               # neither is due yet
        self.assertEqual((a[1].pulls, b[1].pulls), (1, 1))
        watch.one_pass([a, b], self.mem, self.n, now=NOW + watch.BASE_S + 1)
        self.assertEqual((a[1].pulls, b[1].pulls), (1, 2))                  # the failing one backed off; the healthy one is still asked every minute

    def test_a_lock_is_alerted_once_and_survives_a_restart_of_the_watcher(self):
        w = self.worker("w", security=LOCKED)
        r = watch.one_pass([w], self.mem, self.n, now=NOW)
        self.assertEqual(r, [("w", "locked")])
        r = watch.one_pass([w], watch.Memory(self.tmp / "watch.json"), self.n, now=NOW + 120)      # a new process reads the same memory
        self.assertEqual(r, [])
        self.assertEqual(len(self.n.sent), 1)


if __name__ == "__main__":
    unittest.main()


class TheHeartbeatAndTheLadder(unittest.TestCase):
    """The monitoring pass remembers where a worker answered and, when it does not, says where it is now (cheap rungs only); nothing in that can break the pass."""

    def pass_with(self, ok, on_ok=None, on_fail=None):
        import tempfile
        sent = []

        class N:
            def send(self, title, text, **k):
                sent.append(text)

        class Repos:
            results = Path(tempfile.mkdtemp())

            def security(self):
                return {}

            def pull_results(self):
                if not ok:
                    raise OSError("down")
        mem = watch.Memory(Path(tempfile.mkdtemp()) / "w.json")
        for _ in range(watch.FAILURES_BEFORE_ALERT if not ok else 1):
            watch.one_pass([("w", Repos())], mem, N(), force=True, on_ok=on_ok, on_fail=on_fail)
        return sent

    def test_a_worker_that_answers_is_remembered(self):
        seen = []
        self.pass_with(True, on_ok=lambda n, r: seen.append(n))
        self.assertEqual(seen, ["w"])

    def test_an_unreachable_alert_says_where_the_worker_is_now(self):
        sent = self.pass_with(False, on_fail=lambda n, r: "It answers at 192.0.2.9.")
        self.assertTrue(any("192.0.2.9" in t for t in sent), sent)

    def test_a_helper_that_fails_never_breaks_the_pass(self):
        def boom(*a):
            raise RuntimeError("x")
        self.assertEqual(self.pass_with(True, on_ok=boom), [])
        self.assertTrue(self.pass_with(False, on_fail=boom))          # the plain alert still goes out
