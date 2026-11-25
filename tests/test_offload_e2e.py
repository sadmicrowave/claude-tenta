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
    def test_a_time_limit_over_the_workers_cap_is_refused_here_not_after_the_trip(self):
        self.w.fetch()                                                                              # publishes the registry, with the profile-run cap
        code, out, err = self.profile("--timeout-s", "999999")
        self.assertNotEqual(code, 0)
        self.assertIn("profile.timeout.range", out + err)
        self.assertIn("Worker's cap", out + err)
        self.assertEqual(self.profile("--timeout-s", "60")[0], 0)

    def test_fit_answers_registered_unknown_excluded_and_retry_with_the_right_exit_codes(self):
        self.w.fetch()                                                                              # publishes the registry (classes and RAM)
        code, out, err = self.run_cli("offload", "fit", "--", "/bin/echo", "hello")
        self.assertEqual(code, 0)
        self.assertIn("REGISTERED as class echo (light, tier auto): ct-run submit echo --wait", out)
        self.assertEqual(self.run_cli("offload", "fit", "--", "nosuchtool")[0], 4)
        req = self.tmp / "requester"
        sh("git", "checkout", "-q", "--orphan", "data", cwd=req)
        sh("git", "rm", "-rfq", ".", cwd=req)
        entry = {"argv": ["bigtool"], "reasons": ["needs 6000 MB"], "limiting": ["memory"], "profiled": {"mem_total_mb": 4000}}
        (req / "exclusions.json").write_text(json.dumps({"excluded": [entry, dict(entry, argv=["slowtool"], limiting=["time"], reasons=["too slow"])]}))
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", "exclusions", cwd=req)
        sh("git", "push", "-q", "origin", "data", cwd=req)
        code, out, err = self.run_cli("offload", "fit", "--", "slowtool")
        self.assertEqual(code, 3)
        self.assertIn("EXCLUDED: too slow. Do not offload this", out)
        code, out, err = self.run_cli("offload", "fit", "--", "bigtool")
        self.assertEqual(code, 5)                                                                   # the worker now has 8000 MB, it had 4000
        self.assertIn("it has 8000 mb now", out.lower())
        self.assertEqual(self.run_cli("offload", "fit")[0], 2)                                      # no command given

    def test_every_check_is_in_the_history(self):
        self.w.fetch()
        self.run_cli("offload", "fit", "--", "/bin/echo", "hello")
        self.run_cli("offload", "fit", "--", "nosuchtool")
        self.run_cli("offload", "fit", "--", "nosuchtool")
        code, out, err = self.run_cli("offload", "history")
        self.assertEqual(code, 0)
        for needle in ("Every check of whether a command suits testct", "3 checks", "(1 registered, 2 unknown)", "Worth a look", "2x nosuchtool", "echo", "nosuchtool"):
            self.assertIn(needle, out)
        rows = json.loads(self.run_cli("offload", "history", "--json", "--verdict", "unknown")[1])
        self.assertEqual([r["command"] for r in rows], ["nosuchtool", "nosuchtool"])
        self.assertEqual(len(json.loads(self.run_cli("offload", "history", "--json", "-n", "1")[1])), 1)
        self.assertIn("No fit checks recorded for that filter", "".join(self.run_cli("offload", "history", "--command", "zzz")[1:]))
        self.assertIn("Removed", "".join(self.run_cli("offload", "history", "--clear")[1:]))
        self.assertIn("No fit checks recorded yet", "".join(self.run_cli("offload", "history")[1:]))

    def test_profile_to_a_proposal_that_the_registry_accepts(self):
        self.push_main_registry()
        jid = self.run_profile_to_a_result("--name", "my-ls")
        code, out, err = self.run_cli("result", jid)
        self.assertIn("VERDICT  fits", err)
        self.assertIn(f"tct-admin adopt {jid} --name my-ls", err)                                        # the remembered name is the suggestion
        code, out, err = self.run_cli("offload", "propose", jid, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Would register a class", out + err)
        self.assertIn("my-ls", out + err)
        self.repos.fetch_queue()
        self.assertFalse(self.repos.remote_branch_exists("registry/my-ls"))                         # a preview pushes nothing
        code, out, err = self.run_cli("offload", "propose", jid)
        self.assertEqual(code, 0)
        self.assertIn("Proposed a class", out + err)
        self.assertIn("my-ls", out + err)
        self.assertIn("tct-admin apply registry/my-ls", out + err)
        text = sh("git", "show", "registry/my-ls:registry.yaml", cwd=self.tmp / "queue.git")
        self.assertIn("my-ls:", text)
        self.assertIn("existing-one:", text)
        import yaml
        reg = yaml.safe_load(text)
        self.assertEqual(registry.validate(reg, "/opt/x/tools"), [])                                # a proposal is always a valid registry
        self.assertEqual(reg["classes"]["my-ls"]["tier"], "auto")
        self.assertEqual(reg["classes"]["my-ls"]["run"][1:], ["-l"])
        self.assertIn("feat(registry): add my-ls class", sh("git", "log", "-1", "--format=%s", "registry/my-ls", cwd=self.tmp / "queue.git"))
        again = self.run_cli("offload", "propose", jid)
        self.assertEqual(again[0], 2)
        self.assertIn("already waiting", again[1] + again[2])
        self.assertEqual(sh("git", "rev-parse", "main", cwd=self.tmp / "queue.git"), sh("git", "rev-parse", "origin/main", cwd=self.repos.queue))   # main is untouched

    def test_the_tier_and_batchable_options(self):
        self.push_main_registry()
        d = Path(tempfile.mkdtemp())
        (d / "in.txt").write_text("hello")
        jid = self.run_profile_to_a_result("--name", "files-ls", "--dir", str(d))
        code, out, err = self.run_cli("offload", "propose", jid, "--tier", "approve", "--batchable")
        self.assertEqual(code, 0, out + err)
        import yaml
        entry = yaml.safe_load(sh("git", "show", "registry/files-ls:registry.yaml", cwd=self.tmp / "queue.git"))["classes"]["files-ls"]
        self.assertEqual((entry["tier"], entry["batchable"], entry["accepts_files"]), ("approve", True, True))
        no_files = self.run_profile_to_a_result("--name", "plain-ls")
        code, out, err = self.run_cli("offload", "propose", no_files, "--batchable")
        self.assertEqual(code, 2)
        self.assertIn("--batchable only makes sense", out + err)

    def test_a_command_that_does_not_fit_becomes_an_exclusion_and_is_never_offered_again(self):
        self.push_main_registry()
        self.runner.reply = ("ok", 0, "x", {"wall_s": 2.0, "peak_rss_mb": 6000, "mem_total_mb": 8000, "cores_busy": 1.0, "peak_temp_c": 50, "disk_mb": 0}, "")
        jid = self.run_profile_to_a_result()
        code, out, err = self.run_cli("result", jid)
        self.assertIn("VERDICT  does-not-fit", err)
        code, out, err = self.run_cli("offload", "propose", jid)
        self.assertEqual(code, 0)
        self.assertIn("Recorded: this command does not fit", out + err)
        self.assertIn("testct", out + err)
        self.assertIn("Claude will not offload it again", out + err)
        data = json.loads(sh("git", "show", "data:exclusions.json", cwd=self.tmp / "queue.git"))
        (e,) = data["excluded"]
        self.assertEqual((e["argv"], e["limiting"], e["profiled"]["job"], e["profiled"]["mem_total_mb"]), (["ls", "-l"], ["memory"], jid, 8000))
        code, out, err = self.run_cli("offload", "fit", "--", "ls", "-l")
        self.assertEqual(code, 3)
        self.assertIn("EXCLUDED", out)
        code, out, err = self.run_cli("offload", "exclusions")
        self.assertIn("ls -l", out)
        self.assertRegex(out, r"Limited by\s+memory")
        self.assertIn(f"(job {jid})", out)
        code, out, err = self.run_cli("offload", "profile", "--what", "w", "--expect", "e", "--est-time-s", "1", "--est-mem-mb", "1", "--", "ls", "-l")   # asking again is refused...
        self.assertEqual(code, 1)
        self.assertIn("EXCLUDED", out)
        code, out, err = self.profile("--retry")                                                    # ...unless you say it is worth trying again
        self.assertEqual(code, 0)

    def test_a_fitting_profile_lifts_an_old_exclusion(self):
        self.push_main_registry()
        self.runner.reply = ("ok", 0, "x", {"wall_s": 2.0, "peak_rss_mb": 6000, "mem_total_mb": 8000, "cores_busy": 1.0, "peak_temp_c": 50}, "")
        jid = self.run_profile_to_a_result()
        self.run_cli("offload", "propose", jid)
        self.runner.reply = ("ok", 0, "x", {"wall_s": 1.0, "peak_rss_mb": 40, "mem_total_mb": 16000, "cores_busy": 0.5, "peak_temp_c": 50}, "")
        self.w.io.publish({"id": "x", "status": "ok"})
        jid2 = self.run_profile_to_a_result("--retry")
        self.assertEqual(self.run_cli("offload", "propose", jid2, "--name", "ls-again")[0], 0)
        self.assertEqual(json.loads(sh("git", "show", "data:exclusions.json", cwd=self.tmp / "queue.git"))["excluded"], [])

    def test_an_inconclusive_run_proposes_nothing(self):
        self.push_main_registry()
        self.runner.reply = ("error", 2, "boom", {"wall_s": 1.0}, "")
        jid = self.run_profile_to_a_result()
        code, out, err = self.run_cli("offload", "propose", jid)
        self.assertEqual(code, 2)
        self.assertIn("the profiling run was inconclusive", out + err)

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
