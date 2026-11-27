"""The worker side of shared environments: a class with npm or pip packages runs in an environment built on first use, holds it while it runs, says so when it had to rebuild it,
and ends as `needs-environment` (not run, not blamed on the class) when it cannot be built. Time, free disk and the package tools are all pinned or faked."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ct import config, packages  # noqa: E402
from support import REGISTRY, Base  # noqa: E402

DAY = 86400
LINT = '  lint:\n    tier: auto\n    cost: light\n    run: [eslint, "--version"]\n    packages: ["npm:eslint@9.12.0"]\n'


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def fake_tools(fail=None):
    def run(argv, timeout, **kw):
        if argv[0] == "npm":
            if fail:
                return subprocess.CompletedProcess(argv, 1, "", fail)
            prefix = Path(argv[argv.index("--prefix") + 1])
            (prefix / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)
            (prefix / "node_modules" / ".bin" / "eslint").write_text("#!/bin/sh\n")
            (prefix / "node_modules" / ".bin" / "eslint").chmod(0o755)
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


class EnvBase(Base):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.fail = None
        self.use_tools(fake_tools())
        self.registry_path.write_text(REGISTRY + LINT)

    def use_tools(self, run):
        self.pk = packages.Packages(self.tmp / "tools", self.state / "profile-env", run=run, which=lambda n: "/usr/bin/" + n, clock=self.clock, os_release="test-1",
                                    manifest_path=self.state / "apt-manifest.json")
        self.make_worker()

    def lint(self, jid):
        self.job(jid, "lint")
        self.run_all()
        return self.result(jid)

    def held(self):
        """Is any environment lock held by a job right now? (an exclusive try fails exactly when a shared hold exists)"""
        for key in self.pk.envs.keys():
            lease = self.pk.envs.lease(key)
            try:
                if not lease.exclusive(wait=False):
                    return True
            finally:
                lease.close()
        return False


class Running(EnvBase):
    def test_a_class_with_packages_runs_in_its_environment_and_the_lock_is_held_only_while_it_runs(self):
        seen = {}
        orig = self.runner.__call__

        def spy(argv, timeout, wd, **kw):
            seen["held"], seen["path"], seen["argv"] = self.held(), (kw.get("extra_env") or {}).get("PATH", ""), argv
            return orig(argv, timeout, wd, **kw)
        self.w.runner = spy
        res = self.lint("job-1")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(seen["held"])                                                  # the prune pass would skip it while the job runs
        self.assertIn("/shared/", seen["path"])
        self.assertTrue(seen["argv"][0].endswith("/node_modules/.bin/eslint"))        # the very binary the environment provides
        self.assertFalse(self.held())                                                  # released when the job ended
        self.assertNotIn("rebuilt", res.get("note") or "")

    def test_cleanup_racing_a_running_job_leaves_the_environment_intact(self):
        seen = {}
        orig = self.runner.__call__

        def spy(argv, timeout, wd, **kw):
            self.clock.now += 40 * DAY                                                 # the prune pass starts while the job runs
            seen["report"] = self.w.prune_envs()
            seen["exists"] = [self.pk.envs.complete(k) for k in self.pk.envs.keys()]
            return orig(argv, timeout, wd, **kw)
        self.w.runner = spy
        self.lint("job-1")
        self.assertEqual(seen["exists"], [True])
        self.assertEqual(len(seen["report"].skipped), 1)
        self.assertIn("skipped 1 environment in use", seen["report"].text())

    def test_a_class_run_again_after_the_limit_is_rebuilt_and_the_result_says_so(self):
        self.lint("job-1")
        self.clock.now += 31 * DAY
        self.w.prune_envs()
        res = self.lint("job-2")
        self.assertEqual(res["status"], "ok")
        self.assertIn("the packages for this class were rebuilt (unused for 31 days); this run took", res["note"])
        self.assertTrue(res["environment"]["rebuilt"])
        self.assertNotIn("rebuilt", self.lint("job-3").get("note") or "")

    def test_the_rebuild_is_not_charged_to_the_jobs_time_limit(self):
        self.lint("job-1")
        limit = self.runner.calls[0]["timeout"]
        self.clock.now += 31 * DAY
        self.w.prune_envs()
        orig = self.pk._run

        def slow(argv, timeout, **kw):
            self.clock.now += 800                                                      # the rebuild takes longer than the job's whole limit
            return orig(argv, timeout, **kw)
        self.pk._run = slow
        res = self.lint("job-2")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self.runner.calls[1]["timeout"], limit)
        self.assertGreaterEqual(res["environment"]["build_s"], 800)


class CannotBuild(EnvBase):
    def test_offline_during_a_rebuild_the_job_is_not_run_and_is_not_blamed_on_the_class(self):
        self.lint("job-1")
        self.clock.now += 31 * DAY
        self.w.prune_envs()
        self.use_tools(fake_tools(fail="npm ERR! network request failed, reason: getaddrinfo ENOTFOUND registry.npmjs.org"))
        calls = len(self.runner.calls)
        res = self.lint("job-2")
        self.assertEqual(res["status"], "needs-environment")
        self.assertEqual(len(self.runner.calls), calls)                                # never run with missing software
        self.assertIn("removed after 31 unused days and cannot be rebuilt now", res["note"])
        self.assertIn("ENOTFOUND registry.npmjs.org", res["note"])
        self.assertEqual(self.pk.envs.keys(), [])                                      # nothing half built
        self.assertFalse(self.held())

    def test_a_first_build_that_fails_says_what_the_tool_said(self):
        self.use_tools(fake_tools(fail="npm ERR! 404 not found"))
        res = self.lint("job-1")
        self.assertEqual(res["status"], "needs-environment")
        self.assertIn("could not be fetched: ", res["note"])
        self.assertIn("404 not found", res["note"])

    def test_not_enough_disk_says_what_was_needed_and_what_was_freed(self):
        self.lint("job-1")
        self.clock.now += 5 * DAY
        self.pk.free_mb = lambda: 50
        self.pk.envs.prune(keep_days=1)                                                # the first environment goes
        res = self.lint("job-2")
        self.assertEqual(res["status"], "needs-environment")
        self.assertIn("need 200 MB, have 50 MB", res["note"])


class Housekeeping(EnvBase):
    def test_the_setting_decides_the_limit_and_defaults_to_thirty_days(self):
        self.assertEqual(self.settings.pkg_keep_days, 30)
        self.lint("job-1")
        self.clock.now += 20 * DAY
        self.settings.pkg_keep_days = 10
        self.assertEqual(len(self.w.prune_envs().removed), 1)

    def test_disk_pressure_evicts_environments_before_the_file_cache_and_logs_it(self):
        self.lint("job-1")
        (self.pk.envs.path(self.pk.envs.keys()[0]) / "blob").write_bytes(b"x" * 3_000_000)
        self.settings.disk_reserve = True
        with mock.patch("ct.worker.plat.disk_free_gb", return_value=0.5), mock.patch.object(self.w, "disk_reserve_gb", return_value=0.502):
            rep = self.w.prune_envs()
        self.assertEqual(rep.removed, [])                                              # not old: the backstop, not the age rule, removed it
        self.assertEqual(self.pk.envs.keys(), [])
        self.assertTrue(any("disk nearly full" in m and "least recently used environment" in m for m in self.logged))

    def test_apt_leftovers_of_retired_classes_are_logged_once_and_never_removed(self):
        (self.state / "apt-manifest.json").write_text(json.dumps({"v": 1, "packages": {"tree": {"classes": ["gone"], "mb": 1.5}}}))
        self.w.prune_envs()
        self.w.prune_envs()
        said = [m for m in self.logged if "apt package" in m]
        self.assertEqual(len(said), 1)
        self.assertIn("(tree, 1.5 MB)", said[0])
        self.assertIn("only reports them", said[0])

    def test_the_worker_publishes_its_environments_for_ct_admin_workers(self):
        self.lint("job-1")
        info = self.w.environments_info()
        self.assertEqual((info["count"], info["keep_days"], info["apt_left"]), (1, 30, 0))


class Setting(unittest.TestCase):
    def test_keep_days_is_validated_and_documented(self):
        spec = config.SCHEMA["packages"]["keep_days"]
        self.assertEqual(spec[0], 30)
        for bad in (0, -1, 4000, "soon"):
            value, problem = spec[1](bad)
            self.assertIsNone(value)
            self.assertTrue(problem, bad)
        self.assertEqual(spec[1](7), (7, None))
        self.assertIn("rebuilt on the next run", spec[2])

    def test_the_seven_day_constant_is_gone(self):
        root = Path(__file__).resolve().parents[1] / "src"
        self.assertEqual([p.name for p in root.rglob("*.py") if "PKG_KEEP_DAYS" in p.read_text()], [])


if __name__ == "__main__":
    unittest.main()
