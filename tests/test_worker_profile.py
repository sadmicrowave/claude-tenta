"""ct-worker: profile runs and their timing, registry info, isolation, seen time and tool folders."""
import unittest
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from ct import runner  # noqa: E402
from ct import jail as jail_mod  # noqa: E402
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


class ProfileTime(Base):
    """A profile run's time limit follows the person's estimate; the owner's ceiling is only for a shared computer."""
    def worker_with(self, **over):
        import dataclasses
        self.settings = dataclasses.replace(self.settings, **over)
        self.make_worker()

    def test_the_limit_is_the_estimate_times_the_margin_unless_the_computer_is_shared(self):
        self.worker_with(isolation_purpose="special-use")
        reg = self.w.load_registry()
        cls = reg["classes"]["profile-run"]
        self.assertEqual(self.w.time_limit(cls, {"est_time_s": 1700}, reg), 5100)
        self.worker_with(isolation_purpose="shared")
        reg = self.w.load_registry()
        self.assertEqual(self.w.time_limit(reg["classes"]["profile-run"], {"est_time_s": 1700}, reg), reg["classes"]["profile-run"].get("max_timeout_s", 1800))

    def test_the_estimate_is_kept_with_the_queued_job_so_the_deadline_follows_it(self):
        self.worker_with(isolation_purpose="special-use")
        self.job("profile-aaa111", "profile-run", argv=["ls"], what="w", expect="e", est_time_s=100, est_mem_mb=1)
        self.w.fetch()
        self.assertEqual(self.w.approve("profile-aaa111"), (True, ""))
        self.w.fetch()
        meta = json.loads((self.w._p("queued", "profile-aaa111.meta")).read_text())
        self.assertEqual(meta.get("est_time_s"), 100)

    def test_a_worker_that_was_measured_at_install_gets_a_longer_limit_and_publishes_why(self):
        from ct import calibration, calibration_reference
        self.worker_with(isolation_purpose="special-use")
        slow = {p: v * 2 for p, v in calibration_reference.REFERENCE.items()}
        calibration.store(self.w._p("calibration.json"), slow)
        reg = self.w.load_registry()
        self.assertEqual(self.w.speed(), (2.0, f"install, {len(slow)} of 4 steps"))
        self.assertEqual(self.w.time_limit(reg["classes"]["profile-run"], {"est_time_s": 100}, reg), 600)       # 100 s x 2.0 x margin 3
        self.assertTrue(self.w.publish_registry_info(reg))
        pt = json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["profile_time"]
        self.assertEqual((pt["factor"], pt["basis"]), (2.0, f"install, {len(slow)} of 4 steps"))

    def finish_profile(self, jid, est, wall, status="ok"):
        self.job(jid, "profile-run", argv=["ls"], what="w", expect="e", est_time_s=est, est_mem_mb=1)
        self.w.fetch()
        self.assertEqual(self.w.approve(jid), (True, ""))
        self.w.fetch()
        self.runner.reply = (status, 0 if status == "ok" else 1, "out", {"wall_s": wall, "peak_rss_mb": 5, "mem_total_mb": 8000}, "")
        self.w.execute(jid)

    def test_a_finished_profile_run_teaches_the_worker_how_it_compares_with_the_estimate(self):
        self.worker_with(isolation_purpose="special-use")
        self.assertEqual(self.w.speed(), (1.0, "none yet"))
        self.finish_profile("profile-aaa111", est=10, wall=40)
        factor, basis = self.w.speed()
        self.assertEqual((factor, basis), (1.59, "1 profile run"))
        reg = self.w.load_registry()
        self.assertEqual(self.w.time_limit(reg["classes"]["profile-run"], {"est_time_s": 100}, reg), 480)             # 100 x 1.59 x 3, to the next ten

    def test_a_profile_run_that_stops_at_its_limit_is_reported_as_needing_longer(self):
        self.worker_with(isolation_purpose="special-use")
        self.finish_profile("profile-ddd444", est=100, wall=300, status="timeout")
        pr = self.result("profile-ddd444")["profile"]
        self.assertEqual((pr["verdict"], pr["next_limit_s"], pr["at_ceiling"]), ("needs-longer", 600, False))        # 100 s x 3 = 300 s was the limit; the next is double
        self.assertEqual(self.w.speed(), (1.0, "none yet"))                                                          # a run that did not finish teaches no ratio

    def test_a_run_that_did_not_finish_cleanly_or_was_too_short_teaches_nothing(self):
        self.worker_with(isolation_purpose="special-use")
        self.finish_profile("profile-bbb222", est=10, wall=40, status="error")
        self.finish_profile("profile-ccc333", est=10, wall=0.2)
        self.assertEqual(self.w.speed(), (1.0, "none yet"))

    def test_the_published_registry_says_what_the_limit_is_built_from(self):
        self.worker_with(isolation_purpose="special-use")
        self.w.publish_registry_info(self.w.load_registry())
        pt = json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["profile_time"]
        self.assertEqual((pt["ceiling_s"], pt["shared"], pt["factor"]), (None, False, 1.0))
        self.worker_with(isolation_purpose="shared")
        self.assertTrue(self.w.publish_registry_info(self.w.load_registry()))                    # a changed purpose is republished
        self.assertEqual(json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))["profile_time"]["shared"], True)


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


class ToolFolders(Base):
    """A class may name folders of tools that already exist on the computer (Special Use). The worker enforces the owner's allowed folders at EVERY run, whatever the registry said."""

    def setUp(self):
        super().setUp()
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "existing"
        (self.root / "nmap" / "bin").mkdir(parents=True)
        (self.root / "extra").mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.use(["%s/nmap/bin" % self.root, "%s/extra" % self.root], mode="relaxed", roots=(str(self.root),))

    def use(self, tools, mode, roots):
        self.registry_path.write_text(REGISTRY + "  probe:\n    tier: auto\n    cost: light\n    run: [nmap-wrapper]\n    tools: [%s]\n" % ", ".join(tools))
        self.settings.isolation_mode, self.settings.tool_roots = mode, roots
        self.make_worker()

    def run_probe(self):
        self.job("job-0001", "probe")
        self.run_all()
        return self.result("job-0001")

    def test_the_folders_come_first_on_the_jobs_path_in_the_order_given(self):
        res = self.run_probe()
        self.assertEqual(res["status"], "ok", res)
        path = self.runner.calls[0]["kw"]["extra_env"]["PATH"].split(":")
        self.assertEqual(path, [str(self.root.resolve() / "nmap" / "bin"), str(self.root.resolve() / "extra")])

    def test_a_worker_in_full_isolation_refuses_and_says_how_the_owner_changes_it(self):
        self.use(["%s/nmap/bin" % self.root], mode="full", roots=())
        res = self.run_probe()
        self.assertEqual(res["status"], "error")
        self.assertIn("full isolation", res["note"])
        self.assertIn("isolation.tool_roots", res["note"])
        self.assertEqual(self.runner.calls, [])                                           # nothing ran

    def test_a_missing_folder_and_a_link_that_leaves_the_allowed_folders_are_refused(self):
        self.use(["%s/gone" % self.root], mode="relaxed", roots=(str(self.root),))
        self.assertIn("does not exist", self.run_probe()["note"])
        (self.root / "sneaky").symlink_to(self.outside)                                   # inside the root by name, outside it once followed
        self.use(["%s/sneaky" % self.root], mode="relaxed", roots=(str(self.root),))
        self.job("job-0002", "probe")
        self.run_all()
        self.assertIn("leads outside", self.result("job-0002")["note"])
        self.assertEqual(self.runner.calls, [])

    def test_the_published_registry_and_state_say_which_class_uses_which_folders_and_the_mode(self):
        self.w.publish_registry_info(self.w.load_registry())
        info = json.loads(sh("git", "show", "main:registry.json", cwd=self.tmp / "results.git"))
        self.assertEqual(info["classes"]["probe"]["tools"], ["%s/nmap/bin" % self.root, "%s/extra" % self.root])
        from ct import workerstate
        env, _ = workerstate.build(snapshot={}, registry=self.w.load_registry(), extra={"isolation_mode": "relaxed", "tool_roots": [str(self.root)]}, jail=jail_mod.NoJail(), locked=False, now_iso="x")
        self.assertEqual((env["isolation"]["mode"], env["isolation"]["tool_roots"]), ("relaxed", [str(self.root)]))


if __name__ == "__main__":
    unittest.main()
