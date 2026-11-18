"""Checks that are ABOUT one worker (the install check after `ct-admin apply`, the wizard's first job) must run on that worker, never on whichever the scheduler prefers.

Found in the first real `ct-admin apply` on two workers: the Mac mini was upgraded, its check printed "a test job ran and came back: ok", and the job had run on the other worker."""
import unittest
from unittest import mock

import support  # noqa: F401
from ct.admin import cli as admin_cli
from ct.client import cli as client_cli


class TheInstallCheck(unittest.TestCase):
    def test_the_self_test_job_is_pinned_to_the_worker_being_checked(self):
        ctx = mock.Mock()
        ctx.repos.registry_info.return_value = {"classes": {admin_cli.release.SELFTEST_CLASS: {}}}
        ctx.cfg = object()
        with mock.patch.object(client_cli, "main", return_value=0) as run, mock.patch.object(client_cli.Context, "from_config", return_value="that-worker-ctx"):
            ok, _ = admin_cli.selftest(ctx)
        self.assertTrue(ok)
        argv, kw = run.call_args[0][0], run.call_args[1]
        self.assertIn("--here", argv)
        self.assertEqual((kw["ctx"], kw["cfg"]), ("that-worker-ctx", ctx.cfg))              # this worker's own context, not the main one's

    def test_without_a_configuration_it_is_still_pinned(self):
        ctx = mock.Mock(spec=["repos", "cfg"])
        ctx.cfg = None
        ctx.repos.registry_info.return_value = {"classes": {admin_cli.release.SELFTEST_CLASS: {}}}
        with mock.patch.object(client_cli, "main", return_value=0) as run:
            admin_cli.selftest(ctx)
        self.assertIn("--here", run.call_args[0][0])


class HereMeansNoChoosing(unittest.TestCase):
    def test_submit_here_never_reads_the_other_workers(self):
        ctx = object()
        a = mock.Mock(here=True, worker=None)
        a.cls = "sys-status"
        with mock.patch("ct.client.place.route", side_effect=AssertionError("must not choose")):
            self.assertIs(client_cli.route_submit(ctx, object(), a), ctx)


if __name__ == "__main__":
    unittest.main()
