"""Retries (R3): a job of a class that says `retries: N` and is repeatable is run again when it ended killed, up to N more times, after a delay; nothing else is retried; the card says which attempt."""
import json
import time
import unittest

import support  # noqa: F401
from armada import registry
from armada.client import health, queue, retry, view
from test_health import LostBase

NOW = 1_800_000_000
INFO = {"classes": {"sys-status": {"repeatable": True, "retries": 2, "retry_delay_s": 120}}}


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Settings(unittest.TestCase):
    def test_default_is_no_retries_and_a_class_that_is_not_repeatable_never_retries(self):
        self.assertEqual(retry.settings({"classes": {"a": {"repeatable": True}}}, "a")[0], 0)
        self.assertEqual(retry.settings({"classes": {"a": {"retries": 3}}}, "a")[0], 0)
        self.assertEqual(retry.settings(INFO, "sys-status"), (2, 120))
        self.assertEqual(retry.settings({"classes": {"a": {"repeatable": True, "retries": 9}}}, "a"), (retry.MAX_RETRIES, retry.DEFAULT_DELAY_S))

    def test_only_a_stop_from_outside_is_retried(self):
        self.assertEqual(retry.RETRY_STATUSES, ("killed",))             # error: the person decides; timeout and limit: would only happen again


class Registry(unittest.TestCase):
    def check(self, **fields):
        reg = {"classes": {"scan": {"kind": "command", "run": ["echo"], **fields}}}
        return [e for e in registry.validate(reg, "/nonexistent-tools-root", allow_empty=True) if "retr" in e]

    def test_retries_need_repeatable_and_a_range(self):
        self.assertEqual(self.check(), [])
        self.assertEqual(self.check(repeatable=True, retries=2, retry_delay_s=30), [])
        self.assertIn("repeatable: true", self.check(retries=2)[0])
        self.assertTrue(self.check(repeatable=True, retries=6))
        self.assertTrue(self.check(repeatable=True, retries=True))
        self.assertTrue(self.check(repeatable=True, retries=1, retry_delay_s=0))


class Sweep(LostBase):
    def planned(self, jid="scan-1", status="killed", finished=None, attempt=1, of=3, copy=True):
        self.sent(jid, age=100, repeatable=copy)
        retry._update(self.main.home, lambda d: d.update({jid: {"attempt": attempt, "of": of, "delay": 120}}))
        d = self.tmp / "r-vmx" / "results"
        d.mkdir(exist_ok=True)
        (d / f"{jid}.json").write_text(json.dumps({"status": status, **({"finished": iso(finished)} if finished else {})}))

    def go(self, now=NOW):
        return retry.sweep(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], now=now)

    def test_a_killed_job_is_queued_again_as_attempt_two_of_three(self):
        self.planned(finished=NOW - 500)
        (jid, new, attempt, of, status, why), = self.go()
        self.assertEqual((jid, new, attempt, of, status), ("scan-1", "scan-1-try2", 2, 3, "killed"))
        q = queue.find(self.main.home, new)
        self.assertEqual(queue.job_of(self.main.home, q), {"class": "sys-status", "params": {"p": 1}})
        self.assertIn("attempt 2 of 3", q["why"])
        self.assertEqual(retry.attempt_of(self.main.home, new), (2, 3))
        self.assertIsNone(health.kept(self.main.home, "scan-1"))                    # the copy moved to the waiting list
        self.assertEqual(self.go(), [])                                              # once, not at every command

    def test_the_delay_is_waited_for_and_the_copy_kept_meanwhile(self):
        self.planned(finished=NOW - 10)
        self.assertEqual(self.go(), [])
        self.assertEqual(queue.waiting(self.main.home), [])
        health.forget_final(self.main.home, {"scan-1"})                              # the jobs list saw the result: the copy must stay for the retry
        self.assertIsNotNone(health.kept(self.main.home, "scan-1"))
        self.assertEqual(len(self.go(now=NOW + 200)), 1)

    def test_an_error_a_timeout_and_a_clean_result_are_never_retried(self):
        for i, status in enumerate(("error", "timeout", "limit", "ok", "done")):
            self.planned(f"job-{i}", status=status)
        self.assertEqual(self.go(), [])
        self.assertEqual(queue.waiting(self.main.home), [])
        self.assertEqual(retry.pending(self.main.home), set())                       # closed: the copies can go
        health.forget_final(self.main.home, {"job-0"})
        self.assertIsNone(health.kept(self.main.home, "job-0"))

    def test_the_last_attempt_is_not_retried_and_says_so(self):
        self.planned("scan-3", attempt=3, of=3)
        (jid, new, attempt, of, status, why), = self.go()
        self.assertIsNone(new)
        self.assertIn("all 3 attempts", why)
        self.assertEqual(queue.waiting(self.main.home), [])

    def test_a_missing_copy_is_said_not_silently_dropped(self):
        self.planned(copy=False)
        (jid, new, attempt, of, status, why), = self.go()
        self.assertIsNone(new)
        self.assertIn("no copy", why)

    def test_a_job_with_no_result_yet_waits(self):
        self.planned()
        (self.tmp / "r-vmx" / "results" / "scan-1.json").write_text(json.dumps({"status": "running"}))
        self.assertEqual(self.go(), [])
        self.assertEqual(retry.pending(self.main.home), {"scan-1"})

    def test_a_job_lost_and_sent_again_keeps_its_attempt(self):
        self.planned("scan-1", status="running", attempt=2)
        retry.carry(self.main.home, "scan-1", "scan-1-again")
        self.assertEqual(retry.attempt_of(self.main.home, "scan-1-again"), (2, 3))
        self.assertEqual(retry.pending(self.main.home), {"scan-1-again"})

    def test_a_plan_without_a_placement_is_forgotten(self):
        retry._update(self.main.home, lambda d: d.update({"old-1": {"attempt": 1, "of": 2, "delay": 60}}))
        self.go()
        self.assertEqual(retry.pending(self.main.home), set())
        self.assertIsNone(retry.attempt_of(self.main.home, "old-1"))

    def test_a_retry_that_ends_killed_again_is_tried_again_until_the_attempts_are_used(self):
        self.planned(finished=NOW - 500)
        self.go()
        self.sent("scan-1-try2", age=50)                                             # the waiting list sent it
        (self.tmp / "r-vmx" / "results" / "scan-1-try2.json").write_text(json.dumps({"status": "killed", "finished": iso(NOW - 400)}))
        (jid, new, attempt, of, status, why), = self.go()
        self.assertEqual((new, attempt), ("scan-1-try2-try3", 3))


class Plan(LostBase):
    def test_a_plan_is_made_at_send_only_for_a_repeatable_class_with_retries(self):
        retry.plan_sent(self.main.home, INFO, "sys-status", ["aaa-1", "aaa-2"])
        self.assertEqual(retry.attempt_of(self.main.home, "aaa-2"), (1, 3))
        retry.plan_sent(self.main.home, {"classes": {"sys-status": {"repeatable": True}}}, "sys-status", ["bbb-1"])
        self.assertIsNone(retry.attempt_of(self.main.home, "bbb-1"))
        retry.plan_sent(self.main.home, INFO, "sys-status", ["aaa-1"])                    # a second send of the same id does not reset its attempt
        retry._update(self.main.home, lambda d: d["aaa-1"].update(attempt=2))
        retry.plan_sent(self.main.home, INFO, "sys-status", ["aaa-1"])
        self.assertEqual(retry.attempt_of(self.main.home, "aaa-1"), (2, 3))
        self.assertEqual(oct((self.main.home / "retries.json").stat().st_mode & 0o777), "0o600")


class Card(unittest.TestCase):
    RES = {"id": "scan-1-try2", "status": "killed", "output": "x"}

    def test_the_result_card_and_the_plain_text_say_attempt_two_of_three(self):
        card = view.result_card(self.RES, attempt=(2, 3))
        self.assertIn(("Attempt", "2 of 3"), card["head"]["details"])
        self.assertIn("2 of 3", "\n".join(view.format_result(self.RES, attempt=(2, 3))[0]))
        self.assertIn("the last", view.attempt_text((3, 3)))
        self.assertNotIn("Attempt", [r[0] for r in view.result_card(self.RES)["head"]["details"] if isinstance(r, tuple)])


if __name__ == "__main__":
    unittest.main()
