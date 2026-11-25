import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ct import registry  # noqa: E402
from ct.client import cli  # noqa: E402
from ct.client import offload as off  # noqa: E402
from ct.client.cli import adopt_tier as cli_tier  # noqa: E402
from ct.client.repo import ClientError  # noqa: E402
from offload_support import OffloadFixture  # noqa: E402
from support import sh  # noqa: E402

INFO = {"mem_total_mb": 8000, "classes": {"echo": {"run": ["/bin/echo", "{word}"], "cost": "light", "tier": "auto"},
                                          "lint": {"run": ["/usr/bin/eslint", ".", "--max-warnings", "0"], "cost": "medium", "tier": "approve"}}}

class EndToEnd(OffloadFixture):
    """Part 2 of 2 of the offload end-to-end tests."""

    def test_propose_needs_a_finished_profile_run(self):
        self.assertEqual(self.run_cli("offload", "propose", "profile-nosuch1")[0], 2)
        code, out, err = self.profile()
        jid = out.strip()
        self.w.fetch()
        code, out, err = self.run_cli("offload", "propose", jid)                                     # waiting for approval: no profile yet
        self.assertEqual(code, 2)
        self.assertIn("No profile result yet", out + err)

    def test_missing_details_stop_the_request_before_anything_is_pushed(self):
        code, out, err = self.run_cli("offload", "profile", "--what", "lists", "--", "ls", "-l")
        self.assertEqual(code, 2)
        self.assertIn("This profiling run is missing required details", out + err)
        self.assertIn("--est-time-s", out + err)
        self.assertIn("ct-run offload profile --what lists --expect 'a few seconds, tiny, prints one line' --est-time-s 10 --est-mem-mb 100 -- ls -l", out + err)
        self.repos.fetch_queue()
        self.assertFalse(self.repos.remote_branch_exists("jobs"))

    def test_the_approval_instructions_name_the_configured_admin_command(self):
        code, out, err = self.profile("--name", "my-ls", "--why", "to see")
        self.assertEqual(code, 0)
        jid = out.strip()
        for needle in ("This profiling run needs YOUR approval", "ls -l", jid, "my-ls", f"tct-admin approve {jid}", f"ct-run result {jid}"):
            self.assertIn(needle, err)
        self.assertEqual(self.ctx.names.get(jid), "my-ls")                                          # remembered for the job list and the proposal
        self.assertIn("my-ls", self.run_cli("jobs")[1])

    def test_a_worker_that_starts_profile_runs_without_approval_is_not_waited_on_for_one(self):
        (self.repos.results / "worker-state.json").write_text(json.dumps({"profile_approval": "none"}))
        code, out, err = self.profile("--name", "my-ls")
        self.assertEqual(code, 0, out + err)
        jid = out.strip()
        self.assertNotIn("needs YOUR approval", err)
        self.assertNotIn(f"tct-admin approve {jid}", err)
        self.assertIn("without approval", err)
        self.assertIn(jid, err)
        (self.repos.results / "worker-state.json").write_text(json.dumps({"profile_approval": "required"}))
        code, out, err = self.profile("--name", "my-ls2")
        self.assertIn("needs YOUR approval", err)                                                      # a worker that did not say, or says required, is unchanged

    def test_a_taken_or_bad_name_is_refused_up_front(self):
        self.w.fetch()
        code, out, err = self.profile("--name", "echo")
        self.assertEqual(code, 2)
        self.assertIn("already installed or proposed", out + err)
        self.assertEqual(self.profile("--name", "Bad Name")[0], 2)

    def test_a_spec_file_fills_in_the_details(self):
        spec = Path(tempfile.mkdtemp()) / "ls.spec"
        spec.write_text("command: ls -l\nname: spec-ls\nwhat: lists files\nexpect: instant\nest-time-s: 1\nest-mem-mb: 10\nflag -l: long format\n")
        code, out, err = self.run_cli("offload", "profile", "--spec", str(spec))
        self.assertEqual(code, 0, out + err)
        jid = out.strip()
        self.repos.fetch_queue()
        job = json.loads(sh("git", "show", f"jobs:jobs/{jid}.json", cwd=self.tmp / "queue.git"))
        self.assertEqual((job["argv"], job["what"], job["flags"], job["est_time_s"]), (["ls", "-l"], "lists files", {"-l": "long format"}, 1.0))

    def test_profiling_a_command_that_is_already_registered_says_so_instead_of_queuing(self):
        self.w.fetch()
        code, out, err = self.run_cli("offload", "profile", "--what", "w", "--expect", "e", "--est-time-s", "1", "--est-mem-mb", "1", "--", "/bin/echo", "hello")
        self.assertEqual(code, 0)
        self.assertIn("REGISTERED as class echo", out)
        self.repos.fetch_queue()
        self.assertFalse(self.repos.remote_branch_exists("jobs"))

    def test_classes_shows_proposals(self):
        self.w.fetch()
        self.assertNotIn("Proposed, NOT installed yet", self.run_cli("classes")[1])                    # nothing proposed yet
        self.push_main_registry("classes:\n  echo:\n    tier: auto\n    cost: light\n    run: [\"/bin/echo\", \"{word}\"]\n"
                                "  not-yet-installed:\n    tier: auto\n    cost: light\n    run: [\"/bin/true\"]\n")
        code, out, err = self.run_cli("classes")
        self.assertEqual(code, 0)
        self.assertIn("Proposed, NOT installed yet", out)
        self.assertIn("not-yet-installed", out)
        self.assertNotIn("echo, not-yet", out)                                                     # echo is already installed, so it is not "proposed"


if __name__ == "__main__":
    unittest.main()
