import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from armada import packages  # noqa: E402


def done(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


class Fake:
    """A stand-in for run_group that records every command and answers from a table of (prefix -> handler)."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, argv, timeout, **kw):
        self.calls.append((argv, kw))
        for prefix, handler in self.answers.items():
            if argv[:len(prefix)] == list(prefix):
                return handler(argv, kw) if callable(handler) else handler
        return done(argv)


def make(**kw):
    tools, env = Path(tempfile.mkdtemp()) / "tools", Path(tempfile.mkdtemp())
    which = kw.pop("which", lambda n: "/usr/bin/" + n)
    return packages.Packages(tools, env, which=which, **kw), tools, env


def put_npm(root, name, version):
    d = Path(root) / "npm" / "node_modules" / name
    d.mkdir(parents=True)
    (d / "package.json").write_text(json.dumps({"name": name, "version": version}))


def put_pip(root, name, version):
    (Path(root) / "pip" / f"{name}-{version}.dist-info").mkdir(parents=True)


class Installed(unittest.TestCase):
    def test_versions_are_read_from_npm_and_pip_metadata(self):
        root = Path(tempfile.mkdtemp())
        put_npm(root, "eslint", "9.12.0")
        put_pip(root, "Foo_Bar", "1.2.3")
        self.assertEqual(packages.version_in("npm", root, "eslint"), "9.12.0")
        self.assertEqual(packages.version_in("pip", root, "foo-bar"), "1.2.3")              # names compare after normalising
        self.assertIsNone(packages.version_in("npm", root, "nope"))
        self.assertIsNone(packages.version_in("pip", root / "missing", "x"))

    def test_a_pinned_entry_is_only_satisfied_by_exactly_that_version(self):
        p, tools, _ = make()
        put_pip(tools, "pyflakes", "3.2.0")
        put_npm(tools, "eslint", "9.12.0")
        self.assertTrue(p.have("pip", "pyflakes==3.2.0"))
        self.assertFalse(p.have("pip", "pyflakes==3.3.0"))                                 # a bumped pin reinstalls
        self.assertTrue(p.have("pip", "pyflakes"))                                         # unpinned: present is enough
        self.assertTrue(p.have("npm", "eslint@9.12.0"))
        self.assertFalse(p.have("npm", "eslint@9.13.0"))
        self.assertFalse(p.have("npm", "other"))

    def test_resolve_pins_to_what_was_fetched(self):
        p, tools, env = make()
        put_npm(env, "eslint", "9.12.0")
        put_pip(env, "pyflakes", "3.2.0")
        self.assertEqual(p.resolve(env, ["apt:jq", "npm:eslint", "pip:pyflakes", "pip:missing"]),
                         ["apt:jq", "npm:eslint@9.12.0", "pip:missing", "pip:pyflakes==3.2.0"])
        self.assertEqual(p.resolve(env, ["npm:eslint@8.0.0"]), ["npm:eslint@9.12.0"])      # what was fetched, not what was asked
        put_pip(tools, "already", "1.0")
        self.assertEqual(p.resolve(env, ["pip:already"]), ["pip:already==1.0"])            # or what is already installed for real


class Runtime(unittest.TestCase):
    def test_runtime_env_exposes_only_what_exists(self):
        e = Path(tempfile.mkdtemp())
        self.assertEqual(packages.runtime_env(e), {"path": [], "vars": {}})
        for sub in ("apt/usr/bin", "npm/node_modules/.bin", "pip", "apt/usr/lib/x86_64-linux-gnu", "npm/node_modules"):
            (e / sub).mkdir(parents=True, exist_ok=True)
        rt = packages.runtime_env(e)
        self.assertIn(str(e / "apt/usr/bin"), rt["path"])
        self.assertIn(str(e / "npm/node_modules/.bin"), rt["path"])
        self.assertIn(str(e / "apt/usr/lib/x86_64-linux-gnu"), rt["vars"]["LD_LIBRARY_PATH"])
        self.assertEqual(rt["vars"]["PYTHONPATH"], str(e / "pip"))
        self.assertEqual(rt["vars"]["NODE_PATH"], str(e / "npm/node_modules"))

    def test_missing_hint_lists_the_programs_the_packages_provide(self):
        d = Path(tempfile.mkdtemp())
        self.assertIn("no programs at all", packages.missing_hint([d]))
        for n in ("ssh", "scp"):
            f = d / n
            f.write_text("x")
            f.chmod(0o755)
        (d / "data.txt").write_text("not executable")
        hint = packages.missing_hint([d])
        self.assertIn("scp, ssh", hint)
        self.assertNotIn("data.txt", hint)

    def test_promote_path(self):
        p, tools, _ = make()
        env = "/var/x/env/j1"
        self.assertEqual(p.promote_path(f"{env}/apt/usr/bin/jq", env), "/usr/bin/jq")
        self.assertEqual(p.promote_path(f"{env}/npm/node_modules/.bin/eslint", env), f"{tools}/npm/node_modules/.bin/eslint")
        self.assertEqual(p.promote_path(f"{env}/pip/bin/pyflakes", env), f"{tools}/pip/bin/pyflakes")
        self.assertEqual(p.promote_path("/usr/bin/other", env), "/usr/bin/other")


class Planning(unittest.TestCase):
    APT_OK = "Inst jq (1.7 Ubuntu)\nInst libjq1 (1.7 Ubuntu)\n"
    CACHE = "Package: jq\nSize: 2000000\nInstalled-Size: 3072\n\nPackage: libjq1\nSize: 1000000\nInstalled-Size: 1024\n"

    def fake(self, over=None):
        answers = {("apt-get", "-s"): done([], out=self.APT_OK), ("apt-cache",): done([], out=self.CACHE),
                   ("python3", "-m", "pip", "--version"): done([], out="pip 24")}
        answers.update(over or {})
        return Fake(answers)

    def test_apt_plan_reads_the_simulation_and_sizes(self):
        p, _, _ = make(run=self.fake())
        self.assertEqual(p.apt_plan(["jq"]), (["jq", "libjq1"], [], 3.0, 4.0, []))

    def test_apt_plan_problems(self):
        p, _, _ = make(run=self.fake({("apt-get", "-s"): done([], rc=100, out="", err="E: Unable to locate package nope\n")}))
        self.assertIn("Unable to locate package nope", p.apt_plan(["nope"])[4][0])
        p, _, _ = make(run=self.fake({("apt-get", "-s"): done([], out=""), ("dpkg", "-s"): done([], rc=1)}))
        self.assertIn("neither installed nor installable", p.apt_plan(["ghost"])[4][0])
        p, _, _ = make(run=self.fake({("apt-get", "-s"): done([], out=""), ("dpkg", "-s"): done([], out="Status: install ok installed")}))
        self.assertEqual(p.apt_plan(["jq"])[:2], ([], ["jq"]))                              # already installed: nothing to do

        def boom(argv, timeout, **kw):
            raise OSError("no apt")
        p, _, _ = make(run=boom)
        self.assertIn("apt is not available", p.apt_plan(["jq"])[4][0])

    def test_permanent_commands_are_exact_and_safe(self):
        p, tools, _ = make()
        cmds = p.permanent_commands(["apt:jq", "npm:eslint@9.12.0", "pip:pyflakes==3.2.0"])
        self.assertEqual(cmds[0], ["apt-get", "install", "--no-install-recommends", "-y", "jq"])
        self.assertIn("--ignore-scripts", cmds[1])
        self.assertEqual(cmds[1][-1], "eslint@9.12.0")
        for flag in ("--only-binary=:all:", "--upgrade", "--no-input"):
            self.assertIn(flag, cmds[2])
        self.assertIn(str(tools / "pip"), cmds[2])

    def test_plan_for_a_mix_lists_the_install_the_throwaway_commands_and_the_permanent_ones(self):
        p, tools, env = make(run=self.fake())
        put_pip(tools, "already", "1.0")
        plan = p.plan(["apt:jq", "npm:eslint@9.12.0", "pip:already==1.0"], "job-1")
        self.assertEqual(plan["install"], ["apt:jq", "apt:libjq1", "npm:eslint@9.12.0"])
        self.assertEqual(plan["already"], ["pip:already==1.0"])
        self.assertEqual((plan["download_mb"], plan["problems"]), (3.0, []))
        exp = " | ".join(plan["experiment"])
        self.assertIn(f"{p.env_root}/job-1/apt", exp)
        self.assertIn("--ignore-scripts", exp)
        self.assertIn("apt-get install", plan["permanent"][0])

    def test_plan_problems(self):
        p, _, _ = make(run=self.fake(), which=lambda n: None)
        plan = p.plan(["npm:eslint@9.12.0"])
        self.assertIn("npm is not installed on the worker", plan["problems"][0])
        p2, _, _ = make(run=self.fake({("apt-get", "-s"): done([], out="Inst npm (1)\n")}), which=lambda n: None)
        self.assertEqual(p2.plan(["npm:eslint@9.12.0", "apt:npm"])["problems"], [])            # unless apt:npm is part of the same run
        p, _, _ = make(run=self.fake({("python3", "-m", "pip", "--version"): done([], rc=1)}))
        self.assertIn("pip is not available", p.plan(["pip:x==1"])["problems"][0])
        p, _, _ = make(run=self.fake(), limits={"max_download_mb": 2})
        self.assertIn("the limit is 2 MB", p.plan(["apt:jq"])["problems"][0])

    def test_the_approval_text_uses_the_product_and_apply_command(self):
        p, _, _ = make(run=self.fake(), product="homect", apply_command="homect-admin apply")
        text = p.plan_text(p.plan(["apt:jq"], "j"))
        for needle in ("INSTALL REQUIRED", "software homect does not have", "THROWAWAY folder", "`homect-admin apply`", "NO network", "apt-get install"):
            self.assertIn(needle, text)
        self.assertEqual(p.plan_text(p.plan([])), "")
        self.assertNotIn("armada", text)


class Preparing(unittest.TestCase):
    def runner(self, log):
        """Fakes the three fetchers by creating the files the real ones would."""
        def download(argv, kw):
            (Path(kw["cwd"]) / "jq.deb").write_text("deb")
            return done(argv)

        def unpack(argv, kw):
            d = Path(argv[-1]) / "usr" / "bin"
            d.mkdir(parents=True)
            (d / "jq").write_text("x")
            return done(argv)

        def npm(argv, kw):
            put_npm(Path(argv[argv.index("--prefix") + 1]).parent, "eslint", "9.12.0")
            return done(argv)
        return Fake({("apt-get", "-s"): done([], out="Inst jq (1)\n"), ("apt-cache",): done([], out="Package: jq\nSize: 1000\nInstalled-Size: 10\n"),
                     ("apt-get", "download"): download, ("dpkg-deb",): unpack, ("npm",): npm, ("python3", "-m", "pip", "--version"): done([])})

    def test_fetching_is_unprivileged_scripts_free_and_confined_to_the_throwaway_folder(self):
        log = []
        p, tools, env = make(run=self.runner(log))
        steps = []
        rt = p.prepare("job-1", ["apt:jq", "npm:eslint@9.12.0"], on_step=steps.append)
        folder = env / "job-1"
        self.assertTrue((folder / "apt" / "usr" / "bin" / "jq").exists())
        self.assertFalse((folder / "apt-debs").exists())                                    # the .deb files are removed after unpacking
        self.assertIn(str(folder / "apt/usr/bin"), rt["path"])
        self.assertEqual(oct(folder.stat().st_mode & 0o777), "0o700")
        self.assertTrue(any(s.startswith("apt-get download") for s in steps))
        calls = {c[0][0]: c for c in p._run.calls if c[0][0] in ("npm", "dpkg-deb")}
        self.assertIn("--ignore-scripts", calls["npm"][0])
        self.assertEqual(calls["npm"][1]["env"]["HOME"], str(folder / "home"))               # the worker's own home is not touched
        self.assertNotIn("sudo", " ".join(" ".join(c[0]) for c in p._run.calls))
        self.assertTrue(p.resolve(folder, ["npm:eslint"]) == ["npm:eslint@9.12.0"])

    def test_a_failed_step_removes_the_folder_and_says_why(self):
        p, _, env = make(run=Fake({("apt-get", "-s"): done([], out="Inst jq (1)\n"), ("apt-cache",): done([], out=""),
                                   ("apt-get", "download"): done([], rc=100, err="E: Failed to fetch\n")}))
        with self.assertRaises(packages.PrepError) as cm:
            p.prepare("job-2", ["apt:jq"])
        self.assertIn("Failed to fetch", str(cm.exception))
        self.assertFalse((env / "job-2").exists())

    def test_a_timeout_is_explained(self):
        p, _, _ = make(run=Fake({("apt-get", "-s"): done([], out="Inst jq (1)\n"), ("apt-cache",): done([], out=""),
                                 ("apt-get", "download"): done([], rc=124)}))
        with self.assertRaises(packages.PrepError) as cm:
            p.prepare("job-3", ["apt:jq"])
        self.assertIn("took longer than", str(cm.exception))

    def test_too_much_data_is_refused(self):
        def big(argv, kw):
            (Path(kw["cwd"]) / "big.deb").write_bytes(b"x" * 3_000_000)
            return done(argv)
        p, _, env = make(run=Fake({("apt-get", "-s"): done([], out="Inst jq (1)\n"), ("apt-cache",): done([], out=""), ("apt-get", "download"): big}),
                         limits={"max_env_mb": 1})
        with self.assertRaises(packages.PrepError) as cm:
            p.prepare("job-4", ["apt:jq"])
        self.assertIn("more than 1 MB", str(cm.exception))
        self.assertFalse((env / "job-4").exists())

    def test_planning_problems_stop_before_anything_is_fetched(self):
        p, _, env = make(run=Fake({("apt-get", "-s"): done([], rc=100, err="E: Unable to locate package x\n")}))
        with self.assertRaises(packages.PrepError):
            p.prepare("job-5", ["apt:x"])
        self.assertFalse(any(c[0][0] == "apt-get" and c[0][1] == "download" for c in p._run.calls))

    def test_drop_and_prune(self):
        p, _, env = make()
        for name, age_days in (("old", 10), ("new", 1)):
            d = env / name
            d.mkdir()
            t = time.time() - age_days * 86400
            os.utime(d, (t, t))
        self.assertEqual(p.prune_old_envs(), 1)
        self.assertEqual(sorted(x.name for x in env.iterdir()), ["new"])
        p.drop_env("new")
        self.assertEqual(list(env.iterdir()), [])


class InstallingForReal(unittest.TestCase):
    def test_installs_only_what_is_missing_in_order_then_clears_the_throwaway_folders(self):
        p, tools, env = make(run=Fake({("dpkg", "-s"): lambda a, k: done(a, rc=0 if a[-1] == "git" else 1)}))
        put_pip(tools, "have", "1.0")
        (env / "leftover").mkdir()
        ran, said = [], []
        reg = {"classes": {"a": {"packages": ["apt:git", "apt:jq", "npm:eslint@9.12.0", "pip:have==1.0", "pip:new==2.0"]}, "b": {"packages": []}, "c": {}}}
        rc = p.install_registry_packages(reg, out=said.append, run_root=lambda cmd: ran.append(cmd) or done(cmd))
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in ran], ["apt-get", "npm", "python3"])
        self.assertEqual(ran[0][-1], "jq")                                                  # git is already installed
        self.assertEqual(ran[2][-1], "new==2.0")                                            # have==1.0 is already there at that version
        self.assertTrue(tools.exists())
        self.assertFalse((env / "leftover").exists())
        self.assertTrue(any(s.startswith("installing: apt-get") for s in said))

    def test_a_failing_command_stops_the_install_and_keeps_the_folders(self):
        p, _, env = make(run=Fake({("dpkg", "-s"): done([], rc=1)}))
        (env / "keep").mkdir()
        said = []
        rc = p.install_registry_packages({"classes": {"a": {"packages": ["apt:jq", "pip:x==1"]}}}, out=said.append, run_root=lambda cmd: done(cmd, rc=100))
        self.assertEqual(rc, 1)
        self.assertTrue(any("package install failed" in s for s in said))
        self.assertTrue((env / "keep").exists())

    def test_nothing_to_do_changes_nothing(self):
        p, _, env = make(run=Fake({("dpkg", "-s"): done([], rc=0)}))
        (env / "keep").mkdir()
        self.assertEqual(p.install_registry_packages({"classes": {"a": {"packages": ["apt:git"]}}}, run_root=lambda c: self.fail("must not run")), 0)
        self.assertTrue((env / "keep").exists())


if __name__ == "__main__":
    unittest.main()
