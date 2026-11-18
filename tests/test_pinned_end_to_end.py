"""The pinned check, through the real `ct-run submit` and real git queues of two workers, with the scheduler really choosing: the route a `ct-admin apply` check takes.

Unpinned, a job goes to the freer worker (what the scheduler is for). With --here it goes to the worker being checked, even when the other one is freer (what a check about one worker needs).
Found in the first real `ct-admin apply` on two workers, where the check on one worker ran on the other."""
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from support import RepoFixture
from test_place import cfg_file

from ct.client import cli, place


def worker_fixture(testcase, name, running):
    fx = type("Fx", (RepoFixture,), {"test_x": lambda s: None})("test_x")
    fx.setUp()
    testcase.addCleanup(fx.doCleanups)
    (fx.repos.results / "worker-state.json").write_text(json.dumps({"updated": "2999-01-01T00:00:00Z", "name": name, "hardware": {"cores": 4, "capacity": 4}, "isolation": {"ready": True},
                                                                     "locked": False, "classes": ["sys-status"], "observed": {"running": running, "waiting": 0, "load_1m": 0.1}}))
    (fx.repos.results / "registry.json").write_text(json.dumps({"classes": {"sys-status": {"kind": "command", "cost": "light", "tier": "auto"}}, "capacity": 4}))
    return fx


class ThePinnedCheckOnRealQueues(unittest.TestCase):
    def setUp(self):
        self.first = worker_fixture(self, "firstw", running=0)                  # the freer one: what the scheduler would choose
        self.second = worker_fixture(self, "secondw", running=3)                # the one being checked
        cfgs = {"secondw": cfg_file(self.first.tmp, "secondw"), "firstw": cfg_file(self.first.tmp, "firstw")}
        by_name = {"firstw": self.first.repos, "secondw": self.second.repos}
        self.patches = [mock.patch("ct.client.place.entries", lambda cfg: [("secondw", cfgs["secondw"], None), ("firstw", cfgs["firstw"], None)]),
                        mock.patch("ct.client.cli.ClientRepos.from_config", side_effect=lambda c: by_name[c.identity.name]),
                        mock.patch("ct.client.cli.Context.from_config", side_effect=lambda c: (self.second if c.identity.name == "secondw" else self.first).ctx)]
        [p.start() for p in self.patches]
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.cfg = cfgs["secondw"]

    def jobs_on(self, fx):
        fx.repos.fetch_queue()
        out = fx.repos.git(fx.repos.queue, "ls-tree", "-r", "--name-only", f"origin/{fx.repos.jobs_branch}", check=False).stdout.split()
        return [Path(n).stem for n in out if n.startswith("jobs/")]

    def submit(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["submit", "sys-status", "--no-wait", *extra], ctx=self.second.ctx, cfg=self.cfg)
        return code, out.getvalue() + err.getvalue()

    def test_an_unpinned_job_goes_to_the_freer_worker(self):
        code, text = self.submit()
        self.assertEqual(code, 0, text)
        self.assertEqual((len(self.jobs_on(self.first)), len(self.jobs_on(self.second))), (1, 0))
        self.assertIn("firstw", text)

    def test_a_job_pinned_with_here_goes_to_the_worker_being_checked_even_though_the_other_is_freer(self):
        code, text = self.submit("--here")
        self.assertEqual(code, 0, text)
        self.assertEqual((len(self.jobs_on(self.first)), len(self.jobs_on(self.second))), (0, 1))
        self.assertNotIn("Sending to", text)                                        # nothing was chosen, so nothing is said about choosing


if __name__ == "__main__":
    unittest.main()
