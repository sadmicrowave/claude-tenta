import json
import unittest

import support  # noqa: F401
from test_setup import FakeHost, cfg_of
from cw.setup import tripwire
from cw.setup.host import Result


class Tripwire(unittest.TestCase):
    def setUp(self):
        self.h, self.cfg = FakeHost(), cfg_of()
        self.h.replies["systemctl is-active auditd"] = Result(0, "active\n")
        self.h.put(f"{self.cfg.path('run_dir')}/canary-watch.json", json.dumps({"monitoring": True}))
        self.h.put(f"{self.cfg.path('state')}/canary.json", json.dumps({"files": ["/srv/cwworker/.aws/credentials", "/srv/cwworker/.git-credentials"]}))
        self.said, self.slept = [], 0
        self.lock = f"{self.cfg.path('state')}/lockdown"

    def go(self, lock_after=None, confirm=lambda p: True, **kw):
        """lock_after: the number of waits before the (simulated) lockdown appears; None means it never does."""
        def sleep(s):
            self.slept += 1
            if lock_after is not None and self.slept >= lock_after:
                self.h.put(self.lock, "{}")
        real = self.h.run

        def run(argv, **k):
            if "unlock" in argv:
                self.h.remove(self.lock)
                return Result(0, "")
            return real(argv, **k)
        self.h.run = run
        ticks = iter(range(0, 10_000, 5))
        return tripwire.run(self.h, self.cfg, say=self.said.append, confirm=confirm, sleep=sleep, clock=lambda: next(ticks), **kw)

    def test_a_decoy_read_that_locks_the_worker_passes_and_the_worker_is_unlocked_again(self):
        ok, why = self.go(lock_after=2)
        self.assertTrue(ok, why)
        self.assertTrue(self.h.ran("cat /srv/cwworker/.aws/credentials"))
        self.assertFalse(self.h.exists(self.lock))                                     # left as it was found

    def test_a_worker_that_never_locks_fails_with_what_to_check(self):
        ok, why = self.go(lock_after=None, wait_s=30)
        self.assertFalse(ok)
        self.assertIn("did not lock down", why)

    def test_a_machine_with_no_kernel_audit_is_told_so_before_anything_is_read(self):
        self.h.replies["systemctl is-active auditd"] = Result(3, "inactive\n")
        ok, why = self.go(lock_after=1)
        self.assertFalse(ok)
        self.assertIn("auditd is not running", why)
        self.assertFalse(self.h.ran("cat /srv"))

    def test_it_refuses_when_the_watcher_is_not_watching_or_the_worker_is_already_locked(self):
        self.h.put(f"{self.cfg.path('run_dir')}/canary-watch.json", json.dumps({"monitoring": False, "reason": "auditd not installed"}))
        self.assertIn("not monitoring", self.go()[1])
        self.h.put(f"{self.cfg.path('run_dir')}/canary-watch.json", json.dumps({"monitoring": True}))
        self.h.put(self.lock, "{}")
        self.assertIn("already locked", self.go()[1])

    def test_answering_no_reads_nothing(self):
        ok, why = self.go(lock_after=1, confirm=lambda p: False)
        self.assertFalse(ok)
        self.assertFalse(self.h.ran("cat /srv"))


if __name__ == "__main__":
    unittest.main()
