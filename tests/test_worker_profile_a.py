"""Part 1 of 2 of the test_worker_profile tests (ProfileTime, ToolFolders)."""
import unittest
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from ct import jail as jail_mod  # noqa: E402
from support import REGISTRY, Base, sh  # noqa: E402


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
