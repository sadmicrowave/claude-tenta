"""Shared environments on a worker (design: docs/design/WORKSPACE-JOBS.md, "Environment lifecycle"): one per package-set hash, a last-used stamp, removal after packages.keep_days
unused, never one in use, rebuilt on the next use, and a manifest of the apt packages tenta itself installed. The clock, free space and locks are pinned: nothing here depends on the host."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from ct import envs, packages, refusals  # noqa: E402

DAY = 86400


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def days(self, n):
        self.now += n * DAY


def done(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


class Fake:
    """run_group stand-in: records commands, answers from (prefix -> handler)."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, argv, timeout, **kw):
        self.calls.append((argv, kw))
        for prefix, handler in self.answers.items():
            if argv[:len(prefix)] == list(prefix):
                return handler(argv, kw) if callable(handler) else handler
        return done(argv)


def npm_installer(version="9.12.0"):
    def npm(argv, kw):
        d = Path(argv[argv.index("--prefix") + 1]) / "node_modules" / "eslint"
        d.mkdir(parents=True, exist_ok=True)
        (d / "package.json").write_text(json.dumps({"name": "eslint", "version": version}))
        return done(argv)
    return npm


def make(run=None, free_mb=lambda: 10**6, keep=None):
    clock = Clock()
    root = Path(tempfile.mkdtemp())
    p = packages.Packages(root / "tools", root / "env", run=run or Fake({("npm",): npm_installer()}), which=lambda n: "/usr/bin/" + n, clock=clock, free_mb=free_mb,
                          os_release="debian-12", manifest_path=root / "apt-manifest.json")
    return p, clock, root


PKGS = ["npm:eslint@9.12.0"]


class Key(unittest.TestCase):
    def test_the_key_is_the_same_for_the_same_set_in_any_order_and_differs_by_os_and_content(self):
        a = envs.env_key(["pip:b==1", "npm:a@1.0.0"], "debian-12")
        self.assertEqual(a, envs.env_key(["npm:a@1.0.0", "pip:b==1"], "debian-12"))
        self.assertNotEqual(a, envs.env_key(["npm:a@1.0.0", "pip:b==1"], "debian-13"))
        self.assertNotEqual(a, envs.env_key(["npm:a@1.0.1", "pip:b==1"], "debian-12"))


class UseAndPrune(unittest.TestCase):
    def test_every_use_touches_the_last_used_stamp(self):
        p, clock, _ = make()
        a = p.acquire(PKGS)
        a.release()
        first = p.envs.last_used(a.key)
        clock.days(3)
        p.acquire(PKGS).release()
        self.assertEqual(p.envs.last_used(a.key), first + 3 * DAY)

    def test_two_classes_with_the_same_packages_share_one_environment_and_one_build(self):
        p, _, _ = make()
        a, b = p.acquire(PKGS, label="lint"), p.acquire(["npm:eslint@9.12.0"], label="docs")
        self.assertEqual(a.key, b.key)
        self.assertEqual(sum(1 for c in p._run.calls if c[0][0] == "npm"), 1)
        a.release(), b.release()

    def test_prune_at_exactly_the_limit_keeps_it_and_one_second_later_removes_it(self):
        p, clock, _ = make()
        a = p.acquire(PKGS)
        a.release()
        clock.days(30)
        rep = p.prune_old_envs(keep_days=30)
        self.assertEqual(rep.removed, [])
        self.assertTrue(p.envs.complete(a.key))
        clock.now += 1
        rep = p.prune_old_envs(keep_days=30)
        self.assertEqual(rep.removed, [a.key])
        self.assertFalse(p.envs.path(a.key).exists())

    def test_an_environment_in_use_is_skipped_and_reported_never_half_deleted(self):
        p, clock, _ = make()
        held = p.acquire(PKGS)                                   # a running job holds its lease
        clock.days(40)
        rep = p.prune_old_envs(keep_days=30)
        self.assertEqual((rep.removed, rep.skipped), ([], [held.key]))
        self.assertIn("skipped 1 environment in use", rep.text())
        self.assertTrue((p.envs.path(held.key) / ".complete").exists())
        held.release()
        self.assertEqual(p.prune_old_envs(keep_days=30).removed, [held.key])                 # the next pass takes it

    def test_a_shared_environment_stays_until_both_classes_are_idle(self):
        p, clock, _ = make()
        p.acquire(PKGS, label="lint").release()
        clock.days(25)
        p.acquire(PKGS, label="docs").release()                  # the second class uses it on day 25
        clock.days(25)                                           # day 50: lint idle 50 days, docs idle 25
        self.assertEqual(p.prune_old_envs(keep_days=30).removed, [])
        clock.days(6)                                            # docs idle 31 days: both idle now
        self.assertEqual(len(p.prune_old_envs(keep_days=30).removed), 1)

    def test_a_retired_class_does_not_keep_its_environment_forever(self):
        p, clock, _ = make()
        p.acquire(PKGS, label="gone-class").release()            # the class is removed from the registry: nothing runs it again
        clock.days(31)
        self.assertEqual(len(p.prune_old_envs(keep_days=30).removed), 1)

    def test_per_job_throwaway_folders_follow_the_same_setting(self):
        p, clock, root = make()
        old = root / "env" / "job-old"
        old.mkdir(parents=True)
        import os
        os.utime(old, (clock.now - 31 * DAY, clock.now - 31 * DAY))
        self.assertEqual(p.prune_old_envs(keep_days=30).folders, 1)
        self.assertFalse(old.exists())


class Rebuild(unittest.TestCase):
    def test_a_class_run_again_after_the_limit_is_rebuilt_once_and_the_result_says_so(self):
        p, clock, _ = make()
        first = p.acquire(PKGS)
        first.release()
        self.assertIsNone(first.note)                            # a first build says nothing special
        clock.days(31)
        p.prune_old_envs(keep_days=30)
        calls = sum(1 for c in p._run.calls if c[0][0] == "npm")
        again = p.acquire(PKGS, label="lint")
        again.release()
        self.assertEqual(sum(1 for c in p._run.calls if c[0][0] == "npm"), calls + 1)         # rebuilt
        self.assertIn("were rebuilt (unused for 31 days)", again.note)
        self.assertIn("this run took", again.note)
        third = p.acquire(PKGS)
        third.release()
        self.assertEqual(sum(1 for c in p._run.calls if c[0][0] == "npm"), calls + 1)         # not rebuilt a second time
        self.assertIsNone(third.note)

    def test_offline_during_a_rebuild_does_not_run_the_job_and_shows_the_tools_own_error(self):
        p, clock, _ = make()
        p.acquire(PKGS).release()
        clock.days(31)
        p.prune_old_envs(keep_days=30)
        p._run = Fake({("npm",): done([], rc=1, err="npm ERR! network request to https://registry.npmjs.org failed, reason: getaddrinfo ENOTFOUND")})
        with self.assertRaises(packages.PrepError) as cm:
            p.acquire(PKGS, label="lint")
        text = str(cm.exception)
        self.assertIn("`lint`", text)
        self.assertIn("removed after 31 unused days and cannot be rebuilt now", text)
        self.assertIn("ENOTFOUND", text)                                                       # the real error text
        from ct import cards
        self.assertEqual(text.count("`"), 2)                                                   # only the class name: the tool's own quoting is not turned into a command chip by the cards
        self.assertEqual(cards.code_spans(text).count("`"), 2)                                 # and the cards find no command in the tool's words to cut out of the sentence
        self.assertIn("reach the package servers", text)
        self.assertEqual(cm.exception.code, "env.rebuild.failed")
        self.assertFalse(any(p.envs.path(k).exists() for k in p.envs.keys()))                 # nothing half built is left to be reused

    def test_a_rebuild_that_runs_out_of_its_own_time_says_the_job_did_not_start(self):
        p, _, _ = make(run=Fake({("npm",): done([], rc=124)}))
        with self.assertRaises(packages.PrepError) as cm:
            p.acquire(PKGS, label="lint")
        self.assertEqual(cm.exception.code, "env.rebuild.timeout")
        self.assertIn("did not start", str(cm.exception))

    def test_a_rebuild_is_not_charged_to_the_jobs_time_limit(self):
        """acquire has its own limit (total_timeout); the clock it spends is reported, and nothing in it reads the job's timeout_s."""
        p, clock, _ = make(run=Fake({("npm",): lambda argv, kw: (setattr(clock, "now", clock.now + 700), npm_installer()(argv, kw))[1]}))
        a = p.acquire(PKGS)
        a.release()
        self.assertGreaterEqual(a.seconds, 700)
        import inspect
        self.assertNotIn("timeout_s", inspect.getsource(packages.Packages.acquire))

    def test_not_enough_disk_to_rebuild_frees_the_oldest_environments_first_then_says_so(self):
        free = {"mb": 10**6}
        p, clock, _ = make(free_mb=lambda: free["mb"])
        p.acquire(PKGS).release()
        clock.days(40)
        free["mb"] = 100                                         # the disk fills up meanwhile
        old_key = envs.env_key(PKGS, "debian-12")
        with self.assertRaises(packages.PrepError) as cm:
            p.acquire(["npm:other@1.0.0"], need_mb=500)
        self.assertEqual(cm.exception.code, "env.rebuild.no_space")
        self.assertIn("need 500 MB, have 100 MB", str(cm.exception))
        self.assertFalse(p.envs.path(old_key).exists())                                      # it freed what it could first


class DiskPressure(unittest.TestCase):
    def test_eviction_takes_the_least_recently_used_first_and_never_one_in_use(self):
        p, clock, _ = make(run=Fake({("npm",): npm_installer()}))
        keys = []
        for i, name in enumerate(["npm:eslint@1.0.0", "npm:eslint@2.0.0", "npm:eslint@3.0.0"]):
            a = p.acquire([name])
            (p.envs.path(a.key) / "blob").write_bytes(b"x" * 2_000_000)
            keys.append(a.key)
            a.release()
            clock.days(1)
        p.acquire(["npm:eslint@1.0.0"]).release()                                            # the first is now the most recently used
        busy = p.acquire(["npm:eslint@2.0.0"])                                               # the second is in use
        clock.days(1)
        gone = p.envs.evict_lru(need_mb=3)                                                   # needs two environments' worth
        self.assertEqual([k for k, _ in gone], [keys[2], keys[0]])                           # oldest unused first; in-use one skipped
        self.assertTrue(p.envs.path(keys[1]).exists())
        busy.release()


class AptManifest(unittest.TestCase):
    def reg(self, **classes):
        return {"classes": {n: {"packages": pk} for n, pk in classes.items()}}

    def installer(self, preinstalled):
        installed = set(preinstalled)

        def dpkg(argv, kw):
            return done(argv, rc=0 if argv[-1] in installed else 1)
        return Fake({("dpkg", "-s"): dpkg, ("dpkg-query",): lambda a, k: done(a, out="2048\n")})

    def test_the_manifest_lists_only_what_tenta_installed(self):
        p, _, root = make(run=self.installer(preinstalled=["git"]))
        ran = []

        def root_run(cmd):
            ran.append(cmd)
            return done(cmd)
        self.assertEqual(p.install_registry_packages({"classes": {"a": {"packages": ["apt:git", "apt:jq"]}}}, run_root=root_run), 0)
        listed = json.loads((root / "apt-manifest.json").read_text())["packages"]
        self.assertEqual(sorted(listed), ["jq"])                                             # git was there before tenta: never listed
        self.assertEqual(listed["jq"]["classes"], ["a"])

    def test_a_second_class_asking_for_a_tenta_installed_package_is_recorded_too(self):
        p, _, root = make(run=self.installer(preinstalled=[]))
        p.install_registry_packages({"classes": {"a": {"packages": ["apt:jq"]}}}, run_root=lambda c: done(c))
        p._run = self.installer(preinstalled=["jq"])
        p.install_registry_packages({"classes": {"a": {"packages": ["apt:jq"]}, "b": {"packages": ["apt:jq"]}}}, run_root=lambda c: self.fail("already there"))
        self.assertEqual(json.loads((root / "apt-manifest.json").read_text())["packages"]["jq"]["classes"], ["a", "b"])

    def test_leftovers_are_reported_by_name_and_size_and_never_removed(self):
        p, _, root = make(run=self.installer(preinstalled=[]))
        p.install_registry_packages({"classes": {"a": {"packages": ["apt:jq"]}, "b": {"packages": ["apt:tree"]}}}, run_root=lambda c: done(c))
        left = p.apt_leftovers({"classes": {"b": {"packages": ["apt:tree"]}}})               # class a was retired
        self.assertEqual([(n, mb) for n, mb in left], [("jq", 2.0)])
        self.assertIn("1 apt package from classes that no longer run remains", p.apt_leftover_text(left))
        self.assertIn("(jq, 2 MB)", p.apt_leftover_text(left))
        self.assertIn("only reports them", p.apt_leftover_text(left))
        self.assertIn("apt-get remove jq", p.apt_leftover_text(left))
        calls_before = len(p._run.calls)
        p.apt_leftovers({"classes": {}})
        self.assertFalse(any(c[0][0] in ("apt-get", "apt") and "remove" in c[0] for c in p._run.calls[calls_before:]))

    def test_without_a_manifest_nothing_is_listed_and_nothing_can_be_removed(self):
        p, _, _ = make()
        self.assertEqual(p.apt_leftovers({"classes": {}}), [])

    def test_npm_and_pip_are_no_longer_installed_for_real_by_a_registry_install(self):
        p, _, _ = make(run=self.installer(preinstalled=[]))
        ran = []
        p.install_registry_packages({"classes": {"a": {"packages": ["apt:jq", "npm:eslint@9.12.0", "pip:x==1"]}}}, run_root=lambda c: ran.append(c) or done(c))
        self.assertEqual([c[0] for c in ran], ["apt-get"])                                   # they live in shared environments now


class Messages(unittest.TestCase):
    def test_every_environment_refusal_is_in_the_catalog_with_a_next_step(self):
        for code in ("env.rebuild.failed", "env.rebuild.timeout", "env.rebuild.no_space"):
            e = refusals.CATALOG[code]
            self.assertTrue(e.steps, code)


if __name__ == "__main__":
    unittest.main()
