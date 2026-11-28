"""Part 2 of 2 of the test_worker_profile tests (Profile, RegistryInfo, Isolation, SeenTime)."""
import unittest
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from armada import runner  # noqa: E402
from armada import jail as jail_mod  # noqa: E402
from support import REGISTRY, Base, sh  # noqa: E402


class Profile(Base):
    def profile_job(self, jid, **over):
        body = {"argv": ["ls", "-l"], "what": "lists files", "expect": "instant", "est_time_s": 1, "est_mem_mb": 10, "flags": {"-l": "long"}}
        return self.job(jid, "profile-run", **{**body, **over})

    def test_a_profile_run_always_waits_for_approval_and_shows_facts_and_numbers(self):
        self.profile_job("profile-aaa111")
        self.w.fetch()
        res = self.result("profile-aaa111")
        self.assertEqual(res["status"], "awaiting_approval")
        self.assertIn("AT A GLANCE", res["glance"])
        self.assertIn("testct now", res["glance"])
        self.assertEqual(res["facts"]["binary"], "/usr/bin/ls")
        self.assertEqual(self.runner.calls, [])

    def test_after_approval_it_runs_and_the_verdict_drafts_a_registry_entry(self):
        self.profile_job("profile-aaa111")
        self.w.fetch()
        self.w.approve("profile-aaa111")
        self.w.fetch()
        self.w.execute("profile-aaa111")
        res = self.result("profile-aaa111")
        self.assertEqual(res["status"], "ok")
        self.assertEqual((res["profile"]["verdict"], res["profile"]["cost"]), ("fits", "light"))
        self.assertEqual(res["profile"]["entry"]["run"][1:], ["-l"])
        call = self.runner.calls[0]
        self.assertEqual(call["argv"][0], "/usr/bin/ls") if os.path.exists("/usr/bin/ls") else None
        self.assertEqual(call["kw"]["limits"]["max_rss_mb"], 4800)                              # 60% of the sensor's 8000 MB
        self.assertFalse(call["kw"]["network"])
        self.assertFalse((self.state / "work" / "profile-aaa111").exists())                    # a profiled command's leftovers are removed

    def test_a_command_that_is_not_installed_is_refused_before_anything_is_approved(self):
        self.profile_job("profile-aaa111", argv=["nosuchprogram"])
        self.w.fetch()
        self.assertEqual(self.result("profile-aaa111")["status"], "rejected")

    def test_privilege_tools_are_refused(self):
        self.profile_job("profile-aaa111", argv=["sudo", "id"], packages=["apt:sudo"])
        self.w.fetch()
        self.assertIn("not allowed", self.result("profile-aaa111")["note"])


class RegistryInfo(Base):
    def test_a_classs_submit_recipe_is_published_for_the_client_and_never_acted_on(self):
        self.registry_path.write_text(REGISTRY + '  packed:\n    tier: auto\n    run: [/bin/true]\n    accepts_files: true\n    submit: {"dir": "src", "include": ["*.js"]}\n')
        self.w.publish_registry_info(self.w.load_registry())
        info = json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))
        self.assertEqual(info["classes"]["packed"]["submit"], {"dir": "src", "include": ["*.js"]})

    def test_the_installed_registry_is_published_once_and_again_when_it_changes(self):
        reg = self.w.load_registry()
        self.assertTrue(self.w.publish_registry_info(reg))
        info = json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))
        self.assertEqual(info["mem_total_mb"], 8000)
        self.assertIn("profile-run", info["classes"])
        self.assertEqual(info["classes"]["profile-run"]["tier"], "approve")
        self.assertFalse(info["job_network_isolation"])                                          # a NoJail was injected
        n = sh("git", "rev-list", "--count", "main", cwd=self.tmp / "results.git")
        self.assertFalse(self.w.publish_registry_info(reg))
        self.assertEqual(sh("git", "rev-list", "--count", "main", cwd=self.tmp / "results.git"), n)
        self.registry_path.write_text(REGISTRY + "  extra:\n    tier: auto\n    run: [/bin/true]\n")
        self.assertTrue(self.w.publish_registry_info(self.w.load_registry()))
        self.assertIn("extra", json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["classes"])

    def test_each_class_carries_the_day_it_was_first_installed_and_keeps_it_when_another_is_added(self):
        shown = lambda: json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["classes"]          # noqa: E731
        self.w.publish_registry_info(self.w.load_registry())
        first = shown()["profile-run"]["installed"]
        self.assertRegex(first, r"^\d{4}-\d\d-\d\dT")
        self.w.clock = lambda: __import__("datetime").datetime(2100, 1, 1, tzinfo=__import__("datetime").timezone.utc)                                                  # a later day
        self.registry_path.write_text(REGISTRY + "  extra:\n    tier: auto\n    run: [/bin/true]\n")
        self.w.publish_registry_info(self.w.load_registry())
        later = shown()
        self.assertEqual(later["profile-run"]["installed"], first)                            # kept
        self.assertGreater(later["extra"]["installed"], first)                                # the new one is dated when it appeared

    def test_the_trusted_registry_is_the_installed_file_never_the_repo(self):
        req = self.tmp / "requester"
        sh("git", "checkout", "-q", "-B", "jobs", "origin/main", cwd=req)
        (req / "registry.yaml").write_text("classes:\n  evil:\n    tier: auto\n    run: [/bin/sh, -c, id]\n")
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", "evil", cwd=req)
        sh("git", "push", "-q", "origin", "HEAD:refs/heads/jobs", cwd=req)
        self.job("job-0001", "evil")
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "rejected")
        self.assertEqual(self.runner.calls, [])


class Isolation(Base):
    def test_with_the_real_runner_a_no_network_job_is_refused_when_isolation_is_unavailable(self):
        marker = self.tmp / "ran"
        self.registry_path.write_text(f"classes:\n  touchy:\n    tier: auto\n    cost: light\n    run: [{sys.executable}, -c, \"open('{marker}', 'w')\"]\n")
        self.make_worker(runner=runner.run_command)                                              # the real runner; NoJail means "cannot isolate"
        self.job("job-0001", "touchy")
        self.run_all()
        res = self.result("job-0001")
        self.assertEqual(res["status"], "error")
        self.assertIn("not started", res["note"])
        self.assertFalse(marker.exists())                                                         # the command never ran

    def test_with_the_real_runner_a_job_runs_and_its_output_comes_back(self):
        self.registry_path.write_text(f"classes:\n  real:\n    tier: auto\n    cost: light\n    run: [{sys.executable}, -c, \"print(6 * 7)\"]\n    network: true\n")
        self.make_worker(runner=runner.run_command, jail=jail_mod.Unshare(False))      # the older method lets a network class through even with no namespace
        self.job("job-0001", "real")
        self.run_all()
        self.assertEqual((self.result("job-0001")["status"], self.result("job-0001")["output"].strip()), ("ok", "42"))


class SeenTime(Base):
    def test_a_result_says_when_the_worker_first_saw_the_job(self):
        self.job("job-0001", "echo")
        self.run_all()
        res = self.result("job-0001")
        self.assertRegex(res.get("seen", ""), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertLessEqual(res["seen"], res["started"])


if __name__ == "__main__":
    unittest.main()
