"""A job that cannot be shown is never noted as viewed (found by the output audit: `arm-admin review no-such-job` printed MISSING, then "1 job noted as viewed" and offered `approve --all`)."""
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import admin_support  # noqa: E402,F401
from armada import cli as worker_cli  # noqa: E402
from armada.admin import cli as admin_cli  # noqa: E402
from armada.client.repo import ClientError  # noqa: E402


class MissingJobs(unittest.TestCase):
    def test_the_worker_says_failed_when_a_job_is_not_in_its_copy(self):
        ctx = SimpleNamespace(product="armada", w=SimpleNamespace(io=mock.Mock(job_bytes=mock.Mock(return_value=None))))
        with redirect_stdout(io.StringIO()) as out:
            rc = worker_cli.cmd_review(ctx, ["no-such-job"])
        self.assertEqual(rc, 1)
        self.assertIn("has no copy", out.getvalue())

    def test_the_client_does_not_mark_a_missing_job_as_viewed_and_says_so(self):
        viewed = mock.Mock()
        ctx = SimpleNamespace(repos=mock.Mock(read_result=mock.Mock(return_value={})), viewed=viewed, name="armada", store="the mailbox",
                              remote=mock.Mock(return_value=1))
        with mock.patch.object(admin_cli.jobs, "job_bytes", return_value=None), redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
            with self.assertRaises(ClientError):
                admin_cli.cmd_review(ctx, SimpleNamespace(ids=["no-such-job"]))
        viewed.mark.assert_not_called()
        self.assertNotIn("noted as viewed", err.getvalue())

    def test_a_job_the_worker_showed_is_marked_and_one_it_could_not_is_not(self):
        viewed = mock.Mock()
        ctx = SimpleNamespace(repos=mock.Mock(read_result=mock.Mock(return_value={})), viewed=viewed, name="x", store="the mailbox",
                              remote=mock.Mock(side_effect=lambda cmd, jid: 1 if jid == "gone-2" else 0))
        with mock.patch.object(admin_cli.jobs, "job_bytes", return_value=None), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            with self.assertRaises(ClientError):
                admin_cli.cmd_review(ctx, SimpleNamespace(ids=["gone-1", "gone-2"]))
        self.assertEqual([c.args[0] for c in viewed.mark.call_args_list], ["gone-1"])

    def test_one_missing_job_is_said_in_the_singular(self):
        ctx = SimpleNamespace(repos=mock.Mock(read_result=mock.Mock(return_value={})), viewed=mock.Mock(), name="x", store="the mailbox", remote=mock.Mock(return_value=1))
        with mock.patch.object(admin_cli.jobs, "job_bytes", return_value=None), redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
            with self.assertRaises(ClientError):
                admin_cli.cmd_review(ctx, SimpleNamespace(ids=["no-such-job"]))
        self.assertIn("1 job is no longer on the mailbox", " ".join(err.getvalue().split()))


if __name__ == "__main__":
    unittest.main()
